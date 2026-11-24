"""
Chirkoot MK-V: Adaptive Screen Radar Engine
============================================
Provides intelligent, privacy-aware desktop visual streaming with:
1. Perceptual frame-change detection (dHash + downscaled L1 pixel difference).
2. Dynamic rate adaptation (burst on typing/scrolling/transitions, throttle on static reading).
3. Foreground Window Privacy Shield (auto-pause on password managers, banking apps, incognito).
4. HUD Self-Capture prevention (rectangle masking + SetWindowDisplayAffinity WDA_EXCLUDEFROMCAPTURE).
5. Comprehensive adaptive transmission telemetry (effective FPS, bandwidth saved, frame stats).
6. Zero-disk, in-memory dual-tier visual architecture.
"""

import io
import time
import math
import struct
import logging
import threading
import ctypes
from enum import Enum
from typing import Optional, Tuple, Dict, Any, List
from dataclasses import dataclass, field
from PIL import Image, ImageDraw
import numpy as np

from frame_store import FrameType, MediaPacket, VideoFrame, GLOBAL_FRAME_STORE

logger = logging.getLogger("ChirkootRadar")
logger.setLevel(logging.INFO)

# Windows Display Affinity Constants
WDA_NONE = 0x00000000
WDA_MONITOR = 0x00000001
WDA_EXCLUDEFROMCAPTURE = 0x00000011  # Windows 10 2004+ / Windows 11


class RadarState(str, Enum):
    STATIC = "STATIC"       # Idle desktop, reading, static terminal -> throttled to heartbeat
    DYNAMIC = "DYNAMIC"     # Scrolling, coding, typing, video -> high-framerate stream
    PAUSED = "PAUSED"       # Manually paused by user
    SHIELDED = "SHIELDED"   # Automatically paused due to sensitive foreground window


@dataclass
class RadarConfig:
    """Configurable frame-rate and perceptual thresholds for Screen Radar."""
    static_heartbeat_interval_ms: int = 3000   # 1 frame every 3s during static inactivity (0.33 FPS)
    dynamic_min_interval_ms: int = 333         # ~3.0 FPS during active interaction (burst)
    change_threshold: float = 0.002            # 0.2% normalized pixel difference trigger
    dhash_threshold: int = 1                   # dHash Hamming distance threshold
    cooldown_ms: int = 1500                    # Retain dynamic state for 1.5s after change
    jpeg_quality: float = 0.72                 # Compressed JPEG quality for streaming
    max_dimension: int = 960                   # Maximum viewport dimension for Gemini Live stream
    privacy_shield_enabled: bool = True        # Automatic sensitive window suppression
    mask_hud_enabled: bool = True              # Mask Chirkoot's own HUD from streaming feed


@dataclass
class RadarTelemetry:
    """Live telemetry metrics for Screen Radar streaming."""
    state: RadarState = RadarState.STATIC
    total_frames_evaluated: int = 0
    frames_transmitted: int = 0
    frames_suppressed: int = 0
    bandwidth_saved_pct: float = 0.0
    effective_fps: float = 0.0
    effective_kbps: float = 0.0
    last_change_score: float = 0.0
    last_dhash_distance: int = 0
    last_transmission_time: float = 0.0
    foreground_window_title: str = ""
    privacy_shield_active: bool = False
    active_monitor_id: int = 0
    hud_masked: bool = False


# Sensitive application/window title keywords that engage the Privacy Shield
SENSITIVE_WINDOW_KEYWORDS = [
    # Password Managers
    "1password", "bitwarden", "keepass", "lastpass", "dashlane", "enpass",
    "nordpass", "roboform", "authy", "authenticator", "credential manager",
    "master password", "secret key", "recovery phrase", "seed phrase", "vault",
    # Banking & Financial Institutions
    "bank", "chase online", "wells fargo", "citibank", "bank of america",
    "barclays", "hsbc", "paypal", "coinbase", "binance", "kraken", "robinhood",
    "fidelity", "vanguard", "charles schwab", "netbanking", "hdfc", "icici",
    "sbi", "kotak", "axis bank", "account balance", "wire transfer", "checkout",
    # Private / Incognito Browsing
    "incognito", "inprivate", "private browsing", "tor browser"
]


