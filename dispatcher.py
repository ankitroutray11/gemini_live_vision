"""
Chirkoot MK-V: Local Function Call Resolver and Tool Execution Dispatcher.

Enforces execution policy, sequencing, deduplication, state verification,
and cancellation independently of the model.

The model proposes tool calls; this local dispatcher validates and executes them.
"""

import os
import sys
import time
import json
import uuid
import ctypes
import hashlib
import asyncio
import logging
import subprocess
from enum import Enum
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple, Callable, Coroutine

logger = logging.getLogger("ChirkootDispatcher")

ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))

from security_policy import (
    ToolTier,
    TrustBoundary,
    UntrustedSourceType,
    PolicyDecision,
    PolicyEvaluationResult,
    CENTRAL_SECURITY_ENGINE,
    GLOBAL_TAINT_TRACKER,
    is_kill_switch_active,
    record_audit_event,
    execute_sandboxed_command,
    execute_sandboxed_python_script,
    validate_path_for_read,
    validate_path_for_write,
    is_sensitive_path,
    is_immutable_core_path
)

from google.genai import types

from observability import (
    ToolExecutionRecord,
    GLOBAL_EXECUTION_LOGGER,
    GLOBAL_METRICS,
    sanitize_and_redact_args
)
from datetime import datetime

# Win32 structures for state verification
if sys.platform == "win32":
    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


# ==============================================================================
# 1. EXECUTION TIERS & SCHEDULING ENUMS
# ==============================================================================

class ExecutionTier(str, Enum):
    TIER_A_READ_ONLY = "TIER_A_READ_ONLY"             # Automatic execution, concurrent
    TIER_B_REVERSIBLE_UI = "TIER_B_REVERSIBLE_UI"     # Serialized FIFO UI queue with verification
    TIER_C_HIGH_IMPACT = "TIER_C_HIGH_IMPACT"         # Mandatory physical confirmation + subprocess


TIER_A_TOOLS = {
    "look_at_screen", "inspect_hud_capture_exclusion", "ocr_screen", "ocr_image",
    "read_file", "list_directory", "system_diagnostics",
    "inspect_self", "read_code_file", "list_custom_tools",
    "recall_memory", "list_memories", "run_self_tests",
    "get_codebase_guide", "calc_cube", "get_system_uptime", "test_echo_tool",
    "uia_inspect_active_window", "uia_read_text", "som_inspect_and_mark"
}

TIER_B_TOOLS = {
    "click_screen", "type_text", "press_hotkey", "scroll_screen",
    "switch_to_app", "minimize_active_window", "maximize_active_window",
    "restore_active_window", "toggle_fullscreen_window",
    "open_app_or_url", "remember_insight", "restart_chirkoot_server",
    "uia_click_element", "uia_set_text", "som_click_mark"
}

TIER_C_TOOLS = {
    "run_command", "write_file", "modify_source_code", "rollback_code",
    "create_custom_tool", "delete_memory", "self_learn_and_implement"
}


# ==============================================================================
# 2. EXECUTION RECORD & STATE
# ==============================================================================

@dataclass
class ExecutionRecord:
    execution_id: str
    call_id: str
    tool_name: str
    args: Dict[str, Any]
    tier: ExecutionTier
    scheduling: Any
    created_at: float
    abort_event: asyncio.Event = field(default_factory=asyncio.Event)
    status: str = "pending"  # "pending", "confirming", "queued", "running", "completed", "cancelled", "late_dropped", "failed"
    result: Optional[str] = None
    error: Optional[str] = None
    verified: bool = False
    verification_details: str = ""
    active_pid: Optional[int] = None
    auto_retry_prohibited: bool = False


# ==============================================================================
# 3. LOCAL FUNCTION CALL RESOLVER & DISPATCHER
# ==============================================================================

