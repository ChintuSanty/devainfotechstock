"""PortAudio backend (sounddevice).

Used on Linux/macOS during development. On Linux a PipeWire or PulseAudio
"monitor" source is an ordinary input device, so selecting it gives the same
system-audio loopback that WASAPI provides on Windows.
"""

from __future__ import annotations

import queue
from typing import Any

import numpy as np

from ..source import AudioSource

_BLOCK_FRAMES = 1024
_MONITOR_HINTS = ("monitor", "loopback", "stereo mix", "what u hear")


def _sounddevice() -> Any:
    try:
        import sounddevice as sd  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "sounddevice is required for audio capture on this platform. "
            "Install it with: pip install sounddevice"
        ) from exc
    return sd


def _looks_like_monitor(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in _MONITOR_HINTS)


def list_portaudio_devices() -> dict[str, list[dict[str, Any]]]:
    sd = _sounddevice()
    default_input = sd.default.device[0] if isinstance(sd.default.device, (list, tuple)) else None
    loopback: list[dict[str, Any]] = []
    inputs: list[dict[str, Any]] = []
    for index, info in enumerate(sd.query_devices()):
        if info["max_input_channels"] <= 0:
            continue
        entry = {
            "id": str(index),
            "name": info["name"],
            "kind": "loopback" if _looks_like_monitor(info["name"]) else "input",
            "channels": int(info["max_input_channels"]),
            "sample_rate": int(info["default_samplerate"]),
            "is_default": index == default_input,
        }
        (loopback if entry["kind"] == "loopback" else inputs).append(entry)
    return {"loopback": loopback, "input": inputs}


class PortAudioSource(AudioSource):
    def __init__(self, kind: str, device_id: str, target_rate: int) -> None:
        super().__init__(target_rate)
        self.kind = kind
        self.device_id = device_id
        self._queue: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=256)
        self._stream: Any = None
        self._native_rate = target_rate
        self._dropped_chunks = 0

    @property
    def native_rate(self) -> int:
        return self._native_rate

    @property
    def dropped_chunks(self) -> int:
        return self._dropped_chunks

    def _resolve_device(self) -> int:
        sd = _sounddevice()
        if self.device_id:
            return int(self.device_id)
        if self.kind == "loopback":
            for index, info in enumerate(sd.query_devices()):
                if info["max_input_channels"] > 0 and _looks_like_monitor(info["name"]):
                    return index
            raise RuntimeError(
                "No monitor/loopback input found. On Linux, enable the PipeWire or "
                "PulseAudio monitor source for your output device."
            )
        default_input = sd.default.device[0] if isinstance(sd.default.device, (list, tuple)) else None
        if default_input is None:
            raise RuntimeError("No default input device available.")
        return int(default_input)

    def start(self) -> None:
        sd = _sounddevice()
        index = self._resolve_device()
        info = sd.query_devices(index)
        self._native_rate = int(info["default_samplerate"])

        def callback(indata: np.ndarray, _frames: int, _time: Any, _status: Any) -> None:
            try:
                self._queue.put_nowait(indata.copy().mean(axis=1).astype(np.float32))
            except queue.Full:
                self._dropped_chunks += 1

        self._stream = sd.InputStream(
            device=index,
            channels=min(2, int(info["max_input_channels"])),
            samplerate=self._native_rate,
            blocksize=_BLOCK_FRAMES,
            dtype="float32",
            callback=callback,
        )
        self._stream.start()

    def read(self, timeout: float = 1.0) -> np.ndarray:
        try:
            mono = self._queue.get(timeout=timeout)
        except queue.Empty:
            return np.zeros(0, dtype=np.float32)
        return self._convert(mono)

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            finally:
                self._stream = None
