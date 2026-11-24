"""
Chirkoot MK-V: Windows HUD Capture-Exclusion Verification Suite
===============================================================
Validates:
1. In-process HWND ownership verification and safe WDA_EXCLUDEFROMCAPTURE application.
2. Foreign HWND boundary protection (Chrome App Mode cross-process safety).
3. Capture-pipeline HUD rectangle masking with multi-monitor awareness.
4. Window handle recreation and DPI change / HUD restart recovery.
5. Configurable exclusion modes (AUTO, WINDOW_AFFINITY, PIPELINE_MASKING, DISABLED).
6. Dedicated inspect_hud_capture_exclusion diagnostic tool.
7. Integration with look_at_screen and OCR capture pipelines.
8. HTTP endpoint /api/hud-exclusion-status diagnostics.
9. Windows 10/11 version compatibility checks (Build >= 19041).
10. User-facing status telemetry and realistic boundary disclaimer.
"""

import os
import sys
import time
import ctypes
from pathlib import Path
from PIL import Image, ImageDraw

# Ensure workspace is in path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hud_capture_exclusion import (
    HudCaptureExclusionManager,
    CaptureExclusionMode,
    HostType,
    GLOBAL_HUD_EXCLUSION_MANAGER,
    WDA_NONE,
    WDA_EXCLUDEFROMCAPTURE,
    ERROR_SUCCESS,
    ERROR_ACCESS_DENIED,
    ERROR_NOT_SUPPORTED
)
from tools import inspect_hud_capture_exclusion, look_at_screen, ALL_TOOLS, TOOL_MAP
from dispatcher import TIER_A_TOOLS
from security_policy import TIER_1_TOOLS


_GLOBAL_WPROCS = []

def create_in_process_dummy_hwnd(title: str = "Chirkoot InProcess Test"):
    """Creates a real Win32 window owned by the current Python process for affinity testing."""
    user32 = ctypes.windll.user32
    # Proper 64-bit types for Win32 WndProc
    LRESULT = ctypes.c_int64
    UINT = ctypes.c_uint32
    WPARAM = ctypes.c_uint64
    LPARAM = ctypes.c_int64
    HWND = ctypes.c_void_p

    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, HWND, UINT, WPARAM, LPARAM)
    def dummy_wndproc(h, msg, wp, lp):
        return user32.DefWindowProcW(h, msg, wp, lp)

    wproc = WNDPROC(dummy_wndproc)
    _GLOBAL_WPROCS.append(wproc)  # Prevent garbage collection of ctypes callback
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
    wc.lpfnWndProc = wproc
    wc.lpszClassName = f"ChirkootExclTest_{int(time.time()*1000)}"
    user32.RegisterClassExW(ctypes.byref(wc))

    hwnd = user32.CreateWindowExW(0, wc.lpszClassName, title, 0, 100, 100, 500, 400, 0, 0, 0, 0)
    return hwnd, wc.lpszClassName


