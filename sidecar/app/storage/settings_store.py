"""Settings persisted as JSON in the app directory.

Deliberately not inside the meeting store: the meeting store's location is
itself a setting, so it has to be readable before that location is known.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from ..config import Settings

logger = logging.getLogger(__name__)


class SettingsStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def load(self) -> Settings:
        settings = Settings()
        if not self.path.exists():
            return settings
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("settings.json is unreadable; falling back to defaults.")
            return settings
        if not isinstance(stored, dict):
            return settings

        allowed = set(Settings.field_names())
        for key, value in stored.items():
            if key not in allowed:
                continue
            try:
                setattr(settings, key, Settings.coerce(key, value))
            except (TypeError, ValueError):
                continue
        return settings

    def save(self, updates: dict[str, Any]) -> Settings:
        with self._lock:
            settings = self.load()
            allowed = set(Settings.field_names())
            for key, value in updates.items():
                if key not in allowed:
                    continue
                try:
                    setattr(settings, key, Settings.coerce(key, value))
                except (TypeError, ValueError):
                    continue
            self._write(settings)
            return settings

    def reset(self) -> Settings:
        with self._lock:
            settings = Settings()
            self._write(settings)
            return settings

    def _write(self, settings: Settings) -> None:
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(settings.to_dict(), indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)
