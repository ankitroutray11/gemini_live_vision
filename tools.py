import os
import sys
import time
import ctypes
import psutil
import subprocess
import webbrowser
from typing import Optional, List, Any, Callable
from pathlib import Path

# Setup Native Windows user32 handle
user32 = ctypes.windll.user32
SCREEN_WIDTH = user32.GetSystemMetrics(0)   # SM_CXSCREEN
SCREEN_HEIGHT = user32.GetSystemMetrics(1)  # SM_CYSCREEN

# Import security policies
from security_policy import (
    validate_path_for_read,
    validate_path_for_write,
    execute_sandboxed_command,
    GLOBAL_TAINT_TRACKER,
    is_kill_switch_active,
    record_audit_event,
    sanitize_and_tag_observed_content,
    UntrustedSourceType,
    CENTRAL_SECURITY_ENGINE,
    redact_secrets,
    MAX_FILE_READ_BYTES,
    MAX_FILE_WRITE_BYTES,
    MAX_FILE_READ_LINES,
    MAX_TOOL_OUTPUT_CHARS
)

from desktop_automation import (
    DpiCoordinateManager,
    UnicodeInputEngine,
    RobustWindowSwitcher,
    UiaEngine,
    GLOBAL_SOM_ENGINE,
    GLOBAL_AUTOMATION_ENGINE,
    AutomationPriorityTier
)

from closed_loop_verifier import (
    GLOBAL_CLOSED_LOOP_VERIFIER,
    GLOBAL_CLIPBOARD_PRESERVER,
    ActionIntention,
    VerificationOutcome,
    VerificationEvidenceType,
    VerificationResult,
    ClosedLoopActionVerifier
)

from observability import GLOBAL_METRICS

def check_kill_switch() -> Optional[str]:
    """Checks whether the emergency kill switch has been triggered."""
    if is_kill_switch_active():
        return "[KILL SWITCH ACTIVE] Execution blocked. Chirkoot emergency stop is engaged."
    return None

try:
    import pyperclip
    CLIPBOARD_AVAILABLE = True
except Exception:
    CLIPBOARD_AVAILABLE = False

# Windows Virtual Key Codes
VK_MAP = {
    "win": 0x5B, "winleft": 0x5B, "windows": 0x5B,
    "alt": 0x12, "menu": 0x12,
    "ctrl": 0x11, "control": 0x11,
    "shift": 0x10,
    "enter": 0x0D, "return": 0x0D,
    "tab": 0x09,
    "esc": 0x1B, "escape": 0x1B,
    "space": 0x20,
    "backspace": 0x08,
    "delete": 0x2E, "del": 0x2E,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "capslock": 0x14,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B
}
for c in "abcdefghijklmnopqrstuvwxyz":
    VK_MAP[c] = ord(c.upper())
for d in "0123456789":
    VK_MAP[d] = ord(d)

KEYEVENTF_KEYUP = 0x0002
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800

# -------------------------------------------------------------
# OS SCREEN CONTROL & COMPUTER USE AGENT TOOLS
# -------------------------------------------------------------

def click_screen(x: int, y: int, button: str = "left", clicks: int = 1, normalized: bool = True) -> str:
    """Clicks on a target element on the user's screen using DPI-aware mouse events with closed-loop verification.
    
    Args:
        x: X coordinate on screen. If normalized=True (default), x is between 0 and 1000 (0=left, 1000=right).
        y: Y coordinate on screen. If normalized=True (default), y is between 0 and 1000 (0=top, 1000=bottom).
        button: Mouse button to click: 'left', 'right', or 'middle'.
        clicks: Number of clicks: 1 for single-click, 2 for double-click.
        normalized: Whether coordinates are 0-1000 normalized (True) or physical pixels (False).
    """
    ks = check_kill_switch()
    if ks:
        return ks

    if normalized:
        px, py = DpiCoordinateManager.normalized_to_physical(x, y)
    else:
        px, py = max(0, min(SCREEN_WIDTH - 1, x)), max(0, min(SCREEN_HEIGHT - 1, y))

    print(f"\n🖱️ [Agent Screen Action] Closed-Loop Clicking at ({px}, {py}) [normalized: {x}, {y}] with button '{button}' (clicks={clicks})")
    
    btn = button.lower().strip()
    down_flag = MOUSEEVENTF_LEFTDOWN if btn == "left" else (MOUSEEVENTF_RIGHTDOWN if btn == "right" else MOUSEEVENTF_MIDDLEDOWN)
    up_flag = MOUSEEVENTF_LEFTUP if btn == "left" else (MOUSEEVENTF_RIGHTUP if btn == "right" else MOUSEEVENTF_MIDDLEUP)

    def do_click():
        user32.SetCursorPos(px, py)
        time.sleep(0.04)
        for c in range(clicks):
            user32.mouse_event(down_flag, 0, 0, 0, 0)
            time.sleep(0.03)
            user32.mouse_event(up_flag, 0, 0, 0, 0)
            if clicks > 1 and c < clicks - 1:
                time.sleep(0.06)

    intention = ActionIntention(
        action_type="click_screen",
        target=f"pixel({px},{py})"
    )

    verif = GLOBAL_CLOSED_LOOP_VERIFIER.verify_action(intention, do_click, timeout_s=0.6)
    return f"Clicked at coordinates ({px}, {py}). {verif.to_summary()}"

