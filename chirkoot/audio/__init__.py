"""Chirkoot Audio Package.

Provides 16 kHz PCM downsampling with anti-aliasing filtering,
local acoustic & fuzzy wake-word detection, and audio pre-roll buffer.
"""

from wake_word_engine import (
    WakeEvent,
    LocalWakeWordDetector,
    GLOBAL_WAKE_DETECTOR
)

LocalWakeDetector = LocalWakeWordDetector

from chirkoot.audio.downsampling import (
    downsample_pcm16,
    verify_pcm16_properties
)

__all__ = [
    "WakeEvent",
    "LocalWakeWordDetector",
    "LocalWakeDetector",
    "GLOBAL_WAKE_DETECTOR",
    "downsample_pcm16",
    "verify_pcm16_properties"
]