def run_tests():
    print("=" * 70)
    print(" CHIRKOOT MK-V WINDOWS HUD CAPTURE-EXCLUSION VERIFICATION SUITE")
    print("=" * 70)

    total = 0
    passed = 0

    # -------------------------------------------------------------
    # TEST 1: In-Process HWND Ownership & WDA_EXCLUDEFROMCAPTURE
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()

    hwnd1, cls1 = create_in_process_dummy_hwnd("Chirkoot InProcess Test 1")
    assert hwnd1 != 0, "Failed to create in-process dummy test window"

    mgr = HudCaptureExclusionManager(CaptureExclusionMode.WINDOW_AFFINITY)
    mgr.register_hud_window(hwnd1, HostType.PYWEBVIEW, (100, 100, 600, 500))

    diag = mgr.get_diagnostic_status()
    assert diag.hwnd == hwnd1
    assert diag.hwnd_owned_by_current_process is True, "Window must be recognized as owned by current process"
    assert diag.active_mechanism == "WINDOW_AFFINITY"
    assert diag.affinity_applied is True, "Affinity must be applied successfully for in-process window"
    assert diag.affinity_value == "0x00000011"
    assert diag.affinity_last_error == ERROR_SUCCESS

    # Verify query directly from Win32 API
    has_aff, aff_val = mgr.query_window_display_affinity(hwnd1)
    assert has_aff is True
    assert aff_val == WDA_EXCLUDEFROMCAPTURE

    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 1. In-process HWND ownership & WDA_EXCLUDEFROMCAPTURE verified ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 2: Foreign HWND Boundary Protection (Chrome App Mode)
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()

    # Simulate an external HWND owned by another process (e.g. PID 99999)
    foreign_mgr = HudCaptureExclusionManager(CaptureExclusionMode.AUTO)
    # Register with a fake or foreign HWND
    foreign_mgr.register_hud_window(99999999, HostType.CHROME_APP_MODE, (0, 0, 1920, 1080))

    diag_foreign = foreign_mgr.get_diagnostic_status()
    assert diag_foreign.hwnd_owned_by_current_process is False
    assert diag_foreign.affinity_applied is False, "Must NOT call SetWindowDisplayAffinity on foreign HWND"
    assert diag_foreign.active_mechanism == "PIPELINE_MASKING", "Must use PIPELINE_MASKING for foreign host"
    assert "Pipeline Masking" in diag_foreign.user_status_message or "PIPELINE_MASKING" in diag_foreign.user_status_message

    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 2. Foreign HWND boundary protection & safe pipeline fallback verified ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 3: Multi-Monitor Aware Pipeline Masking
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()

    mask_mgr = HudCaptureExclusionManager(CaptureExclusionMode.PIPELINE_MASKING)
    # HUD positioned on Monitor 1: (50, 50, 400, 300)
    mask_mgr.update_hud_geometry(50, 50, 400, 300)

    # Capture Monitor 1 (viewport 960x540)
    test_frame_mon1 = Image.new("RGB", (960, 540), (255, 255, 255))
    masked_mon1 = mask_mgr.mask_hud_in_capture(test_frame_mon1, capture_bounds=(0, 0, 960, 540))

    # Pixel inside HUD on Monitor 1 must be dark carbon (18, 20, 26)
    px_inside = masked_mon1.getpixel((150, 150))
    assert px_inside == (18, 20, 26), f"Expected masked pixel (18, 20, 26), got {px_inside}"

    # Pixel outside HUD on Monitor 1 must be untouched white (255, 255, 255)
    px_outside = masked_mon1.getpixel((500, 400))
    assert px_outside == (255, 255, 255), f"Expected unmasked pixel (255, 255, 255), got {px_outside}"

    # Now simulate capturing Monitor 2: bounds=(1920, 0, 3840, 1080)
    # HUD is at (50, 50, 400, 300) on Monitor 1, so it does NOT overlap Monitor 2
    test_frame_mon2 = Image.new("RGB", (1920, 1080), (200, 200, 200))
    masked_mon2 = mask_mgr.mask_hud_in_capture(test_frame_mon2, capture_bounds=(1920, 0, 3840, 1080))
    # No masking should happen on Monitor 2!
    px_mon2 = masked_mon2.getpixel((150, 150))
    assert px_mon2 == (200, 200, 200), "Monitor 2 capture must remain untouched when HUD is on Monitor 1"

    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 3. Multi-monitor intersection math & pipeline masking verified ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 4: Window Handle Recreation & Recovery
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()

    # Create new HWND representing recreated window (e.g. style change / DPI restart)
    hwnd2, cls2 = create_in_process_dummy_hwnd("Chirkoot InProcess Test 2")
    mgr.on_window_recreated(hwnd2)

    diag_recreated = mgr.get_diagnostic_status()
    assert diag_recreated.hwnd == hwnd2
    assert diag_recreated.affinity_applied is True
    assert diag_recreated.affinity_value == "0x00000011"

    has_aff2, aff_val2 = mgr.query_window_display_affinity(hwnd2)
    assert has_aff2 is True
    assert aff_val2 == WDA_EXCLUDEFROMCAPTURE

    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 4. Window recreation & dynamic affinity reapplication verified ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 5: Configurable Exclusion Modes
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()

    # DISABLED mode
    mgr.set_exclusion_mode(CaptureExclusionMode.DISABLED)
    diag_disabled = mgr.get_diagnostic_status()
    assert diag_disabled.mode == CaptureExclusionMode.DISABLED
    assert diag_disabled.active_mechanism == "NONE"

    # PIPELINE_MASKING mode
    mgr.set_exclusion_mode(CaptureExclusionMode.PIPELINE_MASKING)
    diag_mask = mgr.get_diagnostic_status()
    assert diag_mask.mode == CaptureExclusionMode.PIPELINE_MASKING
    assert diag_mask.active_mechanism == "PIPELINE_MASKING"

    # Restore AUTO mode
    mgr.set_exclusion_mode(CaptureExclusionMode.AUTO)
    assert mgr.mode == CaptureExclusionMode.AUTO

    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 5. Configurable exclusion modes (AUTO, AFFINITY, MASKING, DISABLED) verified ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 6: Diagnostic Tool 'inspect_hud_capture_exclusion'
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()

    assert "inspect_hud_capture_exclusion" in TOOL_MAP
    assert inspect_hud_capture_exclusion in ALL_TOOLS
    assert "inspect_hud_capture_exclusion" in TIER_A_TOOLS
    assert "inspect_hud_capture_exclusion" in TIER_1_TOOLS

    diag_tool_res = inspect_hud_capture_exclusion()
    assert "=== CHIRKOOT MK-V HUD CAPTURE-EXCLUSION DIAGNOSTICS ===" in diag_tool_res
    assert "Configured Mode:" in diag_tool_res
    assert "Active Mechanism:" in diag_tool_res
    assert "Desktop Host Type:" in diag_tool_res
    assert "Windows Build:" in diag_tool_res
    assert "Disclaimer:" in diag_tool_res

    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 6. Dedicated 'inspect_hud_capture_exclusion' tool verified ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 7: Integration with look_at_screen & In-Memory Capture
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()

    # Set HUD rectangle in global manager
    GLOBAL_HUD_EXCLUSION_MANAGER.update_hud_geometry(10, 10, 250, 150)
    res_look = look_at_screen(query=None, include_ocr=False)
    assert "=== CHIRKOOT MK-V HIGH-RESOLUTION SCREEN INSPECTION ===" in res_look
    assert "Active Foreground Window:" in res_look

    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 7. Integration with look_at_screen and in-memory frame capture verified ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 8: Windows 10/11 Version Compatibility
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()

    build_num = mgr.os_build
    assert build_num > 0, "Must detect valid Windows build number"
    if build_num >= 19041:
        assert mgr.os_supported is True, f"Build {build_num} must be marked supported"
    else:
        assert mgr.os_supported is False

    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 8. Windows OS build compatibility: Build {build_num} (Supported={mgr.os_supported}) ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 9: HTTP Endpoint /api/hud-exclusion-status
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()

    from server import api_hud_exclusion_status
    import asyncio

    data = asyncio.run(api_hud_exclusion_status())
    assert data["status"] == "ok"
    assert "diagnostics" in data
    assert "active_mechanism" in data["diagnostics"]
    assert "mode" in data["diagnostics"]
    assert "disclaimer" in data["diagnostics"]

    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 9. HTTP REST endpoint /api/hud-exclusion-status validated ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 10: Realistic Disclaimer & Clear User Status Message
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()

    status_obj = GLOBAL_HUD_EXCLUSION_MANAGER.get_diagnostic_status()
    assert "DWM/WGC" in status_obj.disclaimer
    assert "universal invisibility" in status_obj.disclaimer.lower()
    assert len(status_obj.user_status_message) > 10

    # Cleanup test windows
    user32 = ctypes.windll.user32
    user32.DestroyWindow(hwnd1)
    user32.DestroyWindow(hwnd2)

    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 10. User status telemetry & boundary disclaimer verified ({elapsed:.2f}ms)")
    passed += 1

    print("=" * 70)
    print(f" CAPTURE-EXCLUSION TEST SUMMARY: {passed}/{total} PASSED | 0 FAILED")
    print("=" * 70)


if __name__ == "__main__":
    run_tests()
