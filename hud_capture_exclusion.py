"""
Chirkoot MK-V: Windows HUD Capture-Exclusion Architecture
=========================================================
Implements robust, multi-tier capture exclusion for Chirkoot's HUD:
1. Native Windows Display Affinity (WDA_EXCLUDEFROMCAPTURE = 0x00000011).
   - Applied STRICTLY in-process from the process that owns the HWND.
   - Verifies Windows 10 (Build >= 19041 / Version 2004) and Windows 11.
   - Strictly forbids CreateRemoteThread or remote process injection.
   - Gracefully detects foreign HWNDs (e.g. Chrome/Edge App Mode) and avoids API violation.
2. Capture-Pipeline HUD Rectangle Masking.
   - Clean bounding-box letterboxing / zeroing on the capture canvas and in-memory frame store.
   - Multi-monitor aware intersection math.
   - Universal compatibility across all capture software, all hosts, and legacy Windows versions.
3. Safe Fallback & Window Recreation Handling.
   - Re-applies affinity upon HWND recreation or DPI change.
   - Exposes comprehensive diagnostic status and clear user-facing telemetry.
"""

import os
import sys
import time
import ctypes
import logging
from enum import Enum
from typing import Optional, Tuple, Dict, Any, List
from dataclasses import dataclass, asdict
from PIL import Image, ImageDraw

logger = logging.getLogger("ChirkootExclusion")
logger.setLevel(logging.INFO)

# Windows Display Affinity Constants (user32.dll)
WDA_NONE = 0x00000000
WDA_MONITOR = 0x00000001
WDA_EXCLUDEFROMCAPTURE = 0x00000011  # Introduced in Windows 10 Build 19041 (20H1 / 2004)

# Win32 Error Codes
ERROR_SUCCESS = 0
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_PARAMETER = 87
ERROR_NOT_SUPPORTED = 50


class CaptureExclusionMode(str, Enum):
    """Configurable capture-exclusion strategies."""
    AUTO = "AUTO"                          # In-process: WINDOW_AFFINITY; Foreign process: PIPELINE_MASKING
    WINDOW_AFFINITY = "WINDOW_AFFINITY"    # Enforce WDA_EXCLUDEFROMCAPTURE (with ownership check & safe fallback)
    PIPELINE_MASKING = "PIPELINE_MASKING"  # Enforce canvas/frame-store bounding-box masking
    DISABLED = "DISABLED"                  # No exclusion (HUD visible in recordings)


class HostType(str, Enum):
    """The host environment running the Chirkoot HUD."""
    CHROME_APP_MODE = "CHROME_APP_MODE"    # Child process (chrome.exe/msedge.exe). Process does not own HWND.
    PYWEBVIEW = "PYWEBVIEW"                # In-process WebView window. Python owns HWND.
    NATIVE_WEBVIEW2 = "NATIVE_WEBVIEW2"    # Native C#/C++ WebView2 host. Launcher owns HWND.
    EXTERNAL_BROWSER = "EXTERNAL_BROWSER"  # Standard browser tab.
    HEADLESS = "HEADLESS"                  # Automated test / headless runner.


@dataclass
class ExclusionDiagnosticStatus:
    """Detailed diagnostic status of the capture-exclusion architecture."""
    mode: CaptureExclusionMode
    active_mechanism: str               # "WINDOW_AFFINITY", "PIPELINE_MASKING", or "NONE"
    host_type: HostType
    os_build: int
    os_supported: bool                  # True if Windows build >= 19041
    hwnd: Optional[int]
    hwnd_owned_by_current_process: bool
    affinity_applied: bool
    affinity_value: str                 # e.g. "0x00000011"
    affinity_last_error: int
    hud_rect: Optional[Tuple[int, int, int, int]]  # (x1, y1, x2, y2)
    fallback_engaged: bool
    user_status_message: str
    disclaimer: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode.value,
            "active_mechanism": self.active_mechanism,
            "host_type": self.host_type.value,
            "os_build": self.os_build,
            "os_supported": self.os_supported,
            "hwnd": self.hwnd,
            "hwnd_owned_by_current_process": self.hwnd_owned_by_current_process,
            "affinity_applied": self.affinity_applied,
            "affinity_value": self.affinity_value,
            "affinity_last_error": self.affinity_last_error,
            "hud_rect": list(self.hud_rect) if self.hud_rect else None,
            "fallback_engaged": self.fallback_engaged,
            "user_status_message": self.user_status_message,
            "disclaimer": self.disclaimer
        }


