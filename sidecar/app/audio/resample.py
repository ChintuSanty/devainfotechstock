"""Streaming sample-rate conversion.

Capture devices hand us 44.1/48 kHz; Whisper wants 16 kHz mono. Rather than
pull in SciPy we build a windowed-sinc low-pass once and run it with a carried
tail so chunk boundaries stay clickless.
"""

from __future__ import annotations

import math

import numpy as np


class Resampler:
    def __init__(self, src_rate: int, dst_rate: int, taps: int = 129) -> None:
        if src_rate <= 0 or dst_rate <= 0:
            raise ValueError("sample rates must be positive")
        self.src_rate = src_rate
        self.dst_rate = dst_rate
        self.passthrough = src_rate == dst_rate
        self._tail = np.zeros(0, dtype=np.float32)
        self._phase = 0.0
        if not self.passthrough:
            cutoff = 0.5 * min(dst_rate / src_rate, 1.0)
            self._kernel = _lowpass_kernel(cutoff, taps)
            self._filter_state = np.zeros(len(self._kernel) - 1, dtype=np.float32)

    def process(self, samples: np.ndarray) -> np.ndarray:
        """Feed a mono float32 chunk, get the resampled chunk back."""
        if samples.size == 0:
            return samples.astype(np.float32, copy=False)
        mono = samples.astype(np.float32, copy=False)
        if self.passthrough:
            return mono

        # Anti-alias, keeping the filter's memory across calls.
        padded = np.concatenate([self._filter_state, mono])
        filtered = np.convolve(padded, self._kernel, mode="valid").astype(np.float32)
        self._filter_state = padded[-(len(self._kernel) - 1):]

        # Linear interpolation on a continuous phase so no sample is lost or
        # repeated at chunk edges.
        step = self.src_rate / self.dst_rate
        work = np.concatenate([self._tail, filtered])
        if work.size < 2:
            self._tail = work
            return np.zeros(0, dtype=np.float32)

        count = int(math.floor((work.size - 1 - self._phase) / step)) + 1
        if count <= 0:
            self._tail = work
            return np.zeros(0, dtype=np.float32)

        positions = self._phase + step * np.arange(count, dtype=np.float64)
        left = positions.astype(np.int64)
        frac = (positions - left).astype(np.float32)
        out = work[left] * (1.0 - frac) + work[left + 1] * frac

        consumed = int(left[-1])
        self._tail = work[consumed:]
        self._phase = float(positions[-1] - consumed + step)
        return out.astype(np.float32, copy=False)


def _lowpass_kernel(cutoff: float, taps: int) -> np.ndarray:
    if taps % 2 == 0:
        taps += 1
    n = np.arange(taps, dtype=np.float64) - (taps - 1) / 2
    sinc = np.where(n == 0, 2 * cutoff, np.sin(2 * math.pi * cutoff * n) / (math.pi * np.where(n == 0, 1, n)))
    kernel = sinc * np.hamming(taps)
    kernel /= np.sum(kernel)
    return kernel.astype(np.float32)


def to_mono(frames: np.ndarray, channels: int) -> np.ndarray:
    """Average interleaved channels down to mono float32."""
    if channels <= 1:
        return frames.astype(np.float32, copy=False).reshape(-1)
    usable = (frames.size // channels) * channels
    return frames[:usable].astype(np.float32, copy=False).reshape(-1, channels).mean(axis=1)
