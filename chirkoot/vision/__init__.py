"""Chirkoot Vision Package.

Provides Adaptive Screen Radar (perceptual hashing), In-Memory Frame Store,
HUD capture exclusion (WDA_EXCLUDEFROMCAPTURE), and OCR integration.
"""

from radar_engine import (
    AdaptiveScreenRadar,
    RadarFrame,
    RadarConfig,
    RadarTelemetry,
    GLOBAL_SCREEN_RADAR
)

from frame_store import (
    InMemoryFrameStore,
    FrameRecord,
    GLOBAL_FRAME_STORE
)

from hud_capture_exclusion import (
    HudExclusionManager,
    ExclusionMechanism,
    HudExclusionDiagnosticStatus,
    GLOBAL_HUD_EXCLUSION_MANAGER
)

__all__ = [
    "AdaptiveScreenRadar",
    "RadarFrame",
    "RadarConfig",
    "RadarTelemetry",
    "GLOBAL_SCREEN_RADAR",
    "InMemoryFrameStore",
    "FrameRecord",
    "GLOBAL_FRAME_STORE",
    "HudExclusionManager",
    "ExclusionMechanism",
    "HudExclusionDiagnosticStatus",
    "GLOBAL_HUD_EXCLUSION_MANAGER"
]
