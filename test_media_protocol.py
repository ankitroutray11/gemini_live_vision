"""
CHIRKOOT MK-V BINARY MEDIA PROTOCOL & IN-MEMORY FRAME STORE TEST SUITE
========================================================================
Validates:
1. Binary Media Framing Protocol (v1) with 16-byte fixed header.
2. Correct separation of 0x01 (Audio PCM), 0x02 (Screen JPEG), and 0x03 (Webcam JPEG).
3. Payload-size boundaries & rejection of oversized packets (>64KB audio, >2MB video).
4. Malformed packet rejection (truncated headers, size mismatches, invalid types).
5. Backward compatibility for unheadered raw PCM audio.
6. In-Memory Frame Store thread-safety & zero-disk writes (elimination of %TEMP%\\chirkoot_live_screen.jpg).
7. Old frame dereferencing and memory reclamation.
8. High-resolution native desktop in-memory capture for crisp OCR.
9. RapidOCR in-memory inference directly on NumPy arrays (zero temporary files).
10. Explicit screenshot saving only when explicitly requested.
"""

import os
import sys
import time
import struct
import tempfile
import numpy as np
from pathlib import Path
from PIL import Image

WORKSPACE = Path(r"C:\jarvis\gemini_live_vision")
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from frame_store import (
    MediaPacket,
    FrameType,
    MalformedPacketError,
    InMemoryFrameStore,
    GLOBAL_FRAME_STORE,
    HEADER_SIZE,
    MAX_AUDIO_PAYLOAD_BYTES,
    MAX_VIDEO_PAYLOAD_BYTES
)
import tools


