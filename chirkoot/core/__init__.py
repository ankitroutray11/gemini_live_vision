"""Chirkoot Core Package.

Provides local FunctionCallDispatcher with 3-tier execution model,
session lifecycle controller with resumption and context compression,
bounded async queues, deduplication, and watchdog health checking.
"""

from dispatcher import (
    FunctionCallDispatcher,
    ExecutionTier,
    ExecutionRecord
)

from session_lifecycle import (
    SessionLifecycleController,
    SessionResumptionManager,
    TaskStateManager,
    ToolDeduplicator,
    ClientMessageTracker,
    RollingTranscriptManager
)

from learning_engine import StartupWatchdog

__all__ = [
    "FunctionCallDispatcher",
    "ExecutionTier",
    "ExecutionRecord",
    "SessionLifecycleController",
    "SessionResumptionManager",
    "TaskStateManager",
    "ToolDeduplicator",
    "ClientMessageTracker",
    "RollingTranscriptManager",
    "StartupWatchdog"
]
