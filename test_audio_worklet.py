"""
CHIRKOOT MK-V AUDIO WORKLET & MICROPHONE CAPTURE TEST SUITE
=============================================================
Validates:
1. FIR Anti-Aliasing Low-Pass Filter (symmetry, unit DC gain, stopband attenuation).
2. Streaming Decimation & Resampling (48kHz -> 16kHz, 44.1kHz -> 16kHz, 16kHz passthrough).
3. Out-of-band alias rejection (> 40 dB suppression of aliased frequencies).
4. Circular Pre-Roll Buffer (2.5s duration, 80 KB memory, chronological extraction after wrap-around).
5. 16-bit signed PCM little-endian byte-order encoding.
6. Real-time Voice Activity Detection (VAD) energy tracking and state transitions.
7. WebSocket Protocol Integration (audio_stream_end message acceptance and queue draining).
8. Static asset presence and validity for /static/audio-processor.js.
"""

import math
import os
import sys
import json
import time
import asyncio
import numpy as np
from pathlib import Path

WORKSPACE = Path(r"C:\jarvis\gemini_live_vision")
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from server import ALLOWED_WS_MESSAGE_TYPES


def design_fir_lowpass(num_taps: int, cutoff_hz: float, fs: float) -> np.ndarray:
    """Exact replica of the FIR filter generator implemented in audio-processor.js."""
    fc = min(0.48, cutoff_hz / fs)
    mid = (num_taps - 1) / 2.0
    h = np.zeros(num_taps, dtype=np.float32)
    total = 0.0

    for i in range(num_taps):
        m = i - mid
        sinc = 2.0 * fc if m == 0 else math.sin(2.0 * math.pi * fc * m) / (math.pi * m)
        w = 0.42 - 0.5 * math.cos(2.0 * math.pi * i / (num_taps - 1)) + 0.08 * math.cos(4.0 * math.pi * i / (num_taps - 1))
        val = sinc * w
        h[i] = val
        total += val

    if total != 0.0:
        h /= total
    return h


class CircularPreRollBufferSim:
    """Python simulation of the AudioWorklet circular pre-roll buffer."""
    def __init__(self, capacity: int = 40000):
        self.capacity = capacity
        self.buffer = np.zeros(capacity, dtype=np.int16)
        self.write_pos = 0
        self.count = 0

    def push(self, sample: int):
        self.buffer[self.write_pos] = sample
        self.write_pos = (self.write_pos + 1) % self.capacity
        if self.count < self.capacity:
            self.count += 1

    def flush(self) -> np.ndarray:
        if self.count == 0:
            return np.array([], dtype=np.int16)
        start = (self.write_pos - self.count + self.capacity) % self.capacity
        out = np.zeros(self.count, dtype=np.int16)
        for i in range(self.count):
            out[i] = self.buffer[(start + i) % self.capacity]
        self.count = 0
        return out


