"""Windows capture via WASAPI (PyAudioWPatch).

PyAudioWPatch is PyAudio plus WASAPI loopback support, which is what lets us
record everything the machine *plays* without a virtual cable, extra drivers or
any per-meeting-app integration. Zoom, Teams, Meet, Webex and Slack all render
through the same endpoint, so one loopback stream covers them all.
"""

from __future__ import annotations

import queue
import threading
from typing import Any

import numpy as np

from ..resample import to_mono
from ..source import AudioSource

_CHUNK_FRAMES = 1024


def _pyaudio() -> Any:
    try:
        import pyaudiowpatch as pyaudio  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - Windows-only path
        raise RuntimeError(
            "PyAudioWPatch is required for system-audio capture on Windows. "
            "Install it with: pip install PyAudioWPatch"
        ) from exc
    return pyaudio


def _default_loopback(pa: Any) -> dict[str, Any]:
    """The loopback endpoint that mirrors the current default speakers."""
    wasapi = pa.get_host_api_info_by_type(_pyaudio().paWASAPI)
    speakers = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
    if speakers.get("isLoopbackDevice"):
        return speakers
    for candidate in pa.get_loopback_device_info_generator():
        if speakers["name"] in candidate["name"]:
            return candidate
    raise RuntimeError(
        "No WASAPI loopback device found for the default speakers. "
        "Check that an output device is enabled in Windows sound settings."
    )


def list_wasapi_devices() -> dict[str, list[dict[str, Any]]]:
    pyaudio = _pyaudio()
    pa = pyaudio.PyAudio()
    try:
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_output = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        default_input = pa.get_device_info_by_index(wasapi["defaultInputDevice"])

        loopback = [
            {
                "id": str(info["index"]),
                "name": info["name"],
                "kind": "loopback",
                "channels": int(info["maxInputChannels"]),
                "sample_rate": int(info["defaultSampleRate"]),
                "is_default": default_output["name"] in info["name"],
            }
            for info in pa.get_loopback_device_info_generator()
        ]

        inputs = []
        for index in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(index)
            if info.get("maxInputChannels", 0) <= 0 or info.get("isLoopbackDevice"):
                continue
            inputs.append(
                {
                    "id": str(index),
                    "name": info["name"],
                    "kind": "input",
                    "channels": int(info["maxInputChannels"]),
                    "sample_rate": int(info["defaultSampleRate"]),
                    "is_default": index == default_input["index"],
                }
            )
        return {"loopback": loopback, "input": inputs}
    finally:
        pa.terminate()


class WasapiSource(AudioSource):
    def __init__(self, kind: str, device_id: str, target_rate: int) -> None:
        super().__init__(target_rate)
        self.kind = kind
        self.device_id = device_id
        self._queue: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=256)
        self._pa: Any = None
        self._stream: Any = None
        self._native_rate = target_rate
        self._channels = 1
        self._overflow_lock = threading.Lock()
        self._dropped_chunks = 0

    @property
    def native_rate(self) -> int:
        return self._native_rate

    @property
    def dropped_chunks(self) -> int:
        with self._overflow_lock:
            return self._dropped_chunks

    def start(self) -> None:
        pyaudio = _pyaudio()
        self._pa = pyaudio.PyAudio()

        if self.device_id:
            info = self._pa.get_device_info_by_index(int(self.device_id))
        elif self.kind == "loopback":
            info = _default_loopback(self._pa)
        else:
            info = self._pa.get_default_input_device_info()

        self._native_rate = int(info["defaultSampleRate"])
        self._channels = max(1, int(info["maxInputChannels"]))

        self._stream = self._pa.open(
            format=pyaudio.paFloat32,
            channels=self._channels,
            rate=self._native_rate,
            input=True,
            frames_per_buffer=_CHUNK_FRAMES,
            input_device_index=int(info["index"]),
            stream_callback=self._callback,
        )
        self._stream.start_stream()

    def _callback(self, in_data: bytes, _frame_count: int, _time_info: Any, _status: int) -> tuple[None, int]:
        samples = np.frombuffer(in_data, dtype=np.float32)
        try:
            self._queue.put_nowait(samples)
        except queue.Full:
            # Never block the audio thread; a dropped chunk beats a stalled stream.
            with self._overflow_lock:
                self._dropped_chunks += 1
        return (None, _pyaudio().paContinue)

    def read(self, timeout: float = 1.0) -> np.ndarray:
        try:
            raw = self._queue.get(timeout=timeout)
        except queue.Empty:
            return np.zeros(0, dtype=np.float32)
        return self._convert(to_mono(raw, self._channels))

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            finally:
                self._stream = None
        if self._pa is not None:
            self._pa.terminate()
            self._pa = None
