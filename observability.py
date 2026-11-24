"""Chirkoot MK-V Production-Grade Observability and Metrics System.

Provides:
1. Structured execution logging for every tool call:
   - Tool name, validated & redacted arguments, triggering utterance/instruction ref,
     task & execution IDs, timestamps, duration, status, error info, permission decision.
   - Credentials, private tokens, and raw visual image bytes redacted automatically.
   - Structured JSON Lines output written to %LOCALAPPDATA%\\Chirkoot\\logs\\tool_executions.jsonl.
   - In-memory ring buffer for fast HUD / REST retrieval.

2. Production telemetry tracking:
   - p50/p95 end-of-speech to first audio latency.
   - Wake-word false accepts and misses per hour.
   - Gemini reconnects and session rollover duration.
   - Tool execution success rate & UI verification success rate.
   - Token consumption per hour.
   - Audio dropped frames & Screen frames per second (FPS).
   - OCR latency & UIA latency.
   - System CPU, memory usage, worker thread count, active child processes.
   - Cancellation latency.
"""

import os
import sys
import re
import json
import time
import math
import uuid
import psutil
import logging
import threading
from pathlib import Path
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple, Deque
from collections import deque

logger = logging.getLogger("ChirkootObservability")

ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))

from security_policy import (
    CHIRKOOT_APPDATA_DIR,
    LOGS_DIR,
    redact_secrets,
    get_active_child_process_count
)

TOOL_EXECUTIONS_LOG_FILE = LOGS_DIR / "tool_executions.jsonl"
METRICS_LOG_FILE = LOGS_DIR / "metrics_snapshot.json"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# Maximum records in in-memory ring buffers
MAX_MEMORY_EXECUTION_RECORDS = 200
MAX_METRIC_SAMPLES = 500


# ==============================================================================
# 1. PAYLOAD REDACTION & SANITIZATION HELPER
# ==============================================================================

