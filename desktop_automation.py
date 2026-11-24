"""
Chirkoot MK-V: Structured Desktop Automation and Control Engine
==============================================================
Implements the 6-tier automation priority hierarchy:
  1. Application APIs or supported CLI (e.g., VS Code CLI `code`, process commands)
  2. Chromium CDP where appropriate (e.g., Chrome/Edge/Electron remote debugging)
  3. Windows UI Automation (UIA) via `uiautomation` (buttons, inputs, menus, trees)
  4. RapidOCR (in-memory optical character recognition for canvas/GDI/bitmap controls)
  5. Gemini Visual Grounding (Set-of-Mark with observation token binding)
  6. Normalized coordinate clicks (0–1000 DPI-aware fallback)

Core Features:
- Native per-monitor DPI awareness (DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
- Multi-monitor virtual desktop bounds calculation
- Robust multi-stage window switcher & foreground-lock unlocker (AttachThreadInput + Alt tap)
- UIPI privilege isolation detection & elevation awareness
- Direct Unicode SendInput engine (KEYEVENTF_UNICODE, UTF-16 code units, BMP + surrogate pairs)
- Set-of-Mark (SoM) observation manager with stale token rejection and visual badge overlay
- Comprehensive UIA tree inspector, pattern invoker, value setter, and menu navigator
"""

import os
import sys
import time
import uuid
import struct
import ctypes
import logging
import threading
from enum import Enum
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple, Union

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger("ChirkootAutomation")

# Setup Native Windows Win32 handles
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# ==============================================================================
# 1. PER-MONITOR DPI AWARENESS & SCREEN COORDINATE ENGINE
# ==============================================================================

# Windows Virtual Desktop & DPI Constants
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
SM_CXSCREEN = 0
SM_CYSCREEN = 1

# DPI Awareness Contexts
# -4 corresponds to DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 (Windows 10 1703+)
# -3 corresponds to DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)


def initialize_dpi_awareness() -> bool:
    """Configures the current process for per-monitor V2 DPI awareness."""
    try:
        if hasattr(user32, "SetProcessDpiAwarenessContext"):
            user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            user32.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
            success = user32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
            if success:
                logger.info("DPI awareness configured: Per-Monitor Aware V2.")
                return True
    except Exception as e:
        logger.debug(f"SetProcessDpiAwarenessContext note: {e}")

    try:
        if hasattr(user32, "SetProcessDPIAware"):
            user32.SetProcessDPIAware()
            logger.info("DPI awareness fallback configured: ProcessDPIAware.")
            return True
    except Exception as e:
        logger.debug(f"SetProcessDPIAware note: {e}")

    return False


# Initialize DPI awareness early
initialize_dpi_awareness()


@dataclass
class VirtualScreenMetrics:
    left: int
    top: int
    width: int
    height: int
    right: int
    bottom: int
    primary_width: int
    primary_height: int


class DpiCoordinateManager:
    """Manages coordinate conversions between normalized 0-1000 coordinates,

    window-relative coordinates, and physical multi-monitor coordinates.
    """

    @staticmethod
    def get_virtual_screen_metrics() -> VirtualScreenMetrics:
        """Retrieves exact virtual multi-monitor desktop bounds."""
        vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        vw = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        vh = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        pw = user32.GetSystemMetrics(SM_CXSCREEN)
        ph = user32.GetSystemMetrics(SM_CYSCREEN)

        # Fallback if virtual screen metrics are zero
        if vw <= 0 or vh <= 0:
            vx, vy = 0, 0
            vw, vh = (pw if pw > 0 else 1920), (ph if ph > 0 else 1080)

        return VirtualScreenMetrics(
            left=vx,
            top=vy,
            width=vw,
            height=vh,
            right=vx + vw,
            bottom=vy + vh,
            primary_width=pw,
            primary_height=ph
        )

    @classmethod
    def normalized_to_physical(
        cls,
        norm_x: int,
        norm_y: int,
        target_rect: Optional[Tuple[int, int, int, int]] = None
    ) -> Tuple[int, int]:
        """Converts normalized 0-1000 coordinates into physical pixel coordinates.

        If target_rect (left, top, right, bottom) is specified, maps within that window;
        otherwise, maps across the primary screen / virtual screen viewport.
        """
        clamped_x = max(0, min(1000, norm_x))
        clamped_y = max(0, min(1000, norm_y))

        if target_rect:
            l, t, r, b = target_rect
            w = max(1, r - l)
            h = max(1, b - t)
            px = int(round(l + (clamped_x / 1000.0) * w))
            py = int(round(t + (clamped_y / 1000.0) * h))
            return px, py

        metrics = cls.get_virtual_screen_metrics()
        # For full-screen display interactions, map over the primary screen width/height
        # or virtual display width/height
        px = int(round(metrics.left + (clamped_x / 1000.0) * metrics.primary_width))
        py = int(round(metrics.top + (clamped_y / 1000.0) * metrics.primary_height))
        return px, py

    @classmethod
    def physical_to_normalized(
        cls,
        px: int,
        py: int,
        target_rect: Optional[Tuple[int, int, int, int]] = None
    ) -> Tuple[int, int]:
        """Converts physical pixel coordinates into normalized 0-1000 coordinates."""
        if target_rect:
            l, t, r, b = target_rect
            w = max(1, r - l)
            h = max(1, b - t)
            norm_x = int(round(((px - l) / float(w)) * 1000))
            norm_y = int(round(((py - t) / float(h)) * 1000))
            return max(0, min(1000, norm_x)), max(0, min(1000, norm_y))

        metrics = cls.get_virtual_screen_metrics()
        norm_x = int(round(((px - metrics.left) / float(metrics.primary_width)) * 1000))
        norm_y = int(round(((py - metrics.top) / float(metrics.primary_height)) * 1000))
        return max(0, min(1000, norm_x)), max(0, min(1000, norm_y))


