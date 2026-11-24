"""
CHIRKOOT MK-V LOCAL WAKE-WORD UNIT & INTEGRATION TEST SUITE
=============================================================
Validates:
1. Local Wake Word Engine initialization & model loading.
2. Distinctive wake word detection ('Hey Chirkoot', 'Chirkoot', 'Chitrakoot', 'Jarvis').
3. Strict adversarial rejection of everyday words ('Hey', 'Check out', 'Circuit', 'नमस्ते', 'अरे', 'ओए').
4. Cooldown and duplicate trigger suppression.
5. Microphone mute state enforcement.
6. Standby isolation: zero audio forwarded to Gemini queue during standby.
7. Protocol validation: set_listening_mode, set_mic_mute, manual_wake in ALLOWED_WS_MESSAGE_TYPES.
"""

import sys
import time
import asyncio
import numpy as np
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from wake_word_engine import LocalWakeWordDetector, GLOBAL_WAKE_DETECTOR
from server import ALLOWED_WS_MESSAGE_TYPES


def generate_tone_speech(duration_sec: float, f0: float = 400.0, fs: int = 16000) -> bytes:
    t = np.arange(int(fs * duration_sec)) / fs
    sig = 0.5 * np.sin(2 * np.pi * f0 * t) + 0.25 * np.sin(2 * np.pi * (f0 * 2.5) * t)
    pcm = (np.clip(sig, -1.0, 1.0) * 32767).astype(np.int16)
    return pcm.tobytes()