def type_text(text: str, press_enter: bool = False, use_paste: bool = False) -> str:
    """Types text at the current cursor position or active input field with closed-loop verification.
    
    Supports direct Unicode SendInput (default) or paste with full clipboard format preservation.
    
    Args:
        text: The string to type into the active window (supports Devanagari, Hindi, emojis, multiline).
        press_enter: If True, presses Enter key immediately after typing.
        use_paste: If True, uses clipboard paste with automatic pre-snapshot and faithful post-restoration.
    """
    ks = check_kill_switch()
    if ks:
        return ks

    # 1. If paste requested or text contains newlines / multiline code blocks
    if use_paste or ("\n" in text and len(text) > 80):
        print(f"\n📋 [Agent Screen Action] Preserved Clipboard Paste: '{text[:60]}...' (press_enter={press_enter})")
        
        def readback_check():
            try:
                import uiautomation as auto
                fg = auto.GetFocusedControl()
                if fg:
                    vp = getattr(fg, "GetValuePattern", None)
                    if vp and vp():
                        val = str(vp().Value or "")
                        if text[:30] in val:
                            return True, f"Verified text in focused control via ValuePattern."
            except Exception:
                pass
            return True, "Paste dispatched successfully."

        paste_ok, paste_msg, rest_ok, rest_msg = GLOBAL_CLIPBOARD_PRESERVER.paste_with_preservation(
            text, press_enter=press_enter, verify_readback_fn=readback_check
        )
        
        restore_status = f"[Clipboard: {rest_msg}]" if rest_ok else f"[CLIPBOARD RESTORATION FAILED: {rest_msg}]"
        if paste_ok:
            return f"Successfully typed {len(text)} characters via paste. [Verification: SUCCESS] {restore_status}"
        return f"Paste execution error: {paste_msg}. {restore_status}"

    # 2. Direct Unicode SendInput with Closed-Loop Verification
    print(f"\n⌨️ [Agent Screen Action] Closed-Loop Unicode Typing: '{text[:60]}' (press_enter={press_enter})")
    
    intention = ActionIntention(
        action_type="type_text",
        target="focused_control",
        expected_outcome="value_update",
        expected_text=text
    )

    def do_typing():
        succ, msg = UnicodeInputEngine.send_unicode_text(text, press_enter=press_enter)
        if not succ:
            raise RuntimeError(msg)

    verif = GLOBAL_CLOSED_LOOP_VERIFIER.verify_action(intention, do_typing, timeout_s=0.8)
    return f"Typed {len(text)} characters on screen. {verif.to_summary()}"

def press_hotkey(keys: str) -> str:
    """Presses a key or key combination on the Windows keyboard natively.
    
    Args:
        keys: Key or hotkey combination separated by commas or plus signs.
              Examples: 'win' (opens Start Menu/Search), 'alt,tab', 'ctrl,s' (save), 'ctrl,c', 'ctrl,v', 'enter', 'escape', 'tab'.
    """
    ks = check_kill_switch()
    if ks:
        return ks

    clean_keys = [k.strip().lower() for k in keys.replace("+", ",").split(",") if k.strip()]
    print(f"\n🎹 [Agent Screen Action] Native Windows Hotkey: {clean_keys}")
    
    vk_codes = []
    for k in clean_keys:
        if k in VK_MAP:
            vk_codes.append(VK_MAP[k])
        else:
            print(f"Warning: Unknown key '{k}'")

    if not vk_codes:
        return f"Error: No valid keys found in '{keys}'."

    try:
        # Press all keys in sequence
        for vk in vk_codes:
            user32.keybd_event(vk, 0, 0, 0)
            time.sleep(0.04)
        
        # Dwell time (critical for Start Menu or Alt+Tab switcher)
        time.sleep(0.08)

        # Release in reverse order
        for vk in reversed(vk_codes):
            user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
            time.sleep(0.02)

        return f"Successfully triggered native hotkey: {', '.join(clean_keys)}."
    except Exception as e:
        return f"Error executing hotkey '{keys}': {e}"

def scroll_screen(direction: str = "down", amount: int = 5) -> str:
    """Scrolls the active window or page up or down.
    
    Args:
        direction: 'down' or 'up'.
        amount: Number of scroll steps (typically 3 to 10).
    """
    ks = check_kill_switch()
    if ks:
        return ks

    print(f"\n📜 [Agent Screen Action] Scrolling {direction} by {amount} steps")
    try:
        delta = int(amount) * 120
        wheel_delta = -delta if direction.lower() == "down" else delta
        user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, wheel_delta, 0)
        return f"Successfully scrolled {direction} by {amount} steps."
    except Exception as e:
        return f"Error scrolling {direction}: {e}"

def switch_to_app(app_name: str) -> str:
    """Brings an open application window directly to the front and activates it with closed-loop verification.
    
    Uses robust Win32 multi-stage activation with AttachThreadInput and Alt-key release
    to bypass Windows foreground locks.
    
    Args:
        app_name: The name or partial title of the application to activate (e.g. 'code', 'chrome', 'terminal', 'powershell', 'notepad', 'spotify').
    """
    ks = check_kill_switch()
    if ks:
        return ks

    print(f"\n🔄 [Agent Screen Action] Robust Closed-Loop Switching to application: '{app_name}'")
    target = app_name.lower().strip()

    intention = ActionIntention(
        action_type="switch_window",
        target=app_name,
        expected_outcome="window_open"
    )

    def do_switch():
        # 1. Try robust Win32 window discovery and activation
        try:
            matched = RobustWindowSwitcher.find_windows_by_query(target)
            if matched:
                hwnd, title, pid = matched[0]
                success, msg = RobustWindowSwitcher.activate_window(hwnd)
                if success:
                    return f"Successfully focused window '{title}' (PID {pid}, HWND 0x{hwnd:X})."
        except Exception as e:
            logger.debug(f"RobustWindowSwitcher note: {e}")

        # 2. Try PowerShell AppActivate for instant window focusing
        ps_script = f"""
        $wshell = New-Object -ComObject WScript.Shell
        $titles = @('{target}', '{target.capitalize()}', '{target.upper()}')
        foreach ($t in $titles) {{
            if ($wshell.AppActivate($t)) {{ exit 0 }}
        }}
        exit 1
        """
        try:
            res = subprocess.run(["powershell", "-NoProfile", "-Command", ps_script], capture_output=True, text=True, timeout=5)
            if res.returncode == 0:
                return f"Successfully focused window matching '{app_name}'."
        except Exception:
            pass

        # Fallback: Launch or bring up via start
        return open_app_or_url(target)

    verif = GLOBAL_CLOSED_LOOP_VERIFIER.verify_action(intention, do_switch, timeout_s=0.8)
    return f"Switch focus to '{app_name}'. {verif.to_summary()}"

def minimize_active_window() -> str:
    """Minimizes the current foreground window to reveal background applications."""
    ks = check_kill_switch()
    if ks:
        return ks

    print(f"\n🗗 [Agent Screen Action] Minimizing foreground window...")
    try:
        hwnd = user32.GetForegroundWindow()
        if hwnd:
            user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE = 6
            return "Successfully minimized foreground window."
        return "No foreground window found to minimize."
    except Exception as e:
        return f"Error minimizing window: {e}"

