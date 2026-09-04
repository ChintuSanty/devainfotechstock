"""Speech to text with faster-whisper.

faster-whisper is a CTranslate2 reimplementation of Whisper: roughly 4x the
speed of the reference implementation at a quarter of the memory, and it runs
int8 on plain CPUs. That is what makes on-device transcription cheap enough to
run for a whole meeting without a GPU.

Model sizing guidance (CPU, int8):
    tiny.en   ~0.10x realtime, rough accuracy      - very old laptops
    base.en   ~0.15x realtime, usable              - low-end machines
    small.en  ~0.35x realtime, good                - the default
    medium.en ~1.0x realtime, very good            - needs a fast CPU or GPU
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class TranscriptionResult:
    text: str
    confidence: float | None
    language: str | None


class WhisperEngine:
    """Lazily loaded, thread-safe wrapper around a single Whisper model."""

    def __init__(
        self,
        model_size: str = "small.en",
        device: str = "auto",
        compute_type: str = "auto",
        language: str = "en",
        beam_size: int = 1,
    ) -> None:
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self.beam_size = beam_size
        self._model: Any = None
        self._lock = threading.Lock()

    # -- lifecycle ----------------------------------------------------------
    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def load(self) -> None:
        """Load the model, downloading it on first use. Safe to call twice."""
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            try:
                from faster_whisper import WhisperModel  # type: ignore[import-not-found]
            except ImportError as exc:
                raise RuntimeError(
                    "faster-whisper is not installed. Run: pip install -r sidecar/requirements.txt"
                ) from exc

            device, compute_type = self._resolve_device()
            logger.info(
                "Loading Whisper model %s on %s (%s)", self.model_size, device, compute_type
            )
            self._model = WhisperModel(
                self.model_size, device=device, compute_type=compute_type
            )

    def unload(self) -> None:
        with self._lock:
            self._model = None

    def _resolve_device(self) -> tuple[str, str]:
        device = self.device
        if device == "auto":
            device = "cuda" if _cuda_available() else "cpu"
        compute_type = self.compute_type
        if compute_type == "auto":
            compute_type = "float16" if device == "cuda" else "int8"
        return device, compute_type

    # -- inference ----------------------------------------------------------
    def transcribe(self, audio: np.ndarray, prompt: str | None = None) -> TranscriptionResult:
        """Transcribe one mono 16 kHz float32 utterance."""
        if audio.size == 0:
            return TranscriptionResult(text="", confidence=None, language=None)
        self.load()
        assert self._model is not None

        # CTranslate2 handles its own threading, but one caller at a time keeps
        # memory predictable when both channels finish an utterance together.
        with self._lock:
            segments, info = self._model.transcribe(
                audio.astype(np.float32, copy=False),
                language=self.language or None,
                beam_size=self.beam_size,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 400},
                condition_on_previous_text=False,
                initial_prompt=prompt,
            )
            collected = list(segments)

        text = " ".join(segment.text.strip() for segment in collected).strip()
        logprobs = [segment.avg_logprob for segment in collected if segment.avg_logprob is not None]
        confidence = float(np.exp(np.mean(logprobs))) if logprobs else None
        return TranscriptionResult(
            text=text, confidence=confidence, language=getattr(info, "language", None)
        )


def _cuda_available() -> bool:
    try:
        import ctranslate2  # type: ignore[import-not-found]

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:  # pragma: no cover - depends on the host
        return False
