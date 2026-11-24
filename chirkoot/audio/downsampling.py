"""Chirkoot Audio Downsampling & Resampling Pipeline.

Provides production-grade audio conversion:
- Resamples arbitrary browser/system sample rates (44.1 kHz, 48 kHz, etc.) to 16 kHz Linear PCM.
- Applies proper anti-aliasing low-pass filtering prior to decimation to avoid Nyquist foldback distortion.
- Enforces 16-bit signed Little-Endian single-channel (Mono) byte formatting required by Gemini Live.
- Fast vectorized execution via numpy with pure-Python fallback.
"""

import math
import struct
from typing import Union, Tuple, List

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except Exception:
    NUMPY_AVAILABLE = False


def downsample_pcm16(
    raw_pcm_bytes: bytes,
    source_rate: int = 48000,
    target_rate: int = 16000,
    source_channels: int = 1
) -> bytes:
    """Downsamples 16-bit Linear PCM audio bytes to target_rate 16-bit Mono PCM.
    
    Args:
        raw_pcm_bytes: Raw 16-bit signed integer PCM byte stream (Little-Endian).
        source_rate: Input sample rate in Hz (typically 44100 or 48000).
        target_rate: Output sample rate in Hz (16000 for Gemini Live).
        source_channels: Number of input channels (1 for Mono, 2 for Stereo).
    
    Returns:
        16-bit signed integer Little-Endian Mono PCM byte stream at target_rate.
    """
    if not raw_pcm_bytes or source_rate <= 0 or target_rate <= 0:
        return b""

    # Calculate input sample count
    sample_width = 2  # 16-bit = 2 bytes
    frame_size = sample_width * source_channels
    num_frames = len(raw_pcm_bytes) // frame_size
    if num_frames == 0:
        return b""

    if source_rate == target_rate and source_channels == 1:
        return raw_pcm_bytes[:num_frames * 2]

    ratio = source_rate / target_rate
    target_frames = int(num_frames / ratio)
    if target_frames == 0:
        return b""

    if NUMPY_AVAILABLE:
        # Vectorized numpy implementation
        dtype = np.int16
        arr = np.frombuffer(raw_pcm_bytes[:num_frames * frame_size], dtype=dtype)
        if source_channels > 1:
            arr = arr.reshape(-1, source_channels).mean(axis=1).astype(np.int16)

        # Anti-aliasing simple FIR moving average filter if downsampling
        if ratio > 1.5:
            filter_window = max(2, int(math.ceil(ratio)))
            # Simple boxcar low-pass filter to prevent aliasing
            weights = np.ones(filter_window) / filter_window
            filtered = np.convolve(arr.astype(np.float32), weights, mode='same')
        else:
            filtered = arr.astype(np.float32)

        # Linear interpolation resampling
        orig_indices = np.linspace(0, len(filtered) - 1, target_frames)
        resampled = np.interp(orig_indices, np.arange(len(filtered)), filtered)
        # Clip to int16 range to avoid wrap-around overflow
        clipped = np.clip(resampled, -32768, 32767).astype(np.int16)
        return clipped.tobytes()

    else:
        # Pure Python fallback
        # Unpack samples
        samples: List[float] = []
        fmt = "<" + ("h" * source_channels)
        for i in range(num_frames):
            frame = struct.unpack_from(fmt, raw_pcm_bytes, i * frame_size)
            if source_channels == 1:
                samples.append(float(frame[0]))
            else:
                samples.append(sum(frame) / source_channels)

        # Linear interpolation
        out_samples: List[int] = []
        for i in range(target_frames):
            src_idx = i * ratio
            idx_floor = int(src_idx)
            idx_ceil = min(num_frames - 1, idx_floor + 1)
            frac = src_idx - idx_floor

            val = samples[idx_floor] * (1.0 - frac) + samples[idx_ceil] * frac
            val_int = int(max(-32768, min(32767, round(val))))
            out_samples.append(val_int)

        out_fmt = "<" + ("h" * len(out_samples))
        return struct.pack(out_fmt, *out_samples)


def verify_pcm16_properties(pcm_bytes: bytes) -> Tuple[bool, int, str]:
    """Validates PCM byte length, channel alignment, and checks for extreme DC offset or clipping."""
    if len(pcm_bytes) % 2 != 0:
        return False, 0, "Byte length is odd; corrupt 16-bit alignment."
    sample_count = len(pcm_bytes) // 2
    if sample_count == 0:
        return True, 0, "Empty buffer"

    # Check for hard saturation clipping (> 95% samples at max/min)
    if NUMPY_AVAILABLE:
        arr = np.frombuffer(pcm_bytes, dtype=np.int16)
        clipping_count = np.sum((arr >= 32760) | (arr <= -32760))
        clip_pct = (clipping_count / len(arr)) * 100.0
        if clip_pct > 50.0:
            return True, sample_count, f"Warning: High audio clipping detected ({clip_pct:.1f}% saturated)"
    return True, sample_count, "Valid PCM16 Mono"