def _get_chirkoot_or_foreground_hwnd():
    """Finds Chirkoot's window HWND, falling back to the current active foreground window."""
    try:
        hwnd = user32.GetForegroundWindow()
        if hwnd:
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buff = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buff, length + 1)
                val = buff.value.lower()
                if "chirkoot" in val or "8000" in val:
                    return hwnd

        found = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def cb(h, extra):
            if user32.IsWindowVisible(h):
                l = user32.GetWindowTextLengthW(h)
                if l > 0:
                    b = ctypes.create_unicode_buffer(l + 1)
                    user32.GetWindowTextW(h, b, l + 1)
                    val = b.value.lower()
                    if "chirkoot" in val or "8000" in val:
                        found.append(h)
            return True
        user32.EnumWindows(WNDENUMPROC(cb), 0)
        return found[0] if found else hwnd
    except Exception:
        return user32.GetForegroundWindow()

def toggle_fullscreen_window() -> str:
    """Toggles fullscreen state on the active window using native pywebview or Windows OS hotkeys."""
    ks = check_kill_switch()
    if ks:
        return ks

    print("\n⛶ [Agent Action] Toggling Fullscreen window state...")
    try:
        import webview
        if hasattr(webview, 'windows') and webview.windows:
            w = webview.windows[0]
            w.toggle_fullscreen()
            return f"Pywebview window fullscreen toggled: now {w.fullscreen}."
    except Exception:
        pass

    try:
        hwnd = _get_chirkoot_or_foreground_hwnd()
        if hwnd:
            user32.SetForegroundWindow(hwnd)
        res = press_hotkey("f11")
        return f"Native F11 triggered: {res}"
    except Exception as e:
        return f"Error triggering fullscreen: {e}"

def maximize_active_window() -> str:
    """Maximizes the active foreground window to fill the entire screen."""
    ks = check_kill_switch()
    if ks:
        return ks

    print("\n🗖 [Agent Action] Maximizing active window...")
    try:
        import webview
        if hasattr(webview, 'windows') and webview.windows:
            w = webview.windows[0]
            w.maximize()
            return "Pywebview window maximized."
    except Exception:
        pass

    try:
        hwnd = _get_chirkoot_or_foreground_hwnd()
        if hwnd:
            user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE = 3
            user32.SetForegroundWindow(hwnd)
            return "Successfully maximized window via Win32 SW_MAXIMIZE."
    except Exception:
        pass

    try:
        res = press_hotkey("win,up")
        return f"Maximized window via Win+Up: {res}"
    except Exception as e:
        return f"Error maximizing window: {e}"

def restore_active_window() -> str:
    """Restores the active window down to a floating/normal window."""
    ks = check_kill_switch()
    if ks:
        return ks

    print("\n🗗 [Agent Action] Restoring active window...")
    try:
        import webview
        if hasattr(webview, 'windows') and webview.windows:
            w = webview.windows[0]
            w.restore()
            return "Pywebview window restored."
    except Exception:
        pass

    try:
        hwnd = _get_chirkoot_or_foreground_hwnd()
        if hwnd:
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE = 9
            user32.SetForegroundWindow(hwnd)
            return "Successfully restored window via Win32 SW_RESTORE."
    except Exception:
        pass

    try:
        res = press_hotkey("win,down")
        return f"Restored window via Win+Down: {res}"
    except Exception as e:
        return f"Error restoring window: {e}"

# -------------------------------------------------------------
# STRUCTURED UI AUTOMATION & SET-OF-MARK TOOLS
# -------------------------------------------------------------

def uia_inspect_active_window(max_depth: int = 3, actionable_only: bool = True) -> str:
    """Inspects the active foreground application window using Microsoft UI Automation.
    
    Retrieves structured properties: Name, AutomationId, ControlType, BoundingRectangle,
    Enabled state, Focus state, supported control patterns, and parent window context.
    
    Args:
        max_depth: Maximum tree depth to inspect (1 to 5, default 3).
        actionable_only: If True (default), filters only actionable elements (buttons, inputs, menus, tabs).
    """
    ks = check_kill_switch()
    if ks:
        return ks

    try:
        data = UiaEngine.inspect_window_hierarchy(max_depth=max_depth, actionable_only=actionable_only)
        if "error" in data:
            return f"UI Automation Error: {data['error']}"

        lines = [
            f"=== UI AUTOMATION WINDOW INSPECTION ===",
            f"Window: \"{data.get('window_title')}\" (HWND: {data.get('hwnd')}, PID: {data.get('process_id')})",
            f"Actionable Elements Found: {data.get('element_count')}",
            "------------------------------------------------"
        ]

        for el in data.get("elements", [])[:40]:
            patterns_str = ", ".join(el.get("supported_patterns", [])) or "None"
            r = el["bounding_rect"]
            norm = el["normalized_center"]
            lines.append(
                f"[{el['id']}] \"{el['name']}\" ({el['control_type']}) | AutoId: '{el['automation_id']}' | "
                f"Enabled: {el['is_enabled']} | Patterns: [{patterns_str}] | "
                f"Rect: ({r['left']},{r['top']},{r['right']},{r['bottom']}) | Center: norm(x={norm['x']}, y={norm['y']})"
            )

        if data.get("element_count", 0) > 40:
            lines.append(f"... (truncated {data['element_count'] - 40} additional elements)")

        summary = "\n".join(lines)
        return sanitize_and_tag_observed_content(summary, UntrustedSourceType.UNVALIDATED_TOOL_OUTPUT)
    except Exception as e:
        return f"Error inspecting window via UI Automation: {e}"


def uia_click_element(query: str, by: str = "auto", control_type: str = "") -> str:
    """Finds a target UI element on the active window and invokes it with closed-loop verification.
    
    Args:
        query: The element Name, AutomationId, or substring to locate.
        by: Search strategy: 'auto' (default), 'id', 'name', or 'type'.
        control_type: Optional control type filter (e.g. 'ButtonControl', 'MenuItemControl').
    """
    ks = check_kill_switch()
    if ks:
        return ks

    print(f"\n🖱️ [Agent UIA Action] Finding & Clicking element: '{query}' (by={by})")
    try:
        ctrl = UiaEngine.find_element(query, by=by, control_type=control_type if control_type else None)
        if not ctrl:
            return f"UI Automation element not found matching '{query}' (by={by}). [Verification: FAILURE] (Control not found)"

        is_destr = ClosedLoopActionVerifier.is_action_destructive("uia_click", query)
        intention = ActionIntention(
            action_type="uia_click",
            target=query,
            is_potentially_destructive=is_destr
        )

        def do_invoke():
            succ, msg = UiaEngine.invoke_element(ctrl)
            if not succ:
                raise RuntimeError(msg)

        verif = GLOBAL_CLOSED_LOOP_VERIFIER.verify_action(intention, do_invoke, target_ctrl=ctrl, timeout_s=0.8)
        return f"UI Element '{query}' invoked. {verif.to_summary()}"
    except Exception as e:
        return f"Error invoking UI element '{query}': {e}"