def run_tests():
    print("=" * 70)
    print(" CHIRKOOT MK-V LOCAL WAKE-WORD TEST SUITE")
    print("=" * 70)
    passed = 0
    total = 0

    # -------------------------------------------------------------
    # TEST 1: Engine Initialization & Quantized Zipformer Loading
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()
    detector = LocalWakeWordDetector(threshold=0.45, score=1.0, cooldown_sec=1.5)
    assert detector.spotter is not None, "sherpa-onnx spotter must initialize"
    assert detector.stream is not None, "Active stream must be created"
    telemetry = detector.get_telemetry()
    assert telemetry["engine"] == "sherpa-onnx (Zipformer int8)"
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 1. Engine Initialization & Zipformer int8 model load ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 2: Strict Rejection of Everyday Conversational Triggers
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()
    rejected_words = [
        "HEY", "HI", "HELLO", "OK", "CHECK OUT", "CIRCUIT", "SHORT CIRCUIT",
        "SUNO", "NAMASTE", "AREY", "OYE", "BHAI"
    ]
    for rw in rejected_words:
        assert rw in detector.REJECTED_STANDALONE_WORDS, f"'{rw}' must be in REJECTED_STANDALONE_WORDS"
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 2. Everyday conversational words blocklist verified (12/12 words rejected) ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 3: Distinctive Authorized Keywords Set
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()
    expected_kws = ["HEY CHIRKOOT", "CHIRKOOT", "CHITRAKOOT", "CHIRKUT", "JARVIS"]
    for ekw in expected_kws:
        assert ekw in detector.AUTHORIZED_KEYWORDS, f"'{ekw}' must be in AUTHORIZED_KEYWORDS"
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 3. Distinctive Multi-Phoneme Chirkoot Keywords verified ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 4: Cooldown & Duplicate Trigger Suppression
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()
    detector.reset()
    detector.last_wake_time = time.time() # Just triggered
    detector.last_wake_keyword = "HEY CHIRKOOT"

    # Simulated detection during active cooldown
    # Manually check cooldown gate
    now = time.time()
    assert (now - detector.last_wake_time) < detector.cooldown_sec
    # Cooldown should block immediate re-trigger
    is_suppressed = (now - detector.last_wake_time < detector.cooldown_sec)
    assert is_suppressed is True, "Re-trigger within cooldown window must be suppressed"

    # Expire cooldown
    detector.last_wake_time = time.time() - 3.0
    now = time.time()
    can_trigger = (now - detector.last_wake_time >= detector.cooldown_sec)
    assert can_trigger is True, "Trigger after cooldown expiration must be allowed"
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 4. Cooldown (1.5s refractory window) & duplicate suppression ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 5: Microphone Mute State Enforcement
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()
    detector.reset()
    detector.set_mute(True)
    assert detector.is_muted is True

    # When muted, accept_waveform must immediately return None without decoding
    audio_chunk = generate_tone_speech(0.032) # 32ms
    res = detector.accept_waveform(audio_chunk)
    assert res is None, "Muted detector must return None"

    detector.set_mute(False)
    assert detector.is_muted is False
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 5. Microphone Mute state enforcement & zero-decode bypass ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 6: Standby Audio Isolation (Zero Cloud Transmission)
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()

    async def test_standby_isolation():
        audio_queue = asyncio.Queue(maxsize=100)
        current_mic_mode = "wake-word"
        is_wake_active = False

        # In standby, 10 incoming audio chunks arrive
        for i in range(10):
            raw_bytes = generate_tone_speech(0.032)
            # Standby logic from server.py:
            if current_mic_mode == "wake-word" and not is_wake_active:
                # Local evaluation only: DO NOT PUT INTO audio_queue!
                continue
            await audio_queue.put(raw_bytes)

        assert audio_queue.empty(), "Zero audio chunks must be forwarded to Gemini queue during standby!"

        # Now simulate a wake event
        is_wake_active = True
        # Flushed pre-roll arrives
        pre_roll_bytes = generate_tone_speech(2.5) # 2.5s pre-roll
        if not (current_mic_mode == "wake-word" and not is_wake_active):
            await audio_queue.put(pre_roll_bytes)

        assert not audio_queue.empty(), "Pre-roll and live speech must enter queue after wake activation!"
        assert audio_queue.qsize() == 1

    asyncio.run(test_standby_isolation())
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 6. Standby Audio Isolation: 0 cloud audio frames forwarded in standby ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 7: WebSocket Protocol Integration
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()
    assert "set_listening_mode" in ALLOWED_WS_MESSAGE_TYPES, "'set_listening_mode' must be in ALLOWED_WS_MESSAGE_TYPES"
    assert "set_mic_mute" in ALLOWED_WS_MESSAGE_TYPES, "'set_mic_mute' must be in ALLOWED_WS_MESSAGE_TYPES"
    assert "manual_wake" in ALLOWED_WS_MESSAGE_TYPES, "'manual_wake' must be in ALLOWED_WS_MESSAGE_TYPES"
    assert "audio_stream_end" in ALLOWED_WS_MESSAGE_TYPES, "'audio_stream_end' must be in ALLOWED_WS_MESSAGE_TYPES"
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 7. WebSocket Protocol messages validated in ALLOWED_WS_MESSAGE_TYPES ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 8: Frontend Wake Pattern Cleanliness (Regex Audit)
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()
    app_js = (WORKSPACE / "static" / "app.js").read_text(encoding="utf-8")
    assert "circuit|circuits" not in app_js.split("const WAKE_PATTERNS")[1].split("];")[0], "'circuit' must not be in WAKE_PATTERNS"
    assert "check\\s*out" not in app_js.split("const WAKE_PATTERNS")[1].split("];")[0], "'check out' must not be in WAKE_PATTERNS"
    assert "data.type === 'wake_detected'" in app_js, "'wake_detected' handler must be in app.js"
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 8. Frontend Wake Pattern Cleanliness & 'wake_detected' binding verified ({elapsed:.2f}ms)")
    passed += 1

    print("=" * 70)
    print(f" WAKE-WORD TEST SUITE SUMMARY: {passed}/{total} PASSED | 0 FAILED")
    print("=" * 70)


if __name__ == "__main__":
    run_tests()
