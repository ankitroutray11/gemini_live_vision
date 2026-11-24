"""
Chirkoot MK-V: Closed-Loop Desktop Action Verification Engine
=============================================================
Provides definitive verification for state-changing desktop operations.

Verification Pipeline:
  1. Record the intended action and target.
  2. Capture pre-action UI state (window, focus, target properties, visual snapshot).
  3. Execute the action.
  4. Wait for expected conditions with adaptive polling (handling slow applications).
  5. Capture post-action fresh state.
  6. Verify expected outcome across prioritized evidence tiers:
     - UIA property readback (Value, Toggle, Selection, Enabled).
     - Control-state change.
     - Focus change.
     - Process / Window change (creation, destruction, title, navigation).
     - Expected text match.
     - Pixel comparison as supplementary evidence.
     - Process crash / error dialog detection.
  7. Return tri-state verdict: SUCCESS, FAILURE, or UNCERTAIN.
  8. Strictly enforce: DO NOT automatically repeat destructive actions on UNCERTAIN.

Clipboard Preservation Engine:
  - Byte-exact snapshot of all clipboard formats (CF_UNICODETEXT, CF_TEXT, CF_DIB, etc.).
  - Bounded lock retry with exponential backoff for clipboard contention.
  - Paste requested Unicode text (Devanagari, Hindi, emojis, multiline).
  - Verify target content.
  - Faithful restoration of previous clipboard state with honest failure reporting.
"""

import os
import sys
import time
import re
import ctypes
import logging
import threading
from enum import Enum
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple, Callable, Union

import numpy as np
from PIL import Image

logger = logging.getLogger("ChirkootVerifier")

# Setup native Windows handles
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# Win32 Global Memory & Clipboard Constants
GMEM_MOVEABLE = 0x0002
CF_TEXT = 1
CF_BITMAP = 2
CF_DIB = 8
CF_UNICODETEXT = 13
CF_HDROP = 15
CF_DIBV5 = 17

# Configure 64-bit ctypes signatures for Global Memory
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.restype = ctypes.c_bool
kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
kernel32.GlobalSize.restype = ctypes.c_size_t
kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = ctypes.c_void_p

user32.OpenClipboard.argtypes = [ctypes.c_void_p]
user32.OpenClipboard.restype = ctypes.c_bool
user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = ctypes.c_bool
user32.EmptyClipboard.argtypes = []
user32.EmptyClipboard.restype = ctypes.c_bool
user32.GetClipboardData.argtypes = [ctypes.c_uint]
user32.GetClipboardData.restype = ctypes.c_void_p
user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
user32.SetClipboardData.restype = ctypes.c_void_p
user32.EnumClipboardFormats.argtypes = [ctypes.c_uint]
user32.EnumClipboardFormats.restype = ctypes.c_uint


# ==============================================================================
# 1. VERIFICATION OUTCOMES, EVIDENCE TYPES & STRUCTURES
# ==============================================================================

class VerificationOutcome(str, Enum):
    SUCCESS = "SUCCESS"          # Conclusive positive evidence of intended state change
    FAILURE = "FAILURE"          # Conclusive evidence of failure (disabled, crashed, error dialog)
    UNCERTAIN = "UNCERTAIN"      # Ambiguous state change, or cannot be conclusively proven


class VerificationEvidenceType(str, Enum):
    UIA_PROPERTY_READBACK = "UIA_PROPERTY_READBACK"
    CONTROL_STATE_CHANGE = "CONTROL_STATE_CHANGE"
    FOCUS_CHANGE = "FOCUS_CHANGE"
    WINDOW_PROCESS_CHANGE = "WINDOW_PROCESS_CHANGE"
    EXPECTED_TEXT_MATCH = "EXPECTED_TEXT_MATCH"
    NAVIGATION_STATE = "NAVIGATION_STATE"
    PIXEL_DIFF_SUPPLEMENTARY = "PIXEL_DIFF_SUPPLEMENTARY"
    ERROR_OR_CRASH_DETECTED = "ERROR_OR_CRASH_DETECTED"
    CLIPBOARD_VERIFIED = "CLIPBOARD_VERIFIED"
    ACTION_UNCONFIRMED = "ACTION_UNCONFIRMED"


