"""Chirkoot Intelligence Package.

Provides Gemini 3.8 Live prompt engineering, scoped memory prompt injection,
and local offline LLM fallback capabilities.
"""

from learning_engine import (
    get_memory_prompt_injection,
    get_scoped_memories
)

try:
    from offline_llm import OFFLINE_ENGINE
    OFFLINE_LLM_AVAILABLE = True
except Exception:
    OFFLINE_ENGINE = None
    OFFLINE_LLM_AVAILABLE = False

__all__ = [
    "get_memory_prompt_injection",
    "get_scoped_memories",
    "OFFLINE_ENGINE",
    "OFFLINE_LLM_AVAILABLE"
]