# ==============================================================================
# 2. UNICODE INPUT ENGINE (Direct Win32 SendInput, No Clipboard)
# ==============================================================================

# Win32 SendInput Structures
class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_size_t),
    ]

class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_ulong),
        ("wParamL", ctypes.c_short),
        ("wParamH", ctypes.c_ushort)
    ]

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_size_t)
    ]

class INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("ki", KEYBDINPUT),
        ("mi", MOUSEINPUT),
        ("hi", HARDWAREINPUT)
    ]

class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_ulong),
        ("union", INPUT_UNION)
    ]

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_RETURN = 0x0D

user32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = ctypes.c_uint


class UnicodeInputEngine:
    """Injects Unicode text directly using Win32 SendInput with KEYEVENTF_UNICODE.

    - Supports BMP characters, Devanagari/Hindi (नमस्ते, चिरकूट), Arabic, CJK, etc.
    - Accurately unpacks surrogate pairs for non-BMP characters (emojis 🚀, 🤖).
    - Eliminates clipboard mutation (zero clobber of user copy buffer).
    - Eliminates VkKeyScan-dependent layout mismatches.
    """

    @classmethod
    def text_to_utf16_code_units(cls, text: str) -> List[int]:
        """Encodes text into UTF-16LE 16-bit code units, including surrogate pairs."""
        raw_bytes = text.encode("utf-16-le")
        unit_count = len(raw_bytes) // 2
        return list(struct.unpack(f"<{unit_count}H", raw_bytes))

    @classmethod
    def send_unicode_text(
        cls,
        text: str,
        press_enter: bool = False,
        char_delay_ms: float = 0.005
    ) -> Tuple[bool, str]:
        """Sends the given string as pure Unicode keystrokes to the focused control."""
        if not text:
            if press_enter:
                cls.send_virtual_key(VK_RETURN)
            return True, "No text provided (Enter pressed if requested)."

        code_units = cls.text_to_utf16_code_units(text)
        total_units = len(code_units)

        try:
            for idx, code_unit in enumerate(code_units):
                # Prepare Key Down and Key Up
                inputs = (INPUT * 2)()
                
                # 1. Key Down (UNICODE)
                inputs[0].type = INPUT_KEYBOARD
                inputs[0].union.ki = KEYBDINPUT(
                    wVk=0,
                    wScan=code_unit,
                    dwFlags=KEYEVENTF_UNICODE,
                    time=0,
                    dwExtraInfo=0
                )

                # 2. Key Up (UNICODE | KEYUP)
                inputs[1].type = INPUT_KEYBOARD
                inputs[1].union.ki = KEYBDINPUT(
                    wVk=0,
                    wScan=code_unit,
                    dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP,
                    time=0,
                    dwExtraInfo=0
                )

                sent = user32.SendInput(2, inputs, ctypes.sizeof(INPUT))
                if sent != 2:
                    logger.warning(f"SendInput sent {sent}/2 events for code unit 0x{code_unit:04X}")

                if char_delay_ms > 0 and idx < total_units - 1:
                    time.sleep(char_delay_ms)

            if press_enter:
                time.sleep(0.04)
                cls.send_virtual_key(VK_RETURN)

            return True, f"Successfully typed {len(text)} characters ({total_units} UTF-16 code units) via Unicode SendInput."
        except Exception as e:
            return False, f"Unicode SendInput error: {e}"

    @classmethod
    def send_virtual_key(cls, vk_code: int) -> bool:
        """Sends a standard virtual key event (down and up)."""
        try:
            inputs = (INPUT * 2)()
            inputs[0].type = INPUT_KEYBOARD
            inputs[0].union.ki = KEYBDINPUT(wVk=vk_code, wScan=0, dwFlags=0, time=0, dwExtraInfo=0)

            inputs[1].type = INPUT_KEYBOARD
            inputs[1].union.ki = KEYBDINPUT(wVk=vk_code, wScan=0, dwFlags=KEYEVENTF_KEYUP, time=0, dwExtraInfo=0)

            sent = user32.SendInput(2, inputs, ctypes.sizeof(INPUT))
            return sent == 2
        except Exception as e:
            logger.error(f"Error sending virtual key 0x{vk_code:02X}: {e}")
            return False