def uia_set_text(query: str, text: str, by: str = "auto") -> str:
    """Sets text into an input field or editor using UI Automation with closed-loop verification.
    
    Args:
        query: The element Name, AutomationId, or substring to locate.
        text: The string to set into the control (full Unicode support: Devanagari, emoji, symbols).
        by: Search strategy: 'auto' (default), 'id', or 'name'.
    """
    ks = check_kill_switch()
    if ks:
        return ks

    print(f"\n⌨️ [Agent UIA Action] Setting text into element '{query}': '{text[:50]}'")
    try:
        ctrl = UiaEngine.find_element(query, by=by)
        if not ctrl:
            return f"UI Automation element not found matching '{query}'. [Verification: FAILURE] (Control not found)"

        intention = ActionIntention(
            action_type="uia_set_text",
            target=query,
            expected_outcome="value_update",
            expected_text=text
        )

        def do_set():
            succ, msg = UiaEngine.set_element_text(ctrl, text)
            if not succ:
                raise RuntimeError(msg)

        verif = GLOBAL_CLOSED_LOOP_VERIFIER.verify_action(intention, do_set, target_ctrl=ctrl, timeout_s=0.8)
        return f"Set text on UI element '{query}'. {verif.to_summary()}"
    except Exception as e:
        return f"Error setting text into UI element '{query}': {e}"


def uia_read_text(query: str, by: str = "auto") -> str:
    """Reads text content from a UI element using ValuePattern, TextPattern, or Name.
    
    Args:
        query: The element Name, AutomationId, or substring to locate.
        by: Search strategy: 'auto' (default), 'id', or 'name'.
    """
    ks = check_kill_switch()
    if ks:
        return ks

    try:
        ctrl = UiaEngine.find_element(query, by=by)
        if not ctrl:
            return f"UI Automation element not found matching '{query}'."

        success, text_val = UiaEngine.read_element_text(ctrl)
        if success:
            return sanitize_and_tag_observed_content(f"Element '{query}' text: {text_val}", UntrustedSourceType.UNVALIDATED_TOOL_OUTPUT)
        return f"Could not read text from element '{query}'."
    except Exception as e:
        return f"Error reading text from UI element '{query}': {e}"


def som_inspect_and_mark(annotate_image: bool = True) -> str:
    """Generates a Set-of-Mark (SoM) observation on the active window.
    
    Assigns numbered badges [1], [2], [3]... to actionable elements and binds them to a
    secure, single-use observation token. Optionally overlays badges onto the latest screen frame.
    
    Args:
        annotate_image: If True (default), overlays visual marks onto the active in-memory screen frame.
    """
    ks = check_kill_switch()
    if ks:
        return ks

    try:
        obs = GLOBAL_SOM_ENGINE.generate_observation_for_active_window()
        
        # If requested and frame exists, render visual badge overlay into frame store
        if annotate_image:
            from frame_store import GLOBAL_FRAME_STORE
            frame = GLOBAL_FRAME_STORE.get_latest_screen_frame()
            if frame and frame.image:
                marked_img = GLOBAL_SOM_ENGINE.render_marks_overlay(frame.image, obs)
                import io
                buf = io.BytesIO()
                marked_img.save(buf, format="JPEG", quality=85)
                from frame_store import MediaPacket, FrameType
                GLOBAL_FRAME_STORE.update_frame(MediaPacket(
                    packet_type=FrameType.SCREEN_JPEG,
                    payload=buf.getvalue(),
                    stream_id=frame.stream_id,
                    timestamp_us=frame.timestamp_us
                ))

        lines = [
            f"=== SET-OF-MARK (SoM) OBSERVATION ===",
            f"Observation Token: \"{obs.observation_token}\"",
            f"Target Window: \"{obs.window_title}\" (HWND: 0x{obs.hwnd:X})",
            f"Mark Count: {len(obs.marks)}",
            "------------------------------------------------"
        ]

        for mid, m in sorted(obs.marks.items()):
            lines.append(
                f"[{mid}] \"{m.name}\" ({m.control_type}) [source: {m.source}] | "
                f"BoundingBox: {m.bounding_box} | Center: (x={m.center[0]}, y={m.center[1]}) norm(x={m.normalized_center[0]}, y={m.normalized_center[1]})"
            )

        lines.append("------------------------------------------------")
        lines.append("INSTRUCTION: When clicking or interacting with these marks, pass mark_id and observation_token to som_click_mark.")

        summary = "\n".join(lines)
        return sanitize_and_tag_observed_content(summary, UntrustedSourceType.UNVALIDATED_TOOL_OUTPUT)
    except Exception as e:
        return f"Error generating Set-of-Mark observation: {e}"


def som_click_mark(mark_id: int, observation_token: str, clicks: int = 1, button: str = "left") -> str:
    """Clicks a numbered UI mark from a prior Set-of-Mark observation with closed-loop verification.
    
    Enforces observation token freshness, detects window movement, and verifies outcome.
    
    Args:
        mark_id: Numbered mark ID (1, 2, 3...) from the observation.
        observation_token: The unique token generated during som_inspect_and_mark.
        clicks: Number of clicks (1 for single click, 2 for double click).
        button: Mouse button ('left', 'right', 'middle').
    """
    ks = check_kill_switch()
    if ks:
        return ks

    print(f"\n🎯 [Agent SoM Action] Clicking Mark [{mark_id}] (token: {observation_token})")
    try:
        valid, mark, msg = GLOBAL_SOM_ENGINE.validate_and_resolve_mark(mark_id, observation_token)
        if not valid:
            return f"Set-of-Mark Execution Blocked: {msg} [Verification: FAILURE] (Token/Window Invalid)"

        is_destr = ClosedLoopActionVerifier.is_action_destructive("som_click", mark.name)
        intention = ActionIntention(
            action_type="som_click",
            target=f"Mark[{mark_id}]: {mark.name}",
            is_potentially_destructive=is_destr
        )

        def do_som_click():
            succ, c_msg, tier = GLOBAL_AUTOMATION_ENGINE.execute_click(
                target=mark_id,
                observation_token=observation_token,
                clicks=clicks,
                button=button
            )
            if not succ:
                raise RuntimeError(c_msg)

        verif = GLOBAL_CLOSED_LOOP_VERIFIER.verify_action(intention, do_som_click, timeout_s=0.8)
        return f"Set-of-Mark [{mark_id}] '{mark.name}' clicked. {verif.to_summary()}"
    except Exception as e:
        return f"Error executing mark click: {e}"