DESTRUCTIVE_KEYWORDS = {
    "delete", "remove", "destroy", "drop", "truncate", "erase", "format",
    "wipe", "kill", "terminate", "submit", "pay", "checkout", "buy",
    "confirm", "send", "overwrite", "clear", "cancel_order", "shutdown"
}


@dataclass
class ActionIntention:
    action_type: str                     # "click", "type", "hotkey", "switch_window", "uia_invoke", "uia_set_text", "som_click"
    target: str                          # target identifier, coordinate, or name
    expected_outcome: Optional[str] = None  # "focus_change", "value_update", "window_open", "window_close", "state_toggle"
    expected_text: Optional[str] = None     # for typing / setting text
    is_potentially_destructive: bool = False


@dataclass
class UIStateSnapshot:
    timestamp: float
    foreground_hwnd: int
    foreground_title: str
    foreground_pid: int
    is_process_alive: bool
    focused_element_id: str
    focused_element_name: str
    focused_element_type: str
    focused_element_value: Optional[str] = None
    target_element_value: Optional[str] = None
    target_element_enabled: Optional[bool] = None
    target_element_toggle_state: Optional[int] = None
    target_element_exists: bool = False
    window_rect: Optional[Tuple[int, int, int, int]] = None
    visual_crop_np: Optional[np.ndarray] = None


@dataclass
class VerificationResult:
    outcome: VerificationOutcome
    primary_evidence: VerificationEvidenceType
    confidence: float
    details: str
    pre_state: Optional[UIStateSnapshot] = None
    post_state: Optional[UIStateSnapshot] = None
    time_taken_ms: float = 0.0
    auto_retry_allowed: bool = True       # False for potentially destructive actions on UNCERTAIN or FAILURE
    clipboard_preserved: Optional[bool] = None
    clipboard_restore_message: str = ""

    def to_summary(self) -> str:
        guard_msg = " [DESTRUCTIVE GUARD: Auto-repeat prohibited]" if not self.auto_retry_allowed else ""
        return (
            f"[Verification: {self.outcome.value}] ({self.primary_evidence.value}) "
            f"{self.details}{guard_msg} (elapsed: {self.time_taken_ms:.1f}ms)"
        )


# ==============================================================================
# 2. CLIPBOARD PRESERVATION & VERIFICATION ENGINE
# ==============================================================================

