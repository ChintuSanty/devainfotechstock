"""The recording session: capture -> segment -> transcribe -> store -> broadcast.

One thread per audio channel reads from its device and cuts the stream into
utterances. A single transcription thread drains a shared queue so exactly one
Whisper model is resident no matter how many channels are live. Every finished
utterance is written to SQLite and pushed to the UI as it lands, which is what
makes the transcript appear during the meeting rather than after it.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

import numpy as np

from ..audio.source import AudioSource, open_source
from ..audio.vad import Utterance, UtteranceSegmenter
from ..config import Settings
from ..events import EventBus
from ..speakers import OnlineSpeakerClusterer, SpeakerEmbedder
from ..storage import MeetingStore
from ..stt.engine import WhisperEngine
from .wav import ChannelWavWriter, mix_down

logger = logging.getLogger(__name__)

LEVEL_INTERVAL_S = 0.2

# 'me' is the microphone, 'others' is everything the machine plays back.
CHANNEL_MIC = "me"
CHANNEL_SYSTEM = "others"


class SessionState(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    RECORDING = "recording"
    PAUSED = "paused"
    STOPPING = "stopping"
    ERROR = "error"


@dataclass
class _PendingUtterance:
    channel: str
    utterance: Utterance


@dataclass
class _ChannelRuntime:
    name: str
    kind: str
    source: AudioSource
    segmenter: UtteranceSegmenter
    writer: ChannelWavWriter | None = None
    thread: threading.Thread | None = None
    level: float = 0.0
    error: str | None = None
    wav_path: Path | None = None


@dataclass
class SessionStatus:
    state: str
    meeting_id: str | None = None
    title: str | None = None
    started_at: str | None = None
    elapsed_ms: int = 0
    channels: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    segment_count: int = 0
    pending_utterances: int = 0


class RecordingSession:
    """Owns at most one live recording at a time."""

    def __init__(
        self,
        store: MeetingStore,
        engine: WhisperEngine,
        events: EventBus,
        embedder: SpeakerEmbedder,
    ) -> None:
        self.store = store
        self.engine = engine
        self.events = events
        self.embedder = embedder
        self.clusterer = OnlineSpeakerClusterer()
        self._meeting_dir: Path | None = None

        self._state = SessionState.IDLE
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()

        self._channels: dict[str, _ChannelRuntime] = {}
        self._transcribe_queue: "queue.Queue[_PendingUtterance | None]" = queue.Queue(maxsize=64)
        self._transcriber: threading.Thread | None = None

        self._meeting_id: str | None = None
        self._title: str | None = None
        self._started_at: str | None = None
        self._started_monotonic: float = 0.0
        self._paused_ms: int = 0
        self._pause_started: float | None = None
        self._segment_count = 0
        self._error: str | None = None
        self._settings = Settings()
        self._identify_speakers = False

    # -- introspection ------------------------------------------------------
    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def is_active(self) -> bool:
        return self._state in (SessionState.STARTING, SessionState.RECORDING, SessionState.PAUSED)

    def status(self) -> dict[str, Any]:
        with self._lock:
            status = SessionStatus(
                state=self._state.value,
                meeting_id=self._meeting_id,
                title=self._title,
                started_at=self._started_at,
                elapsed_ms=self._elapsed_ms(),
                error=self._error,
                segment_count=self._segment_count,
                pending_utterances=self._transcribe_queue.qsize(),
                channels={
                    name: {
                        "active": channel.thread is not None and channel.thread.is_alive(),
                        "level": round(channel.level, 4),
                        "error": channel.error,
                        "recording_audio": channel.writer is not None,
                    }
                    for name, channel in self._channels.items()
                },
            )
            return status.__dict__

    def _elapsed_ms(self) -> int:
        if not self._started_monotonic:
            return 0
        paused = self._paused_ms
        if self._pause_started is not None:
            paused += int((time.monotonic() - self._pause_started) * 1000)
        return max(0, int((time.monotonic() - self._started_monotonic) * 1000) - paused)

    # -- lifecycle ----------------------------------------------------------
    def start(self, settings: Settings, title: str | None = None, source_app: str | None = None) -> dict[str, Any]:
        with self._lock:
            if self.is_active:
                raise RuntimeError("A meeting is already being recorded.")
            if not settings.capture_system_audio and not settings.capture_microphone:
                raise RuntimeError("Enable at least one of system audio or microphone capture.")

            self._settings = settings
            self._state = SessionState.STARTING
            self._error = None
            self._segment_count = 0
            self._paused_ms = 0
            self._pause_started = None
            self._stop_event.clear()
            self._pause_event.clear()
            self._channels = {}

            default_title = title or f"Meeting {datetime.now().strftime('%d %b %Y, %H:%M')}"
            meeting = self.store.create_meeting(default_title, source_app)
            self._meeting_id = meeting["id"]
            self._title = meeting["title"]
            self._started_at = meeting["started_at"]
            self._meeting_dir = Path(meeting["folder"])
            self._started_monotonic = time.monotonic()

            # Speaker identification only applies to the meeting-audio channel;
            # the microphone is always the user.
            self._identify_speakers = settings.identify_speakers and settings.capture_system_audio
            self.clusterer = OnlineSpeakerClusterer(
                threshold=settings.speaker_similarity, max_speakers=settings.speaker_max_count
            )
            if self._identify_speakers and not self.embedder.load():
                self._identify_speakers = False
                self.events.publish(
                    "recording_warning",
                    {"message": "Speaker identification is on but no embedding model is installed."},
                )

            try:
                self._open_channels(settings)
            except Exception as exc:
                self._state = SessionState.ERROR
                self._error = str(exc)
                self.store.set_status(meeting["id"], "failed")
                self._close_channels()
                self.events.publish("recording_error", {"message": str(exc)})
                raise

            self._transcriber = threading.Thread(
                target=self._transcribe_loop, name="transcriber", daemon=True
            )
            self._transcriber.start()
            for channel in self._channels.values():
                channel.thread = threading.Thread(
                    target=self._capture_loop, args=(channel,), name=f"capture-{channel.name}", daemon=True
                )
                channel.thread.start()

            self._state = SessionState.RECORDING
            self.events.publish("recording_started", {"meeting": meeting})
            return meeting

    def _open_channels(self, settings: Settings) -> None:
        wanted: list[tuple[str, str, str]] = []
        if settings.capture_system_audio:
            wanted.append((CHANNEL_SYSTEM, "loopback", settings.system_device_id))
        if settings.capture_microphone:
            wanted.append((CHANNEL_MIC, "input", settings.microphone_device_id))

        opened: list[str] = []
        errors: list[str] = []
        for name, kind, device_id in wanted:
            try:
                source = open_source(kind, device_id, settings.sample_rate)
                source.start()
            except Exception as exc:
                logger.warning("Could not open %s channel: %s", name, exc)
                errors.append(f"{name}: {exc}")
                continue

            writer = None
            wav_path = None
            if settings.store_audio and self._meeting_dir is not None:
                wav_path = self._meeting_dir / f"channel-{name}.wav"
                writer = ChannelWavWriter(wav_path, settings.sample_rate)

            self._channels[name] = _ChannelRuntime(
                name=name,
                kind=kind,
                source=source,
                segmenter=UtteranceSegmenter(
                    sample_rate=settings.sample_rate,
                    silence_ms=settings.vad_silence_ms,
                    min_speech_ms=settings.vad_min_speech_ms,
                    max_utterance_ms=settings.max_utterance_ms,
                ),
                writer=writer,
                wav_path=wav_path,
            )
            opened.append(name)

        if not opened:
            raise RuntimeError("No audio channel could be opened. " + " | ".join(errors))
        if errors:
            # Partial success is still a usable recording; surface it, don't fail.
            self.events.publish("recording_warning", {"message": " | ".join(errors)})

    def pause(self) -> None:
        with self._lock:
            if self._state is not SessionState.RECORDING:
                return
            self._pause_event.set()
            self._pause_started = time.monotonic()
            self._state = SessionState.PAUSED
            self.events.publish("recording_paused", {"meeting_id": self._meeting_id})

    def resume(self) -> None:
        with self._lock:
            if self._state is not SessionState.PAUSED:
                return
            if self._pause_started is not None:
                self._paused_ms += int((time.monotonic() - self._pause_started) * 1000)
                self._pause_started = None
            self._pause_event.clear()
            self._state = SessionState.RECORDING
            self.events.publish("recording_resumed", {"meeting_id": self._meeting_id})

    def stop(self) -> dict[str, Any] | None:
        with self._lock:
            if not self.is_active:
                return None
            meeting_id = self._meeting_id
            self._state = SessionState.STOPPING
            self._stop_event.set()
            self._pause_event.clear()

        for channel in list(self._channels.values()):
            if channel.thread is not None:
                channel.thread.join(timeout=5)

        # Flush whatever the segmenters still hold before shutting the queue.
        for channel in self._channels.values():
            trailing = channel.segmenter.flush()
            if trailing is not None and trailing.audio.size:
                self._enqueue(channel.name, trailing)

        self._transcribe_queue.put(None)
        if self._transcriber is not None:
            self._transcriber.join(timeout=120)
            self._transcriber = None

        audio_path = self._finalise_audio(meeting_id)
        self._close_channels()

        duration_ms = self._elapsed_ms()
        with self._lock:
            if meeting_id:
                self.store.finish_meeting(meeting_id, duration_ms, audio_path)
            self._state = SessionState.IDLE
            self._started_monotonic = 0.0
            finished_id, self._meeting_id = meeting_id, None
            self._title = None
            self._meeting_dir = None

        meeting = self.store.get_meeting(finished_id) if finished_id else None
        self.events.publish("recording_stopped", {"meeting": meeting})
        return meeting

    def _finalise_audio(self, meeting_id: str | None) -> str | None:
        paths: list[Path] = []
        for channel in self._channels.values():
            if channel.writer is not None:
                channel.writer.close()
                channel.writer = None
            if channel.wav_path is not None:
                paths.append(channel.wav_path)
        if not meeting_id or not paths or self._meeting_dir is None:
            return None
        try:
            destination = self._meeting_dir / "audio.wav"
            mix_down(paths, destination, self._settings.sample_rate)
            for path in paths:
                path.unlink(missing_ok=True)
            return str(destination)
        except Exception:
            logger.exception("Failed to mix the recording; keeping the per-channel files.")
            return str(paths[0])

    def _close_channels(self) -> None:
        for channel in self._channels.values():
            try:
                channel.source.stop()
            except Exception:
                logger.debug("Error closing %s source", channel.name, exc_info=True)
            if channel.writer is not None:
                channel.writer.close()
                channel.writer = None

    # -- worker loops -------------------------------------------------------
    def _capture_loop(self, channel: _ChannelRuntime) -> None:
        last_level = 0.0
        try:
            while not self._stop_event.is_set():
                chunk = channel.source.read(timeout=0.5)
                if chunk.size == 0:
                    continue
                if self._pause_event.is_set():
                    continue

                channel.level = float(np.sqrt(np.mean(np.square(chunk))))
                now = time.monotonic()
                if now - last_level >= LEVEL_INTERVAL_S:
                    last_level = now
                    self.events.publish(
                        "level", {"channel": channel.name, "level": round(channel.level, 4)}
                    )

                if channel.writer is not None:
                    channel.writer.write(chunk)

                for utterance in channel.segmenter.push(chunk):
                    self._enqueue(channel.name, utterance)
        except Exception as exc:
            channel.error = str(exc)
            logger.exception("Capture loop for %s failed", channel.name)
            self.events.publish("recording_error", {"channel": channel.name, "message": str(exc)})

    def _enqueue(self, channel_name: str, utterance: Utterance) -> None:
        try:
            self._transcribe_queue.put(_PendingUtterance(channel_name, utterance), timeout=5)
        except queue.Full:
            logger.warning("Transcription backlog is full; dropping an utterance on %s", channel_name)
            self.events.publish("transcription_backlog", {"channel": channel_name})

    def _transcribe_loop(self) -> None:
        meeting_id = self._meeting_id
        while True:
            item = self._transcribe_queue.get()
            if item is None:
                return
            if meeting_id is None:
                continue
            try:
                result = self.engine.transcribe(item.utterance.audio)
            except Exception as exc:
                logger.exception("Transcription failed")
                self.events.publish("transcription_error", {"message": str(exc)})
                continue

            text = result.text.strip()
            if not text or _is_noise(text):
                continue

            speaker, embedding = self._identify(item)
            segment = self.store.add_segment(
                meeting_id=meeting_id,
                channel=item.channel,
                start_ms=item.utterance.start_ms,
                end_ms=item.utterance.end_ms,
                text=text,
                confidence=result.confidence,
                speaker=speaker,
                embedding=embedding,
            )
            segment["speaker_name"] = segment["speaker"]
            with self._lock:
                self._segment_count += 1
            self.events.publish("segment", segment)

    def _identify(self, item: _PendingUtterance) -> tuple[str | None, list[float] | None]:
        """Work out which participant just spoke, if speaker ID is enabled."""
        if item.channel == CHANNEL_MIC:
            return None, None
        if not self._identify_speakers:
            return None, None

        duration_ms = item.utterance.end_ms - item.utterance.start_ms
        if duration_ms < self._settings.speaker_min_utterance_ms:
            # Too short to characterise a voice; don't pollute the centroids.
            return None, None

        embedding = self.embedder.embed(item.utterance.audio)
        if embedding is None:
            return None, None

        label = self.clusterer.assign(embedding)
        return label, [float(value) for value in embedding]


# Whisper emits these for music stings, silence and applause.
_NOISE_TOKENS = {
    "you", "thank you.", "thanks for watching!", "[blank_audio]", "[music]",
    "(upbeat music)", "[applause]", ".", "...", "bye.",
}


def _is_noise(text: str) -> bool:
    stripped = text.strip().lower()
    return stripped in _NOISE_TOKENS or len(stripped) < 2
