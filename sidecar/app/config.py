"""Runtime configuration.

Everything the user can tune lives here. Settings are persisted as JSON in the
app directory - separate from the meeting store, because the meeting store's
location is itself a setting. Environment variables win at startup, which is
how the packaged app points the sidecar at Electron's userData directory.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any


def default_data_dir() -> Path:
    """App directory: settings, logs and the default meeting store live here.

    Electron overrides it with its userData path.
    """
    override = os.environ.get("MEETINGSCRIBE_DATA_DIR")
    if override:
        return Path(override)
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif os.uname().sysname == "Darwin":  # pragma: no cover - not a target platform
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "MeetingScribe" / "data"


@dataclass
class Settings:
    """User-visible settings. Field names double as the settings-table keys."""

    # --- Speech to text -------------------------------------------------
    # tiny.en / base.en / small.en / medium.en / large-v3. small.en is the
    # sweet spot on CPU: near-medium accuracy at a fraction of the cost.
    stt_model: str = "small.en"
    stt_device: str = "auto"  # auto | cpu | cuda
    stt_compute_type: str = "auto"  # auto -> int8 on CPU, float16 on CUDA
    stt_language: str = "en"
    stt_beam_size: int = 1  # greedy; 3-5 is slower for marginal gains

    # --- Audio capture --------------------------------------------------
    capture_system_audio: bool = True
    capture_microphone: bool = True
    system_device_id: str = ""  # "" -> default output loopback
    microphone_device_id: str = ""  # "" -> default input
    sample_rate: int = 16_000
    vad_silence_ms: int = 700  # silence that closes an utterance
    vad_min_speech_ms: int = 250
    max_utterance_ms: int = 20_000  # hard flush so long monologues still stream

    # --- Local LLM ------------------------------------------------------
    llm_provider: str = "ollama"  # ollama | openai_compatible
    llm_base_url: str = "http://127.0.0.1:11434"
    llm_model: str = "qwen3:4b"
    llm_api_key: str = ""  # only used by openai_compatible endpoints
    llm_temperature: float = 0.2
    llm_context_chars: int = 12_000  # per map-reduce chunk
    llm_timeout_seconds: int = 300

    # --- Speaker identification -----------------------------------------
    # Splits the meeting-audio channel into individual people. Needs one of the
    # optional embedding models (see requirements-speakers.txt); when none is
    # installed everyone stays labelled "Participant".
    identify_speakers: bool = False
    speaker_similarity: float = 0.62  # cosine similarity to join an existing voice
    speaker_max_count: int = 12
    speaker_min_utterance_ms: int = 700  # too short to characterise a voice
    speaker_embedding_model: str = "auto"  # auto | speechbrain | resemblyzer

    # --- Storage ---------------------------------------------------------
    # Where meeting folders are written. Empty means "meetings" inside the app
    # directory. Meetings are plain text: JSON metadata, Markdown transcripts.
    storage_dir: str = ""

    # --- Privacy / retention -------------------------------------------
    store_audio: bool = False  # off by default: transcripts only
    retention_days: int = 0  # 0 = keep forever
    auto_summarise_on_stop: bool = True

    # --- Behaviour ------------------------------------------------------
    auto_detect_meeting_apps: bool = True
    overlay_always_on_top: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def field_names(cls) -> list[str]:
        return [f.name for f in fields(cls)]

    @classmethod
    def coerce(cls, key: str, raw: Any) -> Any:
        """Cast a stored string back to the field's declared type."""
        types = {f.name: f.type for f in fields(cls)}
        declared = types.get(key)
        if declared in (bool, "bool"):
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in {"1", "true", "yes", "on"}
        if declared in (int, "int"):
            return int(raw)
        if declared in (float, "float"):
            return float(raw)
        return str(raw)


@dataclass
class AppConfig:
    data_dir: Path = field(default_factory=default_data_dir)
    host: str = "127.0.0.1"
    port: int = 0  # 0 -> the OS picks a free port

    @property
    def settings_path(self) -> Path:
        return self.data_dir / "settings.json"

    @property
    def default_storage_dir(self) -> Path:
        return self.data_dir / "meetings"

    def resolve_storage_dir(self, settings: Settings) -> Path:
        chosen = (settings.storage_dir or "").strip()
        return Path(chosen).expanduser() if chosen else self.default_storage_dir

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
