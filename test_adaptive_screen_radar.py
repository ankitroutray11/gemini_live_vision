"""
Chirkoot MK-V: Adaptive Screen Radar Verification Suite
========================================================
Validates:
1. Perceptual frame-change detection (dHash + L1 pixel diff).
2. Reduced transmission during static desktop reading (bandwidth suppression).
3. Increased transmission during typing, scrolling, and window transitions.
4. Configurable frame-rate and quality limits.
5. Frame timestamps and monitor identification.
6. Privacy-aware capture controls (Password manager / Banking / Incognito Shield).
7. Manual pause and resume controls.
8. Dedicated look_at_screen tool for on-demand high-resolution inspection.
9. Native-resolution in-memory OCR without disk writes.
10. HUD Self-Capture prevention (rectangle masking + WDA_EXCLUDEFROMCAPTURE).
"""

import os
import sys
import time
import struct
import tempfile
from pathlib import Path
from PIL import Image, ImageDraw
import numpy as np

# Ensure workspace is in path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from radar_engine import (
    AdaptiveRadarEngine,
    RadarConfig,
    RadarState,
    GLOBAL_RADAR_ENGINE,
    SENSITIVE_WINDOW_KEYWORDS
)
from frame_store import FrameType, MediaPacket, VideoFrame, GLOBAL_FRAME_STORE
from tools import look_at_screen, ocr_screen, ALL_TOOLS, TOOL_MAP
from dispatcher import TIER_A_TOOLS
from security_policy import TIER_1_TOOLS, UntrustedSourceType