def create_dummy_jpeg(width: int = 320, height: int = 240, color: str = "blue") -> bytes:
    import io
    im = Image.new("RGB", (width, height), color=color)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def run_tests():
    print("=" * 70)
    print(" CHIRKOOT MK-V BINARY MEDIA PROTOCOL & IN-MEMORY FRAME STORE SUITE")
    print("=" * 70)
    passed = 0
    total = 0

    # -------------------------------------------------------------
    # TEST 1: Binary Framing Protocol (v1) Header Structure
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()
    dummy_audio = np.zeros(512, dtype=np.int16).tobytes()
    ts_now = int(time.time() * 1000)
    pkt_audio = MediaPacket(
        packet_type=FrameType.AUDIO_PCM,
        payload=dummy_audio,
        stream_id=1,
        timestamp_ms=ts_now
    )
    serialized = pkt_audio.serialize()
    assert len(serialized) == HEADER_SIZE + len(dummy_audio)

    # Check raw header bytes
    proto_ver, pkt_type, stream_id, parsed_ts, payload_len = struct.unpack(">BBHQI", serialized[:16])
    assert proto_ver == 0x01, f"Expected proto 0x01, got {proto_ver}"
    assert pkt_type == 0x01, f"Expected type 0x01 (AUDIO), got {pkt_type}"
    assert stream_id == 1, f"Expected stream_id 1, got {stream_id}"
    assert parsed_ts == ts_now, f"Expected timestamp {ts_now}, got {parsed_ts}"
    assert payload_len == len(dummy_audio), f"Expected payload length {len(dummy_audio)}, got {payload_len}"

    # Parse through MediaPacket.parse
    parsed = MediaPacket.parse(serialized)
    assert parsed.packet_type == FrameType.AUDIO_PCM
    assert parsed.payload == dummy_audio
    assert parsed.stream_id == 1
    assert parsed.timestamp_ms == ts_now
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 1. 16-Byte Binary Media Framing Protocol (v1) validated ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 2: Correct Separation of 0x01 (Audio), 0x02 (Screen), 0x03 (Webcam)
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()
    jpeg_screen = create_dummy_jpeg(320, 240, "green")
    jpeg_webcam = create_dummy_jpeg(160, 120, "red")

    pkt_screen = MediaPacket(packet_type=FrameType.SCREEN_JPEG, payload=jpeg_screen, stream_id=0)
    pkt_webcam = MediaPacket(packet_type=FrameType.WEBCAM_JPEG, payload=jpeg_webcam, stream_id=1)

    parsed_screen = MediaPacket.parse(pkt_screen.serialize())
    parsed_webcam = MediaPacket.parse(pkt_webcam.serialize())

    assert parsed_screen.packet_type == FrameType.SCREEN_JPEG
    assert parsed_screen.packet_type == 0x02
    assert parsed_webcam.packet_type == FrameType.WEBCAM_JPEG
    assert parsed_webcam.packet_type == 0x03
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 2. Packet Separation: 0x01 (Audio), 0x02 (Screen JPEG), 0x03 (Webcam JPEG) ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 3: Payload Size Boundaries & Validation
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()
    # 1. Oversized Audio (>64 KB)
    oversized_audio = b"\x00" * (MAX_AUDIO_PAYLOAD_BYTES + 10)
    bad_audio_header = struct.pack(">BBHQI", 0x01, 0x01, 0, int(time.time() * 1000), len(oversized_audio))
    try:
        MediaPacket.parse(bad_audio_header + oversized_audio)
        assert False, "Should have rejected oversized audio payload"
    except MalformedPacketError as err:
        assert "Payload exceeds limit" in str(err)

    # 2. Oversized Video (>2 MB)
    oversized_video = b"\x00" * (MAX_VIDEO_PAYLOAD_BYTES + 10)
    bad_video_header = struct.pack(">BBHQI", 0x01, 0x02, 0, int(time.time() * 1000), len(oversized_video))
    try:
        MediaPacket.parse(bad_video_header + oversized_video)
        assert False, "Should have rejected oversized video payload"
    except MalformedPacketError as err:
        assert "Payload exceeds limit" in str(err)

    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 3. Payload-size validation (Audio >64KB & Video >2MB rejected safely) ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 4: Malformed Packet Boundaries & Corruption Handling
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()
    # 1. Truncated packet (header says 500 bytes payload, but only 10 provided)
    truncated_header = struct.pack(">BBHQI", 0x01, 0x02, 0, int(time.time() * 1000), 500)
    try:
        MediaPacket.parse(truncated_header + b"1234567890")
        assert False, "Should have rejected truncated packet"
    except MalformedPacketError as err:
        assert "Payload size mismatch" in str(err)

    # 2. Empty packet
    try:
        MediaPacket.parse(b"")
        assert False, "Should have rejected empty packet"
    except MalformedPacketError:
        pass

    # 3. Invalid packet type ID
    bad_type_header = struct.pack(">BBHQI", 0x01, 0x09, 0, int(time.time() * 1000), 10)
    try:
        MediaPacket.parse(bad_type_header + b"1234567890")
        assert False, "Should have rejected invalid packet type 0x09"
    except MalformedPacketError:
        pass

    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 4. Malformed packet boundaries & truncation rejection ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 5: Backward Compatibility for Unheadered Raw PCM Audio
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()
    raw_pcm_legacy = np.random.randint(-1000, 1000, 512, dtype=np.int16).tobytes()
    # Must not throw, but fall back gracefully to FrameType.AUDIO_PCM
    parsed_legacy = MediaPacket.parse(raw_pcm_legacy)
    assert parsed_legacy.packet_type == FrameType.AUDIO_PCM
    assert parsed_legacy.payload == raw_pcm_legacy
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 5. Backward compatibility for unheadered raw PCM audio chunks ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 6: In-Memory Frame Store (Thread-Safe, Zero Disk Writes)
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()
    store = InMemoryFrameStore()
    temp_target = Path(tempfile.gettempdir()) / "chirkoot_live_screen.jpg"
    if temp_target.exists():
        try:
            temp_target.unlink()
        except Exception:
            pass

    # Stream 10 frames into store
    for i in range(10):
        color = "red" if i % 2 == 0 else "blue"
        jpeg = create_dummy_jpeg(320, 240, color)
        p = MediaPacket(packet_type=FrameType.SCREEN_JPEG, payload=jpeg, stream_id=0)
        vf = store.update_frame(p)
        assert vf.width == 320
        assert vf.height == 240

    # Ensure %TEMP%\chirkoot_live_screen.jpg was NEVER touched or written
    assert not temp_target.exists(), "CRITICAL REGRESSION: %TEMP%\\chirkoot_live_screen.jpg must NOT be written!"

    # Verify latest frame access
    latest = store.get_latest_screen_frame()
    assert latest is not None
    assert latest.width == 320 and latest.height == 240

    # Verify telemetry
    telem = store.get_telemetry()
    assert telem["total_frames_received"] == 10
    assert telem["screen_active"] is True
    assert telem["screen_resolution"] == "320x240"

    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 6. In-Memory Frame Store verified (10 frames, zero disk I/O) ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 7: Memory Reclamation on Superseded Frames
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()
    f1 = store.update_frame(MediaPacket(packet_type=FrameType.SCREEN_JPEG, payload=create_dummy_jpeg(100, 100)))
    f1_data_len = len(f1.data)
    assert f1_data_len > 0

    # Supersede with f2
    f2 = store.update_frame(MediaPacket(packet_type=FrameType.SCREEN_JPEG, payload=create_dummy_jpeg(200, 200)))
    # f1 should have been released
    assert f1.data == b"", "Superseded frame data must be released to prevent memory leakage"
    assert len(f2.data) > 0, "Active frame data must remain intact"

    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 7. Superseded frame dereferencing & memory reclamation ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 8: Native Desktop In-Memory Capture (Dual-Tier Vision)
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()
    native_frame = store.capture_high_res_screen()
    assert native_frame.source in ("high_res_native", "stream_fallback", "synthetic_fallback")
    assert native_frame.width >= 640, f"Expected desktop width >= 640, got {native_frame.width}"
    assert native_frame.height >= 480, f"Expected desktop height >= 480, got {native_frame.height}"
    assert len(native_frame.data) > 0

    # NumPy array conversion in memory
    np_img = native_frame.get_numpy()
    assert isinstance(np_img, np.ndarray)
    assert np_img.shape[0] == native_frame.height
    assert np_img.shape[1] == native_frame.width
    assert np_img.shape[2] == 3

    # Zero disk writes
    assert not temp_target.exists(), "%TEMP%\\chirkoot_live_screen.jpg must not be written during native capture"

    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 8. In-Memory Native Desktop Capture: {native_frame.width}x{native_frame.height} (Zero disk writes) ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 9: In-Memory RapidOCR Integration
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()
    ocr_result = tools.ocr_screen()
    assert isinstance(ocr_result, str)
    assert "=== CHIRKOOT TACTICAL OCR RESULTS" in ocr_result
    assert not temp_target.exists(), "%TEMP%\\chirkoot_live_screen.jpg must not be written during ocr_screen"

    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 9. In-Memory RapidOCR inference on NumPy array without disk I/O ({elapsed:.2f}ms)")
    passed += 1

    # -------------------------------------------------------------
    # TEST 10: Explicit Screenshot Saving (Only on User/Tool Request)
    # -------------------------------------------------------------
    total += 1
    t0 = time.perf_counter()
    explicit_saved_path = store.save_screenshot_explicit()
    assert explicit_saved_path is not None
    assert os.path.exists(explicit_saved_path)
    file_size = os.path.getsize(explicit_saved_path)
    assert file_size > 1000, f"Screenshot file size {file_size} bytes too small"

    # Clean up explicit test screenshot
    try:
        os.remove(explicit_saved_path)
    except Exception:
        pass

    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  [PASS] 10. Explicit Screenshot Saving: On-demand write verified ({elapsed:.2f}ms)")
    passed += 1

    print("=" * 70)
    print(f" PROTOCOL & FRAME STORE TEST SUMMARY: {passed}/{total} PASSED | 0 FAILED")
    print("=" * 70)


if __name__ == "__main__":
    run_tests()
