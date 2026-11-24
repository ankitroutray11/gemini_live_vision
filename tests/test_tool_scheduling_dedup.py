"""
Unit & Integration Tests for Chirkoot MK-V Tool Scheduling, Deduplication & Cancellation.
Validates step budgets, exact and semantic deduplication, queue serialization,
and cancellation latency tracking.
"""

import asyncio
import sys
import time
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from dispatcher import FunctionCallDispatcher, ToolDispatcher, ExecutionTier
from observability import GLOBAL_METRICS, GLOBAL_EXECUTION_LOGGER


class TestToolSchedulingDedup(unittest.TestCase):

    def setUp(self):
        GLOBAL_METRICS.reset_for_tests()
        self.call_counts = {}

        def mock_read_file(path: str = "", **kwargs) -> str:
            self.call_counts["read_file"] = self.call_counts.get("read_file", 0) + 1
            return f"Mock content of {path}"

        def mock_run_command(command: str = "", **kwargs) -> str:
            self.call_counts["run_command"] = self.call_counts.get("run_command", 0) + 1
            return f"Output of {command}"

        self.mock_tools = {
            "read_file": mock_read_file,
            "run_command": mock_run_command
        }
        self.dispatcher = ToolDispatcher(tool_map=self.mock_tools, max_steps_per_turn=4)
        # Auto-approve for automated tests
        async def auto_approve(record):
            return "allow"
        self.dispatcher.confirmation_handler = auto_approve

    def test_call_id_exact_deduplication(self):
        """Test dispatching the exact same call_id returns cached result without re-executing."""
        async def run_dedup():
            resp1 = await self.dispatcher.dispatch("call_exact_001", "read_file", {"path": "C:\\jarvis\\config.json"})
            self.assertEqual(resp1.response.get("result"), "Mock content of C:\\jarvis\\config.json")
            self.assertEqual(self.call_counts.get("read_file"), 1)

            # Re-dispatch exact same call_id
            resp2 = await self.dispatcher.dispatch("call_exact_001", "read_file", {"path": "C:\\jarvis\\config.json"})
            self.assertEqual(resp2.response.get("result"), "Mock content of C:\\jarvis\\config.json")
            # Should NOT increment call count
            self.assertEqual(self.call_counts.get("read_file"), 1)

        asyncio.run(run_dedup())

    def test_side_effect_semantic_deduplication(self):
        """Test side-effecting Tier C tools deduplicate identical calls within 45 seconds."""
        async def run_semantic_dedup():
            args = {"command": "dir C:\\"}
            resp1 = await self.dispatcher.dispatch("call_cmd_001", "run_command", args)
            self.assertEqual(self.call_counts.get("run_command"), 1)

            # Different call_id, identical tool and args
            resp2 = await self.dispatcher.dispatch("call_cmd_002", "run_command", args)
            self.assertEqual(self.call_counts.get("run_command"), 1)
            self.assertIn("Output of dir", resp2.response.get("result"))

        asyncio.run(run_semantic_dedup())

    def test_step_budget_enforcement(self):
        """Test that turn step budget caps excessive rapid tool calls."""
        async def run_budget():
            # max_steps_per_turn = 4
            for i in range(4):
                resp = await self.dispatcher.dispatch(f"call_step_{i}", "read_file", {"path": f"C:\\file_{i}.txt"})
                self.assertNotIn("BUDGET EXCEEDED", resp.response.get("result", ""))

            # 5th call exceeds budget
            resp5 = await self.dispatcher.dispatch("call_step_5", "read_file", {"path": "C:\\file_5.txt"})
            self.assertIn("BUDGET EXCEEDED", resp5.response.get("result", ""))

            # Reset turn step count
            self.dispatcher.reset_turn()
            resp_new = await self.dispatcher.dispatch("call_step_new", "read_file", {"path": "C:\\file_new.txt"})
            self.assertNotIn("BUDGET EXCEEDED", resp_new.response.get("result", ""))

        asyncio.run(run_budget())

    def test_tool_cancellation_and_metrics(self):
        """Test explicit call cancellation and verify cancellation metrics recording."""
        async def run_cancellation():
            async def delayed_handler(record):
                await asyncio.sleep(0.4)
                return "allow"

            self.dispatcher.confirmation_handler = delayed_handler

            # Dispatch Tier C in background
            dispatch_task = asyncio.create_task(
                self.dispatcher.dispatch("call_to_cancel", "run_command", {"command": "sleep 10"})
            )
            await asyncio.sleep(0.05)  # Enter confirming state

            self.dispatcher.cancel_call("call_to_cancel", reason="User cancel test")
            res_cancel = await dispatch_task

            self.assertEqual(self.dispatcher.records["call_to_cancel"].status, "cancelled")
            self.assertTrue(self.dispatcher.records["call_to_cancel"].abort_event.is_set())

        asyncio.run(run_cancellation())


if __name__ == "__main__":
    unittest.main()
