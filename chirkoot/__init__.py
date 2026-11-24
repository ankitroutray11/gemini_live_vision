"""Chirkoot MK-V Modular Architecture Package.

Provides high-cohesion, low-coupling modules for:
- api: HTTP & WebSocket endpoints, binary protocol framing, authentication
- core: Session lifecycle, local dispatcher, bounded queues, watchdog
- intelligence: Gemini 3.8 Live client integration, scoped prompt engineering
- audio: Resampling, downsampling, local acoustic wake-word engine
- vision: Adaptive screen radar, frame store, HUD exclusion, OCR
- automation: Win32 OS control, UIA engine, Set-of-Mark, closed-loop verifier
- security: Central policy, DPAPI vault, sandboxing, emergency kill switch
- learning: 10-stage autonomous skill lifecycle, git repository manager
- storage: SQLite FTS5 + sqlite-vec hybrid persistent memory engine
- observability: Structured tool execution logging, rolling metrics telemetry
- deployment: Single-instance mutex, signed updates, LKG rollback, packaging
"""

__version__ = "5.0.0"
__author__ = "Chirkoot Core Engineering"
