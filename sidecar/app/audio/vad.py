"""Energy-based utterance segmentation.

Deliberately dependency-free: an adaptive noise floor plus hangover timing is
enough to cut a stream into utterances. Whisper still runs its own VAD inside
each chunk, so the only job here is finding sensible boundaries cheaply.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

FRAME_MS = 30


@dataclass
class Utterance:
    audio: np.ndarray
    start_ms: int
    end_ms: int


class UtteranceSegmenter:
    def __init__(
        self,
        sample_rate: int,
        silence_ms: int = 700,
        min_speech_ms: int = 250,
        max_utterance_ms: int = 20_000,
        pre_roll_ms: int = 300,
    ) -> None:
        self.sample_rate = sample_rate
        self.frame_size = int(sample_rate * FRAME_MS / 1000)
        self.silence_frames = max(1, silence_ms // FRAME_MS)
        self.min_speech_frames = max(1, min_speech_ms // FRAME_MS)
        self.max_frames = max(self.min_speech_frames, max_utterance_ms // FRAME_MS)
        self.pre_roll_frames = max(0, pre_roll_ms // FRAME_MS)

        self._pending = np.zeros(0, dtype=np.float32)
        self._frames: list[np.ndarray] = []
        self._pre_roll: list[np.ndarray] = []
        self._speech_frames = 0
        self._silence_run = 0
        self._in_speech = False
        self._noise_floor = 1e-4
        self._frames_seen = 0
        self._utterance_start_frame = 0

    @property
    def position_ms(self) -> int:
        return self._frames_seen * FRAME_MS

    def push(self, samples: np.ndarray) -> list[Utterance]:
        """Add mono float32 audio; return any utterances that just closed."""
        self._pending = np.concatenate([self._pending, samples.astype(np.float32, copy=False)])
        finished: list[Utterance] = []

        while self._pending.size >= self.frame_size:
            frame = self._pending[: self.frame_size]
            self._pending = self._pending[self.frame_size :]
            utterance = self._consume_frame(frame)
            if utterance is not None:
                finished.append(utterance)
        return finished

    def flush(self) -> Utterance | None:
        """Close whatever is open - called when recording stops."""
        if self._pending.size:
            self._frames.append(np.pad(self._pending, (0, self.frame_size - self._pending.size)))
            self._pending = np.zeros(0, dtype=np.float32)
        if self._in_speech and self._speech_frames >= self.min_speech_frames:
            return self._close()
        self._reset()
        return None

    # -- internals ----------------------------------------------------------
    def _consume_frame(self, frame: np.ndarray) -> Utterance | None:
        self._frames_seen += 1
        energy = float(np.sqrt(np.mean(np.square(frame))) + 1e-12)

        # Track the quiet baseline; speech is a healthy multiple above it.
        if not self._in_speech:
            self._noise_floor = 0.95 * self._noise_floor + 0.05 * energy
        threshold = max(self._noise_floor * 3.0, 0.004)
        is_speech = energy > threshold

        if is_speech:
            if not self._in_speech:
                self._in_speech = True
                self._frames = list(self._pre_roll)
                self._utterance_start_frame = self._frames_seen - len(self._frames)
                self._pre_roll = []
            self._frames.append(frame)
            self._speech_frames += 1
            self._silence_run = 0
            if len(self._frames) >= self.max_frames:
                return self._close()
            return None

        if self._in_speech:
            self._frames.append(frame)
            self._silence_run += 1
            if self._silence_run >= self.silence_frames:
                if self._speech_frames >= self.min_speech_frames:
                    return self._close()
                self._reset()
            return None

        self._pre_roll.append(frame)
        if len(self._pre_roll) > self.pre_roll_frames:
            self._pre_roll.pop(0)
        return None

    def _close(self) -> Utterance:
        audio = np.concatenate(self._frames) if self._frames else np.zeros(0, dtype=np.float32)
        start_ms = self._utterance_start_frame * FRAME_MS
        end_ms = start_ms + int(audio.size / self.sample_rate * 1000)
        self._reset()
        return Utterance(audio=audio, start_ms=start_ms, end_ms=end_ms)

    def _reset(self) -> None:
        self._frames = []
        self._pre_roll = []
        self._speech_frames = 0
        self._silence_run = 0
        self._in_speech = False