def sanitize_and_redact_args(args: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively redacts API keys, credentials, binary images, and huge document buffers."""
    sanitized = {}
    for k, v in args.items():
        k_str = str(k).lower()
        # Redact known credential / secret keys
        if any(sec in k_str for sec in ("api_key", "token", "password", "secret", "auth", "credential", "cookie")):
            sanitized[k] = "[REDACTED_SECRET]"
            continue

        if isinstance(v, str):
            # Check for base64 data URLs / image buffers
            if v.startswith("data:image/") or ";base64," in v[:100] or len(v) > 20000:
                sanitized[k] = f"[REDACTED_IMAGE_OR_LARGE_BUFFER ({len(v)} chars)]"
            else:
                # Apply regex secrets redaction and length cap
                redacted = redact_secrets(v)
                if len(redacted) > 1000:
                    sanitized[k] = redacted[:1000] + f"... [truncated {len(redacted)-1000} chars]"
                else:
                    sanitized[k] = redacted
        elif isinstance(v, (bytes, bytearray)):
            sanitized[k] = f"[REDACTED_BINARY_PAYLOAD ({len(v)} bytes)]"
        elif isinstance(v, dict):
            sanitized[k] = sanitize_and_redact_args(v)
        elif isinstance(v, list):
            sanitized[k] = [
                sanitize_and_redact_args(item) if isinstance(item, dict)
                else (redact_secrets(str(item))[:200] if isinstance(item, str) else item)
                for item in v[:50]
            ]
        else:
            sanitized[k] = v
    return sanitized


# ==============================================================================
# 2. STRUCTURED EXECUTION RECORD & AUDIT LOGGER
# ==============================================================================

@dataclass
class ToolExecutionRecord:
    execution_id: str
    tool_name: str
    validated_args: Dict[str, Any]
    task_id: str
    instruction_ref: str
    start_time: str
    start_timestamp: float
    end_time: Optional[str] = None
    end_timestamp: Optional[float] = None
    duration_ms: float = 0.0
    status: str = "pending"  # "pending", "confirming", "queued", "running", "completed", "cancelled", "late_dropped", "failed", "denied"
    result_summary: Optional[str] = None
    error_info: Optional[str] = None
    permission_decision: Dict[str, Any] = field(default_factory=dict)
    verified: bool = False
    verification_details: str = ""

    def complete(self, result: Any, verified: bool = False, details: str = ""):
        self.end_timestamp = time.time()
        self.end_time = datetime.fromtimestamp(self.end_timestamp).isoformat()
        self.duration_ms = max(0.0, (self.end_timestamp - self.start_timestamp) * 1000.0)
        self.status = "completed"
        self.verified = verified
        self.verification_details = details
        res_str = redact_secrets(str(result))
        self.result_summary = res_str[:500] if len(res_str) > 500 else res_str

    def fail(self, error: Any):
        self.end_timestamp = time.time()
        self.end_time = datetime.fromtimestamp(self.end_timestamp).isoformat()
        self.duration_ms = max(0.0, (self.end_timestamp - self.start_timestamp) * 1000.0)
        self.status = "failed"
        self.error_info = redact_secrets(str(error))[:1000]

    def cancel(self, reason: str = ""):
        self.end_timestamp = time.time()
        self.end_time = datetime.fromtimestamp(self.end_timestamp).isoformat()
        self.duration_ms = max(0.0, (self.end_timestamp - self.start_timestamp) * 1000.0)
        self.status = "cancelled"
        self.error_info = reason

    def to_dict(self) -> Dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "tool_name": self.tool_name,
            "validated_args": self.validated_args,
            "task_id": self.task_id,
            "instruction_ref": self.instruction_ref,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_ms": round(self.duration_ms, 2),
            "status": self.status,
            "result_summary": self.result_summary,
            "error_info": self.error_info,
            "permission_decision": self.permission_decision,
            "verified": self.verified,
            "verification_details": self.verification_details
        }


class StructuredExecutionLogger:
    """Thread-safe structured audit logger for all tool executions."""

    def __init__(self, log_file: Optional[Path] = None):
        self.log_file = log_file or TOOL_EXECUTIONS_LOG_FILE
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._recent_records: Deque[Dict[str, Any]] = deque(maxlen=MAX_MEMORY_EXECUTION_RECORDS)

    def log_execution(self, record: ToolExecutionRecord):
        """Persists the execution record to JSONL and the in-memory ring buffer."""
        record_dict = record.to_dict()
        with self._lock:
            self._recent_records.appendleft(record_dict)
            try:
                with open(self.log_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record_dict, ensure_ascii=False) + "\n")
            except Exception as e:
                logger.error(f"[ExecutionLogger] Failed to write JSONL execution log: {e}")

    def get_recent_executions(
        self,
        limit: int = 50,
        tool_name: Optional[str] = None,
        status: Optional[str] = None,
        task_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Returns recent tool execution records matching optional filters."""
        with self._lock:
            records = list(self._recent_records)

        filtered = []
        for r in records:
            if tool_name and r["tool_name"] != tool_name:
                continue
            if status and r["status"] != status:
                continue
            if task_id and r["task_id"] != task_id:
                continue
            filtered.append(r)
            if len(filtered) >= limit:
                break
        return filtered

    def clear_memory(self):
        """Clears in-memory ring buffer for test isolation."""
        with self._lock:
            self._recent_records.clear()


# ==============================================================================
# 3. PRODUCTION METRICS COLLECTOR (PERCENTILES & SLIDING WINDOWS)
# ==============================================================================

def compute_percentiles(values: List[float]) -> Dict[str, float]:
    """Computes p50, p95, p99, min, max, avg for float samples."""
    if not values:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "avg": 0.0, "min": 0.0, "max": 0.0, "count": 0}
    s = sorted(values)
    n = len(s)
    p50_idx = min(n - 1, max(0, int(0.50 * (n - 1))))
    p95_idx = min(n - 1, max(0, int(0.95 * (n - 1))))
    p99_idx = min(n - 1, max(0, int(0.99 * (n - 1))))
    return {
        "p50": round(s[p50_idx], 2),
        "p95": round(s[p95_idx], 2),
        "p99": round(s[p99_idx], 2),
        "avg": round(sum(s) / n, 2),
        "min": round(s[0], 2),
        "max": round(s[-1], 2),
        "count": n
    }