class AdaptiveRadarEngine:
    """
    Central Adaptive Screen Radar engine managing perceptual frame diffs,
    rate-adaptation, privacy shields, and HUD self-capture prevention.
    """

    def __init__(self, config: Optional[RadarConfig] = None):
        self.config = config or RadarConfig()
        self._lock = threading.Lock()

        # State tracking
        self.state: RadarState = RadarState.STATIC
        self.manual_pause: bool = False
        self._last_frame_img: Optional[Image.Image] = None
        self._last_frame_thumb: Optional[np.ndarray] = None
        self._last_frame_dhash: Optional[int] = None

        # Timing and Rate adaptation
        self._last_eval_time: float = 0.0
        self._last_transmit_time: float = 0.0
        self._dynamic_cooldown_until: float = 0.0
        self._recent_transmit_timestamps: List[float] = []
        self._recent_bytes: List[int] = []

        # Counters
        self.total_frames_evaluated: int = 0
        self.frames_transmitted: int = 0
        self.frames_suppressed: int = 0
        self.last_change_score: float = 0.0
        self.last_dhash_dist: int = 0

        # HUD geometry for self-capture masking (normalized 0..1 or pixel coords)
        self._hud_rect: Optional[Tuple[int, int, int, int]] = None  # (x1, y1, x2, y2)

        # Foreground tracking
        self._last_foreground_title: str = ""
        self._privacy_shield_engaged: bool = False

    # -------------------------------------------------------------------------
    # Perceptual Frame-Change Detection
    # -------------------------------------------------------------------------
    @staticmethod
    def compute_dhash(image: Image.Image, hash_size: int = 8) -> int:
        """
        Computes a 64-bit difference hash (dHash) on an image.
        Extremely fast (~2-3ms) and robust against minor compression artifacts.
        """
        # Grayscale and resize to (hash_size + 1, hash_size)
        resized = image.convert("L").resize((hash_size + 1, hash_size), Image.Resampling.BILINEAR)
        pixels = np.array(resized, dtype=np.int16)
        # Compare adjacent column pixels
        diff = pixels[:, 1:] > pixels[:, :-1]
        val = 0
        for i, b in enumerate(diff.flatten()):
            if b:
                val |= (1 << i)
        return val

    @staticmethod
    def hamming_distance(h1: int, h2: int) -> int:
        """Computes bitwise Hamming distance between two 64-bit integers."""
        return bin(h1 ^ h2).count("1")

    @staticmethod
    def compute_pixel_diff(thumb_a: np.ndarray, thumb_b: np.ndarray) -> float:
        """Computes normalized L1 difference between two 32x32 thumbnails."""
        return float(np.mean(np.abs(thumb_a - thumb_b)) / 255.0)

    # -------------------------------------------------------------------------
    # Windows Foreground & Privacy Shield Detection
    # -------------------------------------------------------------------------
    def get_foreground_window_title(self) -> str:
        """Inspects the active Windows foreground window title using Win32 API."""
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return ""
            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return ""
            buff = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buff, length + 1)
            return buff.value
        except Exception:
            return ""

    def is_sensitive_window(self, title: str) -> Tuple[bool, str]:
        """
        Checks whether the window title matches known password managers,
        banking sites, or private browsing modes.
        """
        if not title:
            return False, ""
        title_lower = title.lower()
        for kw in SENSITIVE_WINDOW_KEYWORDS:
            if kw in title_lower:
                return True, kw
        return False, ""

    def check_foreground_privacy(self) -> Tuple[bool, str]:
        """Evaluates foreground window and returns whether Privacy Shield should engage."""
        title = self.get_foreground_window_title()
        self._last_foreground_title = title
        if not self.config.privacy_shield_enabled:
            return False, ""
        return self.is_sensitive_window(title)

    # -------------------------------------------------------------------------
    # HUD Self-Capture Prevention
    # -------------------------------------------------------------------------
    def set_hud_rectangle(self, x1: int, y1: int, x2: int, y2: int):
        """Sets the bounding box of Chirkoot's own HUD window for capture masking."""
        with self._lock:
            self._hud_rect = (x1, y1, x2, y2)
        from hud_capture_exclusion import GLOBAL_HUD_EXCLUSION_MANAGER
        GLOBAL_HUD_EXCLUSION_MANAGER.update_hud_geometry(x1, y1, x2, y2)

    def mask_hud_in_image(self, image: Image.Image, capture_bounds: Optional[Tuple[int, int, int, int]] = None) -> Image.Image:
        """
        Blanks out or blurs the region occupied by Chirkoot's own HUD window
        to prevent recursive screen-in-screen mirror feedback.
        Delegates to central HudCaptureExclusionManager for multi-monitor support.
        """
        if not self.config.mask_hud_enabled:
            return image
        from hud_capture_exclusion import GLOBAL_HUD_EXCLUSION_MANAGER
        # Fall back to internal rect if manager has none yet
        if not GLOBAL_HUD_EXCLUSION_MANAGER.hud_rect and self._hud_rect:
            GLOBAL_HUD_EXCLUSION_MANAGER.update_hud_geometry(*self._hud_rect)
        return GLOBAL_HUD_EXCLUSION_MANAGER.mask_hud_in_capture(image, capture_bounds)

    @staticmethod
    def apply_window_display_affinity(hwnd: int) -> bool:
        """
        Applies Windows display affinity WDA_EXCLUDEFROMCAPTURE to exclude native window
        from desktop capture APIs (DWM / WGC / DXGI).
        """
        from hud_capture_exclusion import GLOBAL_HUD_EXCLUSION_MANAGER
        success, _ = GLOBAL_HUD_EXCLUSION_MANAGER._call_set_window_display_affinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
        return success

    # -------------------------------------------------------------------------
    # Manual Controls & Configuration
    # -------------------------------------------------------------------------
    def pause_radar(self, reason: str = "manual"):
        """Pauses radar transmission."""
        with self._lock:
            self.manual_pause = True
            self.state = RadarState.PAUSED
        logger.info(f"🛡️ [Screen Radar] Paused: {reason}")

    def resume_radar(self):
        """Resumes radar transmission."""
        with self._lock:
            self.manual_pause = False
            self.state = RadarState.STATIC
            self._last_transmit_time = 0.0
        logger.info("📡 [Screen Radar] Resumed by user")

    def update_config(self, **kwargs):
        """Dynamically updates radar configuration parameters."""
        with self._lock:
            for k, v in kwargs.items():
                if hasattr(self.config, k):
                    setattr(self.config, k, v)
        logger.info(f"⚙️ [Screen Radar] Config updated: {kwargs}")

    # -------------------------------------------------------------------------
    # Frame Evaluation & Rate Adaptation
    # -------------------------------------------------------------------------
    def evaluate_frame(
        self,
        image: Image.Image,
        timestamp_ms: Optional[int] = None,
        monitor_id: int = 0
    ) -> Tuple[bool, RadarState, RadarTelemetry]:
        """
        Evaluates an incoming screen frame to decide whether it should be transmitted to Gemini Live.
        Returns:
            should_transmit: bool (True = send to Gemini Live; False = skip/suppress)
            new_state: RadarState (STATIC, DYNAMIC, PAUSED, SHIELDED)
            telemetry: RadarTelemetry snapshot
        """
        now = time.time()
        now_ms = timestamp_ms or int(now * 1000)

        with self._lock:
            self.total_frames_evaluated += 1

            # 1. Check Manual Pause
            if self.manual_pause:
                self.state = RadarState.PAUSED
                self.frames_suppressed += 1
                return False, self.state, self._build_telemetry(now, monitor_id)

            # 2. Check Foreground Privacy Shield
            is_sensitive, sensitive_kw = self.check_foreground_privacy()
            if is_sensitive:
                self.state = RadarState.SHIELDED
                self._privacy_shield_engaged = True
                self.frames_suppressed += 1
                logger.warning(f"🔒 [Privacy Shield] Screen Radar suppressed: Sensitive window active ({sensitive_kw})")
                return False, self.state, self._build_telemetry(now, monitor_id)
            else:
                self._privacy_shield_engaged = False

            # 3. Compute Perceptual Hash and Difference
            # Fast downscaled thumbnail (32x32 grayscale)
            current_thumb = np.array(image.convert("L").resize((32, 32), Image.Resampling.BOX), dtype=np.float32)
            current_dhash = self.compute_dhash(image)

            if self._last_frame_thumb is None or self._last_frame_dhash is None:
                # First frame: always transmit to establish baseline
                diff_score = 1.0
                dhash_dist = 64
                is_changed = True
            else:
                diff_score = self.compute_pixel_diff(current_thumb, self._last_frame_thumb)
                dhash_dist = self.hamming_distance(current_dhash, self._last_frame_dhash)
                # Significant change triggered if either thumbnail diff or dHash exceeds threshold
                is_changed = (diff_score >= self.config.change_threshold) or (dhash_dist >= self.config.dhash_threshold)

            self.last_change_score = diff_score
            self.last_dhash_dist = dhash_dist
            self._last_frame_thumb = current_thumb
            self._last_frame_dhash = current_dhash

            # 4. Rate-Adaptation State Machine
            time_since_last_send = (now - self._last_transmit_time)

            if is_changed:
                # Screen activity detected: typing, scrolling, window opening, video
                self.state = RadarState.DYNAMIC
                self._dynamic_cooldown_until = now + (self.config.cooldown_ms / 1000.0)
                
                # Check minimum dynamic interval to enforce frame rate limit (e.g. 333ms = 3 FPS)
                min_dynamic_gap = self.config.dynamic_min_interval_ms / 1000.0
                if time_since_last_send >= min_dynamic_gap:
                    should_transmit = True
                else:
                    # Rate limit burst within dynamic minimum interval
                    should_transmit = False
            else:
                # Screen is static
                if now < self._dynamic_cooldown_until:
                    # Still within dynamic cooldown window
                    self.state = RadarState.DYNAMIC
                    min_dynamic_gap = self.config.dynamic_min_interval_ms / 1000.0
                    should_transmit = (time_since_last_send >= min_dynamic_gap)
                else:
                    self.state = RadarState.STATIC
                    # Send low-frequency heartbeat so Gemini knows screen is still connected
                    heartbeat_gap = self.config.static_heartbeat_interval_ms / 1000.0
                    should_transmit = (time_since_last_send >= heartbeat_gap)

            if should_transmit:
                self.frames_transmitted += 1
                self._last_transmit_time = now
                self._record_transmission(now, len(current_thumb))
            else:
                self.frames_suppressed += 1

            return should_transmit, self.state, self._build_telemetry(now, monitor_id)

    # -------------------------------------------------------------------------
    # Telemetry and Statistics
    # -------------------------------------------------------------------------
    def _record_transmission(self, now: float, byte_count: int):
        self._recent_transmit_timestamps.append(now)
        self._recent_bytes.append(byte_count)
        # Keep trailing 5 seconds of stats
        cutoff = now - 5.0
        while self._recent_transmit_timestamps and self._recent_transmit_timestamps[0] < cutoff:
            self._recent_transmit_timestamps.pop(0)
            self._recent_bytes.pop(0)

    def _build_telemetry(self, now: float, monitor_id: int) -> RadarTelemetry:
        saved_pct = 0.0
        if self.total_frames_evaluated > 0:
            saved_pct = (self.frames_suppressed / self.total_frames_evaluated) * 100.0

        cutoff = now - 5.0
        active_window = [t for t in self._recent_transmit_timestamps if t >= cutoff]
        fps = len(active_window) / 5.0 if active_window else 0.0

        bytes_sent = sum(self._recent_bytes)
        kbps = (bytes_sent * 8.0) / (5.0 * 1024.0) if bytes_sent else 0.0

        return RadarTelemetry(
            state=self.state,
            total_frames_evaluated=self.total_frames_evaluated,
            frames_transmitted=self.frames_transmitted,
            frames_suppressed=self.frames_suppressed,
            bandwidth_saved_pct=round(saved_pct, 1),
            effective_fps=round(fps, 2),
            effective_kbps=round(kbps, 1),
            last_change_score=round(self.last_change_score, 4),
            last_dhash_distance=self.last_dhash_dist,
            last_transmission_time=self._last_transmit_time,
            foreground_window_title=self._last_foreground_title,
            privacy_shield_active=self._privacy_shield_engaged,
            active_monitor_id=monitor_id,
            hud_masked=(self._hud_rect is not None and self.config.mask_hud_enabled)
        )

    def get_telemetry(self) -> Dict[str, Any]:
        """Returns structured JSON-serializable telemetry dictionary."""
        with self._lock:
            telem = self._build_telemetry(time.time(), 0)
            from hud_capture_exclusion import GLOBAL_HUD_EXCLUSION_MANAGER
            return {
                "state": telem.state.value,
                "total_frames_evaluated": telem.total_frames_evaluated,
                "frames_transmitted": telem.frames_transmitted,
                "frames_suppressed": telem.frames_suppressed,
                "bandwidth_saved_pct": telem.bandwidth_saved_pct,
                "effective_fps": telem.effective_fps,
                "effective_kbps": telem.effective_kbps,
                "last_change_score": telem.last_change_score,
                "last_dhash_distance": telem.last_dhash_distance,
                "foreground_window_title": telem.foreground_window_title,
                "privacy_shield_active": telem.privacy_shield_active,
                "active_monitor_id": telem.active_monitor_id,
                "hud_masked": telem.hud_masked,
                "hud_exclusion": GLOBAL_HUD_EXCLUSION_MANAGER.get_diagnostic_status().to_dict()
            }


# Singleton Global Radar Engine
GLOBAL_RADAR_ENGINE = AdaptiveRadarEngine()