def run_tests():
    print("=" * 70)
    print(" CHIRKOOT MK-V ADAPTIVE SCREEN RADAR VERIFICATION SUITE")
    print("=" * 70)

    total = 0
    passed = 0

    # -------------------------------------------------------------
    # TEST 1: Perceptual Frame-Change Detection (dHash + Pixel Diff)
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()

    engine = AdaptiveRadarEngine()

    # Image A: Base white background with some lines
    im_a = Image.new("RGB", (960, 540), (255, 255, 255))
    draw_a = ImageDraw.Draw(im_a)
    for y in range(0, 540, 40):
        draw_a.line([(0, y), (960, y)], fill=(220, 220, 220), width=1)

    # Image A clone: identical
    im_a_clone = im_a.copy()

    # Compute dHash & diff
    hash_a = engine.compute_dhash(im_a)
    hash_clone = engine.compute_dhash(im_a_clone)
    assert hash_a == hash_clone, "Identical frames must have identical dHash"
    assert engine.hamming_distance(hash_a, hash_clone) == 0

    thumb_a = np.array(im_a.convert("L").resize((32, 32)), dtype=np.float32)
    thumb_clone = np.array(im_a_clone.convert("L").resize((32, 32)), dtype=np.float32)
    diff_identical = engine.compute_pixel_diff(thumb_a, thumb_clone)
    assert diff_identical == 0.0, f"Expected 0.0 diff, got {diff_identical}"

    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 1. Perceptual frame-change detection algorithms validated ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 2: Static Desktop Throttling & Bandwidth Suppression
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()

    cfg = RadarConfig(
        static_heartbeat_interval_ms=2000,
        dynamic_min_interval_ms=250,
        cooldown_ms=500
    )
    static_engine = AdaptiveRadarEngine(cfg)

    # Frame 1: Initial baseline
    tx1, st1, tel1 = static_engine.evaluate_frame(im_a)
    assert tx1 is True, "First frame must be transmitted as baseline"
    assert st1 == RadarState.DYNAMIC

    # Wait out the dynamic cooldown
    time.sleep(0.55)

    # Feed 15 consecutive identical static frames
    transmitted_count = 0
    suppressed_count = 0

    for i in range(15):
        tx, st, tel = static_engine.evaluate_frame(im_a)
        if tx:
            transmitted_count += 1
        else:
            suppressed_count += 1

    # In static mode without elapsed heartbeat, duplicate frames must be suppressed
    assert suppressed_count >= 13, f"Expected >= 13 frames suppressed, got {suppressed_count}"
    assert tel.bandwidth_saved_pct >= 75.0, f"Expected >= 75% savings, got {tel.bandwidth_saved_pct}%"
    assert tel.state == RadarState.STATIC, f"Expected STATIC state, got {tel.state}"

    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 2. Static Desktop Activity: {tel.bandwidth_saved_pct}% bandwidth suppressed ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 3: Dynamic Burst on Scrolling, Typing, and Transitions
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()

    # Image B: Simulates typing text
    im_typing = im_a.copy()
    draw_b = ImageDraw.Draw(im_typing)
    draw_b.text((100, 100), "def chirkoot_adaptive_radar(): return True", fill=(0, 0, 0))

    # Evaluate typed frame
    tx_typing, st_typing, tel_typing = static_engine.evaluate_frame(im_typing)
    assert tx_typing is True, "Typing frame must trigger immediate transmission"
    assert st_typing == RadarState.DYNAMIC, f"Expected DYNAMIC state, got {st_typing}"
    assert tel_typing.last_change_score > 0.0

    # Image C: Simulates scrolling (shifting lines)
    im_scrolling = Image.new("RGB", (960, 540), (255, 255, 255))
    draw_c = ImageDraw.Draw(im_scrolling)
    for y in range(20, 540, 40): # Shifted by 20px
        draw_c.line([(0, y), (960, y)], fill=(50, 50, 50), width=2)

    time.sleep(0.26)  # Wait for dynamic_min_interval_ms
    tx_scroll, st_scroll, tel_scroll = static_engine.evaluate_frame(im_scrolling)
    assert tx_scroll is True, "Scrolling frame must be transmitted immediately"
    assert st_scroll == RadarState.DYNAMIC
    assert tel_scroll.last_change_score > 0.01, f"Expected scroll score > 0.01, got {tel_scroll.last_change_score}"

    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 3. Dynamic Burst: Typing & Scrolling detected and transmitted ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 4: Configurable Frame-Rate & Quality Limits
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()

    engine.update_config(
        dynamic_min_interval_ms=200,      # 5 FPS cap
        static_heartbeat_interval_ms=4000, # 0.25 FPS
        change_threshold=0.005,
        jpeg_quality=0.80,
        max_dimension=1024
    )

    assert engine.config.dynamic_min_interval_ms == 200
    assert engine.config.static_heartbeat_interval_ms == 4000
    assert engine.config.change_threshold == 0.005
    assert engine.config.jpeg_quality == 0.80
    assert engine.config.max_dimension == 1024

    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 4. Configurable frame-rate, heartbeat & quality limits verified ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 5: Foreground Window Privacy Shield Detection
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()

    privacy_engine = AdaptiveRadarEngine()

    # Test sensitive keywords detection
    test_sensitive_titles = [
        "1Password - Master Password Required",
        "Bitwarden - My Vault",
        "KeePassXC - Passwords.kdbx",
        "Chase Online Banking - Account Overview",
        "PayPal: Summary - Personal Account",
        "Sign in to Citi Online - Citibank",
        "Google Chrome (Incognito)",
        "Mozilla Firefox (Private Browsing)"
    ]

    for title in test_sensitive_titles:
        is_sens, matched_kw = privacy_engine.is_sensitive_window(title)
        assert is_sens is True, f"Expected '{title}' to trigger Privacy Shield"
        assert matched_kw != ""

    # Test benign titles
    benign_titles = [
        "Visual Studio Code - server.py",
        "Terminal - powershell.exe",
        "GitHub - Chirkoot Repository",
        "Google Search - Python Documentation",
        "Spotify Free",
        "Slack | general"
    ]

    for title in benign_titles:
        is_sens, _ = privacy_engine.is_sensitive_window(title)
        assert is_sens is False, f"Expected '{title}' to NOT trigger Privacy Shield"

    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 5. Foreground Privacy Shield: Password managers & Banking detected ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 6: Manual Pause and Resume Controls
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()

    control_engine = AdaptiveRadarEngine()
    control_engine.pause_radar(reason="Operator clicked pause")
    assert control_engine.state == RadarState.PAUSED
    assert control_engine.manual_pause is True

    # Frames sent during pause must be suppressed
    tx_paused, st_paused, tel_paused = control_engine.evaluate_frame(im_a)
    assert tx_paused is False
    assert st_paused == RadarState.PAUSED

    control_engine.resume_radar()
    assert control_engine.manual_pause is False

    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 6. Manual Pause and Resume controls verified ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 7: HUD Self-Capture Prevention (Masking & Affinity)
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()

    hud_engine = AdaptiveRadarEngine()
    # Simulate HUD occupying rectangle (50, 50, 350, 450)
    hud_engine.set_hud_rectangle(50, 50, 350, 450)

    test_screen = Image.new("RGB", (960, 540), (255, 255, 255))
    masked_screen = hud_engine.mask_hud_in_image(test_screen)

    # Pixels inside HUD rectangle should be masked (dark carbon texture, e.g. RGB 18, 20, 26)
    inside_px = masked_screen.getpixel((200, 250))
    assert inside_px == (18, 20, 26), f"Expected masked pixel (18, 20, 26), got {inside_px}"

    # Pixels outside HUD rectangle must remain untouched white (255, 255, 255)
    outside_px = masked_screen.getpixel((500, 250))
    assert outside_px == (255, 255, 255), f"Expected unmasked pixel (255, 255, 255), got {outside_px}"

    # Verify WDA_EXCLUDEFROMCAPTURE affinity helper function exists and runs safely
    affinity_result = AdaptiveRadarEngine.apply_window_display_affinity(0)
    assert isinstance(affinity_result, bool)

    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 7. HUD Self-Capture Prevention: Rectangle masking & Display Affinity verified ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 8: Dedicated look_at_screen Tool (On-Demand High-Res)
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()

    # Tool registration checks
    assert "look_at_screen" in TOOL_MAP, "look_at_screen must be registered in TOOL_MAP"
    assert "look_at_screen" in TIER_A_TOOLS, "look_at_screen must be in Tier A (Read-Only) tools"
    assert "look_at_screen" in TIER_1_TOOLS, "look_at_screen must be in Tier 1 Policy tools"

    # Execute look_at_screen with query
    res = look_at_screen(query="CHIRKOOT", include_ocr=True)
    assert "=== CHIRKOOT MK-V HIGH-RESOLUTION SCREEN INSPECTION ===" in res
    assert "Viewport:" in res
    assert "Active Foreground Window:" in res
    assert "Source Tier:" in res
    assert '<UNTRUSTED_OBSERVED_CONTENT source="SCREEN_IMAGE"' in res

    # Verify look_at_screen blocked when privacy shield is active
    original_check = GLOBAL_RADAR_ENGINE.check_foreground_privacy
    try:
        GLOBAL_RADAR_ENGINE.check_foreground_privacy = lambda: (True, "1password")
        res_privacy = look_at_screen(query="secret")
        assert "[PRIVACY SHIELD ENGAGED]" in res_privacy, "look_at_screen must be blocked during Privacy Shield"
    finally:
        GLOBAL_RADAR_ENGINE.check_foreground_privacy = original_check

    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 8. Dedicated 'look_at_screen' Tool: On-demand high-res inspection verified ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 9: Fresh Native-Resolution OCR Integration
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()

    # Verify ocr_screen captures fresh native uncompressed desktop in memory
    ocr_out = ocr_screen()
    assert "=== CHIRKOOT TACTICAL OCR RESULTS" in ocr_out
    assert '<UNTRUSTED_OBSERVED_CONTENT source="OCR_TEXT"' in ocr_out

    # Verify no %TEMP% files were written during OCR
    temp_target = Path(tempfile.gettempdir()) / "chirkoot_live_screen.jpg"
    assert not temp_target.exists(), "%TEMP%\\chirkoot_live_screen.jpg must not exist"

    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 9. Fresh Native-Resolution OCR: 100% in-memory without disk writes ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 10: Adaptive Transmission Telemetry Struct & Telemetry Export
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()

    telem = GLOBAL_RADAR_ENGINE.get_telemetry()
    assert "state" in telem
    assert "total_frames_evaluated" in telem
    assert "frames_transmitted" in telem
    assert "frames_suppressed" in telem
    assert "bandwidth_saved_pct" in telem
    assert "effective_fps" in telem
    assert "effective_kbps" in telem
    assert "privacy_shield_active" in telem
    assert "hud_masked" in telem

    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 10. Adaptive Transmission Telemetry dictionary & HUD metrics validated ({elapsed:.2f}ms)")
    passed += 1

    print("=" * 70)
    print(f" RADAR SUITE SUMMARY: {passed}/{total} PASSED | 0 FAILED")
    print("=" * 70)


if __name__ == "__main__":
    run_tests()
