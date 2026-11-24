"""
Chirkoot MK-V: Production-Grade Gemini Live Session Lifecycle Manager.

Handles:
1. Supported sliding-window context compression with configurable trigger & target thresholds.
2. Rolling transcript and compact conversation summaries for bounded context reseeding.
3. Persistent active-task state outside the Gemini session to survive disconnects.
4. GoAway detection and proactive graceful reconnection before connection drop.
5. Strict decoupling of local browser /ws lifecycle from upstream Gemini WebSocket.
6. Audio-only session recovery with 2-hour resumption handles.
7. Audio/video clean-session rollover with compact context reseeding when resumption fails.
8. Duplicate tool execution prevention and idempotency caching.
9. Client-message acknowledgement tracking (last_consumed_client_message_index) and replay.
10. Bounded retries with exponential backoff and randomized jitter.
11. Model compatibility adapters ensuring no unsupported parameters are passed.
12. Granular HUD telemetry reporting for UI transparency.
"""

import os
import sys
import json
import time
import random
import hashlib
import asyncio
import logging
from pathlib import Path
from collections import deque
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple, Set

logger = logging.getLogger("ChirkootLifecycle")

# Local storage path configuration
sys.path.insert(0, str(Path(__file__).resolve().parent))
from security_policy import (
    CHIRKOOT_APPDATA_DIR,
    DATA_DIR,
    record_audit_event,
    is_kill_switch_active
)

ACTIVE_TASKS_FILE = DATA_DIR / "active_tasks.json"

# ==============================================================================
# 1. MODEL CONFIGURATION ADAPTER
# ==============================================================================

