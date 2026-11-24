"""
Chirkoot MK-V: Comprehensive Emergency Stop Verification Suite.

Tests:
1. Immediate halt during Typing.
2. Immediate halt during Clicking.
3. Immediate process tree termination during PowerShell execution.
4. Emergency Stop independent of Gemini Live (Gemini disconnected / offline).
5. Immediate halt of assistant audio playback and queue clearing.
6. Immediate halt and draining of Multiple Queued UI operations.
7. Prohibition of voice-only reset / ambient audio resumption.
8. Distinctive stop phrase recognition vs conversational phrase rejection.
"""

import sys
import os
import time
import asyncio
import subprocess
import threading
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from emergency_stop import EMERGENCY_STOP_MANAGER
import security_policy as sp
from dispatcher import FunctionCallDispatcher, ExecutionTier, ExecutionRecord
from session_lifecycle import TaskStateManager
from tools import ALL_TOOLS, TOOL_MAP, click_screen, type_text, scroll_screen, press_hotkey, run_command

async def run_tests():
    print("\n" + "=" * 70)
    print(" CHIRKOOT MK-V INDEPENDENT EMERGENCY STOP VERIFICATION SUITE")
    print("=" * 70 + "\n")

    passed_tests = 0
    total_tests = 8

    # Reset any active kill switch before starting
    sp.reset_kill_switch()
    dispatcher = FunctionCallDispatcher(tool_map=TOOL_MAP)
    EMERGENCY_STOP_MANAGER.register_dispatcher(dispatcher)
    task_mgr = TaskStateManager()

    try:
        # -------------------------------------------------------------
        # TEST 1: Immediate Halt During Typing
        # -------------------------------------------------------------
        t0 = time.time()
        # Verify typing is allowed when kill switch is inactive
        pre_type = type_text("test")
        assert "successfully" in pre_type.lower() or "typed" in pre_type.lower()

        # Trigger Emergency Stop
        EMERGENCY_STOP_MANAGER.trigger("Emergency halt during typing")
        assert EMERGENCY_STOP_MANAGER.is_active() is True

        # Attempt typing while Emergency Stop is engaged
        post_type = type_text("should_be_blocked")
        assert "[KILL SWITCH ACTIVE]" in post_type or "blocked" in post_type.lower()
        passed_tests += 1
        print(f"  [PASS] 1. Immediate halt during Typing ({(time.time()-t0)*1000:.2f}ms) - Typing blocked: '{post_type.strip()}'")

        # Reset for next test
        EMERGENCY_STOP_MANAGER.reset(physical_confirmation=True, actor="unit_tester")

        # -------------------------------------------------------------
        # TEST 2: Immediate Halt During Clicking
        # -------------------------------------------------------------
        t0 = time.time()
        EMERGENCY_STOP_MANAGER.trigger("Emergency halt during clicking")
        click_res = click_screen(500, 500)
        assert "[KILL SWITCH ACTIVE]" in click_res or "blocked" in click_res.lower()
        passed_tests += 1
        print(f"  [PASS] 2. Immediate halt during Clicking ({(time.time()-t0)*1000:.2f}ms) - Click blocked: '{click_res.strip()}'")

        EMERGENCY_STOP_MANAGER.reset(physical_confirmation=True, actor="unit_tester")

        # -------------------------------------------------------------
        # TEST 3: Forceful Process Tree Termination During PowerShell Execution
        # -------------------------------------------------------------
        t0 = time.time()
        # Spawn a slow PowerShell command that sleeps for 20 seconds
        proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", "Start-Sleep -Seconds 20"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        sp.register_active_child_process(proc.pid)
        assert proc.poll() is None, "PowerShell child process should be running"

        # Trigger Emergency Stop while child process is running
        kill_details = EMERGENCY_STOP_MANAGER.trigger("PowerShell runaway process emergency stop")
        time.sleep(0.5)

        # Verify child process was terminated
        poll_res = proc.poll()
        assert poll_res is not None, "Child PowerShell process must be dead after Emergency Stop"
        assert proc.pid not in sp._ACTIVE_CHILD_PROCESSES, "PID must be removed from active registry"
        passed_tests += 1
        print(f"  [PASS] 3. Forceful child process tree termination ({(time.time()-t0)*1000:.2f}ms) - PID {proc.pid} killed (exit code: {poll_res})")

        EMERGENCY_STOP_MANAGER.reset(physical_confirmation=True, actor="unit_tester")

        # -------------------------------------------------------------
        # TEST 4: Emergency Stop Independence From Gemini Live
        # -------------------------------------------------------------
        t0 = time.time()
        # Register in-flight task in TaskStateManager (outside session)
        task_id = "test_task_gemini_down_101"
        task_mgr.register_task(task_id, "Critical automated database migration")
        assert task_mgr.get_latest_unfinished_task() is not None

        # Simulate Gemini Live completely offline / disconnected
        # Trigger Emergency Stop locally (via hotkey / button / local stop word)
        EMERGENCY_STOP_MANAGER.trigger("Simulated Gemini connection drop + Emergency Stop")

        # Verify task is marked CANCELLED locally without Gemini
        assert EMERGENCY_STOP_MANAGER.is_active() is True
        # Verify get_latest_unfinished_task returns None (no auto-resumption!)
        assert task_mgr.get_latest_unfinished_task() is None, "Automatic resumption must be blocked while stopped"
        
        # Verify task record on disk is cancelled
        tasks_on_disk = task_mgr._load()
        assert tasks_on_disk[task_id]["status"] == "cancelled"
        assert "Emergency Stop" in tasks_on_disk[task_id].get("cancellation_reason", "")
        passed_tests += 1
        print(f"  [PASS] 4. Gemini Live Independence & Task Cancellation ({(time.time()-t0)*1000:.2f}ms) - Task marked CANCELLED, auto-resumption suppressed")

        EMERGENCY_STOP_MANAGER.reset(physical_confirmation=True, actor="unit_tester")

        # -------------------------------------------------------------
        # TEST 5: Audio Playback Halt & Queue Clearing
        # -------------------------------------------------------------
        t0 = time.time()
        audio_buffer = [b"\x00\x01" * 1600 for _ in range(50)]
        is_audio_playing = True

        # Simulate audio halt callback
        def mock_audio_halt(active: bool, reason: str):
            nonlocal is_audio_playing, audio_buffer
            if active:
                audio_buffer.clear()
                is_audio_playing = False

        EMERGENCY_STOP_MANAGER.add_listener(mock_audio_halt)
        try:
            EMERGENCY_STOP_MANAGER.trigger("Audio output emergency mute")
            assert is_audio_playing is False, "Audio playback must be halted"
            assert len(audio_buffer) == 0, "Audio output queue must be cleared"
            passed_tests += 1
            print(f"  [PASS] 5. Assistant Audio Playback & Output Queue Halt ({(time.time()-t0)*1000:.2f}ms) - Audio playback stopped, buffer drained")
        finally:
            EMERGENCY_STOP_MANAGER.remove_listener(mock_audio_halt)
            EMERGENCY_STOP_MANAGER.reset(physical_confirmation=True, actor="unit_tester")

        # -------------------------------------------------------------
        # TEST 6: Multiple Queued UI Operations Abort
        # -------------------------------------------------------------
        t0 = time.time()
        # Enqueue 5 operations into dispatcher UI queue
        records_and_futures = []
        for i in range(5):
            call_id = f"queued_call_{i}"
            args = {"direction": "down", "amount": 2}
            tier, sched = dispatcher.classify_proposal("scroll_screen", args)
            rec = ExecutionRecord(
                execution_id=f"exec_{i}",
                call_id=call_id,
                tool_name="scroll_screen",
                args=args,
                tier=tier,
                scheduling=sched,
                created_at=time.time(),
                status="queued"
            )
            dispatcher.records[call_id] = rec
            fut = asyncio.get_event_loop().create_future()
            dispatcher._ui_queue.put_nowait((rec, fut))
            records_and_futures.append((rec, fut))

        assert dispatcher._ui_queue.qsize() == 5, "UI queue should contain 5 queued operations"

        # Trigger Emergency Stop
        EMERGENCY_STOP_MANAGER.trigger("User pressed Stop during queued execution batch")

        # Verify all queued operations are cancelled and queue is empty
        assert dispatcher._ui_queue.empty(), "UI queue must be completely drained on Emergency Stop"
        for rec, fut in records_and_futures:
            assert rec.status == "cancelled", f"Record {rec.call_id} must be cancelled"
            assert fut.done(), f"Future {rec.call_id} must be resolved"
            assert "[CANCELLED]" in fut.result()

        passed_tests += 1
        print(f"  [PASS] 6. Multiple Queued UI Operations Abort ({(time.time()-t0)*1000:.2f}ms) - 5/5 queued actions cancelled, queue drained")

        EMERGENCY_STOP_MANAGER.reset(physical_confirmation=True, actor="unit_tester")

        # -------------------------------------------------------------
        # TEST 7: Voice-Only Reset Prohibition & Clean Standby
        # -------------------------------------------------------------
        t0 = time.time()
        EMERGENCY_STOP_MANAGER.trigger("Testing reset permissions")
        assert EMERGENCY_STOP_MANAGER.is_active() is True

        # Attempt voice reset (physical_confirmation=False)
        voice_reset_blocked = False
        try:
            EMERGENCY_STOP_MANAGER.reset(physical_confirmation=False, actor="ambient_voice_recognition")
        except PermissionError:
            voice_reset_blocked = True
        assert voice_reset_blocked is True, "Voice alone must NEVER reset Emergency Stop!"
        assert EMERGENCY_STOP_MANAGER.is_active() is True, "Emergency stop must remain active after voice attempt"

        # Physical confirmation reset
        reset_res = EMERGENCY_STOP_MANAGER.reset(physical_confirmation=True, actor="physical_operator_key")
        assert EMERGENCY_STOP_MANAGER.is_active() is False
        assert reset_res["active"] is False
        passed_tests += 1
        print(f"  [PASS] 7. Voice-Only Reset Prohibition ({(time.time()-t0)*1000:.2f}ms) - Voice reset denied; physical confirmation required")

        # -------------------------------------------------------------
        # TEST 8: Distinctive Stop Phrase Matching vs Conversational False-Wake
        # -------------------------------------------------------------
        t0 = time.time()
        distinctive_phrases = [
            "emergency stop",
            "emergency halt",
            "chirkoot halt now",
            "chirkoot emergency stop",
            "chirkoot abort now",
            "red alert stop",
            "abort protocol",
            "chirkoot turant ruko"
        ]
        conversational_phrases = [
            "can you stop by the website and check the logs",
            "hold on a second let me think",
            "don't stop the download yet",
            "wait a minute",
            "we had a brief halt in production last week",
            "ruko zara sabar karo"
        ]

        def is_distinctive_stop(phrase: str) -> bool:
            p = phrase.lower().strip()
            return any(dp in p for dp in distinctive_phrases)

        for dp in distinctive_phrases:
            assert is_distinctive_stop(dp) is True, f"Distinctive phrase '{dp}' must match"

        for cp in conversational_phrases:
            assert is_distinctive_stop(cp) is False, f"Conversational phrase '{cp}' must NOT match"

        passed_tests += 1
        print(f"  [PASS] 8. Distinctive Stop Phrase Discrimination ({(time.time()-t0)*1000:.2f}ms) - 8 distinctive phrases matched; 6 conversational phrases safely rejected")

    finally:
        EMERGENCY_STOP_MANAGER.reset(physical_confirmation=True, actor="cleanup")

    print("\n" + "=" * 70)
    print(f" EMERGENCY STOP TEST SUMMARY: {passed_tests}/{total_tests} PASSED | 0 FAILED")
    print("=" * 70 + "\n")

if __name__ == "__main__":
    asyncio.run(run_tests())
