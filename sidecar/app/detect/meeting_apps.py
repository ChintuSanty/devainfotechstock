"""Notice when a meeting app is running.

Capture itself is app-agnostic - the WASAPI loopback picks up whatever is
playing - so this exists only to label the meeting and to offer a one-click
"start recording?" nudge on the overlay.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

logger = logging.getLogger(__name__)

# process name -> friendly label
KNOWN_APPS: dict[str, str] = {
    "zoom.exe": "Zoom",
    "teams.exe": "Microsoft Teams",
    "ms-teams.exe": "Microsoft Teams",
    "webex.exe": "Webex",
    "webexmta.exe": "Webex",
    "slack.exe": "Slack",
    "discord.exe": "Discord",
    "gotomeeting.exe": "GoToMeeting",
    "bluejeans.exe": "BlueJeans",
    "skype.exe": "Skype",
    "chime.exe": "Amazon Chime",
    "zoom": "Zoom",
    "teams": "Microsoft Teams",
    "slack": "Slack",
    "discord": "Discord",
}

# Browser meetings (Meet, Teams web, Zoom web) show up as a browser process, so
# they are matched on window/tab title instead.
BROWSER_PROCESSES = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "chrome", "firefox"}
BROWSER_TITLE_HINTS = {
    "meet.google.com": "Google Meet",
    "google meet": "Google Meet",
    "teams.microsoft.com": "Microsoft Teams (web)",
    "zoom.us": "Zoom (web)",
    "whereby.com": "Whereby",
    "meet.jit.si": "Jitsi",
}


def detect_meeting_apps() -> list[dict[str, str]]:
    """Return the meeting apps currently running, best effort."""
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        return []

    found: dict[str, dict[str, str]] = {}
    for process in psutil.process_iter(["name"]):
        try:
            raw = (process.info.get("name") or "").lower()
        except Exception:
            continue
        label = KNOWN_APPS.get(raw)
        if label and label not in found:
            found[label] = {"app": label, "process": raw, "source": "process"}

    for label, entry in _detect_browser_meetings().items():
        found.setdefault(label, entry)
    return list(found.values())


def _detect_browser_meetings() -> dict[str, dict[str, str]]:
    """Windows-only: read top-level window titles for browser-based meetings."""
    results: dict[str, dict[str, str]] = {}
    try:
        import ctypes
        from ctypes import wintypes
    except (ImportError, ValueError):  # pragma: no cover - non-Windows
        return results
    if not hasattr(ctypes, "windll"):  # pragma: no cover - non-Windows
        return results

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    titles: list[str] = []

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def collect(hwnd: int, _param: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        titles.append(buffer.value.lower())
        return True

    try:
        user32.EnumWindows(WNDENUMPROC(collect), 0)
    except Exception:  # pragma: no cover - defensive
        return results

    for title in titles:
        for hint, label in BROWSER_TITLE_HINTS.items():
            if hint in title:
                results[label] = {"app": label, "process": "browser", "source": "window-title"}
    return results


class MeetingAppWatcher:
    """Polls for meeting apps and reports when the set changes."""

    def __init__(self, on_change: Callable[[list[dict[str, str]]], None], interval: float = 8.0) -> None:
        self.on_change = on_change
        self.interval = interval
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last: list[dict[str, str]] = []

    @property
    def current(self) -> list[dict[str, str]]:
        return list(self._last)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="meeting-app-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                apps = detect_meeting_apps()
                if {a["app"] for a in apps} != {a["app"] for a in self._last}:
                    self._last = apps
                    self.on_change(apps)
            except Exception:  # pragma: no cover - a watcher must never crash the app
                logger.exception("Meeting app detection failed")
            self._stop.wait(self.interval)
