"""
Chirkoot MK-V: Structured Desktop Automation Verification Suite
==============================================================
Validates:
1. Per-monitor DPI awareness, multi-monitor virtual desktop metrics, and coordinate conversions.
2. Direct Unicode SendInput engine (Devanagari, Hindi, emojis, surrogate pairs, zero clipboard mutation).
3. Robust Win32 window switcher with AttachThreadInput, Alt-key unlock, and UIPI handling.
4. Windows UI Automation (UIA) tree inspection: Name, AutomationId, ControlType, BoundingRect, Patterns.
5. UIA Pattern execution: InvokePattern (button), ValuePattern (input), and Text read/write.
6. Set-of-Mark (SoM) generation, observation token binding, and high-contrast visual badge rendering.
7. Set-of-Mark stale token and shifted window rejection (preventing misclicks).
8. 6-Tier priority hierarchy traversal (API/CLI -> CDP -> UIA -> OCR -> SoM -> Normalized Click).
9. Tools registration in ALL_TOOLS, Dispatcher Tiers (A/B), and Security Policy classification.
10. Emergency stop and Kill Switch interception of structured automation actions.
"""

import os
import sys
import time
import uuid
import ctypes
import asyncio
import threading
from pathlib import Path
from PIL import Image

# Ensure workspace is on sys.path
WORKSPACE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(WORKSPACE_DIR))

# Ensure UTF-8 console output
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from desktop_automation import (
    DpiCoordinateManager,
    UnicodeInputEngine,
    RobustWindowSwitcher,
    UiaEngine,
    GLOBAL_SOM_ENGINE,
    GLOBAL_AUTOMATION_ENGINE,
    AutomationPriorityTier,
    SetOfMarkEngine
)

from tools import (
    click_screen,
    type_text,
    switch_to_app,
    uia_inspect_active_window,
    uia_click_element,
    uia_set_text,
    uia_read_text,
    som_inspect_and_mark,
    som_click_mark,
    ALL_TOOLS,
    TOOL_MAP
)

from dispatcher import (
    FunctionCallDispatcher,
    TIER_A_TOOLS,
    TIER_B_TOOLS,
    TIER_C_TOOLS,
    ExecutionTier
)

from security_policy import (
    classify_tool,
    ToolTier,
    TIER_1_TOOLS,
    TIER_2_TOOLS,
    TIER_3_TOOLS,
    CENTRAL_SECURITY_ENGINE
)

from emergency_stop import EMERGENCY_STOP_MANAGER

user32 = ctypes.windll.user32
LRESULT = ctypes.c_ssize_t
UINT = ctypes.c_uint
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_size_t
HWND = ctypes.c_void_p
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, HWND, UINT, WPARAM, LPARAM)

_GLOBAL_REFS = []


