"""Voice fingerprints for telling participants apart.

Whisper transcribes what was said, not who said it. The microphone channel is
always the user, but the meeting-audio channel is everyone else mixed together,
so separating individuals needs a speaker-embedding model: a fixed-length
vector per utterance where the same voice lands in the same region of the space.

Both backends are optional extras (they pull in PyTorch, ~2 GB). With neither
installed, everyone in the meeting audio stays labelled "Participant" and the
rest of the app is unaffected.

    speechbrain  ECAPA-TDNN, 192-d - the better separator, ~80 MB of weights
    resemblyzer  a small GRU encoder, 256-d - lighter and faster to load
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

MIN_SAMPLES = 8_000  # 0.5 s at 16 kHz; shorter clips give unstable vectors


def _speechbrain_available() -> bool:
    try:
        import speechbrain  # type: ignore[import-not-found]  # noqa: F401

        return True
    except ImportError:
        return False


def _resemblyzer_available() -> bool:
    try:
        import resemblyzer  # type: ignore[import-not-found]  # noqa: F401

        return True
    except ImportError:
        return False


def embedder_status(preferred: str = "auto") -> dict[str, Any]:
    """What the UI shows in Settings, without loading anything."""
    speechbrain = _speechbrain_available()
    resemblyzer = _resemblyzer_available()
    backend = None
    if preferred == "speechbrain" and speechbrain:
        backend = "speechbrain"
    elif preferred == "resemblyzer" and resemblyzer:
        backend = "resemblyzer"
    elif preferred == "auto":
        backend = "speechbrain" if speechbrain else ("resemblyzer" if resemblyzer else None)
    return {
        "available": backend is not None,
        "backend": backend,
        "installed": {"speechbrain": speechbrain, "resemblyzer": resemblyzer},
        "install_hint": "pip install -r sidecar/requirements-speakers.txt",
    }


class SpeakerEmbedder:
    """Lazily loaded, thread-safe wrapper around whichever backend is present."""

    def __init__(self, preferred: str = "auto") -> None:
        self.preferred = preferred
        self._backend: str | None = None
        self._model: Any = None
        self._lock = threading.Lock()
        self._failed = False

    @property
    def backend(self) -> str | None:
        return self._backend

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def unload(self) -> None:
        with self._lock:
            self._model = None
            self._backend = None
            self._failed = False

    def load(self) -> bool:
        """Return True once a backend is ready. Never raises."""
        if self._model is not None:
            return True
        if self._failed:
            return False
        with self._lock:
            if self._model is not None:
                return True
            status = embedder_status(self.preferred)
            backend = status["backend"]
            if backend is None:
                self._failed = True
                return False
            try:
                if backend == "speechbrain":
                    from speechbrain.inference import EncoderClassifier  # type: ignore[import-not-found]

                    self._model = EncoderClassifier.from_hparams(
                        source="speechbrain/spkrec-ecapa-voxceleb",
                        savedir="~/.cache/meetingscribe/ecapa",
                    )
                else:
                    from resemblyzer import VoiceEncoder  # type: ignore[import-not-found]

                    self._model = VoiceEncoder()
                self._backend = backend
                logger.info("Speaker embedding backend: %s", backend)
                return True
            except Exception:
                logger.exception("Could not load the speaker embedding model")
                self._failed = True
                return False

    def embed(self, audio: np.ndarray) -> np.ndarray | None:
        """One L2-normalised vector for a mono 16 kHz utterance, or None."""
        if audio.size < MIN_SAMPLES or not self.load():
            return None
        try:
            with self._lock:
                if self._backend == "speechbrain":
                    import torch  # type: ignore[import-not-found]

                    tensor = torch.from_numpy(audio.astype(np.float32, copy=False)).unsqueeze(0)
                    vector = self._model.encode_batch(tensor).squeeze().detach().cpu().numpy()
                else:
                    vector = np.asarray(self._model.embed_utterance(audio.astype(np.float32, copy=False)))
        except Exception:
            logger.exception("Speaker embedding failed for one utterance")
            return None

        vector = np.asarray(vector, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(vector))
        if not np.isfinite(norm) or norm == 0.0:
            return None
        return vector / norm