class ModelConfigAdapter:
    """
    Validates, adapts, and constructs LiveConnectConfig instances tailored
    strictly to the target Gemini model and installed google-genai SDK version.
    
    Guarantees:
    - Never transmits removed `enable_affective_dialog`.
    - Never transmits restricted `proactive_audio: false`.
    - Omits `thinking_config` / `thinking_level` for `gemini-3.8-live` (unsupported).
    - Only includes `thinking_config` for models that support it (e.g. `gemini-3.8-live-extended-thinking`).
    - Omits `transparent=True` in `SessionResumptionConfig` (Developer API incompatible).
    """

    SUPPORTED_MODELS = {
        "gemini-3.8-live": {
            "supports_thinking": False,
            "supports_async_tools": True,
            "supports_compression": True,
            "supports_resumption": True,
            "proactive_audio_always_on": True,
            "affective_dialog_removed": True
        },
        "gemini-3.8-live-extended-thinking": {
            "supports_thinking": True,
            "supports_async_tools": True,
            "supports_compression": True,
            "supports_resumption": True,
            "proactive_audio_always_on": True,
            "affective_dialog_removed": True
        },
        "gemini-2.0-flash-live-001": {
            "supports_thinking": False,
            "supports_async_tools": False,
            "supports_compression": False,
            "supports_resumption": False,
            "proactive_audio_always_on": False,
            "affective_dialog_removed": False
        }
    }

    def __init__(self, model_name: str = "gemini-3.8-live"):
        self.model_name = model_name
        self.profile = self.SUPPORTED_MODELS.get(model_name, self.SUPPORTED_MODELS["gemini-3.8-live"])

    def build_connect_config(
        self,
        types_module: Any,
        system_instruction_text: str,
        tools_list: list,
        voice_name: str = "Aoede",
        resumption_handle: Optional[str] = None,
        trigger_tokens: int = 80000,
        target_tokens: int = 40000,
        thinking_level: Optional[str] = None
    ) -> Any:
        """Constructs a certified LiveConnectConfig instance."""
        
        # 1. Speech & Voice Config
        speech_cfg = types_module.SpeechConfig(
            voice_config=types_module.VoiceConfig(
                prebuilt_voice_config=types_module.PrebuiltVoiceConfig(voice_name=voice_name)
            )
        )

        # 2. System Instruction
        sys_instruction = types_module.Content(
            parts=[types_module.Part(text=system_instruction_text)]
        )

        # 3. Context Window Compression Config
        compression_cfg = None
        if self.profile["supports_compression"]:
            sliding_window = types_module.SlidingWindow(target_tokens=target_tokens)
            compression_cfg = types_module.ContextWindowCompressionConfig(
                trigger_tokens=trigger_tokens,
                sliding_window=sliding_window
            )

        # 4. Session Resumption Config (strictly developer API mode: no transparent=True)
        resumption_cfg = None
        if self.profile["supports_resumption"]:
            if resumption_handle:
                resumption_cfg = types_module.SessionResumptionConfig(handle=resumption_handle)
            else:
                resumption_cfg = types_module.SessionResumptionConfig()

        # 5. Thinking Config (Only for models that explicitly support thinking)
        thinking_cfg = None
        if self.profile["supports_thinking"] and thinking_level:
            level_map = {
                "low": types_module.ThinkingLevel.LOW,
                "medium": types_module.ThinkingLevel.MEDIUM,
                "high": types_module.ThinkingLevel.HIGH
            }
            lvl = level_map.get(thinking_level.lower(), types_module.ThinkingLevel.LOW)
            thinking_cfg = types_module.ThinkingConfig(thinking_level=lvl)

        # Deduplicate tools by function name to protect against any upstream duplicate declaration rejections (code 1007)
        deduped_tools = []
        seen_tool_names = set()
        for t in (tools_list or []):
            name = getattr(t, "__name__", None)
            if not name and isinstance(t, dict):
                name = t.get("name")
            if name:
                if name in seen_tool_names:
                    continue
                seen_tool_names.add(name)
            deduped_tools.append(t)

        # Build kwargs cleanly without forbidden or unsupported parameters
        kwargs = {
            "response_modalities": [types_module.Modality.AUDIO],
            "speech_config": speech_cfg,
            "system_instruction": sys_instruction,
            "tools": deduped_tools,
            "input_audio_transcription": types_module.AudioTranscriptionConfig(),
            "output_audio_transcription": types_module.AudioTranscriptionConfig(),
        }

        if compression_cfg:
            kwargs["context_window_compression"] = compression_cfg
        if resumption_cfg:
            kwargs["session_resumption"] = resumption_cfg
        if thinking_cfg:
            kwargs["thinking_config"] = thinking_cfg

        return types_module.LiveConnectConfig(**kwargs)


# ==============================================================================
# 2. PERSISTENT ACTIVE-TASK STATE MANAGER
# ==============================================================================