# ==============================================================================
# 3. ROBUST WINDOW SWITCHER & FOREGROUND UNLOCK ENGINE
# ==============================================================================

class RobustWindowSwitcher:
    """Robust Win32 window switcher that handles Windows UIPI and Foreground Lock.

    Does NOT rely solely on AppActivate or bare SetForegroundWindow.
    Uses AttachThreadInput and Alt-key release to reliably yield focus.
    """

    @staticmethod
    def get_window_title(hwnd: int) -> str:
        """Returns the title text of the window."""
        length = user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buff = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buff, length + 1)
            return buff.value
        return ""

    @staticmethod
    def get_window_pid(hwnd: int) -> int:
        """Returns the process ID owning the window."""
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value

    @classmethod
    def find_windows_by_query(cls, query: str) -> List[Tuple[int, str, int]]:
        """Finds visible windows matching the title or executable process name."""
        target = query.lower().strip()
        matched = []

        def enum_proc(hwnd, extra):
            if user32.IsWindowVisible(hwnd):
                title = cls.get_window_title(hwnd)
                pid = cls.get_window_pid(hwnd)
                # Match title
                if target in title.lower():
                    matched.append((hwnd, title, pid))
                else:
                    # Match process name
                    try:
                        import psutil
                        proc = psutil.Process(pid)
                        pname = proc.name().lower()
                        if target in pname or target in pname.replace(".exe", ""):
                            matched.append((hwnd, title or pname, pid))
                    except Exception:
                        pass
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        user32.EnumWindows(WNDENUMPROC(enum_proc), 0)
        return matched

    @classmethod
    def activate_window(cls, hwnd: int, retries: int = 3) -> Tuple[bool, str]:
        """Robustly brings a window to the foreground using multi-stage Win32 unlocking."""
        if not user32.IsWindow(hwnd):
            return False, f"Invalid HWND 0x{hwnd:X}"

        # If window is minimized, restore it
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE = 9
            time.sleep(0.05)

        for attempt in range(retries):
            try:
                curr_thread_id = kernel32.GetCurrentThreadId()
                fg_hwnd = user32.GetForegroundWindow()
                fg_thread_id = user32.GetWindowThreadProcessId(fg_hwnd, None) if fg_hwnd else 0

                # 1. Attach thread input to foreground thread if distinct
                attached = False
                if fg_thread_id and fg_thread_id != curr_thread_id:
                    attached = bool(user32.AttachThreadInput(curr_thread_id, fg_thread_id, True))

                # 2. Simulate Alt key down/up to unlock foreground lock restrictions
                user32.keybd_event(0x12, 0, 0, 0)
                user32.keybd_event(0x12, 0, KEYEVENTF_KEYUP, 0)

                # 3. Bring window to top and set foreground
                user32.BringWindowToTop(hwnd)
                user32.ShowWindow(hwnd, 5)  # SW_SHOW = 5
                user32.SetForegroundWindow(hwnd)
                user32.SetFocus(hwnd)

                if attached:
                    user32.AttachThreadInput(curr_thread_id, fg_thread_id, False)

                time.sleep(0.06)

                # 4. Verify foreground
                if user32.GetForegroundWindow() == hwnd:
                    title = cls.get_window_title(hwnd)
                    return True, f"Successfully activated window '{title}' (HWND 0x{hwnd:X})"

            except Exception as e:
                logger.debug(f"Window activation attempt {attempt + 1} note: {e}")

            time.sleep(0.08)

        # Final check
        if user32.GetForegroundWindow() == hwnd:
            return True, f"Successfully activated window (HWND 0x{hwnd:X})"
        return False, f"Window (HWND 0x{hwnd:X}) was signaled but foreground state could not be verified (possible UIPI restriction)."


# ==============================================================================
# 4. UI AUTOMATION CORE ENGINE (uiautomation Wrapper)
# ==============================================================================

ACTIONABLE_CONTROL_TYPES = {
    "ButtonControl", "EditControl", "CheckBoxControl", "RadioButtonControl",
    "ComboBoxControl", "MenuItemControl", "TabItemControl", "HyperlinkControl",
    "ListItemControl", "TreeItemControl", "SplitButtonControl", "CustomControl"
}

SUPPORTED_PATTERN_NAMES = [
    "InvokePattern", "ValuePattern", "TogglePattern",
    "SelectionItemPattern", "ExpandCollapsePattern", "ScrollPattern", "TextPattern"
]


@dataclass
class UIElementInfo:
    element_id: int
    name: str
    automation_id: str
    control_type: str
    class_name: str
    bounding_rect: Dict[str, int]
    normalized_center: Dict[str, int]
    is_enabled: bool
    has_focus: bool
    supported_patterns: List[str]
    is_actionable: bool
    hwnd: int
    native_control: Any = None