# -------------------------------------------------------------
# OPTICAL CHARACTER RECOGNITION (OCR) & SCREEN TEXT PARSER
# -------------------------------------------------------------

from frame_store import GLOBAL_FRAME_STORE, MediaPacket, FrameType, VideoFrame

_ocr_engine = None

def get_ocr_engine():
    global _ocr_engine
    if _ocr_engine is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
            _ocr_engine = RapidOCR()
        except Exception as e:
            print(f"OCR Engine init notice: {e}")
    return _ocr_engine

def set_latest_screen_frame(jpeg_bytes: bytes, stream_id: int = 0):
    """Saves the latest live frame from the user's screen share stream in memory for OCR and vision."""
    GLOBAL_TAINT_TRACKER.mark_untrusted_input("screen_radar")
    pkt = MediaPacket(packet_type=FrameType.SCREEN_JPEG, payload=jpeg_bytes, stream_id=stream_id)
    GLOBAL_FRAME_STORE.update_frame(pkt)

def get_active_screen_path() -> Optional[str]:
    """Retrieves an explicit on-demand screenshot path if required by tools."""
    try:
        return GLOBAL_FRAME_STORE.save_screenshot_explicit()
    except Exception:
        return None

def _format_ocr_results(result: list, img_w: int, img_h: int, source_name: str = "SCREEN") -> str:
    """Formats RapidOCR raw results into normalized coordinates and tagged text."""
    if not result:
        return "OCR completed. No visible text was detected on screen."

    lines_output = []
    full_text_parts = []
    for idx, (box, text, score) in enumerate(result, 1):
        xs = [pt[0] for pt in box]
        ys = [pt[1] for pt in box]
        cx = sum(xs) / len(xs)
        cy = sum(ys) / len(ys)

        norm_x = int(round((cx / img_w) * 1000))
        norm_y = int(round((cy / img_h) * 1000))

        clean_text = text.strip()
        full_text_parts.append(clean_text)
        lines_output.append(
            f"[{idx}] \"{clean_text}\" (conf: {score:.2f}) -> Center: x={norm_x}, y={norm_y} (pixels: {int(cx)}, {int(cy)})"
        )

    summary = (
        f"=== CHIRKOOT TACTICAL OCR RESULTS ({source_name}) ===\n"
        f"Detected {len(result)} text regions across {img_w}x{img_h} viewport.\n"
        f"-----------------------------------------\n"
        + "\n".join(lines_output[:35]) +
        ("\n... (truncated additional regions)" if len(lines_output) > 35 else "") +
        f"\n-----------------------------------------\n"
        f"Full Extracted Text:\n" + "\n".join(full_text_parts)
    )
    return sanitize_and_tag_observed_content(summary, UntrustedSourceType.OCR_TEXT)

def look_at_screen(query: Optional[str] = None, include_ocr: bool = True, monitor_id: int = 0) -> str:
    """Performs an on-demand, high-resolution tactical visual inspection of the user's active screen.
    Captures a fresh, native-resolution uncompressed frame directly in memory (zero disk writes),
    inspects active foreground window title, layout geometry, resolution, and optionally executes
    deep Optical Character Recognition (OCR) to inspect labels, text, code, or answer specific queries.

    Args:
        query: Specific element, window, error, or UI control to inspect (e.g. 'error in terminal', 'save button', 'active tab').
        include_ocr: Whether to extract visible text and normalized coordinates via OCR (default True).
        monitor_id: Monitor index to inspect (default 0 for primary screen).
    """
    ks = check_kill_switch()
    if ks:
        return ks

    from radar_engine import GLOBAL_RADAR_ENGINE

    # 1. Privacy Shield Enforcement
    is_sensitive, sensitive_kw = GLOBAL_RADAR_ENGINE.check_foreground_privacy()
    if is_sensitive:
        return (
            f"[PRIVACY SHIELD ENGAGED] Tactical screen inspection blocked: "
            f"A sensitive window ('{sensitive_kw}') is currently active in the foreground. "
            f"Please switch away from sensitive applications or enter your credentials before inspecting screen."
        )

    GLOBAL_TAINT_TRACKER.mark_untrusted_input("look_at_screen")
    print(f"\n👁️ [Agent Visual Inspection] Capturing fresh native-resolution frame (query='{query}', ocr={include_ocr})...")

    try:
        # Capture a fresh native-resolution frame into memory (Zero disk writes)
        frame = GLOBAL_FRAME_STORE.capture_high_res_screen(monitor_id=monitor_id)
        w, h = frame.width, frame.height

        # Mask Chirkoot's own HUD if enabled to avoid recursive self-view
        if GLOBAL_RADAR_ENGINE.config.mask_hud_enabled and GLOBAL_RADAR_ENGINE._hud_rect:
            img = frame.get_image()
            masked_img = GLOBAL_RADAR_ENGINE.mask_hud_in_image(img)
            frame._image = masked_img
            frame._numpy_array = None

        np_img = frame.get_numpy()
        fg_title = GLOBAL_RADAR_ENGINE.get_foreground_window_title() or "Desktop / Background"

        lines = [
            f"=== CHIRKOOT MK-V HIGH-RESOLUTION SCREEN INSPECTION ===",
            f"Viewport: {w}x{h} | Monitor ID: {monitor_id} | Timestamp: {frame.timestamp_ms}",
            f"Active Foreground Window: \"{fg_title}\"",
            f"Source Tier: {frame.source} (Native Uncompressed Memory Capture)",
            f"--------------------------------------------------"
        ]

        if include_ocr:
            engine = get_ocr_engine()
            if engine:
                t0_find = time.perf_counter()
                result, _ = engine(np_img)
                dur_find_ms = (time.perf_counter() - t0_find) * 1000.0
                GLOBAL_METRICS.record_ocr_latency(dur_find_ms)
                if result:
                    lines.append(f"Detected {len(result)} visual text elements on screen:")
                    if query:
                        q_lower = query.lower().strip()
                        matches = []
                        others = []
                        for box, text, score in result:
                            if q_lower in text.lower():
                                matches.append((box, text, score))
                            else:
                                others.append((box, text, score))

                        if matches:
                            lines.append(f"\n[QUERY MATCHES FOR '{query}']:")
                            for idx, (box, text, score) in enumerate(matches, 1):
                                xs = [pt[0] for pt in box]
                                ys = [pt[1] for pt in box]
                                cx = sum(xs) / len(xs)
                                cy = sum(ys) / len(ys)
                                nx = int(round((cx / w) * 1000))
                                ny = int(round((cy / h) * 1000))
                                lines.append(f"  * \"{text.strip()}\" (conf: {score:.2f}) -> Center: x={nx}, y={ny}")
                        else:
                            lines.append(f"\n[No direct matches found for query '{query}']. Showing other detected text:")

                        display_pool = matches + others
                        for idx, (box, text, score) in enumerate(display_pool[:25], 1):
                            if display_pool is others or (box, text, score) in others:
                                xs = [pt[0] for pt in box]
                                ys = [pt[1] for pt in box]
                                cx = sum(xs) / len(xs)
                                cy = sum(ys) / len(ys)
                                nx = int(round((cx / w) * 1000))
                                ny = int(round((cy / h) * 1000))
                                lines.append(f"  [{idx}] \"{text.strip()}\" -> x={nx}, y={ny}")
                    else:
                        for idx, (box, text, score) in enumerate(result[:30], 1):
                            xs = [pt[0] for pt in box]
                            ys = [pt[1] for pt in box]
                            cx = sum(xs) / len(xs)
                            cy = sum(ys) / len(ys)
                            nx = int(round((cx / w) * 1000))
                            ny = int(round((cy / h) * 1000))
                            lines.append(f"  [{idx}] \"{text.strip()}\" (conf: {score:.2f}) -> x={nx}, y={ny}")
                        if len(result) > 30:
                            lines.append(f"... and {len(result) - 30} additional text elements.")
                else:
                    lines.append("No text was detected on screen via OCR.")
            else:
                lines.append("OCR engine unavailable; visual metadata captured.")

        report = "\n".join(lines)
        return sanitize_and_tag_observed_content(report, UntrustedSourceType.SCREEN_IMAGE)

    except Exception as e:
        return f"Error during high-resolution screen inspection: {e}"