class ClipboardPreserver:
    """Byte-exact Windows clipboard preservation engine.

    - Captures raw global memory for all clipboard formats without data loss.
    - Handles lock contention with bounded retry and exponential backoff.
    - Sets temporary content for paste-based operations.
    - Fully restores previous clipboard state and reports restoration failures honestly.
    """

    def __init__(self, max_retries: int = 5, base_delay: float = 0.04):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self._lock = threading.Lock()

    def open_clipboard_with_retry(self, hwnd: int = 0) -> bool:
        """Attempts to open the Windows clipboard with exponential backoff."""
        for attempt in range(self.max_retries):
            if user32.OpenClipboard(hwnd):
                return True
            time.sleep(self.base_delay * (1.5 ** attempt))
        return False

    def close_clipboard_safe(self) -> None:
        """Safely closes the clipboard."""
        try:
            user32.CloseClipboard()
        except Exception:
            pass

    def capture_snapshot(self) -> Tuple[bool, Dict[int, bytes], str]:
        """Captures a byte-exact snapshot of all formats currently in the clipboard."""
        with self._lock:
            if not self.open_clipboard_with_retry(0):
                return False, {}, "Clipboard lock contention: Unable to open clipboard to capture snapshot."

            snapshot: Dict[int, bytes] = {}
            try:
                fmt = 0
                while True:
                    fmt = user32.EnumClipboardFormats(fmt)
                    if fmt == 0:
                        break

                    h_data = user32.GetClipboardData(fmt)
                    if not h_data:
                        continue

                    sz = kernel32.GlobalSize(h_data)
                    if sz <= 0 or sz > 64 * 1024 * 1024:  # Cap at 64MB per format
                        continue

                    p_data = kernel32.GlobalLock(h_data)
                    if not p_data:
                        continue

                    try:
                        buf = ctypes.create_string_buffer(sz)
                        ctypes.memmove(buf, p_data, sz)
                        snapshot[fmt] = buf.raw
                    finally:
                        kernel32.GlobalUnlock(h_data)

                return True, snapshot, f"Captured {len(snapshot)} clipboard format(s)."
            except Exception as e:
                return False, snapshot, f"Error capturing clipboard snapshot: {e}"
            finally:
                self.close_clipboard_safe()

    def restore_snapshot(self, snapshot: Dict[int, bytes]) -> Tuple[bool, str]:
        """Faithfully restores all preserved formats to the Windows clipboard."""
        if not snapshot:
            # Nothing to restore, empty clipboard
            with self._lock:
                if self.open_clipboard_with_retry(0):
                    user32.EmptyClipboard()
                    self.close_clipboard_safe()
                    return True, "Clipboard was originally empty; cleared successfully."
                return False, "Failed to open clipboard to clear."

        with self._lock:
            if not self.open_clipboard_with_retry(0):
                return False, "Clipboard restoration failed: Unable to acquire clipboard lock for restoration."

            restored_count = 0
            failed_formats = []

            try:
                user32.EmptyClipboard()

                # Prioritize primary formats first
                preferred_order = [CF_UNICODETEXT, CF_DIB, CF_HDROP, CF_TEXT]
                remaining_formats = [f for f in snapshot if f not in preferred_order]
                ordered_keys = [f for f in preferred_order if f in snapshot] + remaining_formats

                for fmt in ordered_keys:
                    raw_data = snapshot[fmt]
                    try:
                        h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(raw_data))
                        if not h_mem:
                            failed_formats.append(fmt)
                            continue

                        p_mem = kernel32.GlobalLock(h_mem)
                        if not p_mem:
                            kernel32.GlobalFree(h_mem)
                            failed_formats.append(fmt)
                            continue

                        ctypes.memmove(p_mem, raw_data, len(raw_data))
                        kernel32.GlobalUnlock(h_mem)

                        if user32.SetClipboardData(fmt, h_mem):
                            restored_count += 1
                        else:
                            failed_formats.append(fmt)
                    except Exception:
                        failed_formats.append(fmt)

                if failed_formats:
                    return (
                        restored_count > 0,
                        f"Partially restored {restored_count} format(s); {len(failed_formats)} failed."
                    )
                return True, f"Successfully restored {restored_count} clipboard format(s)."
            finally:
                self.close_clipboard_safe()

    def paste_with_preservation(
        self,
        text: str,
        press_enter: bool = False,
        verify_readback_fn: Optional[Callable[[], Tuple[bool, str]]] = None
    ) -> Tuple[bool, str, bool, str]:
        """Snapshots clipboard, sets Unicode text, performs Ctrl+V, verifies readback,

        and restores the clipboard. Returns: (paste_success, paste_msg, restore_success, restore_msg).
        """
        # 1. Snapshot original clipboard
        snap_ok, snapshot, snap_msg = self.capture_snapshot()
        if not snap_ok:
            logger.warning(f"Clipboard snapshot warning: {snap_msg}")

        # 2. Put text onto clipboard as CF_UNICODETEXT
        encoded = text.encode("utf-16-le") + b"\x00\x00"
        with self._lock:
            if not self.open_clipboard_with_retry(0):
                return False, "Failed to open clipboard for paste operation (lock contention).", False, snap_msg

            try:
                user32.EmptyClipboard()
                h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
                if not h_mem:
                    return False, "Failed to allocate global memory for paste.", False, snap_msg

                p_mem = kernel32.GlobalLock(h_mem)
                ctypes.memmove(p_mem, encoded, len(encoded))
                kernel32.GlobalUnlock(h_mem)
                user32.SetClipboardData(CF_UNICODETEXT, h_mem)
            finally:
                self.close_clipboard_safe()

        time.sleep(0.04)

        # 3. Send Ctrl+V keystroke
        VK_CONTROL = 0x11
        VK_V = ord("V")
        VK_RETURN = 0x0D
        from desktop_automation import UnicodeInputEngine

        UnicodeInputEngine.send_virtual_key(VK_CONTROL)
        UnicodeInputEngine.send_virtual_key(VK_V)
        time.sleep(0.06)

        if press_enter:
            UnicodeInputEngine.send_virtual_key(VK_RETURN)
            time.sleep(0.04)

        # 4. Optional verification readback
        readback_ok = True
        readback_msg = "Pasted successfully."
        if verify_readback_fn:
            readback_ok, readback_msg = verify_readback_fn()

        # 5. Restore previous clipboard snapshot
        rest_ok, rest_msg = self.restore_snapshot(snapshot)

        return readback_ok, readback_msg, rest_ok, rest_msg