class UiaEngine:
    """Windows UI Automation Engine using Microsoft UIA COM via `uiautomation`.

    Inspects active windows, searches controls, invokes buttons/patterns, and manipulates text.
    """

    @staticmethod
    def get_active_window_handle() -> Optional[int]:
        """Returns the HWND of the currently focused or active window."""
        hwnd = user32.GetForegroundWindow()
        return hwnd if hwnd and user32.IsWindow(hwnd) else None

    @classmethod
    def get_active_window_control(cls, hwnd: Optional[int] = None):
        """Retrieves the top-level UIA Control for the given HWND or active foreground window."""
        try:
            import uiautomation as auto
            target_hwnd = hwnd or cls.get_active_window_handle()
            if target_hwnd:
                return auto.ControlFromHandle(target_hwnd)
            return auto.GetFocusedControl()
        except Exception as e:
            logger.error(f"UIA get_active_window_control error: {e}")
            return None

    @classmethod
    def inspect_element_properties(
        cls,
        ctrl,
        element_id: int,
        window_rect: Optional[Tuple[int, int, int, int]] = None
    ) -> UIElementInfo:
        """Extracts complete structured properties from a UIA Control."""
        name = getattr(ctrl, "Name", "") or ""
        auto_id = getattr(ctrl, "AutomationId", "") or ""
        ctrl_type = getattr(ctrl, "ControlTypeName", "") or ""
        cls_name = getattr(ctrl, "ClassName", "") or ""
        is_enabled = bool(getattr(ctrl, "IsEnabled", True))
        has_focus = bool(getattr(ctrl, "HasKeyboardFocus", False))
        hwnd = getattr(ctrl, "NativeWindowHandle", 0) or 0

        # Extract Bounding Box
        rect = getattr(ctrl, "BoundingRectangle", None)
        if rect:
            l, t, r, b = rect.left, rect.top, rect.right, rect.bottom
            w, h = rect.width(), rect.height()
            cx, cy = rect.xcenter(), rect.ycenter()
        else:
            l, t, r, b, w, h, cx, cy = 0, 0, 0, 0, 0, 0, 0, 0

        rect_dict = {
            "left": l, "top": t, "right": r, "bottom": b,
            "width": w, "height": h, "center_x": cx, "center_y": cy
        }

        # Calculate normalized coordinates (0-1000)
        norm_x, norm_y = DpiCoordinateManager.physical_to_normalized(cx, cy, window_rect)
        norm_center = {"x": norm_x, "y": norm_y}

        # Identify supported patterns
        patterns = []
        for pat in SUPPORTED_PATTERN_NAMES:
            getter = getattr(ctrl, f"Get{pat}", None)
            if getter:
                try:
                    instance = getter()
                    if instance is not None:
                        patterns.append(pat)
                except Exception:
                    pass

        # Determine actionability
        is_actionable = (
            ctrl_type in ACTIONABLE_CONTROL_TYPES or
            any(p in patterns for p in ["InvokePattern", "ValuePattern", "TogglePattern", "SelectionItemPattern"])
        )

        return UIElementInfo(
            element_id=element_id,
            name=name,
            automation_id=auto_id,
            control_type=ctrl_type,
            class_name=cls_name,
            bounding_rect=rect_dict,
            normalized_center=norm_center,
            is_enabled=is_enabled,
            has_focus=has_focus,
            supported_patterns=patterns,
            is_actionable=is_actionable,
            hwnd=hwnd,
            native_control=ctrl
        )

    @classmethod
    def inspect_window_hierarchy(
        cls,
        hwnd: Optional[int] = None,
        max_depth: int = 3,
        actionable_only: bool = True
    ) -> Dict[str, Any]:
        """Inspects the active or specified window and returns a structured UI hierarchy."""
        t0_uia = time.perf_counter()
        root_ctrl = cls.get_active_window_control(hwnd)
        if not root_ctrl:
            return {"error": "Could not access active window UI automation tree."}

        target_hwnd = hwnd or cls.get_active_window_handle() or 0
        window_title = RobustWindowSwitcher.get_window_title(target_hwnd)
        pid = RobustWindowSwitcher.get_window_pid(target_hwnd)

        # Window physical bounding rect
        win_rect = None
        r = getattr(root_ctrl, "BoundingRectangle", None)
        if r:
            win_rect = (r.left, r.top, r.right, r.bottom)

        elements: List[UIElementInfo] = []
        element_counter = 1

        def traverse(ctrl, current_depth: int):
            nonlocal element_counter
            if current_depth > max_depth or element_counter > 150:
                return

            try:
                children = ctrl.GetChildren()
            except Exception:
                children = []

            for child in children:
                try:
                    info = cls.inspect_element_properties(child, element_counter, win_rect)
                    if not actionable_only or info.is_actionable:
                        elements.append(info)
                        element_counter += 1
                    traverse(child, current_depth + 1)
                except Exception as e:
                    logger.debug(f"Traversal element error: {e}")

        traverse(root_ctrl, 1)

        serialized_elements = []
        for e in elements:
            serialized_elements.append({
                "id": e.element_id,
                "name": e.name,
                "automation_id": e.automation_id,
                "control_type": e.control_type,
                "bounding_rect": e.bounding_rect,
                "normalized_center": e.normalized_center,
                "is_enabled": e.is_enabled,
                "has_focus": e.has_focus,
                "supported_patterns": e.supported_patterns,
                "is_actionable": e.is_actionable
            })

        try:
            from observability import GLOBAL_METRICS
            GLOBAL_METRICS.record_uia_latency((time.perf_counter() - t0_uia) * 1000.0)
        except Exception:
            pass

        return {
            "window_title": window_title,
            "hwnd": hex(target_hwnd),
            "process_id": pid,
            "control_type": getattr(root_ctrl, "ControlTypeName", "WindowControl"),
            "element_count": len(serialized_elements),
            "elements": serialized_elements
        }

    @classmethod
    def cast_to_typed_control(cls, ctrl):
        """Casts a generic auto.Control to its specialized subclass (e.g. EditControl, ButtonControl)."""
        if not ctrl:
            return ctrl
        try:
            import uiautomation as auto
            type_name = getattr(ctrl, "ControlTypeName", "")
            cls_type = getattr(auto, type_name, None)
            if cls_type and hasattr(ctrl, "Element") and ctrl.Element:
                return cls_type(element=ctrl.Element)
        except Exception:
            pass
        return ctrl

    @classmethod
    def find_element(
        cls,
        query: str,
        by: str = "auto",
        control_type: Optional[str] = None,
        hwnd: Optional[int] = None
    ) -> Optional[Any]:
        """Finds a specific control within the target window."""
        import uiautomation as auto
        root_ctrl = cls.get_active_window_control(hwnd)
        if not root_ctrl:
            return None

        clean_query = query.strip()

        # 1. By AutomationId
        if by in ("auto", "id", "automation_id"):
            try:
                res = auto.Control(searchFromControl=root_ctrl, searchDepth=6, AutomationId=clean_query)
                if res and res.Exists(maxSearchSeconds=0.2):
                    return cls.cast_to_typed_control(res)
            except Exception:
                pass

        # 2. By Exact Name
        if by in ("auto", "name"):
            try:
                res = auto.Control(searchFromControl=root_ctrl, searchDepth=6, Name=clean_query)
                if res and res.Exists(maxSearchSeconds=0.2):
                    return cls.cast_to_typed_control(res)
            except Exception:
                pass

        # 3. By Substring / Regex in Name or ControlType
        try:
            children = []
            def collect(c, depth=0):
                if depth > 5:
                    return
                for ch in c.GetChildren():
                    children.append(ch)
                    collect(ch, depth + 1)

            collect(root_ctrl)
            for c in children:
                c_name = getattr(c, "Name", "") or ""
                c_type = getattr(c, "ControlTypeName", "") or ""
                c_id = getattr(c, "AutomationId", "") or ""

                if control_type and control_type.lower() not in c_type.lower():
                    continue

                if clean_query.lower() in c_name.lower() or clean_query.lower() in c_id.lower():
                    return cls.cast_to_typed_control(c)
        except Exception as e:
            logger.debug(f"find_element regex/substring note: {e}")

        return None

    @classmethod
    def invoke_element(cls, ctrl) -> Tuple[bool, str]:
        """Invokes a control using InvokePattern, TogglePattern, SelectionItemPattern, or coordinate center click."""
        if not ctrl:
            return False, "Control not found or invalid."
        ctrl = cls.cast_to_typed_control(ctrl)

        # 1. Try InvokePattern
        try:
            inv = ctrl.GetInvokePattern()
            if inv:
                inv.Invoke()
                return True, f"Invoked control '{ctrl.Name}' ({ctrl.ControlTypeName}) via InvokePattern."
        except Exception as e:
            logger.debug(f"InvokePattern failed: {e}")

        # 2. Try TogglePattern
        try:
            tog = ctrl.GetTogglePattern()
            if tog:
                tog.Toggle()
                return True, f"Toggled control '{ctrl.Name}' ({ctrl.ControlTypeName}) via TogglePattern."
        except Exception as e:
            logger.debug(f"TogglePattern failed: {e}")

        # 3. Try SelectionItemPattern
        try:
            sel = ctrl.GetSelectionItemPattern()
            if sel:
                sel.Select()
                return True, f"Selected control '{ctrl.Name}' ({ctrl.ControlTypeName}) via SelectionItemPattern."
        except Exception as e:
            logger.debug(f"SelectionItemPattern failed: {e}")

        # 4. Fallback to physical bounding rect center click
        try:
            rect = getattr(ctrl, "BoundingRectangle", None)
            if rect and rect.width() > 0 and rect.height() > 0:
                cx, cy = rect.xcenter(), rect.ycenter()
                user32.SetCursorPos(cx, cy)
                time.sleep(0.04)
                user32.mouse_event(0x0002, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
                time.sleep(0.03)
                user32.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP
                return True, f"Clicked center of control '{ctrl.Name}' at physical ({cx}, {cy})."
        except Exception as e:
            return False, f"Failed to click control center: {e}"

        return False, f"Control '{ctrl.Name}' does not support invocation or click fallback."

    @classmethod
    def set_element_text(cls, ctrl, text: str) -> Tuple[bool, str]:
        """Sets text into an edit or input control via ValuePattern or focused Unicode typing."""
        if not ctrl:
            return False, "Control not found or invalid."
        ctrl = cls.cast_to_typed_control(ctrl)

        # 1. Try ValuePattern
        try:
            val_pat = ctrl.GetValuePattern()
            if val_pat:
                val_pat.SetValue(text)
                return True, f"Successfully set text via ValuePattern on '{ctrl.Name}' ({ctrl.ControlTypeName})."
        except Exception as e:
            logger.debug(f"ValuePattern set value failed: {e}")

        # 2. Focus and send Unicode typing
        try:
            if hasattr(ctrl, "SetFocus"):
                ctrl.SetFocus()
                time.sleep(0.05)
            # Select all existing text if possible, then type
            UnicodeInputEngine.send_virtual_key(0x11)  # Ctrl
            UnicodeInputEngine.send_virtual_key(ord("A"))
            time.sleep(0.02)
            success, msg = UnicodeInputEngine.send_unicode_text(text, press_enter=False)
            return success, f"Focused and typed: {msg}"
        except Exception as e:
            return False, f"Failed to set text via focused typing: {e}"

    @classmethod
    def read_element_text(cls, ctrl) -> Tuple[bool, str]:
        """Reads text from an element via ValuePattern, TextPattern, or Name attribute."""
        if not ctrl:
            return False, "Control not found or invalid."
        ctrl = cls.cast_to_typed_control(ctrl)

        # 1. Try ValuePattern
        try:
            val_pat = ctrl.GetValuePattern()
            if val_pat and val_pat.Value is not None:
                return True, str(val_pat.Value)
        except Exception:
            pass

        # 2. Try TextPattern
        try:
            txt_pat = ctrl.GetTextPattern()
            if txt_pat:
                doc_range = txt_pat.DocumentRange
                if doc_range:
                    return True, doc_range.GetText(-1)
        except Exception:
            pass

        # 3. Fallback to Name
        name = getattr(ctrl, "Name", "") or ""
        return True, name


# ==============================================================================
# 5. SET-OF-MARK (SoM) ENGINE (Visual Grounding with Observation Tokens)
# ==============================================================================

@dataclass
class MarkedElement:
    mark_id: int
    name: str
    control_type: str
    bounding_box: Tuple[int, int, int, int]  # (left, top, right, bottom)
    center: Tuple[int, int]                  # (cx, cy)
    normalized_center: Tuple[int, int]       # (norm_x, norm_y)
    source: str                              # "uia" or "ocr"
    native_control: Any = None


@dataclass
class SoMObservation:
    observation_token: str
    timestamp: float
    hwnd: int
    window_rect: Tuple[int, int, int, int]
    window_title: str
    marks: Dict[int, MarkedElement] = field(default_factory=dict)


class SetOfMarkEngine:
    """Assigns numbered badges [1], [2], [3] to actionable elements on screen,

    overlays them for Gemini visual grounding, and binds them to an observation token.
    Stale tokens or window movements are detected and rejected.
    """

    def __init__(self):
        self.active_observation: Optional[SoMObservation] = None
        self._lock = threading.Lock()

    def generate_observation_for_active_window(
        self,
        hwnd: Optional[int] = None,
        max_marks: int = 40
    ) -> SoMObservation:
        """Inspects the target window, extracts actionable elements, and creates a token-bound observation."""
        with self._lock:
            target_hwnd = hwnd or UiaEngine.get_active_window_handle() or 0
            title = RobustWindowSwitcher.get_window_title(target_hwnd)
            token = f"som_{uuid.uuid4().hex[:8]}"

            # Determine window bounding rect
            root_ctrl = UiaEngine.get_active_window_control(target_hwnd)
            rect = getattr(root_ctrl, "BoundingRectangle", None) if root_ctrl else None
            if rect:
                win_rect = (rect.left, rect.top, rect.right, rect.bottom)
            else:
                m = DpiCoordinateManager.get_virtual_screen_metrics()
                win_rect = (m.left, m.top, m.right, m.bottom)

            obs = SoMObservation(
                observation_token=token,
                timestamp=time.time(),
                hwnd=target_hwnd,
                window_rect=win_rect,
                window_title=title
            )

            # 1. Collect actionable UIA elements
            hierarchy = UiaEngine.inspect_window_hierarchy(target_hwnd, max_depth=4, actionable_only=True)
            elements_list = hierarchy.get("elements", [])

            mark_id = 1
            for el in elements_list:
                if mark_id > max_marks:
                    break
                r = el["bounding_rect"]
                if r["width"] <= 5 or r["height"] <= 5:
                    continue  # Ignore invisible / 0-size elements

                bbox = (r["left"], r["top"], r["right"], r["bottom"])
                center = (r["center_x"], r["center_y"])
                norm_c = (el["normalized_center"]["x"], el["normalized_center"]["y"])

                marked = MarkedElement(
                    mark_id=mark_id,
                    name=el["name"],
                    control_type=el["control_type"],
                    bounding_box=bbox,
                    center=center,
                    normalized_center=norm_c,
                    source="uia"
                )
                obs.marks[mark_id] = marked
                mark_id += 1

            # 2. Fallback to OCR if UIA found no actionable elements
            if not obs.marks:
                try:
                    from frame_store import GLOBAL_FRAME_STORE
                    frame = GLOBAL_FRAME_STORE.get_latest_screen_frame()
                    if frame and frame.image:
                        from tools import get_ocr_engine
                        ocr = get_ocr_engine()
                        if ocr:
                            import numpy as np
                            img_np = np.array(frame.image)
                            ocr_results, _ = ocr(img_np)
                            if ocr_results:
                                for idx, (box, text, score) in enumerate(ocr_results[:max_marks], 1):
                                    xs = [pt[0] for pt in box]
                                    ys = [pt[1] for pt in box]
                                    l, t, r, b = int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))
                                    cx, cy = (l + r) // 2, (t + b) // 2
                                    norm_x, norm_y = DpiCoordinateManager.physical_to_normalized(cx, cy, win_rect)
                                    marked = MarkedElement(
                                        mark_id=idx,
                                        name=text.strip(),
                                        control_type="OCRTextRegion",
                                        bounding_box=(l, t, r, b),
                                        center=(cx, cy),
                                        normalized_center=(norm_x, norm_y),
                                        source="ocr"
                                    )
                                    obs.marks[idx] = marked
                except Exception as e:
                    logger.debug(f"SoM OCR fallback note: {e}")

            self.active_observation = obs
            return obs

    def render_marks_overlay(self, image: Image.Image, observation: SoMObservation) -> Image.Image:
        """Renders high-contrast numbered badges on top of an image for visual grounding."""
        annotated = image.copy().convert("RGB")
        draw = ImageDraw.Draw(annotated)

        palette = [
            (255, 59, 48),    # Red
            (0, 122, 255),    # Blue
            (52, 199, 89),    # Green
            (255, 149, 0),    # Orange
            (175, 82, 222),   # Purple
            (255, 45, 85),    # Pink
            (88, 86, 214),    # Indigo
        ]

        # Use default font
        font = ImageFont.load_default()

        for mark_id, m in observation.marks.items():
            color = palette[(mark_id - 1) % len(palette)]
            l, t, r, b = m.bounding_box

            # Draw bounding box
            draw.rectangle([l, t, r, b], outline=color, width=2)

            # Badge text
            badge_text = f" {mark_id} "
            badge_w = max(22, len(str(mark_id)) * 12 + 10)
            badge_h = 18

            # Place badge at top-left of box
            bl = max(0, l)
            bt = max(0, t - badge_h) if t >= badge_h else t

            draw.rounded_rectangle(
                [bl, bt, bl + badge_w, bt + badge_h],
                radius=4,
                fill=color,
                outline=(255, 255, 255),
                width=1
            )
            draw.text((bl + 4, bt + 3), str(mark_id), fill=(255, 255, 255), font=font)

        return annotated

    def validate_and_resolve_mark(
        self,
        mark_id: int,
        observation_token: str
    ) -> Tuple[bool, Optional[MarkedElement], str]:
        """Validates token freshness and window stability before returning the marked element."""
        with self._lock:
            if not self.active_observation:
                return False, None, "No active Set-of-Mark observation exists. Call som_inspect_and_mark first."

            obs = self.active_observation

            # 1. Token validation
            if obs.observation_token != observation_token:
                return False, None, (
                    f"[STALE OBSERVATION] Token '{observation_token}' is invalid or expired. "
                    f"Current token is '{obs.observation_token}'. The UI state has changed. Re-inspect before acting."
                )

            # 2. Window validation
            if obs.hwnd and user32.IsWindow(obs.hwnd):
                # Verify window didn't move significantly
                root_ctrl = UiaEngine.get_active_window_control(obs.hwnd)
                rect = getattr(root_ctrl, "BoundingRectangle", None) if root_ctrl else None
                if rect:
                    curr_rect = (rect.left, rect.top, rect.right, rect.bottom)
                    orig_rect = obs.window_rect
                    # Shift threshold: 60 physical pixels
                    dx = abs(curr_rect[0] - orig_rect[0])
                    dy = abs(curr_rect[1] - orig_rect[1])
                    if dx > 60 or dy > 60:
                        return False, None, (
                            f"[WINDOW MOVED] Target window moved by ({dx}px, {dy}px) since observation. "
                            f"Action aborted to prevent misclick. Re-inspect to confirm positions."
                        )

            # 3. Mark existence
            if mark_id not in obs.marks:
                return False, None, f"Mark ID [{mark_id}] was not found in observation '{observation_token}'."

            return True, obs.marks[mark_id], "Valid mark."


