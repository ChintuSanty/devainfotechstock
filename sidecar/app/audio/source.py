"""Backend-agnostic audio source."""

from __future__ import annotations

import platform
from abc import ABC, abstractmethod

import numpy as np

from .resample import Resampler


class AudioSource(ABC):
    """A running capture stream that yields mono float32 at `target_rate`."""

    def __init__(self, target_rate: int) -> None:
        self.target_rate = target_rate
        self._resampler: Resampler | None = None

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def read(self, timeout: float = 1.0) -> np.ndarray:
        """Block for the next chunk. Returns an empty array on timeout."""

    @property
    @abstractmethod
    def native_rate(self) -> int: ...

    def _convert(self, mono_native: np.ndarray) -> np.ndarray:
        if self._resampler is None:
            self._resampler = Resampler(self.native_rate, self.target_rate)
        return self._resampler.process(mono_native)

    def __enter__(self) -> "AudioSource":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()


def open_source(kind: str, device_id: str, target_rate: int) -> AudioSource:
    """Build the right backend for this OS.

    `kind` is 'loopback' (system sound) or 'input' (microphone).
    """
    if platform.system() == "Windows":
        from .backends.windows_wasapi import WasapiSource

        return WasapiSource(kind=kind, device_id=device_id, target_rate=target_rate)

    # Linux/macOS: used for development. On Linux a PipeWire/PulseAudio monitor
    # source shows up as a normal input, which gives us loopback for free.
    from .backends.portaudio import PortAudioSource

    return PortAudioSource(kind=kind, device_id=device_id, target_rate=target_rate)