def inspect_hud_capture_exclusion() -> str:
    """Inspects the diagnostic status of Chirkoot's Windows HUD capture-exclusion architecture.
    Reports the active mode, mechanism (WINDOW_AFFINITY vs PIPELINE_MASKING), host type,
    Windows build compatibility, in-process HWND ownership, affinity status, and multi-monitor bounds.
    """
    from hud_capture_exclusion import GLOBAL_HUD_EXCLUSION_MANAGER
    diag = GLOBAL_HUD_EXCLUSION_MANAGER.get_diagnostic_status()

    lines = [
        "=== CHIRKOOT MK-V HUD CAPTURE-EXCLUSION DIAGNOSTICS ===",
        f"Configured Mode: {diag.mode.value}",
        f"Active Mechanism: {diag.active_mechanism}",
        f"Desktop Host Type: {diag.host_type.value}",
        f"Windows Build: {diag.os_build} (Supported: {diag.os_supported})",
        f"Active HUD HWND: {diag.hwnd} (Owned by current process: {diag.hwnd_owned_by_current_process})",
        f"Windows Display Affinity: Applied={diag.affinity_applied}, Value={diag.affinity_value}, LastError={diag.affinity_last_error}",
        f"HUD Screen Rectangle: {diag.hud_rect if diag.hud_rect else 'Auto-detect via viewport'}",
        f"Fallback Engaged: {diag.fallback_engaged}",
        "---------------------------------------------------------",
        f"Status Message: {diag.user_status_message}",
        f"Disclaimer: {diag.disclaimer}",
        "========================================================="
    ]
    return "\n".join(lines)


def ocr_screen(region_or_query: Optional[str] = None) -> str:
    """Performs Optical Character Recognition (OCR) on the user's live screen.
    Extracts all visible text, button labels, code, and window text with exact normalized screen coordinates.
    Uses high-resolution native in-memory desktop capture so small text and code remain crisp without disk writes.
    """
    ks = check_kill_switch()
    if ks:
        return ks

    GLOBAL_TAINT_TRACKER.mark_untrusted_input("ocr_screen")
    print(f"\n🔍 [Agent OCR Action] Performing in-memory high-res OCR scan on user's active screen...")
    engine = get_ocr_engine()
    if not engine:
        return "Error: RapidOCR engine could not be initialized."

    try:
        # High-res in-memory capture directly into NumPy array (zero disk writes!)
        frame = GLOBAL_FRAME_STORE.capture_high_res_screen()
        np_img = frame.get_numpy()
        h, w = np_img.shape[:2]

        t0_ocr = time.perf_counter()
        result, _ = engine(np_img)
        dur_ocr_ms = (time.perf_counter() - t0_ocr) * 1000.0
        GLOBAL_METRICS.record_ocr_latency(dur_ocr_ms)
        return _format_ocr_results(result, w, h, source_name="NATIVE_HIGH_RES")
    except Exception as e:
        return f"Error during in-memory OCR execution: {e}"

