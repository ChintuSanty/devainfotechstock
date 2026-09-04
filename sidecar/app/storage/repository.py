"""Data access for meetings, transcript segments, artifacts and settings."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from ..config import Settings
from .database import Database


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_meeting_id() -> str:
    return uuid.uuid4().hex


class MeetingRepository:
    def __init__(self, db: Database, audio_dir: Path) -> None:
        self.db = db
        self.audio_dir = audio_dir

    # -- meetings -----------------------------------------------------------
    def create_meeting(self, title: str, source_app: str | None = None) -> dict[str, Any]:
        meeting_id = new_meeting_id()
        self.db.execute(
            "INSERT INTO meetings (id, title, started_at, status, source_app) VALUES (?, ?, ?, 'recording', ?)",
            (meeting_id, title, _now(), source_app),
        )
        meeting = self.get_meeting(meeting_id)
        assert meeting is not None
        return meeting

    def finish_meeting(self, meeting_id: str, duration_ms: int, audio_path: str | None) -> None:
        self.db.execute(
            "UPDATE meetings SET ended_at = ?, status = 'recorded', duration_ms = ?, audio_path = ? WHERE id = ?",
            (_now(), duration_ms, audio_path, meeting_id),
        )

    def set_status(self, meeting_id: str, status: str) -> None:
        self.db.execute("UPDATE meetings SET status = ? WHERE id = ?", (status, meeting_id))

    def rename_meeting(self, meeting_id: str, title: str) -> None:
        self.db.execute("UPDATE meetings SET title = ? WHERE id = ?", (title, meeting_id))

    def get_meeting(self, meeting_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM meetings WHERE id = ?", (meeting_id,))
        return dict(row) if row else None

    def list_meetings(self, limit: int = 100, offset: int = 0, search: str = "") -> list[dict[str, Any]]:
        if search:
            pattern = f"%{search}%"
            rows = self.db.query(
                """
                SELECT m.*, (SELECT COUNT(*) FROM segments s WHERE s.meeting_id = m.id) AS segment_count
                FROM meetings m
                WHERE m.title LIKE ?
                   OR EXISTS (SELECT 1 FROM segments s WHERE s.meeting_id = m.id AND s.text LIKE ?)
                ORDER BY m.started_at DESC LIMIT ? OFFSET ?
                """,
                (pattern, pattern, limit, offset),
            )
        else:
            rows = self.db.query(
                """
                SELECT m.*, (SELECT COUNT(*) FROM segments s WHERE s.meeting_id = m.id) AS segment_count
                FROM meetings m ORDER BY m.started_at DESC LIMIT ? OFFSET ?
                """,
                (limit, offset),
            )
        return [dict(r) for r in rows]

    # -- segments -----------------------------------------------------------
    def add_segment(
        self,
        meeting_id: str,
        channel: str,
        start_ms: int,
        end_ms: int,
        text: str,
        confidence: float | None = None,
    ) -> dict[str, Any]:
        with self.db.write() as conn:
            cursor = conn.execute(
                "INSERT INTO segments (meeting_id, channel, start_ms, end_ms, text, confidence) VALUES (?, ?, ?, ?, ?, ?)",
                (meeting_id, channel, start_ms, end_ms, text, confidence),
            )
            segment_id = cursor.lastrowid
        return {
            "id": segment_id,
            "meeting_id": meeting_id,
            "channel": channel,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "text": text,
            "confidence": confidence,
        }

    def list_segments(self, meeting_id: str) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM segments WHERE meeting_id = ? ORDER BY start_ms ASC, id ASC",
            (meeting_id,),
        )
        return [dict(r) for r in rows]

    def transcript_text(self, meeting_id: str) -> str:
        """Flat, speaker-labelled transcript - the LLM's input."""
        lines = []
        for seg in self.list_segments(meeting_id):
            speaker = "You" if seg["channel"] == "me" else "Participant"
            lines.append(f"[{_format_ms(seg['start_ms'])}] {speaker}: {seg['text']}")
        return "\n".join(lines)

    # -- artifacts ----------------------------------------------------------
    def save_artifact(self, meeting_id: str, kind: str, content: str, model: str | None) -> dict[str, Any]:
        with self.db.write() as conn:
            conn.execute("DELETE FROM artifacts WHERE meeting_id = ? AND kind = ?", (meeting_id, kind))
            cursor = conn.execute(
                "INSERT INTO artifacts (meeting_id, kind, content, model) VALUES (?, ?, ?, ?)",
                (meeting_id, kind, content, model),
            )
            artifact_id = cursor.lastrowid
        return {"id": artifact_id, "meeting_id": meeting_id, "kind": kind, "content": content, "model": model}

    def list_artifacts(self, meeting_id: str) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM artifacts WHERE meeting_id = ? ORDER BY kind ASC", (meeting_id,)
        )
        return [dict(r) for r in rows]

    # -- deletion (the admin panel) ----------------------------------------
    def delete_meeting(self, meeting_id: str) -> bool:
        meeting = self.get_meeting(meeting_id)
        if meeting is None:
            return False
        self._remove_audio(meeting.get("audio_path"))
        # segments/artifacts go with it via ON DELETE CASCADE
        self.db.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
        self.db.vacuum()
        return True

    def delete_meetings(self, meeting_ids: Iterable[str]) -> int:
        deleted = 0
        for meeting_id in meeting_ids:
            if self.delete_meeting(meeting_id):
                deleted += 1
        return deleted

    def delete_all(self) -> int:
        rows = self.db.query("SELECT id, audio_path FROM meetings")
        for row in rows:
            self._remove_audio(row["audio_path"])
        self.db.execute("DELETE FROM meetings")
        self.db.vacuum()
        return len(rows)

    def delete_audio_only(self, meeting_id: str | None = None) -> int:
        """Drop recordings but keep transcripts and minutes."""
        if meeting_id:
            rows = self.db.query("SELECT id, audio_path FROM meetings WHERE id = ?", (meeting_id,))
        else:
            rows = self.db.query("SELECT id, audio_path FROM meetings WHERE audio_path IS NOT NULL")
        removed = 0
        for row in rows:
            if self._remove_audio(row["audio_path"]):
                removed += 1
            self.db.execute("UPDATE meetings SET audio_path = NULL WHERE id = ?", (row["id"],))
        return removed

    def purge_expired(self, retention_days: int) -> int:
        """Retention sweep. 0 means keep everything."""
        if retention_days <= 0:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(timespec="seconds")
        rows = self.db.query("SELECT id FROM meetings WHERE started_at < ?", (cutoff,))
        return self.delete_meetings([r["id"] for r in rows])

    def storage_stats(self) -> dict[str, Any]:
        meetings = self.db.query_one("SELECT COUNT(*) AS n FROM meetings")
        segments = self.db.query_one("SELECT COUNT(*) AS n FROM segments")
        artifacts = self.db.query_one("SELECT COUNT(*) AS n FROM artifacts")
        db_bytes = self.db.path.stat().st_size if self.db.path.exists() else 0
        audio_bytes = sum(f.stat().st_size for f in self.audio_dir.glob("**/*") if f.is_file())
        oldest = self.db.query_one("SELECT MIN(started_at) AS t FROM meetings")
        return {
            "meetings": meetings["n"] if meetings else 0,
            "segments": segments["n"] if segments else 0,
            "artifacts": artifacts["n"] if artifacts else 0,
            "database_bytes": db_bytes,
            "audio_bytes": audio_bytes,
            "total_bytes": db_bytes + audio_bytes,
            "oldest_meeting_at": oldest["t"] if oldest else None,
            "data_dir": str(self.db.path.parent),
        }

    def _remove_audio(self, audio_path: str | None) -> bool:
        if not audio_path:
            return False
        path = Path(audio_path)
        # Only ever delete inside our own recordings directory.
        try:
            path.relative_to(self.audio_dir)
        except ValueError:
            return False
        if path.exists():
            path.unlink()
            return True
        return False

    # -- settings -----------------------------------------------------------
    def load_settings(self) -> Settings:
        settings = Settings()
        for row in self.db.query("SELECT key, value FROM settings"):
            key = row["key"]
            if key not in Settings.field_names():
                continue
            try:
                setattr(settings, key, Settings.coerce(key, json.loads(row["value"])))
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
        return settings

    def save_settings(self, updates: dict[str, Any]) -> Settings:
        allowed = set(Settings.field_names())
        rows = [
            (key, json.dumps(Settings.coerce(key, value)))
            for key, value in updates.items()
            if key in allowed
        ]
        if rows:
            self.db.executemany(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                rows,
            )
        return self.load_settings()

    def reset_settings(self) -> Settings:
        self.db.execute("DELETE FROM settings")
        return self.load_settings()


def _format_ms(ms: int) -> str:
    seconds, _ = divmod(int(ms), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