# Global Set-of-Mark instance
GLOBAL_SOM_ENGINE = SetOfMarkEngine()


# ==============================================================================
# 6. AUTOMATION HIERARCHY RESOLVER & EXECUTOR
# ==============================================================================

class AutomationPriorityTier(int, Enum):
    TIER_1_APP_API_OR_CLI = 1
    TIER_2_CHROMIUM_CDP = 2
    TIER_3_WINDOWS_UIA = 3
    TIER_4_RAPID_OCR = 4
    TIER_5_GEMINI_GROUNDING_SOM = 5
    TIER_6_NORMALIZED_COORDINATES = 6


class StructuredAutomationEngine:
    """Central orchestrator implementing the 6-tier automation hierarchy."""

    @staticmethod
    def get_priority_hierarchy() -> List[Dict[str, Any]]:
        """Returns the formal execution hierarchy and routing descriptions."""
        return [
            {"tier": 1, "name": "Application API / CLI", "desc": "Direct application CLI (code, powershell) or REST/RPC"},
            {"tier": 2, "name": "Chromium CDP", "desc": "Chrome DevTools Protocol for browser or Electron debugging"},
            {"tier": 3, "name": "Windows UI Automation (UIA)", "desc": "Structured UIA tree, buttons, value patterns, menus"},
            {"tier": 4, "name": "RapidOCR Text Parsing", "desc": "In-memory OCR detection for canvas, games, or GDI controls"},
            {"tier": 5, "name": "Set-of-Mark Grounding", "desc": "Numbered badge overlays bound to fresh observation tokens"},
            {"tier": 6, "name": "Normalized Coordinates", "desc": "0-1000 DPI-aware physical cursor click fallback"}
        ]

    @classmethod
    def execute_click(
        cls,
        target: Union[str, int],
        by: str = "auto",
        observation_token: Optional[str] = None,
        clicks: int = 1,
        button: str = "left"
    ) -> Tuple[bool, str, AutomationPriorityTier]:
        """Dispatches a click operation adhering strictly to the priority hierarchy:

        - If target is mark_id (int) + token -> Tier 5 (Set-of-Mark)
        - If target is element name/id (str) -> Tier 3 (Windows UIA)
        - Fallback: coordinate click
        """
        # 1. Set-of-Mark Resolution (Tier 5)
        if isinstance(target, int) or (isinstance(target, str) and target.isdigit() and observation_token):
            mid = int(target)
            valid, mark, msg = GLOBAL_SOM_ENGINE.validate_and_resolve_mark(mid, observation_token or "")
            if not valid:
                return False, msg, AutomationPriorityTier.TIER_5_GEMINI_GROUNDING_SOM

            # Execute click on marked element
            if mark.source == "uia":
                el = UiaEngine.find_element(mark.name, by="auto")
                if el:
                    succ, invoke_msg = UiaEngine.invoke_element(el)
                    if succ:
                        return True, f"SoM [{mid}] '{mark.name}': {invoke_msg}", AutomationPriorityTier.TIER_5_GEMINI_GROUNDING_SOM

            # Physical click at center
            px, py = mark.center
            user32.SetCursorPos(px, py)
            time.sleep(0.04)
            down_flag = 0x0002 if button == "left" else (0x0008 if button == "right" else 0x0020)
            up_flag = 0x0004 if button == "left" else (0x0010 if button == "right" else 0x0040)
            for _ in range(clicks):
                user32.mouse_event(down_flag, 0, 0, 0, 0)
                time.sleep(0.03)
                user32.mouse_event(up_flag, 0, 0, 0, 0)
            return True, f"SoM [{mid}] '{mark.name}': Clicked center at ({px}, {py}).", AutomationPriorityTier.TIER_5_GEMINI_GROUNDING_SOM

        # 2. Windows UI Automation (Tier 3)
        if isinstance(target, str):
            el = UiaEngine.find_element(target, by=by)
            if el:
                succ, uia_msg = UiaEngine.invoke_element(el)
                if succ:
                    return True, uia_msg, AutomationPriorityTier.TIER_3_WINDOWS_UIA

        return False, f"Could not resolve click target '{target}' via structured UIA or SoM.", AutomationPriorityTier.TIER_3_WINDOWS_UIA


GLOBAL_AUTOMATION_ENGINE = StructuredAutomationEngine()
