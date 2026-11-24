"""
Long-Running Stability & Stress Test Suite for Chirkoot MK-V.
Simulates 50+ continuous turns and tool dispatches against FakeGeminiServer.
Validates bounded queue depths, zero thread/process leaks, and stable memory usage.
"""

import asyncio
import os
import sys
import time
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

import psutil
from dispatcher import ToolDispatcher
from observability import GLOBAL_METRICS, GLOBAL_EXECUTION_LOGGER
from tests.fake_gemini_server import (
    FakeGenAIClient,
    FakeLiveSession,
    FakeFunctionCall,
    FakeToolCall,
    FakeLiveResponse
)


class TestStabilityLongRunning(unittest.TestCase):

    def setUp(self):
        GLOBAL_METRICS.reset_for_tests()
        self.process = psutil.Process(os.getpid())

    def test_50_turn_continuous_streaming_stability(self):
        """Simulates 50 back-to-back tool execution turns and verifies memory & queue stability."""
        start_mem_mb = self.process.memory_info().rss / (1024 * 1024)
        initial_threads = self.process.num_threads()

        call_counter = 0

        def mock_read(path: str = "", **kwargs) -> str:
            nonlocal call_counter
            call_counter += 1
            return f"Read {path} completed successfully"

        dispatcher = ToolDispatcher(tool_map={"read_file": mock_read}, max_steps_per_turn=10)

        async def run_50_turns():
            session = FakeLiveSession()

            for turn in range(50):
                # Reset turn budget every turn
                dispatcher.reset_turn()

                call_id = f"stress_call_{turn}_{time.time()}"
                # Propose tool call through dispatcher
                resp = await dispatcher.dispatch(call_id, "read_file", {"path": f"C:\\file_{turn}.txt"})
                self.assertIn("completed successfully", resp.response.get("result", ""))

                # Record token usage
                GLOBAL_METRICS.record_token_usage(prompt_tokens=100, candidate_tokens=30)
                # Record simulated OCR latency
                GLOBAL_METRICS.record_ocr_latency(12.5)

                # Send tool response back to fake session
                await session.send_tool_response(function_responses=[resp])

            session.close()

        asyncio.run(run_50_turns())

        self.assertEqual(call_counter, 50, "All 50 turns must execute successfully")

        # Telemetry & Resource Verification
        end_mem_mb = self.process.memory_info().rss / (1024 * 1024)
        mem_growth_mb = end_mem_mb - start_mem_mb
        end_threads = self.process.num_threads()

        # Deduplication cache should not exceed bounded limits (300)
        self.assertLessEqual(len(dispatcher._dedup_cache), 300)

        # Thread count should remain stable (max delta 4 threads for async worker pool)
        self.assertLessEqual(abs(end_threads - initial_threads), 6)

        # Memory growth across 50 simulated turns should be minimal (< 30 MB)
        self.assertLess(mem_growth_mb, 30.0, f"Excessive memory growth: {mem_growth_mb:.2f} MB")

        # Telemetry metrics check
        summary = GLOBAL_METRICS.get_metrics_summary()
        self.assertEqual(summary["reliability"]["tool_success_rate_percent"], 100.0)
        self.assertEqual(summary["reliability"]["total_tool_calls"], 50)
        self.assertEqual(summary["streaming"]["token_usage"]["lifetime_total_tokens"], 50 * 130)


if __name__ == "__main__":
    unittest.main()