class ChirkootMetricsCollector:
    """Thread-safe production metrics aggregator with sliding 1-hour and sample windows."""

    def __init__(self):
        self._lock = threading.Lock()
        self.boot_time = time.time()
        self.process = psutil.Process(os.getpid())

        # Latency samples (rolling deques)
        self.speech_to_first_audio_samples: Deque[float] = deque(maxlen=MAX_METRIC_SAMPLES)
        self.ocr_latency_samples: Deque[float] = deque(maxlen=MAX_METRIC_SAMPLES)
        self.uia_latency_samples: Deque[float] = deque(maxlen=MAX_METRIC_SAMPLES)
        self.cancellation_latency_samples: Deque[float] = deque(maxlen=MAX_METRIC_SAMPLES)
        self.session_rollover_duration_samples: Deque[float] = deque(maxlen=MAX_METRIC_SAMPLES)

        # Wake word events (timestamped for per-hour calculations)
        self.wake_events_1h: Deque[Tuple[float, str]] = deque(maxlen=2000)  # (timestamp, 'accept' | 'false_accept' | 'miss')

        # Reliability & counts
        self.total_tool_calls: int = 0
        self.completed_tool_calls: int = 0
        self.failed_tool_calls: int = 0
        self.cancelled_tool_calls: int = 0
        self.tool_latencies: Deque[float] = deque(maxlen=MAX_METRIC_SAMPLES)

        # UI verification
        self.total_ui_verifications: int = 0
        self.successful_ui_verifications: int = 0

        # Gemini connection
        self.gemini_reconnect_count: int = 0
        self.audio_dropped_frames_count: int = 0

        # Tokens (timestamped for per-hour calculations)
        self.token_events_1h: Deque[Tuple[float, int, int]] = deque(maxlen=5000)  # (timestamp, prompt_tok, cand_tok)
        self.lifetime_prompt_tokens: int = 0
        self.lifetime_candidate_tokens: int = 0

        # Screen FPS tracking (timestamps of last 10 seconds of frames)
        self.screen_frame_timestamps: Deque[float] = deque(maxlen=300)

    # --------------------------------------------------------------------------
    # Record Handlers
    # --------------------------------------------------------------------------

    def record_speech_to_first_audio(self, latency_ms: float):
        """Records end-of-speech to first audio output latency in ms."""
        if latency_ms >= 0:
            with self._lock:
                self.speech_to_first_audio_samples.append(float(latency_ms))

    def record_wake_word_event(self, event_type: str):
        """Records a wake word event: 'accept', 'false_accept', or 'miss'."""
        now = time.time()
        with self._lock:
            self.wake_events_1h.append((now, event_type.lower().strip()))

    def record_gemini_reconnect(self):
        """Records a Gemini session reconnect event."""
        with self._lock:
            self.gemini_reconnect_count += 1

    def record_session_rollover(self, duration_ms: float):
        """Records session rollover / GoAway reconnect turnaround duration in ms."""
        if duration_ms >= 0:
            with self._lock:
                self.session_rollover_duration_samples.append(float(duration_ms))

    def record_tool_call(self, tool_name: str, success: bool, duration_ms: float = 0.0):
        """Records a completed tool execution outcome and duration."""
        with self._lock:
            self.total_tool_calls += 1
            if success:
                self.completed_tool_calls += 1
            else:
                self.failed_tool_calls += 1
            if duration_ms > 0:
                self.tool_latencies.append(float(duration_ms))

    def record_tool_cancellation(self, duration_ms: float = 0.0):
        """Records a cancelled tool execution."""
        with self._lock:
            self.total_tool_calls += 1
            self.cancelled_tool_calls += 1
            if duration_ms > 0:
                self.cancellation_latency_samples.append(float(duration_ms))

    def record_ui_verification(self, verified: bool):
        """Records a closed-loop desktop UI verification outcome."""
        with self._lock:
            self.total_ui_verifications += 1
            if verified:
                self.successful_ui_verifications += 1

    def record_token_usage(self, prompt_tokens: int, candidate_tokens: int):
        """Records token usage for prompt and response parts."""
        now = time.time()
        p = max(0, int(prompt_tokens))
        c = max(0, int(candidate_tokens))
        with self._lock:
            self.token_events_1h.append((now, p, c))
            self.lifetime_prompt_tokens += p
            self.lifetime_candidate_tokens += c

    def record_audio_drop(self):
        """Records a dropped audio frame due to queue overflow or buffer overrun."""
        with self._lock:
            self.audio_dropped_frames_count += 1

    def record_screen_frame(self):
        """Records a captured or streamed screen frame for FPS calculation."""
        now = time.time()
        with self._lock:
            self.screen_frame_timestamps.append(now)

    def record_ocr_latency(self, duration_ms: float):
        """Records OCR parsing duration in ms."""
        if duration_ms >= 0:
            with self._lock:
                self.ocr_latency_samples.append(float(duration_ms))

    def record_uia_latency(self, duration_ms: float):
        """Records Windows UI Automation operation duration in ms."""
        if duration_ms >= 0:
            with self._lock:
                self.uia_latency_samples.append(float(duration_ms))

    def record_cancellation_latency(self, duration_ms: float):
        """Records time taken from cancellation signal to abort completion in ms."""
        if duration_ms >= 0:
            with self._lock:
                self.cancellation_latency_samples.append(float(duration_ms))

    # --------------------------------------------------------------------------
    # Derived Metric Calculations & Summaries
    # --------------------------------------------------------------------------

    def get_screen_fps(self) -> float:
        """Computes current screen capture FPS based on timestamps in the last 10 seconds."""
        now = time.time()
        cutoff = now - 10.0
        with self._lock:
            recent = [t for t in self.screen_frame_timestamps if t >= cutoff]
        if len(recent) < 2:
            return 0.0
        span = recent[-1] - recent[0]
        if span > 0.05:
            return round((len(recent) - 1) / span, 2)
        return float(len(recent))

    def get_wake_word_stats(self) -> Dict[str, Any]:
        """Calculates wake-word false accepts and misses per hour."""
        now = time.time()
        cutoff_1h = now - 3600.0
        with self._lock:
            recent = [ev for t, ev in self.wake_events_1h if t >= cutoff_1h]

        accepts = recent.count("accept")
        false_accepts = recent.count("false_accept")
        misses = recent.count("miss")

        # Elapsed hours since boot, capped at 1.0 for scaling if < 1 hr
        elapsed_hours = max(0.016, min(1.0, (now - self.boot_time) / 3600.0))
        fa_per_hour = round(false_accepts / elapsed_hours, 2)
        misses_per_hour = round(misses / elapsed_hours, 2)

        return {
            "false_accepts_per_hour": fa_per_hour,
            "misses_per_hour": misses_per_hour,
            "recent_1h_accepts": accepts,
            "recent_1h_false_accepts": false_accepts,
            "recent_1h_misses": misses,
            "total_recorded_events": len(recent)
        }

    def get_token_consumption_stats(self) -> Dict[str, Any]:
        """Calculates token consumption rate per hour and lifetime totals."""
        now = time.time()
        cutoff_1h = now - 3600.0
        with self._lock:
            recent = [(p, c) for t, p, c in self.token_events_1h if t >= cutoff_1h]
            life_p = self.lifetime_prompt_tokens
            life_c = self.lifetime_candidate_tokens

        p_1h = sum(p for p, _ in recent)
        c_1h = sum(c for _, c in recent)
        total_1h = p_1h + c_1h

        elapsed_hours = max(0.016, min(1.0, (now - self.boot_time) / 3600.0))
        tokens_per_hour = int(round(total_1h / elapsed_hours))

        return {
            "tokens_per_hour": tokens_per_hour,
            "recent_1h_prompt_tokens": p_1h,
            "recent_1h_candidate_tokens": c_1h,
            "recent_1h_total_tokens": total_1h,
            "lifetime_prompt_tokens": life_p,
            "lifetime_candidate_tokens": life_c,
            "lifetime_total_tokens": life_p + life_c
        }

    def get_system_resource_metrics(self) -> Dict[str, Any]:
        """Retrieves real CPU %, process memory (MB), and active worker thread/process counts."""
        try:
            mem_info = self.process.memory_info()
            mem_mb = round(mem_info.rss / (1024 * 1024), 2)
            cpu_pct = round(self.process.cpu_percent(interval=None), 1)
            num_threads = self.process.num_threads()
        except Exception:
            mem_mb = 0.0
            cpu_pct = 0.0
            num_threads = threading.active_count()

        active_children = get_active_child_process_count()
        uptime_s = round(time.time() - self.boot_time, 1)

        return {
            "cpu_percent": cpu_pct,
            "memory_rss_mb": mem_mb,
            "thread_count": num_threads,
            "active_child_processes": active_children,
            "uptime_seconds": uptime_s
        }

    def get_metrics_summary(self) -> Dict[str, Any]:
        """Assembles a comprehensive JSON-ready telemetry report for HUD and REST API."""
        with self._lock:
            s2a_list = list(self.speech_to_first_audio_samples)
            ocr_list = list(self.ocr_latency_samples)
            uia_list = list(self.uia_latency_samples)
            canc_list = list(self.cancellation_latency_samples)
            roll_list = list(self.session_rollover_duration_samples)
            tool_dur_list = list(self.tool_latencies)

            tot_tools = self.total_tool_calls
            comp_tools = self.completed_tool_calls
            fail_tools = self.failed_tool_calls
            canc_tools = self.cancelled_tool_calls

            tot_ui = self.total_ui_verifications
            succ_ui = self.successful_ui_verifications

            gemini_reconn = self.gemini_reconnect_count
            drop_audio = self.audio_dropped_frames_count

        # Success rates
        decided_tools = comp_tools + fail_tools
        tool_success_rate = round((comp_tools / decided_tools * 100.0), 1) if decided_tools > 0 else 100.0
        ui_success_rate = round((succ_ui / tot_ui * 100.0), 1) if tot_ui > 0 else 100.0

        return {
            "timestamp": datetime.now().isoformat(),
            "latency": {
                "speech_to_first_audio_ms": compute_percentiles(s2a_list),
                "ocr_latency_ms": compute_percentiles(ocr_list),
                "uia_latency_ms": compute_percentiles(uia_list),
                "cancellation_latency_ms": compute_percentiles(canc_list),
                "session_rollover_duration_ms": compute_percentiles(roll_list),
                "tool_execution_duration_ms": compute_percentiles(tool_dur_list)
            },
            "wake_word": self.get_wake_word_stats(),
            "reliability": {
                "tool_success_rate_percent": tool_success_rate,
                "ui_verification_success_rate_percent": ui_success_rate,
                "total_tool_calls": tot_tools,
                "completed_tool_calls": comp_tools,
                "failed_tool_calls": fail_tools,
                "cancelled_tool_calls": canc_tools,
                "total_ui_verifications": tot_ui,
                "successful_ui_verifications": succ_ui,
                "gemini_reconnects": gemini_reconn,
                "audio_dropped_frames": drop_audio
            },
            "streaming": {
                "screen_fps": self.get_screen_fps(),
                "token_usage": self.get_token_consumption_stats()
            },
            "system_resources": self.get_system_resource_metrics()
        }

    def reset_for_tests(self):
        """Resets all metrics state for test isolation."""
        with self._lock:
            self.speech_to_first_audio_samples.clear()
            self.ocr_latency_samples.clear()
            self.uia_latency_samples.clear()
            self.cancellation_latency_samples.clear()
            self.session_rollover_duration_samples.clear()
            self.wake_events_1h.clear()
            self.total_tool_calls = 0
            self.completed_tool_calls = 0
            self.failed_tool_calls = 0
            self.cancelled_tool_calls = 0
            self.tool_latencies.clear()
            self.total_ui_verifications = 0
            self.successful_ui_verifications = 0
            self.gemini_reconnect_count = 0
            self.audio_dropped_frames_count = 0
            self.token_events_1h.clear()
            self.lifetime_prompt_tokens = 0
            self.lifetime_candidate_tokens = 0
            self.screen_frame_timestamps.clear()


# Global Singleton Instances
GLOBAL_EXECUTION_LOGGER = StructuredExecutionLogger()
GLOBAL_METRICS = ChirkootMetricsCollector()
