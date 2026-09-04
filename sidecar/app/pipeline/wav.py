"""Optional WAV capture.

Off by default: MeetingScribe keeps transcripts, not recordings. When the user
turns it on, each channel is written separately and mixed down on stop so the
saved file matches what a listener would have heard.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np


class ChannelWavWriter:
    def __init__(self, path: Path, sample_rate: int) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._wave = wave.open(str(path), "wb")
        self._wave.setnchannels(1)
        self._wave.setsampwidth(2)
        self._wave.setframerate(sample_rate)

    def write(self, samples: np.ndarray) -> None:
        if samples.size == 0:
            return
        clipped = np.clip(samples, -1.0, 1.0)
        self._wave.writeframes((clipped * 32767.0).astype(np.int16).tobytes())

    def close(self) -> None:
        try:
            self._wave.close()
        except Exception:
            pass


def mix_down(sources: list[Path], destination: Path, sample_rate: int) -> Path:
    """Sum mono WAV files into one, padding the shorter ones with silence."""
    tracks: list[np.ndarray] = []
    for source in sources:
        if not source.exists():
            continue
        with wave.open(str(source), "rb") as handle:
            frames = handle.readframes(handle.getnframes())
        tracks.append(np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32767.0)

    if not tracks:
        raise FileNotFoundError("No channel recordings to mix.")

    length = max(track.size for track in tracks)
    mixed = np.zeros(length, dtype=np.float32)
    for track in tracks:
        mixed[: track.size] += track
    peak = float(np.max(np.abs(mixed))) if mixed.size else 0.0
    if peak > 1.0:
        mixed /= peak

    destination.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(destination), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        out.writeframes((mixed * 32767.0).astype(np.int16).tobytes())
    return destination