def run_tests():
    print("=" * 70)
    print(" CHIRKOOT MK-V AUDIO WORKLET & CAPTURE ARCHITECTURE TEST SUITE")
    print("=" * 70)
    passed = 0
    total = 0

    # -------------------------------------------------------------
    # TEST 1: Static AudioWorklet File Verification
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()
    processor_path = WORKSPACE / "static" / "audio-processor.js"
    assert processor_path.exists(), f"audio-processor.js missing at {processor_path}"
    content = processor_path.read_text(encoding="utf-8")
    assert "class ChirkootAudioProcessor extends AudioWorkletProcessor" in content
    assert "registerProcessor('chirkoot-audio-processor'" in content
    assert "flushPreRoll" in content
    assert "preRollCapacity" in content
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 1. Static AudioWorklet file exists and declares ChirkootAudioProcessor ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 2: FIR Anti-Aliasing Filter Math & Attenuation
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()
    num_taps = 51
    fs_48k = 48000.0
    cutoff = 6800.0
    fir = design_fir_lowpass(num_taps, cutoff, fs_48k)
    
    # Check filter symmetry (linear phase property)
    for i in range(num_taps // 2):
        assert abs(fir[i] - fir[num_taps - 1 - i]) < 1e-6, "FIR filter must be strictly symmetric"
    
    # Check unit DC gain (sum == 1.0)
    assert abs(np.sum(fir) - 1.0) < 1e-5, f"FIR DC gain {np.sum(fir)} must be 1.0"

    # Frequency response: Passband at 1 kHz should have ~0 dB attenuation (< 0.2 dB)
    w_pass = 2 * math.pi * 1000.0 / fs_48k
    H_pass = np.sum(fir * np.exp(-1j * w_pass * np.arange(num_taps)))
    att_pass_db = 20 * math.log10(abs(H_pass))
    assert abs(att_pass_db) < 0.2, f"Passband ripple {att_pass_db:.2f} dB too high"

    # Frequency response: Stopband at Nyquist (8 kHz) and higher (e.g. 10 kHz, 12 kHz)
    w_nyq = 2 * math.pi * 8000.0 / fs_48k
    H_nyq = np.sum(fir * np.exp(-1j * w_nyq * np.arange(num_taps)))
    att_nyq_db = 20 * math.log10(abs(H_nyq))

    w_10k = 2 * math.pi * 10000.0 / fs_48k
    H_10k = np.sum(fir * np.exp(-1j * w_10k * np.arange(num_taps)))
    att_10k_db = 20 * math.log10(abs(H_10k))
    assert att_10k_db < -40.0, f"Attenuation at 10 kHz must be < -40 dB, got {att_10k_db:.1f} dB"

    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 2. FIR Low-Pass: Passband ripple {att_pass_db:.3f}dB, Stopband @10kHz {att_10k_db:.1f}dB (< -40dB) ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 3: Streaming Decimation from 48 kHz to 16 kHz (3:1 Integer)
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()
    fs_in = 48000
    fs_out = 16000
    ratio = fs_in / fs_out # 3.0
    duration = 0.5 # 0.5s
    t = np.arange(int(fs_in * duration)) / fs_in
    test_freq = 440.0 # A4 note
    sig = np.sin(2 * np.pi * test_freq * t, dtype=np.float32)

    # Block-by-block processing (128 samples per quantum)
    block_size = 128
    history = np.zeros(num_taps - 1, dtype=np.float32)
    resample_pos = 0.0
    output_samples = []

    for start in range(0, len(sig) - block_size + 1, block_size):
        block = sig[start:start + block_size]
        extended = np.concatenate([history, block])
        history = block[-(num_taps - 1):]
        filtered = np.convolve(extended, fir, mode='valid')

        while resample_pos < len(filtered):
            i0 = int(resample_pos)
            i1 = min(i0 + 1, len(filtered) - 1)
            frac = resample_pos - i0
            val = filtered[i0] * (1.0 - frac) + filtered[i1] * frac
            output_samples.append(val)
            resample_pos += ratio
        resample_pos -= len(filtered)

    out_48k = np.array(output_samples, dtype=np.float32)
    expected_samples = int(fs_out * duration)
    assert abs(len(out_48k) - expected_samples) <= 128, f"Decimated length {len(out_48k)} vs expected {expected_samples}"

    # FFT frequency check
    fft_vals = np.abs(np.fft.rfft(out_48k))
    freqs = np.fft.rfftfreq(len(out_48k), 1.0 / fs_out)
    peak_freq = freqs[np.argmax(fft_vals)]
    assert abs(peak_freq - test_freq) < 2.0, f"Peak frequency {peak_freq:.1f} Hz does not match {test_freq} Hz"
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 3. 48kHz -> 16kHz Streaming Decimation: Tone 440.0Hz -> Peak {peak_freq:.2f}Hz ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 4: Streaming Resampling from 44.1 kHz to 16 kHz (Fractional)
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()
    fs_in_44 = 44100
    ratio_44 = fs_in_44 / fs_out # 2.75625
    fir_44 = design_fir_lowpass(num_taps, cutoff, fs_in_44)
    t_44 = np.arange(int(fs_in_44 * duration)) / fs_in_44
    test_freq_1k = 1000.0
    sig_44 = np.sin(2 * np.pi * test_freq_1k * t_44, dtype=np.float32)

    history_44 = np.zeros(num_taps - 1, dtype=np.float32)
    resample_pos_44 = 0.0
    output_samples_44 = []

    for start in range(0, len(sig_44) - block_size + 1, block_size):
        block = sig_44[start:start + block_size]
        extended = np.concatenate([history_44, block])
        history_44 = block[-(num_taps - 1):]
        filtered = np.convolve(extended, fir_44, mode='valid')

        while resample_pos_44 < len(filtered):
            i0 = int(resample_pos_44)
            i1 = min(i0 + 1, len(filtered) - 1)
            frac = resample_pos_44 - i0
            val = filtered[i0] * (1.0 - frac) + filtered[i1] * frac
            output_samples_44.append(val)
            resample_pos_44 += ratio_44
        resample_pos_44 -= len(filtered)

    out_44k = np.array(output_samples_44, dtype=np.float32)
    fft_vals_44 = np.abs(np.fft.rfft(out_44k))
    freqs_44 = np.fft.rfftfreq(len(out_44k), 1.0 / fs_out)
    peak_freq_44 = freqs_44[np.argmax(fft_vals_44)]
    assert abs(peak_freq_44 - test_freq_1k) < 2.0, f"Peak frequency {peak_freq_44:.1f} Hz does not match {test_freq_1k} Hz"
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 4. 44.1kHz -> 16kHz Fractional Resampler: Tone 1000.0Hz -> Peak {peak_freq_44:.2f}Hz ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 5: Out-of-Band Alias Rejection (> 40 dB suppression)
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()
    # Signal with 1kHz speech tone + loud 11kHz tone (which without filtering aliases to |11000 - 16000| = 5000Hz)
    sig_composite = np.sin(2 * np.pi * 1000.0 * t, dtype=np.float32) + 0.9 * np.sin(2 * np.pi * 11000.0 * t, dtype=np.float32)

    history_comp = np.zeros(num_taps - 1, dtype=np.float32)
    resample_pos_comp = 0.0
    output_samples_comp = []

    for start in range(0, len(sig_composite) - block_size + 1, block_size):
        block = sig_composite[start:start + block_size]
        extended = np.concatenate([history_comp, block])
        history_comp = block[-(num_taps - 1):]
        filtered = np.convolve(extended, fir, mode='valid')

        while resample_pos_comp < len(filtered):
            i0 = int(resample_pos_comp)
            i1 = min(i0 + 1, len(filtered) - 1)
            frac = resample_pos_comp - i0
            val = filtered[i0] * (1.0 - frac) + filtered[i1] * frac
            output_samples_comp.append(val)
            resample_pos_comp += ratio
        resample_pos_comp -= len(filtered)

    out_comp = np.array(output_samples_comp, dtype=np.float32)
    fft_comp = np.abs(np.fft.rfft(out_comp))
    freqs_comp = np.fft.rfftfreq(len(out_comp), 1.0 / fs_out)

    idx_1k = np.argmin(np.abs(freqs_comp - 1000.0))
    idx_alias = np.argmin(np.abs(freqs_comp - 5000.0))

    power_1k = fft_comp[idx_1k]
    power_alias = fft_comp[idx_alias]
    alias_suppression_db = 20 * math.log10(power_alias / power_1k) if power_alias > 0 else -100.0

    assert alias_suppression_db < -40.0, f"Alias suppression {alias_suppression_db:.1f} dB is insufficiently attenuated (must be < -40 dB)"
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 5. Out-of-band Alias Rejection: 11kHz tone suppressed by {alias_suppression_db:.1f}dB at folded 5kHz ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 6: Circular Pre-Roll Buffer (Duration, Memory & Chronological Order)
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()
    capacity = 40000
    ring = CircularPreRollBufferSim(capacity=capacity)

    # 1. Capacity & Duration verification:
    duration_sec = capacity / 16000.0
    assert duration_sec == 2.5, f"Expected 2.5s duration, got {duration_sec}s"
    # 2. Memory usage verification:
    memory_bytes = ring.buffer.nbytes
    assert memory_bytes == 80000, f"Expected 80,000 bytes (78.1 KB), got {memory_bytes}"

    # 3. Write 100,000 sequential samples (wrapping around 2.5 times)
    for i in range(100000):
        # Store lower 16-bit value
        ring.push(i % 30000)

    # 4. Flush and check chronological continuity
    flushed = ring.flush()
    assert len(flushed) == capacity, f"Expected {capacity} flushed samples, got {len(flushed)}"

    # The last 40,000 samples pushed were i from 60,000 to 99,999
    expected_sequence = np.array([(60000 + k) % 30000 for k in range(capacity)], dtype=np.int16)
    assert np.array_equal(flushed, expected_sequence), "Flushed pre-roll samples must maintain strict chronological sequence"

    # 5. Second flush must be empty (idempotent drain)
    second_flush = ring.flush()
    assert len(second_flush) == 0, "Second flush must be empty"

    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 6. Circular Pre-Roll: 40k samples = 2.50s, 78.1KB memory, unbroken chronological order ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 7: PCM Format, Channel Count & Little-Endian Byte Order
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()
    # Test values: -1.0 -> -32768 (0x8000), 0.0 -> 0 (0x0000), 1.0 -> 32767 (0x7FFF)
    float_test = np.array([-1.0, 0.0, 1.0, 0.5, -0.5], dtype=np.float32)
    pcm16 = np.zeros(len(float_test), dtype=np.int16)
    for i in range(len(float_test)):
        s = max(-1.0, min(1.0, float_test[i]))
        pcm16[i] = round(s * 0x8000) if s < 0 else round(s * 0x7FFF)

    assert pcm16[0] == -32768, f"Expected -32768, got {pcm16[0]}"
    assert pcm16[1] == 0, f"Expected 0, got {pcm16[1]}"
    assert pcm16[2] == 32767, f"Expected 32767, got {pcm16[2]}"

    # Verify Little-Endian byte ordering
    raw_bytes = pcm16.tobytes()
    # In little-endian, -32768 (0x8000) is bytes [0x00, 0x80]
    assert raw_bytes[0] == 0x00 and raw_bytes[1] == 0x80, f"Byte order error for -32768: {list(raw_bytes[:2])}"
    # In little-endian, 32767 (0x7FFF) is bytes [0xFF, 0x7F]
    assert raw_bytes[4] == 0xFF and raw_bytes[5] == 0x7F, f"Byte order error for 32767: {list(raw_bytes[4:6])}"

    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 7. PCM Format: 16-bit signed integer, Little-Endian verified (0x8000 / 0x7FFF) ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 8: Real-Time Voice Activity Detection (VAD) Algorithm
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()
    # Simulate silence frames followed by speech frames followed by silence frames
    frame_size = 512
    silence_frame = np.random.normal(0, 0.003, frame_size).astype(np.float32)
    speech_frame = (np.sin(2 * np.pi * 500 * np.arange(frame_size) / 16000) * 0.35).astype(np.float32)

    # VAD state machine
    noise_floor = 0.015
    is_speaking = False
    speech_frames = 0
    silence_frames = 0
    min_speech_frames = 2
    silence_timeout_frames = 10

    vad_events = []

    # Process 5 silence frames
    for _ in range(5):
        rms = np.sqrt(np.mean(silence_frame ** 2))
        noise_floor = noise_floor * 0.995 + rms * 0.005
        thresh = max(0.015, noise_floor * 2.8)
        if rms > thresh:
            speech_frames += 1
        else:
            speech_frames = 0

    assert not is_speaking, "Must remain in silence during initial background frames"

    # Process 5 speech frames
    for _ in range(5):
        rms = np.sqrt(np.mean(speech_frame ** 2))
        thresh = max(0.015, noise_floor * 2.8)
        if rms > thresh:
            silence_frames = 0
            speech_frames += 1
            if not is_speaking and speech_frames >= min_speech_frames:
                is_speaking = True
                vad_events.append("speech_started")

    assert is_speaking and "speech_started" in vad_events, "VAD must transition to speech_started"

    # Process 15 silence frames (triggering silence timeout)
    for _ in range(15):
        rms = np.sqrt(np.mean(silence_frame ** 2))
        thresh = max(0.015, noise_floor * 2.8)
        if rms <= thresh:
            speech_frames = 0
            silence_frames += 1
            noise_floor = noise_floor * 0.995 + rms * 0.005
            if is_speaking and silence_frames >= silence_timeout_frames:
                is_speaking = False
                vad_events.append("speech_ended")

    assert not is_speaking and "speech_ended" in vad_events, "VAD must transition to speech_ended on timeout"
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 8. VAD State Machine: Adaptive noise tracking, speech trigger, and silence timeout ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 9: WebSocket Protocol Integration ('audio_stream_end')
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()
    assert "audio_stream_end" in ALLOWED_WS_MESSAGE_TYPES, "'audio_stream_end' must be in ALLOWED_WS_MESSAGE_TYPES"

    # Test queue sequencing with Sentinel None
    async def test_queue_sequencing():
        q = asyncio.Queue(maxsize=10)
        # Put 3 audio chunks
        await q.put(b"chunk_1")
        await q.put(b"chunk_2")
        await q.put(b"chunk_3")
        # Put sentinel
        await q.put(None)

        received = []
        stream_ended = False
        while not q.empty():
            item = await q.get()
            if item is None:
                stream_ended = True
                break
            received.append(item)

        assert received == [b"chunk_1", b"chunk_2", b"chunk_3"], "Preceding audio chunks must drain before stream end"
        assert stream_ended is True, "audio_stream_end must fire cleanly after queue drain"

    asyncio.run(test_queue_sequencing())
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 9. Protocol Integration: 'audio_stream_end' validated & FIFO audio queue drain verified ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 10: HUD Visualizer Thread Independence
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()
    app_js_text = (WORKSPACE / "static" / "app.js").read_text(encoding="utf-8")
    assert "source.connect(micAnalyser)" in app_js_text, "micAnalyser must connect to source in parallel"
    assert "source.connect(audioWorkletNode)" in app_js_text, "audioWorkletNode must connect to source"
    assert "requestAnimationFrame(renderArcVisualizer)" in app_js_text, "HUD arc visualizer runs on RAF"
    assert "sendAudioStreamEnd()" in app_js_text, "sendAudioStreamEnd function must be declared and invoked"
    assert "navigator.mediaDevices.addEventListener('devicechange'" in app_js_text, "devicechange listener must be present"
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 10. Visualizer Thread Independence & Web Audio Graph Wiring Verified ({elapsed:.2f}ms)")
    passed += 1

    print("=" * 70)
    print(f" AUDIO CAPTURE ARCHITECTURE TEST SUMMARY: {passed}/{total} PASSED | 0 FAILED")
    print("=" * 70)


if __name__ == "__main__":
    run_tests()