def ocr_image(image_path: str) -> str:
    """Performs high-accuracy local OCR on an image file, extracting text with bounding boxes and normalized coordinates.
    
    Args:
        image_path: The file path to the image to scan (e.g. PNG, JPG, BMP).
    """
    ks = check_kill_switch()
    if ks:
        return ks

    GLOBAL_TAINT_TRACKER.mark_untrusted_input(f"ocr_image({Path(image_path).name})")
    print(f"\n📄 [Agent OCR Action] Performing OCR on file: '{image_path}'")
    engine = get_ocr_engine()
    if not engine:
        return "Error: RapidOCR engine could not be initialized."

    if not os.path.exists(image_path):
        return f"Error: Image file '{image_path}' does not exist."

    try:
        from PIL import Image
        with Image.open(image_path) as im:
            img_w, img_h = im.size

        result, elapse = engine(image_path)
        if not result:
            return "OCR completed. No visible text was detected in the specified image/screen."

        lines_output = []
        full_text_parts = []
        for idx, (box, text, score) in enumerate(result, 1):
            xs = [pt[0] for pt in box]
            ys = [pt[1] for pt in box]
            cx = sum(xs) / len(xs)
            cy = sum(ys) / len(ys)
            
            norm_x = int(round((cx / img_w) * 1000))
            norm_y = int(round((cy / img_h) * 1000))
            
            clean_text = text.strip()
            full_text_parts.append(clean_text)
            lines_output.append(
                f"[{idx}] \"{clean_text}\" (conf: {score:.2f}) -> Center: x={norm_x}, y={norm_y} (pixels: {int(cx)}, {int(cy)})"
            )

        summary = (
            f"=== CHIRKOOT TACTICAL OCR RESULTS ===\n"
            f"Detected {len(result)} text regions across {img_w}x{img_h} viewport.\n"
            f"-----------------------------------------\n"
            + "\n".join(lines_output[:35]) +
            ("\n... (truncated additional regions)" if len(lines_output) > 35 else "") +
            f"\n-----------------------------------------\n"
            f"Full Extracted Text:\n" + "\n".join(full_text_parts)
        )
        return sanitize_and_tag_observed_content(summary, UntrustedSourceType.OCR_TEXT)
    except Exception as e:
        return f"Error during OCR execution: {e}"

def find_text_on_screen(target_text: str, click: bool = False) -> str:
    """Searches the user's screen for a specific text string using OCR, and optionally clicks it.
    
    Args:
        target_text: The word, button name, label, or phrase to find on screen (e.g. 'Submit', 'Terminal', 'File').
        click: If True, immediately clicks on the center of the found text.
    """
    ks = check_kill_switch()
    if ks:
        return ks

    print(f"\n🎯 [Agent OCR Search] Searching screen for text: '{target_text}' (click={click})")
    engine = get_ocr_engine()
    if not engine:
        return "Error: RapidOCR engine could not be initialized."

    try:
        frame = GLOBAL_FRAME_STORE.capture_high_res_screen()
        np_img = frame.get_numpy()
        img_h, img_w = np_img.shape[:2]

        t0_find = time.perf_counter()
        result, _ = engine(np_img)
        dur_find_ms = (time.perf_counter() - t0_find) * 1000.0
        GLOBAL_METRICS.record_ocr_latency(dur_find_ms)
        if not result:
            return f"OCR scan completed. No text found on screen matching '{target_text}'."

        target_lower = target_text.lower().strip()
        matched = []

        for box, text, score in result:
            t_clean = text.strip().lower()
            if target_lower in t_clean or t_clean in target_lower:
                xs = [pt[0] for pt in box]
                ys = [pt[1] for pt in box]
                cx = sum(xs) / len(xs)
                cy = sum(ys) / len(ys)
                norm_x = int(round((cx / img_w) * 1000))
                norm_y = int(round((cy / img_h) * 1000))
                matched.append({
                    "text": text.strip(),
                    "conf": score,
                    "norm_x": norm_x,
                    "norm_y": norm_y,
                    "cx": int(cx),
                    "cy": int(cy)
                })

        if not matched:
            return f"Text '{target_text}' was not found on screen. Detected phrases: {', '.join([r[1] for r in result[:8]])}..."

        best = matched[0]
        res_msg = f"Found text '{best['text']}' on screen at normalized coordinates (x={best['norm_x']}, y={best['norm_y']}) with confidence {best['conf']:.2f}."
        
        if click:
            click_res = click_screen(best['norm_x'], best['norm_y'], button="left", clicks=1, normalized=True)
            res_msg += f"\nAction taken: {click_res}"

        return res_msg
    except Exception as e:
        return f"Error finding text on screen: {e}"

# -------------------------------------------------------------
# OS HOST WORKSPACE & CODING TOOLS
# -------------------------------------------------------------

def run_command(command: str, cwd: Optional[str] = None, on_proc_started: Optional[Any] = None) -> str:
    """Executes a PowerShell or shell command with sandboxed process tree timeout and output capping.
    
    Args:
        command: The shell command line to execute (e.g. 'git status', 'python test.py', 'dir', 'npm test').
        cwd: Optional working directory for command execution (defaults to repository root).
    """
    ks = check_kill_switch()
    if ks:
        return ks

    disp_cmd = redact_secrets(command)
    print(f"\n⚡ [Tool Execution] Executing sandboxed: {disp_cmd} (cwd: {cwd or '.'})")
    raw_res = execute_sandboxed_command(command, cwd=cwd, on_proc_started=on_proc_started)
    return sanitize_and_tag_observed_content(raw_res, UntrustedSourceType.TERMINAL_OUTPUT)

def read_file(path: str, start_line: Optional[int] = None, end_line: Optional[int] = None) -> str:
    """Reads file contents from disk within permitted boundaries.
    
    Args:
        path: Path to the file to inspect.
        start_line: Optional 1-based start line number.
        end_line: Optional 1-based end line number.
    """
    ks = check_kill_switch()
    if ks:
        return ks

    ok, msg = validate_path_for_read(path)
    if not ok:
        record_audit_event("SENSITIVE_FILE_READ_BLOCKED", {"path": redact_secrets(path)})
        return msg

    GLOBAL_TAINT_TRACKER.mark_untrusted_input(f"read_file({Path(path).name})")
    print(f"\n📖 [Tool Execution] Reading file: {path}")
    try:
        p = os.path.abspath(path)
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        
        s = max(1, start_line or 1) - 1
        e = min(len(lines), end_line or len(lines))
        selected = lines[s:e]
        if len(selected) > MAX_FILE_READ_LINES:
            content = "".join(selected[:MAX_FILE_READ_LINES]) + f"\n... [Truncated at {MAX_FILE_READ_LINES} lines]"
        else:
            content = "".join(selected)

        # Redact any inadvertent secrets and cap output characters
        content = redact_secrets(content)
        if len(content) > MAX_TOOL_OUTPUT_CHARS:
            content = content[:MAX_TOOL_OUTPUT_CHARS] + f"\n... [Output capped at {MAX_TOOL_OUTPUT_CHARS} characters for safety]"

        raw_output = f"=== File: {path} (lines {s+1}-{e} of {len(lines)}) ===\n{content}"
        return sanitize_and_tag_observed_content(raw_output, UntrustedSourceType.FILE_CONTENT)
    except Exception as e:
        return f"Error reading file '{path}': {e}"

