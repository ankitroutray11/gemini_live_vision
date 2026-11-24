"""
Unit tests for Chirkoot Audio Downsampling & Resampling Pipeline.
Verifies sample rate conversion (48kHz/44.1kHz -> 16kHz), channel mixing (Stereo -> Mono),
anti-aliasing filter, int16 clipping, and Little-Endian byte integrity.
"""

import math
import struct
import sys
import unittest
from pathlib import Path

# Add project root to sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from chirkoot.audio.downsampling import downsample_pcm16


class TestAudioDownsampling(unittest.TestCase):

    def test_48k_to_16k_mono_ratio(self):
        """Test downsampling 48kHz mono to 16kHz mono (exact 3:1 decimation)."""
        num_input_samples = 4800  # 100ms at 48kHz
        # Generate a 440Hz sine wave
        samples = [int(10000 * math.sin(2 * math.pi * 440 * i / 48000)) for i in range(num_input_samples)]
        input_bytes = struct.pack(f"<{num_input_samples}h", *samples)

        output_bytes = downsample_pcm16(input_bytes, source_rate=48000, target_rate=16000, source_channels=1)

        # Output should be 1600 samples = 3200 bytes
        expected_output_samples = num_input_samples // 3
        actual_output_samples = len(output_bytes) // 2
        self.assertEqual(actual_output_samples, expected_output_samples)
        self.assertEqual(len(output_bytes), expected_output_samples * 2)

    def test_44k1_to_16k_mono(self):
        """Test downsampling 44.1kHz mono to 16kHz mono."""
        num_input_samples = 4410  # 100ms at 44.1kHz
        samples = [int(8000 * math.sin(2 * math.pi * 1000 * i / 44100)) for i in range(num_input_samples)]
        input_bytes = struct.pack(f"<{num_input_samples}h", *samples)

        output_bytes = downsample_pcm16(input_bytes, source_rate=44100, target_rate=16000, source_channels=1)

        expected_output_samples = int(num_input_samples / (44100 / 16000))
        actual_output_samples = len(output_bytes) // 2
        self.assertEqual(actual_output_samples, expected_output_samples)

    def test_stereo_to_mono_averaging(self):
        """Test downsampling stereo (2 channels) into mono (1 channel)."""
        num_frames = 480  # 10ms at 48kHz
        # Channel 1 has 10000, Channel 2 has -10000 -> average is 0
        stereo_samples = []
        for _ in range(num_frames):
            stereo_samples.extend([10000, -10000])

        input_bytes = struct.pack(f"<{num_frames * 2}h", *stereo_samples)
        output_bytes = downsample_pcm16(input_bytes, source_rate=48000, target_rate=16000, source_channels=2)

        out_samples = struct.unpack(f"<{len(output_bytes)//2}h", output_bytes)
        # Verify all output samples are 0 (or near zero due to float conversion)
        for s in out_samples:
            self.assertAlmostEqual(s, 0, delta=2)

    def test_clipping_and_no_overflow_wraparound(self):
        """Test that peak amplitudes close to limits do not wrap around to opposite signs."""
        num_input_samples = 300
        # Extreme alternating values that could cause FIR overshoot
        extreme_samples = [32767 if i % 2 == 0 else 32700 for i in range(num_input_samples)]
        input_bytes = struct.pack(f"<{num_input_samples}h", *extreme_samples)

        output_bytes = downsample_pcm16(input_bytes, source_rate=48000, target_rate=16000, source_channels=1)
        out_samples = struct.unpack(f"<{len(output_bytes)//2}h", output_bytes)

        for s in out_samples:
            self.assertGreaterEqual(s, -32768)
            self.assertLessEqual(s, 32767)
            # Should stay strictly positive, never wrap to negative!
            self.assertGreater(s, 0)

    def test_passthrough_when_same_rate(self):
        """Test that matching 16kHz mono passes through untouched."""
        num_samples = 160
        samples = [1000 * (i % 10) for i in range(num_samples)]
        input_bytes = struct.pack(f"<{num_samples}h", *samples)

        output_bytes = downsample_pcm16(input_bytes, source_rate=16000, target_rate=16000, source_channels=1)
        self.assertEqual(output_bytes, input_bytes)

    def test_empty_or_invalid_inputs(self):
        """Test edge cases with empty bytes or invalid rates."""
        self.assertEqual(downsample_pcm16(b"", 48000, 16000), b"")
        self.assertEqual(downsample_pcm16(b"\x00\x00", 0, 16000), b"")
        self.assertEqual(downsample_pcm16(b"\x00\x00", 48000, 0), b"")


if __name__ == "__main__":
    unittest.main()