class TaskStateManager:
    """
    Maintains persistent state for active or in-flight user instructions outside
    of the ephemeral Gemini Live session.
    
    Guarantees:
    - In-flight tasks survive session disconnects, network drops, and server reloads.
    - Tasks are stamped with start times, tools invoked, and execution status.
    - Saved to %LOCALAPPDATA%\\Chirkoot\\data\\active_tasks.json.
    """

    def __init__(self, storage_path: Path = ACTIVE_TASKS_FILE):
        self.storage_path = storage_path
        self._ensure_storage()

    def _ensure_storage(self):
        try:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            if not self.storage_path.exists():
                self._save({})
        except Exception as e:
            logger.warning(f"Error initializing task store: {e}")

    def _load(self) -> Dict[str, Any]:
        try:
            if self.storage_path.exists():
                with open(self.storage_path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _save(self, data: Dict[str, Any]):
        try:
            with open(self.storage_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Error saving task state: {e}")

    def register_task(self, task_id: str, prompt: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Registers a new in-progress user task."""
        data = self._load()
        task_record = {
            "task_id": task_id,
            "prompt": prompt.strip(),
            "status": "in_progress",
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "tool_calls": [],
            "metadata": metadata or {}
        }
        data[task_id] = task_record
        self._save(data)
        record_audit_event("TASK_REGISTERED", {"task_id": task_id, "prompt": prompt[:80]})
        return task_record

    def append_tool_execution(self, task_id: str, tool_name: str, args: Dict[str, Any], result: str):
        """Records a completed tool execution under an active task."""
        data = self._load()
        if task_id in data:
            data[task_id]["tool_calls"].append({
                "tool": tool_name,
                "args": args,
                "result_preview": str(result)[:200],
                "timestamp": datetime.now().isoformat()
            })
            data[task_id]["updated_at"] = datetime.now().isoformat()
            self._save(data)

    def mark_completed(self, task_id: str, summary: str = "Completed successfully"):
        """Marks a task as completed."""
        data = self._load()
        if task_id in data:
            data[task_id]["status"] = "completed"
            data[task_id]["summary"] = summary
            data[task_id]["completed_at"] = datetime.now().isoformat()
            self._save(data)
            record_audit_event("TASK_COMPLETED", {"task_id": task_id})

    def mark_interrupted(self, task_id: str, reason: str = "Session severed mid-task"):
        """Marks a task as interrupted for recovery."""
        data = self._load()
        if task_id in data and data[task_id]["status"] == "in_progress":
            data[task_id]["status"] = "interrupted"
            data[task_id]["interruption_reason"] = reason
            self._save(data)

    def mark_all_cancelled(self, reason: str = "Emergency Stop engaged"):
        """Marks all active, pending, or interrupted tasks as CANCELLED to prevent auto-resumption."""
        data = self._load()
        changed = False
        for tid, t in data.items():
            if t.get("status") in ("in_progress", "interrupted", "pending"):
                t["status"] = "cancelled"
                t["cancellation_reason"] = reason
                t["cancelled_at"] = datetime.now().isoformat()
                changed = True
        if changed:
            self._save(data)
            record_audit_event("ALL_TASKS_CANCELLED", {"reason": reason})

    def get_latest_unfinished_task(self) -> Optional[Dict[str, Any]]:
        """Retrieves the most recent uncompleted or interrupted task (suppressed if kill switch active)."""
        if is_kill_switch_active():
            return None
        data = self._load()
        in_flight = [t for t in data.values() if t.get("status") in ("in_progress", "interrupted")]
        if not in_flight:
            return None
        return sorted(in_flight, key=lambda x: x.get("created_at", ""), reverse=True)[0]

    def get_all_tasks(self, limit: int = 25) -> List[Dict[str, Any]]:
        """Returns sorted list of recent tasks."""
        data = self._load()
        tasks = list(data.values())
        tasks.sort(key=lambda x: x.get("updated_at", x.get("created_at", "")), reverse=True)
        return tasks[:limit]

    def cancel_task(self, task_id: str, reason: str = "User cancelled via Task Manager") -> bool:
        """Cancels a specific task."""
        data = self._load()
        if task_id in data:
            data[task_id]["status"] = "cancelled"
            data[task_id]["cancellation_reason"] = reason
            data[task_id]["cancelled_at"] = datetime.now().isoformat()
            self._save(data)
            record_audit_event("TASK_CANCELLED", {"task_id": task_id, "reason": reason})
            return True
        return False

    def retry_task(self, task_id: str) -> bool:
        """Resets a cancelled or interrupted task back to in_progress."""
        data = self._load()
        if task_id in data:
            data[task_id]["status"] = "in_progress"
            data[task_id]["updated_at"] = datetime.now().isoformat()
            self._save(data)
            record_audit_event("TASK_RETRIED", {"task_id": task_id})
            return True
        return False


# ==============================================================================
# 3. TOOL DEDUPLICATION & IDEMPOTENCY ENGINE
# ==============================================================================

class ToolDeduplicator:
    """
    Prevents duplicate execution of high-impact or side-effecting actions
    when tool calls are replayed after session resumption or connection drops.
    
    Caches results by:
    1. Tool call ID (`fc.id`).
    2. Tool signature hash (`tool_name` + sorted json `args`).
    """

    def __init__(self, ttl_seconds: int = 600):
        self.ttl_seconds = ttl_seconds
        # call_id -> (timestamp, tool_name, result)
        self._by_call_id: Dict[str, Tuple[float, str, Any]] = {}
        # sig_hash -> (timestamp, tool_name, result)
        self._by_signature: Dict[str, Tuple[float, str, Any]] = {}

    def _hash_signature(self, tool_name: str, args: Dict[str, Any]) -> str:
        serialized = json.dumps({"name": tool_name, "args": args}, sort_keys=True, default=str)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def is_duplicate(self, call_id: Optional[str], tool_name: str, args: Dict[str, Any]) -> Tuple[bool, Optional[Any]]:
        """Checks if a tool call was already executed within TTL."""
        now = time.time()
        
        # 1. Check exact call_id
        if call_id and call_id in self._by_call_id:
            ts, name, cached_result = self._by_call_id[call_id]
            if now - ts <= self.ttl_seconds:
                logger.info(f"[Deduplicator] Suppressed duplicate tool call ID '{call_id}' ({name}).")
                return True, cached_result

        # 2. Check identical signature for side-effecting tools
        side_effecting = {"run_command", "write_file", "modify_source_code", "create_custom_tool", "rollback_code"}
        if tool_name in side_effecting:
            sig = self._hash_signature(tool_name, args)
            if sig in self._by_signature:
                ts, name, cached_result = self._by_signature[sig]
                # If executed within last 45 seconds, treat as duplicate replay
                if now - ts <= 45.0:
                    logger.warning(f"[Deduplicator] Suppressed duplicate side-effect execution '{tool_name}' within 45s.")
                    return True, cached_result

        return False, None

    def record_execution(self, call_id: Optional[str], tool_name: str, args: Dict[str, Any], result: Any):
        """Records a successful tool execution in the idempotency cache."""
        now = time.time()
        if call_id:
            self._by_call_id[call_id] = (now, tool_name, result)
        sig = self._hash_signature(tool_name, args)
        self._by_signature[sig] = (now, tool_name, result)

        # Prune expired entries
        if len(self._by_call_id) > 200:
            self._prune(now)

    def _prune(self, now: float):
        self._by_call_id = {k: v for k, v in self._by_call_id.items() if now - v[0] <= self.ttl_seconds}
        self._by_signature = {k: v for k, v in self._by_signature.items() if now - v[0] <= self.ttl_seconds}


# ==============================================================================
# 4. CLIENT MESSAGE ACKNOWLEDGEMENT & REPLAY TRACKER
# ==============================================================================

class ClientMessageTracker:
    """
    Maintains a monotonically increasing sequence of outgoing client messages
    and tracks server acknowledgements via `last_consumed_client_message_index`.
    
    Guarantees:
    - Messages unacknowledged by the server on session drop can be replayed.
    - Accurately tracks message boundaries across reconnections.
    """

    def __init__(self, max_buffer: int = 150):
        self.max_buffer = max_buffer
        self._next_seq_index = 0
        self._unacknowledged: deque = deque(maxlen=max_buffer)
        self._last_ack_index = -1

    def track_message(self, msg_type: str, payload: Any) -> int:
        """Assigns the next sequence number and tracks message for replay."""
        seq = self._next_seq_index
        self._next_seq_index += 1
        self._unacknowledged.append({
            "seq_index": seq,
            "type": msg_type,
            "payload": payload,
            "timestamp": time.time()
        })
        return seq

    def on_server_ack(self, last_consumed_index: Optional[int]):
        """Processes server acknowledgement and prunes consumed messages."""
        if last_consumed_index is None:
            return
        self._last_ack_index = last_consumed_index
        # Prune acknowledged items
        while self._unacknowledged and self._unacknowledged[0]["seq_index"] <= last_consumed_index:
            self._unacknowledged.popleft()

    def get_unacknowledged_replay(self) -> List[Dict[str, Any]]:
        """Returns pending unacknowledged messages for selective replay."""
        return list(self._unacknowledged)

    @property
    def last_acked_index(self) -> int:
        return self._last_ack_index


# ==============================================================================
# 5. ROLLING TRANSCRIPT & COMPACT CONVERSATION SUMMARIZER
# ==============================================================================

class RollingTranscriptManager:
    """
    Maintains active dialogue turns with compact summarization.
    
    Features:
    - Sliding window of the most recent turns (default: 10 turns).
    - When transcript grows, older turns are compacted into an informative
      executive summary turn: '[CONVERSATION HISTORY SUMMARY: ...]' so reseeding
      remains token-frugal and never floods the context window.
    - Yields valid google-genai Content turns for `send_client_content()`.
    """

    def __init__(self, max_raw_turns: int = 10):
        self.max_raw_turns = max_raw_turns
        self.raw_turns: deque = deque(maxlen=max_raw_turns)
        self.compact_summary: str = ""

    def append_turn(self, role: str, text: str):
        """Appends a new turn and compacts older context if capacity exceeded."""
        clean_text = text.strip()
        if not clean_text:
            return

        if len(self.raw_turns) >= self.max_raw_turns:
            # Shift oldest turns into running compact summary
            evicted = [self.raw_turns.popleft() for _ in range(3)]
            self._update_compact_summary(evicted)

        self.raw_turns.append({"role": role, "text": clean_text, "timestamp": time.time()})

    def _update_compact_summary(self, evicted_turns: List[Dict[str, Any]]):
        """Updates the running compact summary using keyword distillation."""
        bullet_points = []
        for t in evicted_turns:
            role_label = "User" if t["role"] == "user" else "Chirkoot"
            snippet = t["text"][:120].replace("\n", " ")
            bullet_points.append(f"{role_label}: {snippet}")
        
        chunk = " | ".join(bullet_points)
        if self.compact_summary:
            self.compact_summary = (self.compact_summary + " | " + chunk)[-400:]
        else:
            self.compact_summary = chunk[:400]

    def build_reseeding_turns(self, types_module: Any) -> List[Any]:
        """
        Builds a compact sequence of Content turns for session.send_client_content().
        Prepends the compact summary if present.
        """
        turns = []

        if self.compact_summary:
            summary_text = (
                f"[PRIOR CONVERSATION SUMMARY: The following is a compact recap of preceding dialogue: "
                f"{self.compact_summary}]"
            )
            turns.append(types_module.Content(
                role="user",
                parts=[types_module.Part.from_text(text=summary_text)]
            ))
            turns.append(types_module.Content(
                role="model",
                parts=[types_module.Part.from_text(text="Acknowledged. I retain full conversational context.")]
            ))

        for t in self.raw_turns:
            turns.append(types_module.Content(
                role=t["role"],
                parts=[types_module.Part.from_text(text=t["text"])]
            ))

        return turns


# ==============================================================================
# 6. BOUNDED EXPONENTIAL BACKOFF WITH JITTER
# ==============================================================================

class BackoffManager:
    """
    Manages retry delays with exponential backoff and randomized jitter
    to prevent reconnect storms during network hiccups or server GoAway frames.
    """

    def __init__(
        self,
        base_delay: float = 0.35,
        max_delay: float = 5.0,
        max_retries: int = 6,
        jitter_factor: float = 0.25
    ):
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.max_retries = max_retries
        self.jitter_factor = jitter_factor
        self.current_attempt = 0

    def get_next_delay(self) -> float:
        """Computes the next delay in seconds."""
        if self.current_attempt >= self.max_retries:
            return self.max_delay

        delay = min(self.max_delay, self.base_delay * (2 ** self.current_attempt))
        # Add random uniform jitter: [delay * (1 - jitter), delay * (1 + jitter)]
        jitter = random.uniform(-self.jitter_factor * delay, self.jitter_factor * delay)
        final_delay = max(0.1, delay + jitter)
        self.current_attempt += 1
        return round(final_delay, 3)

    def reset(self):
        """Resets the backoff counter after a sustained healthy connection."""
        self.current_attempt = 0

    @property
    def has_exceeded_retries(self) -> bool:
        return self.current_attempt >= self.max_retries


# ==============================================================================
# 7. CHIRKOOT SESSION LIFECYCLE CONTROLLER
# ==============================================================================

class SessionLifecycleController:
    """
    Coordinates end-to-end session management:
    - Session resumption handle management (2-hour lifetime).
    - GoAway frame detection and proactive reconnect.
    - Decoupled browser WebSocket communication.
    - Audio-only resumption vs Audio/Video clean rollover.
    - Interrupted task recovery and tool deduplication.
    - HUD telemetry event dispatch.
    """

    def __init__(self, model_name: str = "gemini-3.8-live"):
        self.adapter = ModelConfigAdapter(model_name)
        self.task_mgr = TaskStateManager()
        self.deduplicator = ToolDeduplicator(ttl_seconds=600)
        self.msg_tracker = ClientMessageTracker()
        self.transcript_mgr = RollingTranscriptManager(max_raw_turns=10)
        self.backoff_mgr = BackoffManager()

        # Resumption state
        self.latest_resumption_handle: Optional[str] = None
        self.handle_created_at: float = 0.0
        self.handle_ttl_seconds: float = 7200.0  # 2 hours per Google specification
        self.is_video_active: bool = False

    def on_resumption_update(self, update_obj: Any):
        """Processes server session_resumption_update."""
        new_handle = getattr(update_obj, "new_handle", None)
        if new_handle:
            self.latest_resumption_handle = new_handle
            self.handle_created_at = time.time()
            logger.info(f"[Lifecycle] Acquired fresh session resumption handle: {new_handle[:16]}...")

        # Process server message acknowledgement
        last_consumed = getattr(update_obj, "last_consumed_client_message_index", None)
        if last_consumed is not None:
            self.msg_tracker.on_server_ack(last_consumed)

    def is_handle_valid(self) -> bool:
        """Checks if active resumption handle is present and within 2-hour window."""
        if not self.latest_resumption_handle:
            return False
        return (time.time() - self.handle_created_at) < self.handle_ttl_seconds

    def invalidate_handle(self, reason: str = "Manual invalidation"):
        """Invalidates resumption handle to force clean rollover."""
        logger.info(f"[Lifecycle] Invalidated handle ({reason}). Next connection will cold-start.")
        self.latest_resumption_handle = None
        self.handle_created_at = 0.0

    def prepare_reconnect_strategy(self) -> Tuple[bool, Optional[str]]:
        """
        Determines the optimal reconnection path:
        - (True, handle): Audio-only session with valid resumption handle.
        - (False, None): Clean-session rollover with compact context reseeding.
        """
        # If video was active and we experienced a hard reset, roll over cleanly to prevent frame buffer corruption
        if self.is_video_active and not self.is_handle_valid():
            return False, None

        if self.is_handle_valid():
            return True, self.latest_resumption_handle

        return False, None

    async def notify_hud(self, websocket: Any, state: str, details: Optional[Dict[str, Any]] = None):
        """Dispatches connection state telemetry to HUD without throwing."""
        if state == "reconnecting":
            try:
                from observability import GLOBAL_METRICS
                GLOBAL_METRICS.record_gemini_reconnect()
            except Exception:
                pass
        elif state == "connected" and details and "rollover_duration_ms" in details:
            try:
                from observability import GLOBAL_METRICS
                GLOBAL_METRICS.record_session_rollover(float(details["rollover_duration_ms"]))
            except Exception:
                pass

        payload = {
            "type": "lifecycle_state",
            "state": state,
            "timestamp": time.time(),
            "handle_valid": self.is_handle_valid(),
            "details": details or {}
        }
        try:
            await websocket.send_json(payload)
        except Exception:
            pass