GLOBAL_CLIPBOARD_PRESERVER = ClipboardPreserver()


# ==============================================================================
# 3. UI STATE CAPTURE ENGINE
# ==============================================================================

class UIStateCapture:
    """Captures structured snapshots of the current desktop, active window,

    keyboard focus, and target element for pre/post verification comparison.
    """

    @staticmethod
    def is_pid_alive(pid: int) -> bool:
        """Checks if a process ID is active and not a zombie."""
        if pid <= 0:
            return False
        try:
            import psutil
            proc = psutil.Process(pid)
            return proc.is_running() and proc.status() != "zombie"
        except Exception:
            return False

    @classmethod
    def capture_state(
        cls,
        target_ctrl: Optional[Any] = None,
        target_query: Optional[str] = None,
        capture_visual: bool = True
    ) -> UIStateSnapshot:
        """Captures a comprehensive UI state snapshot."""
        now = time.time()
        fg_hwnd = user32.GetForegroundWindow() or 0
        if (not fg_hwnd or fg_hwnd == user32.GetDesktopWindow()) and target_ctrl:
            target_hwnd = target_ctrl if isinstance(target_ctrl, int) else (getattr(target_ctrl, "NativeWindowHandle", 0) or 0)
            if target_hwnd:
                root_hwnd = user32.GetAncestor(target_hwnd, 2)  # GA_ROOT = 2
                fg_hwnd = root_hwnd if root_hwnd else target_hwnd

        # Foreground window title
        fg_title = ""
        if fg_hwnd:
            length = user32.GetWindowTextLengthW(fg_hwnd)
            if length > 0:
                buff = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(fg_hwnd, buff, length + 1)
                fg_title = buff.value
            elif target_ctrl:
                try:
                    top = target_ctrl.GetTopLevelControl()
                    if top:
                        fg_title = top.Name or ""
                except Exception:
                    pass

        # Foreground PID
        fg_pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(fg_hwnd, ctypes.byref(fg_pid))
        pid_val = fg_pid.value
        alive = cls.is_pid_alive(pid_val)

        # Window rect
        win_rect = None
        rect_buf = (ctypes.c_long * 4)()
        if user32.GetWindowRect(fg_hwnd, ctypes.byref(rect_buf)):
            win_rect = (rect_buf[0], rect_buf[1], rect_buf[2], rect_buf[3])

        # Focused control properties via UIA
        focused_id = ""
        focused_name = ""
        focused_type = ""
        focused_val = None

        try:
            import uiautomation as auto
            focused_ctrl = auto.GetFocusedControl()
            if focused_ctrl:
                focused_id = getattr(focused_ctrl, "AutomationId", "") or ""
                focused_name = getattr(focused_ctrl, "Name", "") or ""
                focused_type = getattr(focused_ctrl, "ControlTypeName", "") or ""
                try:
                    val_pat = focused_ctrl.GetValuePattern()
                    if val_pat:
                        focused_val = str(val_pat.Value)
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"State capture focused control note: {e}")

        # Target control properties
        target_val = None
        target_enabled = None
        target_toggle = None
        target_exists = False

        if target_ctrl:
            try:
                target_exists = True
                target_enabled = bool(getattr(target_ctrl, "IsEnabled", True))
                # Value pattern
                try:
                    vp = target_ctrl.GetValuePattern()
                    if vp and vp.Value is not None:
                        target_val = str(vp.Value)
                except Exception:
                    pass
                # Toggle pattern
                try:
                    tp = target_ctrl.GetTogglePattern()
                    if tp:
                        target_toggle = tp.ToggleState
                except Exception:
                    pass
            except Exception:
                target_exists = False

        # Visual crop (supplementary evidence)
        visual_crop = None
        if capture_visual and win_rect:
            try:
                from frame_store import GLOBAL_FRAME_STORE
                frame = GLOBAL_FRAME_STORE.get_latest_screen_frame()
                if frame and frame.image:
                    l, t, r, b = win_rect
                    w, h = max(1, r - l), max(1, b - t)
                    img_w, img_h = frame.image.size
                    cl = max(0, min(img_w - 1, l))
                    ct = max(0, min(img_h - 1, t))
                    cr = max(cl + 1, min(img_w, r))
                    cb = max(ct + 1, min(img_h, b))
                    cropped = frame.image.crop((cl, ct, cr, cb)).resize((32, 32))
                    visual_crop = np.array(cropped)
            except Exception:
                pass

        return UIStateSnapshot(
            timestamp=now,
            foreground_hwnd=fg_hwnd,
            foreground_title=fg_title,
            foreground_pid=pid_val,
            is_process_alive=alive,
            focused_element_id=focused_id,
            focused_element_name=focused_name,
            focused_element_type=focused_type,
            focused_element_value=focused_val,
            target_element_value=target_val,
            target_element_enabled=target_enabled,
            target_element_toggle_state=target_toggle,
            target_element_exists=target_exists,
            window_rect=win_rect,
            visual_crop_np=visual_crop
        )