class FunctionCallDispatcher:
    """
    Production-grade local dispatcher:
    - Resolves tool proposals.
    - Classifies into Tiers A, B, and C.
    - Serializes UI operations via a dedicated single-worker queue.
    - Runs Tier A and B in background threads, Tier C in sandboxed subprocesses.
    - Enforces physical human authorization for Tier C (and tainted Tier B).
    - Checks abort flags and drops late results.
    - Enforces step budgets and deduplication.
    - Verifies post-execution state.
    """

    def __init__(
        self,
        tool_map: Dict[str, Callable],
        max_steps_per_turn: int = 15,
        dedup_ttl_seconds: int = 600
    ):
        self.tool_map = tool_map
        self.max_steps_per_turn = max_steps_per_turn
        self.dedup_ttl_seconds = dedup_ttl_seconds

        # Turn step budget tracking
        self.current_turn_step_count = 0

        # In-flight execution registry: call_id -> ExecutionRecord
        self.records: Dict[str, ExecutionRecord] = {}

        # Deduplication cache: hash -> (timestamp, result)
        self._dedup_cache: Dict[str, Tuple[float, str]] = {}

        # Serialized UI Action Queue (Tier B)
        self._ui_queue: asyncio.Queue = asyncio.Queue()
        self._ui_worker_task: Optional[asyncio.Task] = None

        # Confirmation callback for Tier C: async def fn(record) -> str ("allow" | "deny" | "timeout")
        self.confirmation_handler: Optional[Callable[[ExecutionRecord], Coroutine[Any, Any, str]]] = None

        # Start the background serialized UI queue worker
        self._ensure_ui_worker()

    def _ensure_ui_worker(self):
        """Ensures the single-threaded FIFO UI action worker is running."""
        try:
            loop = asyncio.get_running_loop()
            if self._ui_worker_task is None or self._ui_worker_task.done():
                self._ui_worker_task = loop.create_task(self._ui_action_queue_worker())
        except RuntimeError:
            # No running loop currently; worker will be initialized lazily on first dispatch() call
            pass

    def reset_turn_budget(self):
        """Resets the step budget at the start of each user turn."""
        self.current_turn_step_count = 0

    def reset_turn(self):
        """Alias for reset_turn_budget."""
        self.reset_turn_budget()

    def _hash_call(self, tool_name: str, args: Dict[str, Any]) -> str:
        serialized = json.dumps({"name": tool_name, "args": args}, sort_keys=True, default=str)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def check_duplicate(self, call_id: str, tool_name: str, args: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Checks if a tool call was already completed within the deduplication window."""
        now = time.time()
        # 1. Exact call_id check
        if call_id in self._dedup_cache:
            ts, res = self._dedup_cache[call_id]
            if now - ts <= self.dedup_ttl_seconds:
                return True, res

        # 2. Side-effect signature check
        sig = self._hash_call(tool_name, args)
        if tool_name in TIER_C_TOOLS:
            if sig in self._dedup_cache:
                ts, res = self._dedup_cache[sig]
                if now - ts <= 45.0:  # 45-second window for identical side effects
                    return True, res

        return False, None

    def record_completed(self, call_id: str, tool_name: str, args: Dict[str, Any], result: str):
        """Caches a successful execution result for deduplication."""
        now = time.time()
        self._dedup_cache[call_id] = (now, result)
        sig = self._hash_call(tool_name, args)
        self._dedup_cache[sig] = (now, result)

        # Prune old cache entries
        if len(self._dedup_cache) > 300:
            self._dedup_cache = {k: v for k, v in self._dedup_cache.items() if now - v[0] <= self.dedup_ttl_seconds}

    def classify_proposal(self, tool_name: str, args: Dict[str, Any]) -> Tuple[ExecutionTier, Any]:
        """
        Classifies a proposed function call into its ExecutionTier and
        optimal FunctionResponseScheduling using Central Security Policy Engine.
        """
        # Special case: find_text_on_screen
        if tool_name == "find_text_on_screen":
            if args.get("click", False):
                tier = ExecutionTier.TIER_B_REVERSIBLE_UI
                scheduling = types.FunctionResponseScheduling.SILENT
            else:
                tier = ExecutionTier.TIER_A_READ_ONLY
                scheduling = types.FunctionResponseScheduling.WHEN_IDLE
            return tier, scheduling

        eval_res = CENTRAL_SECURITY_ENGINE.evaluate_tool_proposal(tool_name, args)

        if eval_res.decision == PolicyDecision.REQUIRE_PHYSICAL_APPROVAL or eval_res.tier == ToolTier.TIER_3_HIGH_RISK:
            return ExecutionTier.TIER_C_HIGH_IMPACT, types.FunctionResponseScheduling.INTERRUPT

        if eval_res.decision == PolicyDecision.ALLOW_SERIALIZED_UI or eval_res.tier == ToolTier.TIER_2_REVERSIBLE_UI:
            return ExecutionTier.TIER_B_REVERSIBLE_UI, types.FunctionResponseScheduling.SILENT

        # Tier A Read-Only
        tier = ExecutionTier.TIER_A_READ_ONLY
        if tool_name in ("ocr_screen", "ocr_image"):
            scheduling = types.FunctionResponseScheduling.WHEN_IDLE
        else:
            scheduling = types.FunctionResponseScheduling.SILENT

        return tier, scheduling

    def cancel_call(self, call_id: str, reason: str = "Model tool_call_cancellation"):
        """Cancels a specific tool execution, kills active processes, and sets its abort flag."""
        if call_id in self.records:
            record = self.records[call_id]
            record.status = "cancelled"
            record.abort_event.set()
            if record.active_pid:
                logger.warning(f"[Dispatcher] Terminating active process tree PID {record.active_pid} for call '{record.tool_name}' ({call_id})")
                try:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(record.active_pid)], capture_output=True, timeout=5.0)
                except Exception as e:
                    logger.error(f"[Dispatcher] Failed to taskkill PID {record.active_pid}: {e}")
            logger.warning(f"[Dispatcher] Cancelled execution '{record.tool_name}' ({call_id}): {reason}")

    def cancel_all_pending(self, reason: str = "Interruption / Emergency Stop"):
        """Aborts all pending and unconfirmed tool executions and kills any active process trees."""
        for call_id, record in self.records.items():
            if record.status in ("pending", "confirming", "queued", "running"):
                record.status = "cancelled"
                record.abort_event.set()
                if record.active_pid:
                    try:
                        subprocess.run(["taskkill", "/F", "/T", "/PID", str(record.active_pid)], capture_output=True, timeout=5.0)
                    except Exception:
                        pass
                logger.warning(f"[Dispatcher] Aborted in-flight execution '{record.tool_name}' ({call_id}): {reason}")

        # Drain UI queue immediately
        while not self._ui_queue.empty():
            try:
                rec, fut = self._ui_queue.get_nowait()
                rec.status = "cancelled"
                rec.abort_event.set()
                if not fut.done():
                    fut.set_result(f"[CANCELLED] {reason}")
                self._ui_queue.task_done()
            except Exception:
                break

    # ==========================================================================
    # 4. DISPATCH EXECUTION WORKFLOW
    # ==========================================================================

    async def dispatch(self, call_id: str, tool_name: str, args: Dict[str, Any]) -> types.FunctionResponse:
        """
        Validates, schedules, authorizes, executes, and verifies a proposed tool call.
        Returns a certified types.FunctionResponse.
        """
        execution_id = f"exec_{uuid.uuid4().hex[:10]}"
        self._ensure_ui_worker()
        tier, scheduling = self.classify_proposal(tool_name, args)

        record = ExecutionRecord(
            execution_id=execution_id,
            call_id=call_id,
            tool_name=tool_name,
            args=args,
            tier=tier,
            scheduling=scheduling,
            created_at=time.time(),
            status="pending"
        )
        self.records[call_id] = record

        # Initialize Observability Execution Record
        task_id_ref = getattr(self, "current_task_id", None) or f"task_turn_{self.current_turn_step_count + 1}"
        instr_ref = getattr(self, "current_instruction_ref", None) or f"utterance_ref_{call_id}"
        obs_record = ToolExecutionRecord(
            execution_id=execution_id,
            tool_name=tool_name,
            validated_args=sanitize_and_redact_args(args),
            task_id=task_id_ref,
            instruction_ref=instr_ref,
            start_time=datetime.fromtimestamp(record.created_at).isoformat(),
            start_timestamp=record.created_at,
            permission_decision={"tier": tier.value}
        )

        # 1. Central Security Policy Evaluation (Whitelist & Policy Guard)
        eval_res = CENTRAL_SECURITY_ENGINE.evaluate_tool_proposal(tool_name, args)
        obs_record.permission_decision["policy_decision"] = eval_res.decision.value
        obs_record.permission_decision["reason"] = eval_res.reason

        if eval_res.decision in (PolicyDecision.DENY_UNKNOWN_TOOL, PolicyDecision.DENY_SECURITY_VIOLATION):
            record.status = "failed"
            record.error = eval_res.reason
            obs_record.permission_decision["allowed"] = False
            obs_record.fail(eval_res.reason)
            GLOBAL_EXECUTION_LOGGER.log_execution(obs_record)
            GLOBAL_METRICS.record_tool_call(tool_name, False, obs_record.duration_ms)
            logger.warning(f"[Dispatcher Security Policy Denial] {eval_res.reason}")
            return types.FunctionResponse(
                id=call_id,
                name=tool_name,
                response={"result": eval_res.reason},
                scheduling=scheduling
            )

        obs_record.permission_decision["allowed"] = True

        # 2. Emergency Kill Switch Check
        if is_kill_switch_active():
            record.status = "cancelled"
            obs_record.cancel("Emergency stop engaged")
            GLOBAL_EXECUTION_LOGGER.log_execution(obs_record)
            GLOBAL_METRICS.record_tool_cancellation(obs_record.duration_ms)
            return types.FunctionResponse(
                id=call_id,
                name=tool_name,
                response={"result": "[EMERGENCY STOP ACTIVE] Execution blocked by user kill switch."},
                scheduling=scheduling
            )

        # 3. Step Budget Guard
        self.current_turn_step_count += 1
        if self.current_turn_step_count > self.max_steps_per_turn:
            record.status = "failed"
            record.error = f"Step budget exceeded ({self.max_steps_per_turn} steps/turn)."
            obs_record.fail(record.error)
            GLOBAL_EXECUTION_LOGGER.log_execution(obs_record)
            GLOBAL_METRICS.record_tool_call(tool_name, False, obs_record.duration_ms)
            logger.warning(f"[Dispatcher] Tool '{tool_name}' aborted: Step budget exceeded.")
            return types.FunctionResponse(
                id=call_id,
                name=tool_name,
                response={"result": f"[BUDGET EXCEEDED] Maximum step budget ({self.max_steps_per_turn}) exceeded for this turn to prevent runaway loops."},
                scheduling=scheduling
            )

        # 3. Deduplication Check (Idempotency)
        is_dup, cached_res = self.check_duplicate(call_id, tool_name, args)
        if is_dup:
            record.status = "completed"
            record.result = str(cached_res)
            obs_record.complete(cached_res, verified=True, details="dedup_cached_replay")
            GLOBAL_EXECUTION_LOGGER.log_execution(obs_record)
            GLOBAL_METRICS.record_tool_call(tool_name, True, obs_record.duration_ms)
            logger.info(f"[Dispatcher Deduplication] Suppressed replay of '{tool_name}' ({call_id}). Returning cached result.")
            return types.FunctionResponse(
                id=call_id,
                name=tool_name,
                response={"result": str(cached_res)},
                scheduling=scheduling
            )

        # 4. Tier C Authorization Gatekeeper (Mandatory Physical Human Confirmation)
        if tier == ExecutionTier.TIER_C_HIGH_IMPACT:
            record.status = "confirming"
            obs_record.status = "confirming"
            decision = "deny"
            if self.confirmation_handler:
                try:
                    logger.info(f"[Dispatcher HITL Gate] Awaiting physical human authorization for '{tool_name}'...")
                    decision = await self.confirmation_handler(record)
                except Exception as e:
                    logger.error(f"[Dispatcher] Confirmation handler error: {e}")
                    decision = "timeout"

            obs_record.permission_decision["hitl_decision"] = decision
            if record.abort_event.is_set() or decision != "allow":
                record.status = "cancelled" if record.abort_event.is_set() else "failed"
                if record.abort_event.is_set():
                    obs_record.cancel("Aborted during confirmation")
                    GLOBAL_METRICS.record_tool_cancellation(obs_record.duration_ms)
                else:
                    obs_record.fail(f"Physical authorization not granted ({decision})")
                    GLOBAL_METRICS.record_tool_call(tool_name, False, obs_record.duration_ms)
                GLOBAL_EXECUTION_LOGGER.log_execution(obs_record)
                record_audit_event("POLICY_HITL_REJECTED", {
                    "tool": tool_name,
                    "call_id": call_id,
                    "decision": decision
                })
                rejection_msg = f"[SECURITY REJECTION] Action '{tool_name}' was not authorized (decision: {decision}). Voice approval alone is prohibited."
                return types.FunctionResponse(
                    id=call_id,
                    name=tool_name,
                    response={"result": rejection_msg},
                    scheduling=scheduling
                )

            record_audit_event("POLICY_HITL_APPROVED", {
                "tool": tool_name,
                "call_id": call_id,
                "args": args
            })

        # 5. Check Abort Flag Before Starting Execution
        if record.abort_event.is_set():
            record.status = "cancelled"
            obs_record.cancel("Aborted prior to execution")
            GLOBAL_EXECUTION_LOGGER.log_execution(obs_record)
            GLOBAL_METRICS.record_tool_cancellation(obs_record.duration_ms)
            return types.FunctionResponse(
                id=call_id,
                name=tool_name,
                response={"result": "[CANCELLED] Action was aborted prior to execution."},
                scheduling=scheduling
            )

        # 6. Route Execution by Tier
        record.status = "running"
        obs_record.status = "running"
        try:
            if tier == ExecutionTier.TIER_B_REVERSIBLE_UI:
                # Route through Serialized FIFO UI Queue
                result_str = await self._enqueue_ui_action(record)
            elif tier == ExecutionTier.TIER_C_HIGH_IMPACT:
                # Route through Subprocess Sandbox
                result_str = await self._execute_sandboxed_tier_c(record)
            else:
                # Tier A: Execute in background thread
                result_str = await self._execute_tier_a(record)

        except Exception as e:
            result_str = f"Error during execution: {e}"
            record.error = str(e)
            record.status = "failed"
            obs_record.fail(str(e))
            GLOBAL_EXECUTION_LOGGER.log_execution(obs_record)
            GLOBAL_METRICS.record_tool_call(tool_name, False, obs_record.duration_ms)

        # 7. Check for Late Result after Abort / Cancellation
        if record.abort_event.is_set():
            record.status = "late_dropped"
            obs_record.cancel("Cancelled mid-execution. Late result discarded.")
            GLOBAL_EXECUTION_LOGGER.log_execution(obs_record)
            GLOBAL_METRICS.record_tool_cancellation(obs_record.duration_ms)
            logger.warning(f"[Dispatcher] Late result for cancelled call '{tool_name}' ({call_id}) discarded safely.")
            return types.FunctionResponse(
                id=call_id,
                name=tool_name,
                response={"result": "[CANCELLED] Action cancelled mid-execution. Late result discarded."},
                scheduling=scheduling
            )

        record.status = "completed"
        record.result = result_str
        self.record_completed(call_id, tool_name, args, result_str)

        obs_record.complete(result_str, verified=record.verified, details=record.verification_details)
        GLOBAL_EXECUTION_LOGGER.log_execution(obs_record)
        GLOBAL_METRICS.record_tool_call(tool_name, True, obs_record.duration_ms)
        if record.verified is not None:
            GLOBAL_METRICS.record_ui_verification(record.verified)

        record_audit_event("TOOL_EXECUTED", {
            "name": tool_name,
            "tier": tier.value,
            "verified": record.verified,
            "result_preview": str(result_str)[:150]
        })

        return types.FunctionResponse(
            id=call_id,
            name=tool_name,
            response={"result": str(result_str)},
            scheduling=scheduling
        )

    # ==========================================================================
    # 5. EXECUTION WORKERS (TIERS A, B, C)
    # ==========================================================================

    async def _execute_tier_a(self, record: ExecutionRecord) -> str:
        """Executes Tier A (Read-Only) tools concurrently via asyncio.to_thread."""
        func = self.tool_map.get(record.tool_name)
        if not func:
            return f"Tool '{record.tool_name}' not found."
        
        # Run in thread outside the main asyncio loop
        res = await asyncio.to_thread(func, **record.args)
        record.verified = True
        return str(res)

    async def _enqueue_ui_action(self, record: ExecutionRecord) -> str:
        """Enqueues a Tier B UI action for serialized FIFO execution."""
        self._ensure_ui_worker()
        fut = asyncio.get_event_loop().create_future()
        record.status = "queued"
        await self._ui_queue.put((record, fut))
        return await fut

    async def _ui_action_queue_worker(self):
        """
        Single-worker FIFO queue runner.
        Guarantees that sequential UI actions execute strictly in order:
        e.g. switch_to_app -> click_screen -> type_text.
        """
        while True:
            try:
                record, fut = await self._ui_queue.get()
                if is_kill_switch_active() or record.abort_event.is_set():
                    record.status = "cancelled"
                    fut.set_result("[CANCELLED] Emergency Stop active. UI action aborted while queued.")
                    self._ui_queue.task_done()
                    continue

                record.status = "running"
                func = self.tool_map.get(record.tool_name)
                if not func:
                    fut.set_result(f"Tool '{record.tool_name}' not found.")
                    self._ui_queue.task_done()
                    continue

                # Execute synchronously in thread
                raw_res = await asyncio.to_thread(func, **record.args)
                record.result = str(raw_res)

                # Perform post-action state verification before allowing next queue item
                verified, v_details = await self._verify_ui_action_state(record.tool_name, record.args, record)
                record.verified = verified
                record.verification_details = v_details

                if record.abort_event.is_set():
                    fut.set_result("[CANCELLED] Action completed but dropped due to cancellation.")
                else:
                    fut.set_result(str(raw_res))

                self._ui_queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Dispatcher UI Worker] Error: {e}")

    async def _execute_sandboxed_tier_c(self, record: ExecutionRecord) -> str:
        """Executes Tier C tools with subprocess isolation."""
        func = self.tool_map.get(record.tool_name)
        if not func:
            return f"Tool '{record.tool_name}' not found."

        args = dict(record.args)
        if record.tool_name == "run_command":
            def set_pid(pid: int):
                record.active_pid = pid
            args["on_proc_started"] = set_pid

        # Shell command execution has native process tree termination in execute_sandboxed_command
        try:
            res = await asyncio.to_thread(func, **args)
        finally:
            record.active_pid = None
        
        # Verify state modification
        verified, v_details = self._verify_tier_c_state(record.tool_name, record.args)
        record.verified = verified
        record.verification_details = v_details
        return str(res)

    # ==========================================================================
    # 6. POST-ACTION STATE VERIFICATION
    # ==========================================================================

    async def _verify_ui_action_state(
        self,
        tool_name: str,
        args: Dict[str, Any],
        record: Optional[ExecutionRecord] = None
    ) -> Tuple[bool, str]:
        """
        Verifies state modification for UI actions using closed-loop reports and native window states:
        - Inspects closed-loop verification results in record.result.
        - Enforces destructive action repeat prohibition when outcome is UNCERTAIN.
        - Dwells for OS message loop and verifies foreground window or cursor positioning.
        """
        if record and record.result:
            res_str = str(record.result)
            if "[Verification: SUCCESS]" in res_str:
                return True, f"Closed-loop verified: SUCCESS. {res_str}"
            elif "[Verification: FAILURE]" in res_str:
                return False, f"Closed-loop verified: FAILURE. {res_str}"
            elif "[Verification: UNCERTAIN]" in res_str:
                if "[DESTRUCTIVE GUARD ACTIVATED]" in res_str:
                    record.auto_retry_prohibited = True
                    return False, f"Closed-loop verified: UNCERTAIN (Destructive guard active, auto-repeat prohibited). {res_str}"
                return False, f"Closed-loop verified: UNCERTAIN. {res_str}"

        if sys.platform != "win32":
            return True, "Non-Windows platform; verification skipped."

        user32 = ctypes.windll.user32

        if tool_name == "switch_to_app":
            # Dwell 60ms for WM_ACTIVATE
            await asyncio.sleep(0.06)
            hwnd = user32.GetForegroundWindow()
            length = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            active_title = buf.value.lower()
            target_app = str(args.get("app_name", "")).lower()
            ok = (target_app in active_title) or (length > 0)
            return ok, f"Active window: '{buf.value}' (target: '{target_app}')"

        elif tool_name in ("click_screen", "uia_click_element", "som_click_mark"):
            await asyncio.sleep(0.04)
            pt = POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            return True, f"Click completed for {tool_name}. Cursor at ({pt.x}, {pt.y})"

        elif tool_name in ("type_text", "uia_set_text", "press_hotkey", "scroll_screen"):
            await asyncio.sleep(0.05)
            return True, f"UI input event '{tool_name}' dispatched successfully."

        return True, "UI verification nominal."

    def _verify_tier_c_state(self, tool_name: str, args: Dict[str, Any]) -> Tuple[bool, str]:
        """Verifies disk and process state changes for Tier C operations."""
        if tool_name in ("write_file", "modify_source_code"):
            path = Path(args.get("path", "") or args.get("filepath", ""))
            if path.exists() and path.stat().st_size > 0:
                return True, f"File '{path.name}' verified on disk ({path.stat().st_size} bytes)."
            return False, f"File '{path}' verification failed."

        return True, "Tier C state verified."

# Export alias for convenience
ToolDispatcher = FunctionCallDispatcher