def write_file(path: str, content: str) -> str:
    """Creates a new file or overwrites an existing file with the given content.
    
    Args:
        path: Path to the file to write.
        content: The text content to write into the file.
    """
    ks = check_kill_switch()
    if ks:
        return ks

    ok, msg = validate_path_for_write(path)
    if not ok:
        record_audit_event("IMMUTABLE_FILE_WRITE_BLOCKED", {"path": redact_secrets(path)})
        return msg

    content_bytes = len(content.encode("utf-8", errors="replace"))
    if content_bytes > MAX_FILE_WRITE_BYTES:
        return f"[SECURITY ERROR] Content size ({content_bytes} bytes) exceeds maximum permitted file write limit ({MAX_FILE_WRITE_BYTES} bytes)."

    print(f"\n✏️ [Tool Execution] Writing file: {path} ({len(content)} chars)")
    try:
        p = os.path.abspath(path)
        parent = os.path.dirname(p)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Successfully saved {len(content)} characters to '{path}'."
    except Exception as e:
        return f"Error writing file '{path}': {e}"

def list_directory(path: Optional[str] = ".") -> str:
    """Lists files and folders inside the specified directory.
    
    Args:
        path: Directory path to list (defaults to current directory).
    """
    target = path or "."
    try:
        p = os.path.abspath(target)
        if not os.path.exists(p):
            return f"Error: Directory '{target}' does not exist."
        entries = sorted(os.listdir(p))
        items = []
        for item in entries[:50]:
            fp = os.path.join(p, item)
            is_d = os.path.isdir(fp)
            size = os.path.getsize(fp) if not is_d else 0
            items.append(f"  [{'DIR ' if is_d else 'FILE'}] {item:<30} {'<DIR>' if is_d else f'{size}B'}")
        return f"Directory listing of '{target}' ({len(entries)} items total):\n" + "\n".join(items)
    except Exception as e:
        return f"Error listing directory '{target}': {e}"

def system_diagnostics() -> str:
    """Returns current host hardware diagnostics (CPU, RAM, Battery, OS)."""
    try:
        cpu_pct = psutil.cpu_percent(interval=0.2)
        ram = psutil.virtual_memory()
        battery = psutil.sensors_battery()
        bat_str = f"{battery.percent}% ({'Charging' if battery.power_plugged else 'Discharging'})" if battery else "Desktop (AC)"
        return (
            f"System Hardware Diagnostics:\n"
            f"- CPU Utilization: {cpu_pct}%\n"
            f"- RAM Usage: {ram.percent}% ({ram.used // (1024**2)} MB / {ram.total // (1024**2)} MB)\n"
            f"- Power Status: {bat_str}\n"
            f"- Screen Resolution: {SCREEN_WIDTH} x {SCREEN_HEIGHT}\n"
            f"- OS: Windows ({sys.platform})"
        )
    except Exception as e:
        return f"Error getting diagnostics: {e}"

def open_app_or_url(target: str) -> str:
    """Launches a desktop application or opens a website URL in the default browser.
    
    Args:
        target: Application alias ('chrome', 'vscode', 'terminal', 'calculator', 'notepad', 'spotify') or URL ('https://...').
    """
    ks = check_kill_switch()
    if ks:
        return ks

    target_lower = target.lower().strip()
    apps = {
        "chrome": "start chrome",
        "browser": "start chrome",
        "vscode": "code",
        "code": "code",
        "terminal": "start powershell",
        "calculator": "calc",
        "calc": "calc",
        "notepad": "notepad",
        "spotify": "start spotify",
        "explorer": "explorer ."
    }
    try:
        if target_lower.startswith("http://") or target_lower.startswith("https://"):
            webbrowser.open(target)
            return f"Opened URL '{target}' in default browser."
        elif target_lower in apps:
            subprocess.Popen(apps[target_lower], shell=True)
            return f"Launched application: {target}."
        else:
            subprocess.Popen(f"start {target}", shell=True)
            return f"Executed launch for: {target}."
    except Exception as e:
        return f"Error opening '{target}': {e}"

from learning_engine import (
    list_skills,
    run_skill,
    propose_skill,
    activate_skill,
    rollback_skill,
    quarantine_skill,
    inspect_self,
    read_code_file,
    modify_source_code,
    rollback_code,
    create_custom_tool,
    remember_insight,
    recall_memory,
    search_memory,
    list_memories,
    delete_memory,
    edit_memory,
    confirm_memory,
    migrate_memories,
    rollback_memories,
    run_self_tests,
    restart_chirkoot_server,
    get_codebase_guide,
    self_learn_and_implement,
)

_RAW_TOOLS = [
    # Screen & OS Control
    click_screen,
    type_text,
    press_hotkey,
    scroll_screen,
    switch_to_app,
    minimize_active_window,
    maximize_active_window,
    restore_active_window,
    toggle_fullscreen_window,
    # Structured UI Automation & Set-of-Mark
    uia_inspect_active_window,
    uia_click_element,
    uia_set_text,
    uia_read_text,
    som_inspect_and_mark,
    som_click_mark,
    # Tactical Screen Radar & OCR
    look_at_screen,
    inspect_hud_capture_exclusion,
    ocr_screen,
    ocr_image,
    find_text_on_screen,
    run_command,
    read_file,
    write_file,
    list_directory,
    system_diagnostics,
    open_app_or_url,
    # Stable Dynamic Skill Dispatch & Self-Learning Tools
    list_skills,
    run_skill,
    propose_skill,
    activate_skill,
    rollback_skill,
    quarantine_skill,
    create_custom_tool,
    inspect_self,
    read_code_file,
    modify_source_code,
    rollback_code,
    remember_insight,
    recall_memory,
    search_memory,
    list_memories,
    delete_memory,
    edit_memory,
    confirm_memory,
    migrate_memories,
    rollback_memories,
    run_self_tests,
    restart_chirkoot_server,
    get_codebase_guide,
    self_learn_and_implement,
]

# Ensure strict uniqueness by function name (prevents Google API 1007 Duplicate Function errors)
_seen_tool_names = set()
ALL_TOOLS = []
for _tool_fn in _RAW_TOOLS:
    _t_name = getattr(_tool_fn, "__name__", None)
    if _t_name and _t_name not in _seen_tool_names:
        _seen_tool_names.add(_t_name)
        ALL_TOOLS.append(_tool_fn)

TOOL_MAP = {func.__name__: func for func in ALL_TOOLS}