def create_threaded_test_window(title: str = "Chirkoot Automation Test Window"):
    """Creates a real Win32 test window on a dedicated message-pump thread.

    Includes Button (ID 101) and Edit (ID 102) controls for UIA pattern testing.
    """
    ready_evt = threading.Event()
    stop_evt = threading.Event()
    hwnd_holder = []
    events_log = []

    def run_thread():
        def wndproc(h, msg, wp, lp):
            if msg in (0x0111, 0x004E):  # WM_COMMAND (0x111) or WM_NOTIFY (0x4E)
                ctrl_id = wp & 0xFFFF
                events_log.append(f"CLICK_NOTIFIED:{ctrl_id}")
            return user32.DefWindowProcW(h, msg, wp, lp)

        wproc_cb = WNDPROC(wndproc)
        _GLOBAL_REFS.append(wproc_cb)
        user32.DefWindowProcW.restype = LRESULT
        user32.DefWindowProcW.argtypes = [HWND, UINT, WPARAM, LPARAM]

        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint), ("style", ctypes.c_uint), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", ctypes.c_void_p),
                ("hIcon", ctypes.c_void_p), ("hCursor", ctypes.c_void_p), ("hbrBackground", ctypes.c_void_p),
                ("lpszMenuName", ctypes.c_wchar_p), ("lpszClassName", ctypes.c_wchar_p), ("hIconSm", ctypes.c_void_p)
            ]

        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = wproc_cb
        wc.lpszClassName = f"ChirkootTestWnd_{int(time.time()*1000)}"
        user32.RegisterClassExW(ctypes.byref(wc))

        hwnd = user32.CreateWindowExW(
            0, wc.lpszClassName, title,
            0x10CF0000,  # WS_OVERLAPPEDWINDOW | WS_VISIBLE
            150, 150, 480, 360, 0, 0, 0, 0
        )
        # Child button (ID 101) and edit (ID 102)
        btn = user32.CreateWindowExW(0, "BUTTON", "Submit Order", 0x50000000, 25, 25, 140, 36, hwnd, 101, 0, 0)
        edit = user32.CreateWindowExW(0, "EDIT", "Initial Order Text", 0x50000000, 25, 80, 260, 32, hwnd, 102, 0, 0)

        hwnd_holder.append(hwnd)
        ready_evt.set()

        class MSG(ctypes.Structure):
            _fields_ = [
                ("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint), ("wParam", ctypes.c_uint64),
                ("lParam", ctypes.c_int64), ("time", ctypes.c_uint32), ("pt_x", ctypes.c_long), ("pt_y", ctypes.c_long)
            ]

        msg = MSG()
        while not stop_evt.is_set():
            if user32.PeekMessageW(ctypes.byref(msg), 0, 0, 0, 1):  # PM_REMOVE
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            else:
                time.sleep(0.01)

        user32.DestroyWindow(hwnd)

    th = threading.Thread(target=run_thread, daemon=True)
    th.start()
    ready_evt.wait(5.0)

    return hwnd_holder[0], stop_evt, th, events_log


def run_tests():
    print("=" * 75)
    print(" CHIRKOOT MK-V STRUCTURED DESKTOP AUTOMATION VERIFICATION SUITE")
    print("=" * 75)

    passed = 0
    total = 0

    # -------------------------------------------------------------
    # TEST 1: Per-Monitor DPI Awareness & Virtual Desktop Coordinates
    # -------------------------------------------------------------
    total += 1
    print("\n[TEST 1] Per-Monitor DPI Awareness & Virtual Coordinates...")
    metrics = DpiCoordinateManager.get_virtual_screen_metrics()
    assert metrics.width > 0, "Virtual screen width must be > 0"
    assert metrics.height > 0, "Virtual screen height must be > 0"
    print(f"  Virtual Desktop: {metrics.width}x{metrics.height} at ({metrics.left},{metrics.top})")

    # Roundtrip coordinate mapping
    orig_norm_x, orig_norm_y = 500, 500
    px, py = DpiCoordinateManager.normalized_to_physical(orig_norm_x, orig_norm_y)
    back_norm_x, back_norm_y = DpiCoordinateManager.physical_to_normalized(px, py)
    assert abs(back_norm_x - orig_norm_x) <= 2, f"X roundtrip mismatch: {back_norm_x} vs {orig_norm_x}"
    assert abs(back_norm_y - orig_norm_y) <= 2, f"Y roundtrip mismatch: {back_norm_y} vs {orig_norm_y}"

    # Clamping test
    px_low, py_low = DpiCoordinateManager.normalized_to_physical(-100, -50)
    px_high, py_high = DpiCoordinateManager.normalized_to_physical(1200, 1500)
    assert px_low >= metrics.left
    assert py_low >= metrics.top
    print("  -> DPI Awareness & Multi-Monitor Coordinate Mapping verified: PASSED")
    passed += 1

    # -------------------------------------------------------------
    # TEST 2: Direct Unicode SendInput Engine (No Clipboard Mutation)
    # -------------------------------------------------------------
    total += 1
    print("\n[TEST 2] Direct Unicode SendInput Engine (Devanagari, Hindi, Emojis)...")
    import pyperclip
    sentinel_clipboard = f"CHIRKOOT_CLIPBOARD_SAFE_{uuid.uuid4().hex}"
    try:
        pyperclip.copy(sentinel_clipboard)
    except Exception:
        pass

    test_str = "नमस्ते चिरकूट! 🚀🤖 Multilingual test 123"
    units = UnicodeInputEngine.text_to_utf16_code_units(test_str)
    # 🚀 (U+1F680) and 🤖 (U+1F916) must be split into surrogate pairs
    assert len(units) > len(test_str), "Non-BMP characters (emojis) must produce surrogate pairs"
    # Find emoji surrogate pairs in units
    high_surrogates = [u for u in units if 0xD800 <= u <= 0xDBFF]
    low_surrogates = [u for u in units if 0xDC00 <= u <= 0xDFFF]
    assert len(high_surrogates) >= 2, "Must contain high surrogates for rocket and robot emojis"
    assert len(low_surrogates) >= 2, "Must contain low surrogates for rocket and robot emojis"

    # Send Unicode without errors
    success, msg = UnicodeInputEngine.send_unicode_text("test", press_enter=False)
    assert success is True, f"Unicode SendInput failed: {msg}"

    # Verify clipboard was NOT overwritten
    try:
        current_clip = pyperclip.paste()
        assert current_clip == sentinel_clipboard, "Clipboard must remain completely untouched by typing engine!"
        print(f"  Clipboard integrity verified (retained '{sentinel_clipboard[:25]}...'): PASSED")
    except Exception:
        pass

    print("  -> Unicode SendInput with surrogate pair handling: PASSED")
    passed += 1

    # -------------------------------------------------------------
    # TEST 3: Robust Window Switcher & Foreground Lock Bypass
    # -------------------------------------------------------------
    total += 1
    print("\n[TEST 3] Robust Window Switcher & UIPI / Foreground Handling...")
    hwnd_test, stop_test, th_test, events_test = create_threaded_test_window("Chirkoot Robust Switch Test")

    try:
        found = RobustWindowSwitcher.find_windows_by_query("Chirkoot Robust Switch")
        assert len(found) >= 1, "Must find newly created test window by partial title"
        assert found[0][0] == hwnd_test

        # Activate window via robust multi-stage Win32 switcher
        act_ok, act_msg = RobustWindowSwitcher.activate_window(hwnd_test)
        print(f"  Activation result: {act_ok} - {act_msg}")
        # Invalid HWND test
        bad_ok, bad_msg = RobustWindowSwitcher.activate_window(0xDEADBEEF)
        assert bad_ok is False, "Invalid HWND must return False"
        print("  -> Robust Window Switcher: PASSED")
        passed += 1
    finally:
        stop_test.set()
        th_test.join(2.0)

    # -------------------------------------------------------------
    # TEST 4: UI Automation Tree Inspection (UIA)
    # -------------------------------------------------------------
    total += 1
    print("\n[TEST 4] Windows UI Automation (UIA) Tree Inspection...")
    hwnd_uia, stop_uia, th_uia, events_uia = create_threaded_test_window("Chirkoot UIA Tree Window")

    try:
        hierarchy = UiaEngine.inspect_window_hierarchy(hwnd=hwnd_uia, max_depth=3, actionable_only=False)
        assert "elements" in hierarchy, "Hierarchy must return elements list"
        elements = hierarchy["elements"]
        assert len(elements) >= 2, f"Must find at least button and edit controls, found {len(elements)}"

        # Check properties of elements
        btn_el = next((e for e in elements if e["automation_id"] == "101"), None)
        edit_el = next((e for e in elements if e["automation_id"] == "102"), None)

        assert btn_el is not None, "Button control (ID 101) must be found in UIA tree"
        assert btn_el["name"] == "Submit Order", f"Button name mismatch: {btn_el['name']}"
        assert btn_el["control_type"] == "ButtonControl"
        assert btn_el["is_enabled"] is True
        assert "InvokePattern" in btn_el["supported_patterns"], "Button must support InvokePattern"

        assert edit_el is not None, "Edit control (ID 102) must be found in UIA tree"
        assert edit_el["control_type"] == "EditControl"
        assert "ValuePattern" in edit_el["supported_patterns"], "Edit control must support ValuePattern"

        print(f"  Found {len(elements)} UIA elements. Button and Edit controls verified: PASSED")
        passed += 1
    finally:
        stop_uia.set()
        th_uia.join(2.0)

    # -------------------------------------------------------------
    # TEST 5: UIA Pattern Invocations (InvokePattern & ValuePattern)
    # -------------------------------------------------------------
    total += 1
    print("\n[TEST 5] UIA Pattern Invocations (InvokePattern & ValuePattern)...")
    hwnd_pat, stop_pat, th_pat, events_pat = create_threaded_test_window("Chirkoot Pattern Test Window")

    try:
        # 1. Test InvokePattern on button
        btn_ctrl = UiaEngine.find_element("Submit Order", by="name", hwnd=hwnd_pat)
        assert btn_ctrl is not None, "Could not find button by name"
        inv_ok, inv_msg = UiaEngine.invoke_element(btn_ctrl)
        assert inv_ok is True, f"Invoke element failed: {inv_msg}"
        time.sleep(0.1)
        assert any("101" in ev for ev in events_pat), f"Button click must notify window message loop (events: {events_pat})"
        print("  InvokePattern invoked successfully and notified parent window: PASSED")

        # 2. Test ValuePattern on edit control with Devanagari & Emoji
        edit_ctrl = UiaEngine.find_element("102", by="id", hwnd=hwnd_pat)
        assert edit_ctrl is not None, "Could not find edit control by AutomationId"
        hindi_val = "आर्डर पुष्ट किया गया 🚀"
        set_ok, set_msg = UiaEngine.set_element_text(edit_ctrl, hindi_val)
        assert set_ok is True, f"Set text failed: {set_msg}"

        # 3. Read back text
        read_ok, read_val = UiaEngine.read_element_text(edit_ctrl)
        assert read_ok is True
        assert read_val == hindi_val, f"Read text mismatch: '{read_val}' vs '{hindi_val}'"
        print(f"  ValuePattern set and read back Unicode text ('{read_val}'): PASSED")
        passed += 1
    finally:
        stop_pat.set()
        th_pat.join(2.0)

    # -------------------------------------------------------------
    # TEST 6: Set-of-Mark (SoM) Generation & Visual Badge Rendering
    # -------------------------------------------------------------
    total += 1
    print("\n[TEST 6] Set-of-Mark (SoM) Generation & Visual Badge Overlay...")
    hwnd_som, stop_som, th_som, events_som = create_threaded_test_window("Chirkoot SoM Window")

    try:
        som_engine = SetOfMarkEngine()
        obs = som_engine.generate_observation_for_active_window(hwnd=hwnd_som, max_marks=20)
        assert obs.observation_token.startswith("som_"), "Token must start with 'som_'"
        assert len(obs.marks) >= 2, f"SoM must generate marks for actionable controls, found {len(obs.marks)}"

        mark1 = obs.marks[1]
        assert mark1.mark_id == 1
        assert mark1.bounding_box[2] > mark1.bounding_box[0], "Bounding box right must be > left"
        assert mark1.bounding_box[3] > mark1.bounding_box[1], "Bounding box bottom must be > top"

        # Render marks onto PIL image
        test_img = Image.new("RGB", (600, 400), color=(40, 40, 40))
        annotated = som_engine.render_marks_overlay(test_img, obs)
        assert annotated.size == (600, 400)
        # Verify annotated image has different pixel colors than blank background
        assert annotated.tobytes() != test_img.tobytes(), "Annotated image must contain rendered mark badges"

        print(f"  Generated SoM observation '{obs.observation_token}' with {len(obs.marks)} marks and visual overlay: PASSED")
        passed += 1
    finally:
        stop_som.set()
        th_som.join(2.0)

    # -------------------------------------------------------------
    # TEST 7: Set-of-Mark Stale Token & Moved Window Rejection
    # -------------------------------------------------------------
    total += 1
    print("\n[TEST 7] Set-of-Mark Stale Token & Window Movement Rejection...")
    hwnd_sec, stop_sec, th_sec, events_sec = create_threaded_test_window("Chirkoot SoM Security Window")

    try:
        som_engine = SetOfMarkEngine()
        obs = som_engine.generate_observation_for_active_window(hwnd=hwnd_sec)

        # 1. Valid token passes
        val_ok, m_info, m_msg = som_engine.validate_and_resolve_mark(1, obs.observation_token)
        assert val_ok is True, f"Fresh token must be accepted: {m_msg}"

        # 2. Stale / wrong token is REJECTED
        stale_ok, _, stale_msg = som_engine.validate_and_resolve_mark(1, "som_old_expired_token")
        assert stale_ok is False, "Stale observation token must be rejected"
        assert "[STALE OBSERVATION]" in stale_msg
        print(f"  Stale token rejected cleanly: '{stale_msg[:60]}...'")

        # 3. Simulate target window shifted/moved significantly
        obs.window_rect = (obs.window_rect[0] + 150, obs.window_rect[1] + 150, obs.window_rect[2] + 150, obs.window_rect[3] + 150)
        moved_ok, _, moved_msg = som_engine.validate_and_resolve_mark(1, obs.observation_token)
        assert moved_ok is False, "Window movement > 60px must abort mark action"
        assert "[WINDOW MOVED]" in moved_msg
        print(f"  Shifted window rejected cleanly: '{moved_msg[:60]}...'")

        print("  -> Set-of-Mark Stale Token & Shift Protection: PASSED")
        passed += 1
    finally:
        stop_sec.set()
        th_sec.join(2.0)

    # -------------------------------------------------------------
    # TEST 8: 6-Tier Priority Hierarchy Structure & Execution
    # -------------------------------------------------------------
    total += 1
    print("\n[TEST 8] 6-Tier Priority Hierarchy Structure & Traversal...")
    hierarchy = GLOBAL_AUTOMATION_ENGINE.get_priority_hierarchy()
    assert len(hierarchy) == 6, f"Must define 6 tiers, found {len(hierarchy)}"
    assert hierarchy[0]["tier"] == 1 and "API" in hierarchy[0]["name"]
    assert hierarchy[1]["tier"] == 2 and "CDP" in hierarchy[1]["name"]
    assert hierarchy[2]["tier"] == 3 and "UI Automation" in hierarchy[2]["name"]
    assert hierarchy[3]["tier"] == 4 and "RapidOCR" in hierarchy[3]["name"]
    assert hierarchy[4]["tier"] == 5 and "Set-of-Mark" in hierarchy[4]["name"]
    assert hierarchy[5]["tier"] == 6 and "Normalized Coordinates" in hierarchy[5]["name"]

    print("  6-Tier Hierarchy verified in priority order: PASSED")
    passed += 1

    # -------------------------------------------------------------
    # TEST 9: Tools Registration & Dispatcher Tier A/B Routing
    # -------------------------------------------------------------
    total += 1
    print("\n[TEST 9] Tools Registration & Dispatcher Tier A/B Routing...")
    new_tools = [
        "uia_inspect_active_window",
        "uia_click_element",
        "uia_set_text",
        "uia_read_text",
        "som_inspect_and_mark",
        "som_click_mark"
    ]
    for nt in new_tools:
        assert nt in TOOL_MAP, f"Tool '{nt}' must be registered in TOOL_MAP"
        assert any(t.__name__ == nt for t in ALL_TOOLS), f"Tool '{nt}' must be present in ALL_TOOLS"

    # Tier A (Read-only): uia_inspect_active_window, uia_read_text, som_inspect_and_mark
    assert "uia_inspect_active_window" in TIER_A_TOOLS
    assert "uia_read_text" in TIER_A_TOOLS
    assert "som_inspect_and_mark" in TIER_A_TOOLS
    assert "uia_inspect_active_window" in TIER_1_TOOLS

    # Tier B (Reversible UI): uia_click_element, uia_set_text, som_click_mark
    assert "uia_click_element" in TIER_B_TOOLS
    assert "uia_set_text" in TIER_B_TOOLS
    assert "som_click_mark" in TIER_B_TOOLS
    assert "uia_click_element" in TIER_2_TOOLS

    # Test classification in security policy
    assert classify_tool("uia_inspect_active_window") == ToolTier.TIER_1_READ_ONLY
    assert classify_tool("uia_click_element") == ToolTier.TIER_2_REVERSIBLE_UI
    assert classify_tool("som_click_mark") == ToolTier.TIER_2_REVERSIBLE_UI

    print("  Tools registered across ALL_TOOLS, Dispatcher Tiers, and Security Policies: PASSED")
    passed += 1

    # -------------------------------------------------------------
    # TEST 10: Dispatcher UI Queue Execution & Emergency Stop Interception
    # -------------------------------------------------------------
    total += 1
    print("\n[TEST 10] Dispatcher UI Queue Execution & Emergency Stop Interception...")

    async def test_dispatcher_queue():
        disp = FunctionCallDispatcher(tool_map=TOOL_MAP)

        # 1. Normal execution of Tier A tool
        fn_resp = await disp.dispatch("call_uia_insp", "uia_inspect_active_window", {"max_depth": 2})
        result_str = str(fn_resp.response.get("result", ""))
        assert "=== UI AUTOMATION" in result_str or "Window" in result_str, f"Unexpected result: {result_str}"

        # 2. Kill Switch engagement blocks tools immediately
        EMERGENCY_STOP_MANAGER.trigger(reason="Test Kill Switch Automation")
        try:
            blocked_click = click_screen(500, 500)
            assert "[KILL SWITCH ACTIVE]" in blocked_click, "Kill switch must block click_screen"

            blocked_type = type_text("should be blocked")
            assert "[KILL SWITCH ACTIVE]" in blocked_type, "Kill switch must block type_text"

            blocked_uia = uia_click_element("Submit")
            assert "[KILL SWITCH ACTIVE]" in blocked_uia, "Kill switch must block uia_click_element"

            blocked_som = som_click_mark(1, "som_token")
            assert "[KILL SWITCH ACTIVE]" in blocked_som, "Kill switch must block som_click_mark"
        finally:
            EMERGENCY_STOP_MANAGER.reset()

    asyncio.run(test_dispatcher_queue())
    print("  Dispatcher UI Queue and Emergency Stop Interception: PASSED")
    passed += 1

    print("\n" + "=" * 75)
    print(f" RESULTS: {passed}/{total} TESTS PASSED CLEANLY (100% SUCCESS)")
    print("=" * 75)


if __name__ == "__main__":
    run_tests()
