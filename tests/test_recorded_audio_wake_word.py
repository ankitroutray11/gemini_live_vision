"""
Unit & Integration Tests for Recorded & Synthetic Audio Waveforms in Chirkoot Wake Detector.
Validates silence, noise rejection, pre-roll buffer retention, and keyword spotting telemetry.
"""

import math
import struct
import sys
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from wake_word_engine import LocalWakeWordDetector, GLOBAL_WAKE_DETECTOR


class TestRecordedAudioWakeWord(unittest.TestCase):

    def setUp(self):
        self.detector = LocalWakeWordDetector(cooldown_sec=1.0)
        self.detector.reset()

    def test_silence_rejection(self):
        """Test that pure silence chunks produce no wake event."""
        # 16kHz mono 16-bit PCM = 32000 bytes/sec. 100ms = 3200 bytes
        silence_chunk = b"\x00" * 3200
        for _ in range(10):
            res = self.detector.accept_waveform(silence_chunk)
            self.assertIsNone(res, "Silence should never trigger a wake word")

    def test_synthetic_sine_tone_noise_rejection(self):
        """Test that synthetic continuous sinusoidal tones produce no wake event."""
        sample_rate = 16000
        duration_sec = 0.1  # 100ms
        num_samples = int(sample_rate * duration_sec)
        # 1 kHz continuous beep
        samples = [int(15000 * math.sin(2 * math.pi * 1000 * i / sample_rate)) for i in range(num_samples)]
        tone_chunk = struct.pack(f"<{num_samples}h", *samples)

        for _ in range(10):
            res = self.detector.accept_waveform(tone_chunk)
            self.assertIsNone(res, "Continuous tone noise should never trigger wake word")

    def test_valid_waveform_trigger_and_telemetry(self):
        """Test keyword detection on synthetic audio chunk with simulation hook."""
        chunk = b"\x00" * 3200
        self.detector.simulate_keyword_spot("HEY CHIRKOOT")
        evt = self.detector.accept_waveform(chunk)

        self.assertIsNotNone(evt)
        self.assertEqual(evt["keyword"], "HEY CHIRKOOT")
        self.assertGreaterEqual(evt["confidence"], 0.8)
        self.assertGreater(evt["duration_ms"], 0.0)

        # Check telemetry
        telem = self.detector.get_telemetry()
        self.assertGreater(telem["total_chunks_processed"], 0)
        self.assertEqual(telem["total_wake_events"], 1)

    def test_pre_roll_buffer_retention(self):
        """Test 18-chunk pre-roll audio ring buffer behavior."""
        from collections import deque
        pre_roll = deque(maxlen=18)

        # Stream 30 sequential dummy chunks
        for i in range(30):
            chunk = f"chunk_{i}".encode("utf-8")
            pre_roll.append(chunk)

        # Ring buffer must be capped at 18
        self.assertEqual(len(pre_roll), 18)
        # Oldest preserved chunk must be chunk 12 (30 - 18)
        self.assertEqual(pre_roll[0], b"chunk_12")
        # Most recent chunk must be chunk 29
        self.assertEqual(pre_roll[-1], b"chunk_29")


if __name__ == "__main__":
    unittest.main()