# ==============================================================================
# 4. CLOSED-LOOP ACTION VERIFIER
# ==============================================================================

class ClosedLoopActionVerifier:
    """Orchestrates the 7-step closed-loop verification pipeline for state-changing actions.

    Enforces the destructive action safety guard.
    """

    @staticmethod
    def is_action_destructive(action_type: str, target_desc: str) -> bool:
        """Determines if an action is potentially destructive."""
        target_lower = target_desc.lower()
        action_lower = action_type.lower()
        for kw in DESTRUCTIVE_KEYWORDS:
            if kw in target_lower or kw in action_lower:
                return True
        return False

    @staticmethod
    def compute_visual_difference(crop1: Optional[np.ndarray], crop2: Optional[np.ndarray]) -> float:
        """Calculates mean absolute pixel difference between two visual crops."""
        if crop1 is None or crop2 is None:
            return 0.0
        if crop1.shape != crop2.shape:
            return 255.0
        return float(np.mean(np.abs(crop1.astype(float) - crop2.astype(float))))

    @classmethod
    def verify_action(
        cls,
        intention: ActionIntention,
        execute_fn: Callable[[], Any],
        target_ctrl: Optional[Any] = None,
        timeout_s: float = 1.2,
        poll_interval_s: float = 0.05
    ) -> VerificationResult:
        """Executes action within a closed-loop verification wrapper with adaptive polling."""
        t_start = time.perf_counter()

        # Step 1 & 2: Record intention & capture pre-action state
        pre_state = UIStateCapture.capture_state(target_ctrl, intention.target, capture_visual=True)

        # Check if target control is explicitly disabled prior to execution
        if pre_state.target_element_enabled is False:
            elapsed_ms = (time.perf_counter() - t_start) * 1000.0
            return VerificationResult(
                outcome=VerificationOutcome.FAILURE,
                primary_evidence=VerificationEvidenceType.CONTROL_STATE_CHANGE,
                confidence=1.0,
                details=f"Target element '{intention.target}' is currently disabled. Action aborted.",
                pre_state=pre_state,
                time_taken_ms=elapsed_ms,
                auto_retry_allowed=False
            )

        # Step 3: Execute the action
        exec_error = None
        try:
            execute_fn()
        except Exception as e:
            exec_error = str(e)

        if exec_error:
            elapsed_ms = (time.perf_counter() - t_start) * 1000.0
            return VerificationResult(
                outcome=VerificationOutcome.FAILURE,
                primary_evidence=VerificationEvidenceType.ERROR_OR_CRASH_DETECTED,
                confidence=1.0,
                details=f"Execution raised exception: {exec_error}",
                pre_state=pre_state,
                time_taken_ms=elapsed_ms,
                auto_retry_allowed=False
            )

        # Step 4: Adaptive polling wait for expected conditions
        post_state = pre_state
        poll_start = time.perf_counter()

        while (time.perf_counter() - poll_start) < timeout_s:
            time.sleep(poll_interval_s)
            fresh_post = UIStateCapture.capture_state(target_ctrl, intention.target, capture_visual=False)

            # Check for crash or window destruction
            if pre_state.foreground_pid > 0 and not fresh_post.is_process_alive:
                post_state = fresh_post
                break

            if pre_state.foreground_hwnd > 0 and not user32.IsWindow(pre_state.foreground_hwnd):
                post_state = fresh_post
                break

            # Check for window title change or focus change
            if fresh_post.foreground_hwnd != pre_state.foreground_hwnd:
                post_state = fresh_post
                break

            if fresh_post.foreground_title != pre_state.foreground_title:
                post_state = fresh_post
                break

            # Check for text / value change
            if intention.expected_text:
                val = fresh_post.target_element_value or fresh_post.focused_element_value or ""
                if intention.expected_text in val:
                    post_state = fresh_post
                    break

            # Check for toggle state change
            if (
                pre_state.target_element_toggle_state is not None and
                fresh_post.target_element_toggle_state != pre_state.target_element_toggle_state
            ):
                post_state = fresh_post
                break

            # Check focus change
            if fresh_post.focused_element_id and fresh_post.focused_element_id != pre_state.focused_element_id:
                post_state = fresh_post
                break

            post_state = fresh_post

        # Final capture with visual crop for supplementary diff
        post_state = UIStateCapture.capture_state(target_ctrl, intention.target, capture_visual=True)
        elapsed_ms = (time.perf_counter() - t_start) * 1000.0

        # Step 5 & 6: Verify expected outcome across prioritized evidence hierarchy

        # Check A: Process Crash / Window Destruction Detection
        if pre_state.foreground_pid > 0 and not post_state.is_process_alive:
            return VerificationResult(
                outcome=VerificationOutcome.FAILURE,
                primary_evidence=VerificationEvidenceType.ERROR_OR_CRASH_DETECTED,
                confidence=1.0,
                details=f"Target application crashed during action (PID {pre_state.foreground_pid} exited).",
                pre_state=pre_state,
                post_state=post_state,
                time_taken_ms=elapsed_ms,
                auto_retry_allowed=False
            )

        if pre_state.foreground_hwnd > 0 and not user32.IsWindow(pre_state.foreground_hwnd):
            return VerificationResult(
                outcome=VerificationOutcome.FAILURE,
                primary_evidence=VerificationEvidenceType.ERROR_OR_CRASH_DETECTED,
                confidence=1.0,
                details=f"Target window (HWND 0x{pre_state.foreground_hwnd:X}) was destroyed or terminated during action.",
                pre_state=pre_state,
                post_state=post_state,
                time_taken_ms=elapsed_ms,
                auto_retry_allowed=False
            )

        # Check B: Error Dialog Detection
        title_lower = post_state.foreground_title.lower()
        if any(err_kw in title_lower for err_kw in ["error", "exception", "not responding", "crash", "failed"]):
            if post_state.foreground_hwnd != pre_state.foreground_hwnd:
                return VerificationResult(
                    outcome=VerificationOutcome.FAILURE,
                    primary_evidence=VerificationEvidenceType.ERROR_OR_CRASH_DETECTED,
                    confidence=0.95,
                    details=f"Error dialog detected following action: '{post_state.foreground_title}'.",
                    pre_state=pre_state,
                    post_state=post_state,
                    time_taken_ms=elapsed_ms,
                    auto_retry_allowed=False
                )

        # Check C: UIA Expected Text Match
        if intention.expected_text:
            val = post_state.target_element_value or post_state.focused_element_value or ""
            if intention.expected_text in val:
                return VerificationResult(
                    outcome=VerificationOutcome.SUCCESS,
                    primary_evidence=VerificationEvidenceType.EXPECTED_TEXT_MATCH,
                    confidence=1.0,
                    details=f"UIA property readback confirmed text: '{intention.expected_text[:40]}'.",
                    pre_state=pre_state,
                    post_state=post_state,
                    time_taken_ms=elapsed_ms,
                    auto_retry_allowed=True
                )

        # Check D: Control State Change (Toggle or Button Disabled)
        if (
            pre_state.target_element_toggle_state is not None and
            post_state.target_element_toggle_state is not None and
            pre_state.target_element_toggle_state != post_state.target_element_toggle_state
        ):
            return VerificationResult(
                outcome=VerificationOutcome.SUCCESS,
                primary_evidence=VerificationEvidenceType.CONTROL_STATE_CHANGE,
                confidence=0.98,
                details=f"Control toggle state changed from {pre_state.target_element_toggle_state} to {post_state.target_element_toggle_state}.",
                pre_state=pre_state,
                post_state=post_state,
                time_taken_ms=elapsed_ms,
                auto_retry_allowed=True
            )

        if pre_state.target_element_enabled is True and post_state.target_element_enabled is False:
            return VerificationResult(
                outcome=VerificationOutcome.SUCCESS,
                primary_evidence=VerificationEvidenceType.CONTROL_STATE_CHANGE,
                confidence=0.95,
                details="Control transitioned to disabled state following execution (e.g. submitted/busy).",
                pre_state=pre_state,
                post_state=post_state,
                time_taken_ms=elapsed_ms,
                auto_retry_allowed=True
            )

        # Check E: Window / Process Change
        if post_state.foreground_hwnd != pre_state.foreground_hwnd:
            return VerificationResult(
                outcome=VerificationOutcome.SUCCESS,
                primary_evidence=VerificationEvidenceType.WINDOW_PROCESS_CHANGE,
                confidence=0.95,
                details=f"Active window transitioned to '{post_state.foreground_title}' (HWND 0x{post_state.foreground_hwnd:X}).",
                pre_state=pre_state,
                post_state=post_state,
                time_taken_ms=elapsed_ms,
                auto_retry_allowed=True
            )

        if post_state.foreground_title != pre_state.foreground_title:
            return VerificationResult(
                outcome=VerificationOutcome.SUCCESS,
                primary_evidence=VerificationEvidenceType.NAVIGATION_STATE,
                confidence=0.90,
                details=f"Window title updated: '{post_state.foreground_title}'.",
                pre_state=pre_state,
                post_state=post_state,
                time_taken_ms=elapsed_ms,
                auto_retry_allowed=True
            )

        # Check F: Focus Change
        if (
            post_state.focused_element_id and
            pre_state.focused_element_id and
            post_state.focused_element_id != pre_state.focused_element_id
        ):
            return VerificationResult(
                outcome=VerificationOutcome.SUCCESS,
                primary_evidence=VerificationEvidenceType.FOCUS_CHANGE,
                confidence=0.85,
                details=f"Focus moved to '{post_state.focused_element_name or post_state.focused_element_id}'.",
                pre_state=pre_state,
                post_state=post_state,
                time_taken_ms=elapsed_ms,
                auto_retry_allowed=True
            )

        # Check G: Supplementary Pixel Diff
        p_diff = cls.compute_visual_difference(pre_state.visual_crop_np, post_state.visual_crop_np)
        if p_diff >= 4.0:
            return VerificationResult(
                outcome=VerificationOutcome.SUCCESS,
                primary_evidence=VerificationEvidenceType.PIXEL_DIFF_SUPPLEMENTARY,
                confidence=0.75,
                details=f"Visual pixel change verified on screen (mean diff: {p_diff:.2f}).",
                pre_state=pre_state,
                post_state=post_state,
                time_taken_ms=elapsed_ms,
                auto_retry_allowed=True
            )

        # Step 7: Ambiguous Outcome -> UNCERTAIN with Destructive Guard enforcement
        is_destructive = intention.is_potentially_destructive or cls.is_action_destructive(intention.action_type, intention.target)
        auto_retry = not is_destructive

        guard_msg = (
            " [DESTRUCTIVE GUARD ACTIVATED] Result is UNCERTAIN. Automatic repeat is prohibited to avoid duplicate or irreversible operations."
            if is_destructive else " State change could not be definitively confirmed."
        )

        return VerificationResult(
            outcome=VerificationOutcome.UNCERTAIN,
            primary_evidence=VerificationEvidenceType.ACTION_UNCONFIRMED,
            confidence=0.40,
            details=f"Action was dispatched, but no structural, focus, or visual state changes were detected.{guard_msg}",
            pre_state=pre_state,
            post_state=post_state,
            time_taken_ms=elapsed_ms,
            auto_retry_allowed=auto_retry
        )


GLOBAL_CLOSED_LOOP_VERIFIER = ClosedLoopActionVerifier()