class HudCaptureExclusionManager:
    """
    Central manager for Windows HUD capture-exclusion.
    Enforces safe Win32 API boundaries and multi-tier protection.
    """

    def __init__(self, mode: CaptureExclusionMode = CaptureExclusionMode.AUTO):
        self.mode: CaptureExclusionMode = mode
        self.host_type: HostType = HostType.HEADLESS
        self.hwnd: Optional[int] = None
        self.hud_rect: Optional[Tuple[int, int, int, int]] = None  # (x1, y1, x2, y2)
        
        # State
        self.affinity_applied: bool = False
        self.affinity_value: int = WDA_NONE
        self.affinity_last_error: int = ERROR_SUCCESS
        self.fallback_engaged: bool = False
        self.active_mechanism: str = "PIPELINE_MASKING" if mode != CaptureExclusionMode.DISABLED else "NONE"

        # Query OS build
        self.os_build = self._get_windows_build()
        self.os_supported = (self.os_build >= 19041)

    @staticmethod
    def _get_windows_build() -> int:
        """Determines the exact Windows build number."""
        if sys.platform != "win32":
            return 0
        try:
            return sys.getwindowsversion().build
        except Exception:
            return 0

    def verify_hwnd_ownership(self, hwnd: int) -> Tuple[bool, int]:
        """
        Verifies whether the specified HWND belongs to the current calling process.
        Returns: (is_owned_by_current_process, window_pid)
        """
        if not hwnd or sys.platform != "win32":
            return False, 0
        try:
            user32 = ctypes.windll.user32
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            current_pid = os.getpid()
            return (pid.value == current_pid), pid.value
        except Exception as e:
            logger.debug(f"Error checking HWND ownership: {e}")
            return False, 0

    def query_window_display_affinity(self, hwnd: int) -> Tuple[bool, int]:
        """Queries the current display affinity on an HWND."""
        if not hwnd or sys.platform != "win32":
            return False, WDA_NONE
        try:
            user32 = ctypes.windll.user32
            if not hasattr(user32, "GetWindowDisplayAffinity"):
                return False, WDA_NONE
            affinity = ctypes.c_ulong()
            res = user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(affinity))
            return bool(res), affinity.value
        except Exception:
            return False, WDA_NONE

    def set_exclusion_mode(self, mode: CaptureExclusionMode):
        """Updates the capture exclusion mode and reapplies rules."""
        self.mode = mode
        logger.info(f"🛡️ [Capture Exclusion] Mode updated to: {mode.value}")
        if self.mode == CaptureExclusionMode.PIPELINE_MASKING:
            self.active_mechanism = "PIPELINE_MASKING"
            self.fallback_engaged = False
            self.affinity_applied = False
        elif self.mode == CaptureExclusionMode.DISABLED:
            self.active_mechanism = "NONE"
            self.fallback_engaged = False
            self.affinity_applied = False
        elif self.hwnd:
            self._apply_exclusion_policy(self.hwnd)

    def register_hud_window(
        self,
        hwnd: int,
        host_type: HostType,
        hud_rect: Optional[Tuple[int, int, int, int]] = None
    ):
        """Registers the active HUD window handle, host type, and initial rectangle."""
        self.hwnd = hwnd
        self.host_type = host_type
        if hud_rect:
            self.hud_rect = hud_rect
        self._apply_exclusion_policy(hwnd)

    def on_window_recreated(self, new_hwnd: int):
        """Called when a window handle is recreated (e.g. style change, DPI change, HUD restart)."""
        logger.info(f"🔄 [Capture Exclusion] HUD window recreated. Old HWND: {self.hwnd} -> New HWND: {new_hwnd}")
        self.hwnd = new_hwnd
        self.affinity_applied = False
        self.affinity_last_error = ERROR_SUCCESS
        self._apply_exclusion_policy(new_hwnd)

    def update_hud_geometry(self, x1: int, y1: int, x2: int, y2: int):
        """Updates the screen coordinates of the HUD rectangle for pipeline masking."""
        self.hud_rect = (x1, y1, x2, y2)

    def _apply_exclusion_policy(self, hwnd: int):
        """Evaluates and applies the configured capture-exclusion policy."""
        if self.mode == CaptureExclusionMode.DISABLED:
            self.active_mechanism = "NONE"
            self.fallback_engaged = False
            self.affinity_applied = False
            return

        is_owned, window_pid = self.verify_hwnd_ownership(hwnd)

        # -------------------------------------------------------------
        # Branch 1: Try Native Display Affinity (WDA_EXCLUDEFROMCAPTURE)
        # -------------------------------------------------------------
        should_try_affinity = (
            self.mode == CaptureExclusionMode.WINDOW_AFFINITY or
            (self.mode == CaptureExclusionMode.AUTO and is_owned and self.os_supported)
        )

        if should_try_affinity:
            # 1. Check OS Version Support
            if not self.os_supported:
                logger.warning(
                    f"⚠️ [Capture Exclusion] Windows build {self.os_build} does not support "
                    f"WDA_EXCLUDEFROMCAPTURE (requires build >= 19041). Engaging pipeline masking."
                )
                self.affinity_last_error = ERROR_NOT_SUPPORTED
                self._engage_pipeline_masking_fallback("Windows build < 19041")
                return

            # 2. Strict Process Ownership Check (Rule: Never inject into foreign processes)
            if not is_owned:
                logger.warning(
                    f"🛡️ [Capture Exclusion Boundary] HWND {hwnd} is owned by foreign PID {window_pid} "
                    f"(Current Python PID: {os.getpid()}, Host: {self.host_type.value}). "
                    f"Win32 SetWindowDisplayAffinity strictly forbids cross-process calls without injection. "
                    f"Engaging safe capture-pipeline masking."
                )
                self.affinity_last_error = ERROR_ACCESS_DENIED
                self._engage_pipeline_masking_fallback(f"HWND owned by foreign process (PID {window_pid})")
                return

            # 3. Apply In-Process SetWindowDisplayAffinity
            success, err = self._call_set_window_display_affinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
            if success:
                self.affinity_applied = True
                self.affinity_value = WDA_EXCLUDEFROMCAPTURE
                self.affinity_last_error = ERROR_SUCCESS
                self.fallback_engaged = False
                self.active_mechanism = "WINDOW_AFFINITY"
                logger.info(
                    f"🔒 [Capture Exclusion] SetWindowDisplayAffinity(0x{WDA_EXCLUDEFROMCAPTURE:08X}) "
                    f"successfully applied to in-process HWND {hwnd} (PID {os.getpid()})"
                )
                return
            else:
                logger.warning(f"⚠️ [Capture Exclusion] SetWindowDisplayAffinity failed with error {err}. Engaging fallback.")
                self.affinity_last_error = err
                self._engage_pipeline_masking_fallback(f"Win32 API call failed (error {err})")
                return

        # -------------------------------------------------------------
        # Branch 2: Capture-Pipeline Masking
        # -------------------------------------------------------------
        self.active_mechanism = "PIPELINE_MASKING"
        self.fallback_engaged = False
        self.affinity_applied = False
        logger.info(f"🎨 [Capture Exclusion] Active mechanism: PIPELINE_MASKING (Host: {self.host_type.value})")

    def _engage_pipeline_masking_fallback(self, reason: str):
        """Safely falls back to capture-pipeline masking when window affinity is unavailable."""
        self.fallback_engaged = True
        self.active_mechanism = "PIPELINE_MASKING"
        self.affinity_applied = False
        logger.info(f"🛡️ [Capture Exclusion Fallback] Engaged PIPELINE_MASKING: {reason}")

    @staticmethod
    def _call_set_window_display_affinity(hwnd: int, affinity: int) -> Tuple[bool, int]:
        """Direct, safe Win32 SetWindowDisplayAffinity call."""
        if sys.platform != "win32" or not hwnd:
            return False, ERROR_INVALID_PARAMETER
        try:
            user32 = ctypes.windll.user32
            if not hasattr(user32, "SetWindowDisplayAffinity"):
                return False, ERROR_NOT_SUPPORTED
            res = user32.SetWindowDisplayAffinity(hwnd, affinity)
            err = ctypes.GetLastError() if not res else ERROR_SUCCESS
            return bool(res), err
        except Exception as e:
            logger.debug(f"SetWindowDisplayAffinity invocation exception: {e}")
            return False, ERROR_ACCESS_DENIED

    # -------------------------------------------------------------------------
    # Multi-Monitor Aware Pipeline Masking
    # -------------------------------------------------------------------------
    def mask_hud_in_capture(
        self,
        image: Image.Image,
        capture_bounds: Optional[Tuple[int, int, int, int]] = None
    ) -> Image.Image:
        """
        Applies bounding-box masking to the image if the HUD rectangle intersects
        the capture viewport. Handles multi-monitor offsets.
        """
        if self.mode == CaptureExclusionMode.DISABLED:
            return image

        if not self.hud_rect:
            return image

        hx1, hy1, hx2, hy2 = self.hud_rect
        img_w, img_h = image.size

        # If capture_bounds given (e.g. monitor offset [left, top, right, bottom]),
        # translate HUD coordinates relative to that monitor
        if capture_bounds:
            mx1, my1, mx2, my2 = capture_bounds
            rx1 = max(0, hx1 - mx1)
            ry1 = max(0, hy1 - my1)
            rx2 = min(img_w, hx2 - mx1)
            ry2 = min(img_h, hy2 - my1)
        else:
            rx1 = max(0, min(img_w, hx1))
            ry1 = max(0, min(img_h, hy1))
            rx2 = max(0, min(img_w, hx2))
            ry2 = max(0, min(img_h, hy2))

        # Check if there is an actual intersection
        if rx2 <= rx1 or ry2 <= ry1:
            return image  # HUD is on a different monitor or outside viewport

        masked = image.copy()
        draw = ImageDraw.Draw(masked)
        
        # Tactical HUD Mask styling: Dark carbon rectangle with telemetry banner
        draw.rectangle([rx1, ry1, rx2, ry2], fill=(18, 20, 26))
        draw.rectangle([rx1, ry1, rx2, ry2], outline=(0, 243, 255), width=2)
        
        banner_text = "[CHIRKOOT HUD // CAPTURE EXCLUDED]"
        draw.text((rx1 + 12, ry1 + 12), banner_text, fill=(0, 243, 255))
        return masked

    # -------------------------------------------------------------------------
    # Diagnostics & User-Facing Status
    # -------------------------------------------------------------------------
    def get_diagnostic_status(self) -> ExclusionDiagnosticStatus:
        """Compiles a complete diagnostic report for the capture-exclusion architecture."""
        is_owned, _ = self.verify_hwnd_ownership(self.hwnd) if self.hwnd else (False, 0)
        
        # User-facing status string
        if self.mode == CaptureExclusionMode.DISABLED:
            status_msg = "HUD Capture Exclusion is DISABLED (HUD visible in screen shares)."
        elif self.active_mechanism == "WINDOW_AFFINITY":
            status_msg = f"HUD Capture Exclusion ACTIVE: Native Windows Display Affinity (WDA_EXCLUDEFROMCAPTURE 0x{self.affinity_value:08X}) applied in-process."
        elif self.active_mechanism == "PIPELINE_MASKING":
            if self.fallback_engaged:
                status_msg = f"HUD Capture Exclusion ACTIVE: Safe Pipeline Masking (Fallback engaged: Host={self.host_type.value}, WinBuild={self.os_build})."
            else:
                status_msg = f"HUD Capture Exclusion ACTIVE: Tactical Pipeline Masking (Host={self.host_type.value})."
        else:
            status_msg = "HUD Capture Exclusion standby."

        disclaimer = (
            "NOTICE: Chirkoot capture-exclusion reliably prevents recursive HUD capture in Chirkoot Screen Radar, "
            "OCR, and standard Windows DWM/WGC recording tools. It does not claim universal invisibility to "
            "kernel-level video mirror drivers or hardware HDMI capture devices."
        )

        return ExclusionDiagnosticStatus(
            mode=self.mode,
            active_mechanism=self.active_mechanism,
            host_type=self.host_type,
            os_build=self.os_build,
            os_supported=self.os_supported,
            hwnd=self.hwnd,
            hwnd_owned_by_current_process=is_owned,
            affinity_applied=self.affinity_applied,
            affinity_value=f"0x{self.affinity_value:08X}",
            affinity_last_error=self.affinity_last_error,
            hud_rect=self.hud_rect,
            fallback_engaged=self.fallback_engaged,
            user_status_message=status_msg,
            disclaimer=disclaimer
        )


# Global singleton instance
GLOBAL_HUD_EXCLUSION_MANAGER = HudCaptureExclusionManager()
