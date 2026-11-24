"""
Integration Tests for Chirkoot MK-V Session Reconnect & Lifecyle Controller.
Uses FakeGeminiServer to test GoAway frame handling, session resumption handles,
context re-seeding, token usage tracking, and cold-start fallback.
"""

import asyncio
import sys
import time
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from session_lifecycle import SessionLifecycleController, ModelConfigAdapter
from observability import GLOBAL_METRICS
from tests.fake_gemini_server import (
    FakeGenAIClient,
    FakeLiveSession,
    FakeGoAway,
    FakeSessionResumptionUpdate,
    FakeUsageMetadata,
    FakeLiveResponse
)


class TestSessionReconnect(unittest.TestCase):

    def setUp(self):
        GLOBAL_METRICS.reset_for_tests()
        self.client = FakeGenAIClient()
        self.lifecycle = SessionLifecycleController("gemini-3.8-live")

    def test_reconnect_strategy_cold_vs_resumed(self):
        """Test prepare_reconnect_strategy returns cold start initially, then resumed after handle."""
        is_resumed, handle = self.lifecycle.prepare_reconnect_strategy()
        self.assertFalse(is_resumed)
        self.assertIsNone(handle)

        # Update resumption handle
        update = FakeSessionResumptionUpdate(
            new_handle="hdl_resumption_2hr_token_test",
            last_acked_index=5
        )
        self.lifecycle.on_resumption_update(update)

        is_resumed, handle = self.lifecycle.prepare_reconnect_strategy()
        self.assertTrue(is_resumed)
        self.assertEqual(handle, "hdl_resumption_2hr_token_test")

    def test_go_away_and_proactive_reconnect_flow(self):
        """Test GoAway signal handling and proactive reconnect flag."""
        session = FakeLiveSession(model_name="gemini-3.8-live")

        # Simulate receiving resumption token first
        session.inject_resumption_update("hdl_proactive_test_123")
        # Then simulate receiving GoAway frame
        session.inject_go_away("8s")

        async def run_receive():
            async for resp in session.receive():
                if resp.session_resumption_update:
                    self.lifecycle.on_resumption_update(resp.session_resumption_update)
                if resp.go_away:
                    time_left = resp.go_away.time_left
                    self.assertEqual(time_left, "8s")
                    session.close()
                    break

        asyncio.run(run_receive())

        self.assertEqual(self.lifecycle.latest_resumption_handle, "hdl_proactive_test_123")
        is_resumed, handle = self.lifecycle.prepare_reconnect_strategy()
        self.assertTrue(is_resumed)
        self.assertEqual(handle, "hdl_proactive_test_123")

    def test_invalid_handle_invalidation_and_cold_start_fallback(self):
        """Test handle expiration error 1008 forces clean cold start fallback."""
        update = FakeSessionResumptionUpdate(new_handle="hdl_expired_handle")
        self.lifecycle.on_resumption_update(update)
        self.assertEqual(self.lifecycle.latest_resumption_handle, "hdl_expired_handle")

        # Simulate connection error with 1008 expired handle
        err_msg = "WebSocket closed with code 1008: resumption handle expired or invalid"
        self.lifecycle.invalidate_handle(err_msg)

        self.assertIsNone(self.lifecycle.latest_resumption_handle)
        is_resumed, handle = self.lifecycle.prepare_reconnect_strategy()
        self.assertFalse(is_resumed)
        self.assertIsNone(handle)

    def test_token_usage_metadata_recording(self):
        """Test token consumption tracking via usage_metadata."""
        session = FakeLiveSession()
        session.inject_usage_metadata(prompt_tokens=320, candidate_tokens=80)

        async def run_tokens():
            async for resp in session.receive():
                if resp.usage_metadata:
                    p = resp.usage_metadata.prompt_token_count
                    c = resp.usage_metadata.candidates_token_count
                    GLOBAL_METRICS.record_token_usage(p, c)
                    session.close()
                    break

        asyncio.run(run_tokens())

        tok_stats = GLOBAL_METRICS.get_token_consumption_stats()
        self.assertEqual(tok_stats["lifetime_prompt_tokens"], 320)
        self.assertEqual(tok_stats["lifetime_candidate_tokens"], 80)
        self.assertEqual(tok_stats["lifetime_total_tokens"], 400)


if __name__ == "__main__":
    unittest.main()
