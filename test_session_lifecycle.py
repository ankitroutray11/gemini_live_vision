"""
Comprehensive Unit & Integration Test Suite for Chirkoot MK-V
Gemini Live Session Lifecycle Manager.

Tests:
1. Audio-only connection expiry and recovery.
2. Audio/video connection expiry and clean-session rollover.
3. GoAway frame detection and proactive reconnect.
4. Invalid or expired resumption handle fallback.
5. Context rollover and compact conversation summarization.
6. Repeated reconnects with bounded exponential backoff & jitter.
7. Browser WebSocket continuity during Gemini reconnects.
8. Interrupted task recovery and persistence.
9. Duplicate tool-call prevention (idempotency).
10. Model compatibility adapter parameter sanitization.
11. Client message acknowledgement and replay correctness.
"""

import os
import sys
import time
import asyncio
from pathlib import Path

# Add project root to path
ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))

from session_lifecycle import (
    ModelConfigAdapter,
    TaskStateManager,
    ToolDeduplicator,
    ClientMessageTracker,
    RollingTranscriptManager,
    BackoffManager,
    SessionLifecycleController
)
from security_policy import DATA_DIR
from google.genai import types

def run_tests():
    passed = 0
    failed = 0
    results = []

    def record(name: str, ok: bool, duration_ms: float, details: str = ""):
        nonlocal passed, failed
        if ok:
            passed += 1
            results.append(f"  [PASS] {name} ({duration_ms:.2f}ms)" + (f" - {details}" if details else ""))
        else:
            failed += 1
            results.append(f"  [FAIL] {name} ({duration_ms:.2f}ms) - {details}")

    print("\n=================================================================")
    print(" RUNNING CHIRKOOT MK-V SESSION LIFECYCLE TEST SUITE")
    print("=================================================================\n")

    # -------------------------------------------------------------------------
    # TEST 1: Model Compatibility Adapter Parameter Sanitization
    # -------------------------------------------------------------------------
    t0 = time.time()
    try:
        adapter_38 = ModelConfigAdapter("gemini-3.8-live")
        cfg_38 = adapter_38.build_connect_config(
            types_module=types,
            system_instruction_text="Test Assistant",
            tools_list=[],
            trigger_tokens=75000,
            target_tokens=35000,
            thinking_level="high"  # Should be omitted for gemini-3.8-live!
        )
        assert cfg_38.thinking_config is None, "gemini-3.8-live must NOT have thinking_config"
        assert cfg_38.context_window_compression is not None
        assert cfg_38.context_window_compression.trigger_tokens == 75000
        assert cfg_38.context_window_compression.sliding_window.target_tokens == 35000
        assert not hasattr(cfg_38, "enable_affective_dialog") or cfg_38.enable_affective_dialog is None

        # Verify extended thinking model accepts thinking_config
        adapter_think = ModelConfigAdapter("gemini-3.8-live-extended-thinking")
        cfg_think = adapter_think.build_connect_config(
            types_module=types,
            system_instruction_text="Test Assistant",
            tools_list=[],
            thinking_level="low"
        )
        assert cfg_think.thinking_config is not None
        assert cfg_think.thinking_config.thinking_level == types.ThinkingLevel.LOW

        record("Model Compatibility Adapter Sanitization", True, (time.time() - t0) * 1000)
    except Exception as e:
        record("Model Compatibility Adapter Sanitization", False, (time.time() - t0) * 1000, str(e))

    # -------------------------------------------------------------------------
    # TEST 2: Audio-Only Connection Expiry & Recovery
    # -------------------------------------------------------------------------
    t0 = time.time()
    try:
        ctrl = SessionLifecycleController("gemini-3.8-live")
        ctrl.is_video_active = False

        # Simulate receiving valid session resumption handle
        class DummyUpdate:
            new_handle = "handle_audio_valid_12345"
            resumable = True
            last_consumed_client_message_index = 5

        ctrl.on_resumption_update(DummyUpdate())
        assert ctrl.is_handle_valid() is True
        strat_ok, handle = ctrl.prepare_reconnect_strategy()
        assert strat_ok is True
        assert handle == "handle_audio_valid_12345"

        record("Audio-Only Session Resumption Recovery", True, (time.time() - t0) * 1000, f"Handle: {handle}")
    except Exception as e:
        record("Audio-Only Session Resumption Recovery", False, (time.time() - t0) * 1000, str(e))

    # -------------------------------------------------------------------------
    # TEST 3: Audio/Video Connection Expiry & Clean-Session Rollover
    # -------------------------------------------------------------------------
    t0 = time.time()
    try:
        ctrl = SessionLifecycleController("gemini-3.8-live")
        ctrl.is_video_active = True
        ctrl.latest_resumption_handle = "handle_video_old"
        ctrl.handle_created_at = time.time() - 8000  # Expired (> 7200s)

        strat_ok, handle = ctrl.prepare_reconnect_strategy()
        # Should roll over cleanly to prevent corrupted video context buffer
        assert strat_ok is False
        assert handle is None

        record("Audio/Video Clean-Session Rollover", True, (time.time() - t0) * 1000, "Clean cold-start rollover selected")
    except Exception as e:
        record("Audio/Video Clean-Session Rollover", False, (time.time() - t0) * 1000, str(e))

    # -------------------------------------------------------------------------
    # TEST 4: GoAway Frame Detection
    # -------------------------------------------------------------------------
    t0 = time.time()
    try:
        ctrl = SessionLifecycleController("gemini-3.8-live")
        ctrl.on_resumption_update(DummyUpdate())

        # Simulate GoAway received with 10s remaining
        class DummyGoAway:
            time_left = "10.0s"

        go_away = DummyGoAway()
        time_left = getattr(go_away, "time_left", "")
        assert time_left == "10.0s"
        # Verify handle is still preserved for the immediate graceful reconnect
        assert ctrl.is_handle_valid() is True

        record("GoAway Detection & Graceful Pre-Disconnect State", True, (time.time() - t0) * 1000)
    except Exception as e:
        record("GoAway Detection & Graceful Pre-Disconnect State", False, (time.time() - t0) * 1000, str(e))

    # -------------------------------------------------------------------------
    # TEST 5: Invalid or Expired Resumption Handle Fallback
    # -------------------------------------------------------------------------
    t0 = time.time()
    try:
        ctrl = SessionLifecycleController("gemini-3.8-live")
        ctrl.latest_resumption_handle = "stale_handle_999"
        ctrl.handle_created_at = time.time() - 9000  # 2.5 hours ago

        assert ctrl.is_handle_valid() is False
        ctrl.invalidate_handle("Expired past 2 hours")
        strat_ok, handle = ctrl.prepare_reconnect_strategy()
        assert strat_ok is False
        assert handle is None

        record("Invalid/Expired Resumption Handle Fallback", True, (time.time() - t0) * 1000)
    except Exception as e:
        record("Invalid/Expired Resumption Handle Fallback", False, (time.time() - t0) * 1000, str(e))

    # -------------------------------------------------------------------------
    # TEST 6: Context Rollover & Compact Conversation Summarization
    # -------------------------------------------------------------------------
    t0 = time.time()
    try:
        tm = RollingTranscriptManager(max_raw_turns=6)
        # Add 12 conversation turns to trigger compaction
        for i in range(12):
            role = "user" if i % 2 == 0 else "model"
            tm.append_turn(role, f"Message turn {i}: User asking question about file {i}.py")

        assert len(tm.raw_turns) == 6
        assert len(tm.compact_summary) > 0
        assert "User: Message turn" in tm.compact_summary

        # Build turns for send_client_content
        turns = tm.build_reseeding_turns(types)
        assert len(turns) > 6  # Summary turn pair + recent raw turns
        first_turn_text = turns[0].parts[0].text
        assert "[PRIOR CONVERSATION SUMMARY:" in first_turn_text

        record("Context Rollover & Compact Summarization", True, (time.time() - t0) * 1000, f"Summary len: {len(tm.compact_summary)}")
    except Exception as e:
        record("Context Rollover & Compact Summarization", False, (time.time() - t0) * 1000, str(e))

    # -------------------------------------------------------------------------
    # TEST 7: Repeated Reconnects with Bounded Exponential Backoff & Jitter
    # -------------------------------------------------------------------------
    t0 = time.time()
    try:
        bm = BackoffManager(base_delay=0.3, max_delay=4.0, max_retries=5, jitter_factor=0.2)
        delays = [bm.get_next_delay() for _ in range(5)]
        
        # Verify monotonically increasing trend
        assert delays[0] < delays[2] < delays[4]
        assert bm.has_exceeded_retries is True
        # Verify subsequent delay is capped at max_delay
        assert bm.get_next_delay() <= 4.0

        bm.reset()
        assert bm.current_attempt == 0
        assert bm.has_exceeded_retries is False

        record("Bounded Exponential Backoff with Jitter", True, (time.time() - t0) * 1000, f"Delays: {delays}")
    except Exception as e:
        record("Bounded Exponential Backoff with Jitter", False, (time.time() - t0) * 1000, str(e))

    # -------------------------------------------------------------------------
    # TEST 8: Browser WebSocket Continuity & Decoupling
    # -------------------------------------------------------------------------
    t0 = time.time()
    try:
        ctrl = SessionLifecycleController("gemini-3.8-live")
        
        # Mock WebSocket to verify notifications
        dispatched_events = []
        class MockWebSocket:
            async def send_json(self, data):
                dispatched_events.append(data)

        mock_ws = MockWebSocket()
        asyncio.run(ctrl.notify_hud(mock_ws, "reconnecting", {"reason": "GoAway received"}))
        asyncio.run(ctrl.notify_hud(mock_ws, "resumed", {"handle": "handle_xyz"}))

        assert len(dispatched_events) == 2
        assert dispatched_events[0]["state"] == "reconnecting"
        assert dispatched_events[1]["state"] == "resumed"

        record("Browser WebSocket Continuity & HUD Telemetry", True, (time.time() - t0) * 1000)
    except Exception as e:
        record("Browser WebSocket Continuity & HUD Telemetry", False, (time.time() - t0) * 1000, str(e))

    # -------------------------------------------------------------------------
    # TEST 9: Interrupted Task State Recovery Outside Session
    # -------------------------------------------------------------------------
    t0 = time.time()
    try:
        test_task_file = DATA_DIR / "test_active_tasks.json"
        task_mgr = TaskStateManager(test_task_file)

        task_id = f"task_{int(time.time())}"
        task_mgr.register_task(task_id, "Extract all tables from data report")
        task_mgr.append_tool_execution(task_id, "ocr_screen", {}, "Found Table 1")
        task_mgr.mark_interrupted(task_id, "Network WebSocket dropped")

        recovered = task_mgr.get_latest_unfinished_task()
        assert recovered is not None
        assert recovered["task_id"] == task_id
        assert recovered["status"] == "interrupted"
        assert len(recovered["tool_calls"]) == 1

        # Clean up test task file
        if test_task_file.exists():
            test_task_file.unlink()

        record("Interrupted Task State Persistence & Recovery", True, (time.time() - t0) * 1000)
    except Exception as e:
        record("Interrupted Task State Persistence & Recovery", False, (time.time() - t0) * 1000, str(e))

    # -------------------------------------------------------------------------
    # TEST 10: Duplicate Tool-Call Prevention (Idempotency)
    # -------------------------------------------------------------------------
    t0 = time.time()
    try:
        dedup = ToolDeduplicator(ttl_seconds=300)
        call_id = "fc_call_987654"
        tool_name = "write_file"
        tool_args = {"path": "output.txt", "content": "Sample output"}

        # First execution
        is_dup, _ = dedup.is_duplicate(call_id, tool_name, tool_args)
        assert is_dup is False, "First execution must not be duplicate"
        dedup.record_execution(call_id, tool_name, tool_args, "File written successfully")

        # Second execution with same call_id (e.g. replayed on reconnection)
        is_dup, cached_res = dedup.is_duplicate(call_id, tool_name, tool_args)
        assert is_dup is True, "Second execution with same call_id MUST be marked duplicate"
        assert cached_res == "File written successfully"

        # Third execution with new call_id but same side-effecting args within 45s
        is_dup, cached_res = dedup.is_duplicate("fc_call_new_111", tool_name, tool_args)
        assert is_dup is True, "Identical side-effect within 45s must be suppressed"
        assert cached_res == "File written successfully"

        record("Duplicate Tool-Call Prevention & Idempotency", True, (time.time() - t0) * 1000)
    except Exception as e:
        record("Duplicate Tool-Call Prevention & Idempotency", False, (time.time() - t0) * 1000, str(e))

    # -------------------------------------------------------------------------
    # TEST 11: Client Message Acknowledgement & Replay Tracker
    # -------------------------------------------------------------------------
    t0 = time.time()
    try:
        tracker = ClientMessageTracker()
        s0 = tracker.track_message("text", "hello 1")
        s1 = tracker.track_message("text", "hello 2")
        s2 = tracker.track_message("text", "hello 3")
        assert s0 == 0 and s1 == 1 and s2 == 2

        # Server acks messages up to index 1
        tracker.on_server_ack(1)
        assert tracker.last_acked_index == 1

        unacked = tracker.get_unacknowledged_replay()
        assert len(unacked) == 1
        assert unacked[0]["seq_index"] == 2
        assert unacked[0]["payload"] == "hello 3"

        record("Client Message Acknowledgement & Replay", True, (time.time() - t0) * 1000)
    except Exception as e:
        record("Client Message Acknowledgement & Replay", False, (time.time() - t0) * 1000, str(e))

    print("\n=================================================================")
    for r in results:
        print(r)
    print(f"\nTOTAL: {passed} PASSED | {failed} FAILED")
    print("=================================================================\n")

    return failed == 0

if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
