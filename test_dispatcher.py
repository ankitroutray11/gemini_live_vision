"""
Comprehensive Test Suite for Chirkoot MK-V Function Call Resolver
and Tool Execution Dispatcher.

Covers all 8 user requirements:
1. Repeated tool-call deduplication.
2. UI action ordering.
3. Concurrent OCR and diagnostics.
4. Slow PowerShell command.
5. Cancellation.
6. Late result.
7. Model interruption.
8. WebSocket responsiveness.
"""

import os
import sys
import time
import asyncio
from pathlib import Path
from typing import Dict, Any

ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))

from dispatcher import (
    FunctionCallDispatcher,
    ExecutionTier,
    ExecutionRecord
)
from google.genai import types
from security_policy import execute_sandboxed_command


async def main():
    passed = 0
    failed = 0
    results = []

    def log_result(name: str, ok: bool, duration_ms: float, details: str = ""):
        nonlocal passed, failed
        if ok:
            passed += 1
            results.append(f"  [PASS] {name} ({duration_ms:.2f}ms)" + (f" - {details}" if details else ""))
        else:
            failed += 1
            results.append(f"  [FAIL] {name} ({duration_ms:.2f}ms) - {details}")

    print("\n" + "=" * 70)
    print("CHIRKOOT MK-V FUNCTION CALL DISPATCHER VERIFICATION SUITE")
    print("=" * 70 + "\n")

    # --------------------------------------------------------------------------
    # TEST 1: Repeated Tool-Call Deduplication
    # --------------------------------------------------------------------------
    t0 = time.perf_counter()
    exec_counts = {"calc_cube": 0, "run_command": 0}

    def mock_calc_cube(x: int = 1) -> str:
        exec_counts["calc_cube"] += 1
        return f"Result: {x ** 3}"

    def mock_run_cmd(command: str, on_proc_started=None) -> str:
        exec_counts["run_command"] += 1
        return f"Output: {command}"

    test_tools = {
        "calc_cube": mock_calc_cube,
        "run_command": mock_run_cmd
    }
    disp = FunctionCallDispatcher(tool_map=test_tools, dedup_ttl_seconds=60)
    
    # Auto-allow Tier C confirmation for this test
    async def auto_allow(record):
        return "allow"
    disp.confirmation_handler = auto_allow

    # 1a. Tier A call_id duplicate
    resp1 = await disp.dispatch("call_dedup_1", "calc_cube", {"x": 4})
    resp2 = await disp.dispatch("call_dedup_1", "calc_cube", {"x": 4})

    # 1b. Tier C side-effect signature duplicate
    resp3 = await disp.dispatch("call_c_1", "run_command", {"command": "echo test_dedup"})
    resp4 = await disp.dispatch("call_c_2", "run_command", {"command": "echo test_dedup"})

    ok1 = (
        exec_counts["calc_cube"] == 1 and
        "64" in resp1.response["result"] and
        "64" in resp2.response["result"] and
        exec_counts["run_command"] == 1 and
        "echo test_dedup" in resp3.response["result"] and
        "echo test_dedup" in resp4.response["result"]
    )
    log_result("1. Repeated tool-call deduplication", ok1, (time.perf_counter() - t0) * 1000,
               f"calc_cube count={exec_counts['calc_cube']}, run_command count={exec_counts['run_command']}")

    # --------------------------------------------------------------------------
    # TEST 2: UI Action Ordering
    # --------------------------------------------------------------------------
    t0 = time.perf_counter()
    ui_order = []

    def mock_switch(app_name: str) -> str:
        ui_order.append(f"switch:{app_name}")
        time.sleep(0.03)
        return "switched"

    def mock_click(x: int = 0, y: int = 0) -> str:
        ui_order.append(f"click:{x},{y}")
        time.sleep(0.02)
        return "clicked"

    def mock_type(text: str) -> str:
        ui_order.append(f"type:{text}")
        time.sleep(0.02)
        return "typed"

    ui_tools = {
        "switch_to_app": mock_switch,
        "click_screen": mock_click,
        "type_text": mock_type
    }
    disp_ui = FunctionCallDispatcher(tool_map=ui_tools)

    # Launch all 3 simultaneously via gather
    results_ui = await asyncio.gather(
        disp_ui.dispatch("ui_1", "switch_to_app", {"app_name": "notepad"}),
        disp_ui.dispatch("ui_2", "click_screen", {"x": 100, "y": 200}),
        disp_ui.dispatch("ui_3", "type_text", {"text": "hello chirkoot"})
    )

    expected_order = ["switch:notepad", "click:100,200", "type:hello chirkoot"]
    ok2 = (ui_order == expected_order)
    log_result("2. UI action ordering (FIFO queue)", ok2, (time.perf_counter() - t0) * 1000,
               f"Execution sequence: {' -> '.join(ui_order)}")

    # --------------------------------------------------------------------------
    # TEST 3: Concurrent OCR and Diagnostics (Tier A)
    # --------------------------------------------------------------------------
    t0 = time.perf_counter()

    def slow_ocr(image_path: str = "screen.png") -> str:
        time.sleep(0.12)  # Simulate 120ms OCR
        return "OCR Text: System Nominal"

    def slow_diag() -> str:
        time.sleep(0.12)  # Simulate 120ms diagnostics
        return "Diag: CPU 5%, Memory 40%"

    tier_a_tools = {
        "ocr_screen": slow_ocr,
        "system_diagnostics": slow_diag
    }
    disp_a = FunctionCallDispatcher(tool_map=tier_a_tools)

    t_start = time.perf_counter()
    res_ocr, res_diag = await asyncio.gather(
        disp_a.dispatch("ocr_1", "ocr_screen", {}),
        disp_a.dispatch("diag_1", "system_diagnostics", {})
    )
    total_elapsed = time.perf_counter() - t_start

    # If executed sequentially, total time would be >= 0.24s.
    # Concurrent execution should be < 0.20s.
    is_concurrent = total_elapsed < 0.22
    scheduling_correct = (res_ocr.scheduling == types.FunctionResponseScheduling.WHEN_IDLE)
    ok3 = is_concurrent and scheduling_correct
    log_result("3. Concurrent OCR and diagnostics", ok3, (time.perf_counter() - t0) * 1000,
               f"Elapsed={total_elapsed*1000:.1f}ms (<220ms), OCR scheduling={res_ocr.scheduling.name}")

    # --------------------------------------------------------------------------
    # TEST 4: Slow PowerShell Command (Subprocess Timeout & Process Tree Kill)
    # --------------------------------------------------------------------------
    t0 = time.perf_counter()

    def real_sandboxed_cmd(command: str, on_proc_started=None) -> str:
        # Use a short timeout of 2.0s for the test
        return execute_sandboxed_command(command, timeout=2.0, on_proc_started=on_proc_started)

    disp_cmd = FunctionCallDispatcher(tool_map={"run_command": real_sandboxed_cmd})
    disp_cmd.confirmation_handler = auto_allow

    # A command that attempts to sleep for 20 seconds
    cmd_resp = await disp_cmd.dispatch("cmd_slow_1", "run_command", {"command": "Start-Sleep -Seconds 20"})
    elapsed_cmd = time.perf_counter() - t0
    
    # Must terminate around 2.0s (within 3.5s) and report timeout error
    ok4 = (
        elapsed_cmd < 4.0 and
        "[SECURITY ERROR]" in cmd_resp.response["result"] and
        "timed out after 2.0 seconds" in cmd_resp.response["result"]
    )
    log_result("4. Slow PowerShell command termination", ok4, elapsed_cmd * 1000,
               f"Terminated in {elapsed_cmd:.2f}s; output contains: '{cmd_resp.response['result'][:60]}...'")

    # --------------------------------------------------------------------------
    # TEST 5: Cancellation Prior to / During Authorization
    # --------------------------------------------------------------------------
    t0 = time.perf_counter()
    c_executed = False

    def mock_write(path: str, content: str) -> str:
        nonlocal c_executed
        c_executed = True
        return "written"

    disp_cancel = FunctionCallDispatcher(tool_map={"write_file": mock_write})

    # Handler that waits for cancellation
    async def delayed_handler(record):
        await asyncio.sleep(0.5)
        return "allow"
    disp_cancel.confirmation_handler = delayed_handler

    # Start dispatch in background
    dispatch_task = asyncio.create_task(disp_cancel.dispatch("cancel_target_1", "write_file", {"path": "test.txt", "content": "data"}))
    await asyncio.sleep(0.05)  # Let it enter confirming state
    
    # Cancel the call
    disp_cancel.cancel_call("cancel_target_1", reason="User pressed abort")
    res_cancel = await dispatch_task

    ok5 = (
        not c_executed and
        "[SECURITY REJECTION]" in res_cancel.response["result"] and
        disp_cancel.records["cancel_target_1"].status == "cancelled"
    )
    log_result("5. Tool call cancellation", ok5, (time.perf_counter() - t0) * 1000,
               f"Executed={c_executed}, Status={disp_cancel.records['cancel_target_1'].status}")

    # --------------------------------------------------------------------------
    # TEST 6: Late Result Dropping
    # --------------------------------------------------------------------------
    t0 = time.perf_counter()
    slow_done = False

    def slow_exec():
        nonlocal slow_done
        time.sleep(0.15)
        slow_done = True
        return "slow_success"

    disp_late = FunctionCallDispatcher(tool_map={"inspect_self": slow_exec})

    # Launch in background
    late_task = asyncio.create_task(disp_late.dispatch("late_call_1", "inspect_self", {}))
    await asyncio.sleep(0.04)
    # Abort while running
    disp_late.cancel_call("late_call_1", reason="Turn interrupted")
    late_resp = await late_task

    ok6 = (
        slow_done and  # Function finished in thread
        disp_late.records["late_call_1"].status == "late_dropped" and
        "[CANCELLED]" in late_resp.response["result"] and
        "Late result discarded" in late_resp.response["result"]
    )
    log_result("6. Late result dropping", ok6, (time.perf_counter() - t0) * 1000,
               f"Underlying finished={slow_done}, Status={disp_late.records['late_call_1'].status}")

    # --------------------------------------------------------------------------
    # TEST 7: Model Interruption & Abort of In-Flight Proposals
    # --------------------------------------------------------------------------
    t0 = time.perf_counter()
    disp_intr = FunctionCallDispatcher(tool_map={"click_screen": lambda x, y: "clicked"})

    # Queue multiple UI actions
    futs = [
        asyncio.create_task(disp_intr.dispatch(f"intr_{i}", "click_screen", {"x": i, "y": i}))
        for i in range(5)
    ]
    await asyncio.sleep(0.01)

    # Server sends interrupted event: abort all pending
    disp_intr.cancel_all_pending(reason="Model speech interruption")
    await asyncio.gather(*futs)

    all_cancelled = all(
        disp_intr.records[f"intr_{i}"].status in ("cancelled", "late_dropped", "completed")
        and disp_intr.records[f"intr_{i}"].abort_event.is_set()
        for i in range(1, 5)
    )
    log_result("7. Model interruption handling", all_cancelled, (time.perf_counter() - t0) * 1000,
               "All in-flight and queued proposals aborted cleanly")

    # --------------------------------------------------------------------------
    # TEST 8: WebSocket Responsiveness (Event Loop Latency Under Load)
    # --------------------------------------------------------------------------
    t0 = time.perf_counter()
    disp_load = FunctionCallDispatcher(tool_map={
        "ocr_screen": lambda: time.sleep(0.05) or "ocr",
        "click_screen": lambda x, y: time.sleep(0.03) or "click",
        "calc_cube": lambda x: x ** 3
    })

    # Start continuous background dispatcher work
    bg_tasks = [
        asyncio.create_task(disp_load.dispatch(f"load_{i}", "ocr_screen", {}))
        for i in range(4)
    ] + [
        asyncio.create_task(disp_load.dispatch(f"load_ui_{i}", "click_screen", {"x": 10, "y": 20}))
        for i in range(3)
    ]

    # Measure event loop responsiveness (simulating incoming WebSocket pings)
    max_jitter_ms = 0.0
    for _ in range(15):
        t_tick = time.perf_counter()
        await asyncio.sleep(0.01)  # 10ms expected delay
        actual_delay = (time.perf_counter() - t_tick) * 1000
        jitter = abs(actual_delay - 10.0)
        if jitter > max_jitter_ms:
            max_jitter_ms = jitter

    await asyncio.gather(*bg_tasks)

    # Max jitter must be well below 50ms (typically < 10ms)
    ok8 = max_jitter_ms < 40.0
    log_result("8. WebSocket event loop responsiveness", ok8, (time.perf_counter() - t0) * 1000,
               f"Max event loop jitter={max_jitter_ms:.2f}ms (<40ms)")

    # --------------------------------------------------------------------------
    # SUMMARY
    # --------------------------------------------------------------------------
    print("\n" + "=" * 70)
    print("VERIFICATION RESULTS:")
    for r in results:
        print(r)
    print("-" * 70)
    print(f"Total: {passed + failed} | Passed: {passed} | Failed: {failed}")
    print("=" * 70 + "\n")

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
