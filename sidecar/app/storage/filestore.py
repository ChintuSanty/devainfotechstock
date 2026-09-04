"""Plain-text meeting store.

Every meeting is a folder of readable files, so the data stays useful without
this app: open it in Notepad, grep it, sync it, diff it, or delete a folder in
Explorer and it is gone.

    <storage folder>/
      2026-09-04_1030_budget-review_a1b2c3d4/
        meeting.json        metadata: title, times, status, speaker names
        transcript.md       timestamped, speaker-labelled, human readable
        transcript.jsonl    one JSON object per line, append-only
        minutes.md          generated minutes of meeting
        suggestions.md      generated follow-ups and recommendations
        notes.md            the intermediate notes the minutes were built from
        speakers.jsonl      voice fingerprints, only when speaker ID is on
        audio.wav           only when the user opts in to keeping audio

transcript.jsonl is the source of truth for the transcript and is appended one
line at a time, so a crash mid-meeting costs at most the last utterance.
transcript.md is a rendering of it and is rebuilt whenever speaker names change.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
MEETING_FILE = "meeting.json"
TRANSCRIPT_JSONL = "transcript.jsonl"
TRANSCRIPT_MD = "transcript.md"
SPEAKERS_JSONL = "speakers.jsonl"
AUDIO_FILE = "audio.wav"
ARTIFACT_FILES = {"minutes": "minutes.md", "suggestions": "suggestions.md", "notes": "notes.md"}

MIC_SPEAKER = "You"
DEFAULT_PARTICIPANT = "Participant"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(text: str, limit: int = 40) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return (cleaned[:limit].strip("-")) or "meeting"


def format_timestamp(ms: int) -> str:
    seconds = max(0, int(ms)) // 1000
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


class MeetingStore:
    """File-backed meeting storage. Safe to call from any thread."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._index: dict[str, Path] = {}
        self._reindex()

    # -- index --------------------------------------------------------------
    def _reindex(self) -> None:
        index: dict[str, Path] = {}
        for meeting_file in sorted(self.root.glob(f"*/{MEETING_FILE}")):
            try:
                data = json.loads(meeting_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                logger.warning("Skipping unreadable meeting at %s", meeting_file.parent)
                continue
            meeting_id = data.get("id")
            if meeting_id:
                index[str(meeting_id)] = meeting_file.parent
        self._index = index

    def _dir_for(self, meeting_id: str) -> Path | None:
        path = self._index.get(meeting_id)
        if path is not None and (path / MEETING_FILE).exists():
            return path
        self._reindex()
        return self._index.get(meeting_id)

    # -- meeting records ----------------------------------------------------
    def create_meeting(self, title: str, source_app: str | None = None) -> dict[str, Any]:
        with self._lock:
            meeting_id = uuid.uuid4().hex
            started = datetime.now()
            folder = self._unique_dir(f"{started:%Y-%m-%d_%H%M}_{_slug(title)}_{meeting_id[:8]}")
            folder.mkdir(parents=True)

            record = {
                "schema": SCHEMA_VERSION,
                "id": meeting_id,
                "title": title,
                "started_at": _now(),
                "ended_at": None,
                "status": "recording",
                "source_app": source_app,
                "duration_ms": 0,
                "audio_file": None,
                "segment_count": 0,
                "speakers": {},
                "artifacts": {},
            }
            self._write_record(folder, record)
            (folder / TRANSCRIPT_JSONL).touch()
            self._index[meeting_id] = folder
            self._render_markdown(folder)
            return self._expand(folder, record)

    def _unique_dir(self, name: str) -> Path:
        candidate = self.root / name
        suffix = 2
        while candidate.exists():
            candidate = self.root / f"{name}-{suffix}"
            suffix += 1
        return candidate

    def _write_record(self, folder: Path, record: dict[str, Any]) -> None:
        """Write meeting.json atomically so a crash cannot truncate it."""
        target = folder / MEETING_FILE
        temporary = folder / f".{MEETING_FILE}.tmp"
        temporary.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temporary.replace(target)

    def _read_record(self, folder: Path) -> dict[str, Any] | None:
        try:
            return json.loads((folder / MEETING_FILE).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _expand(self, folder: Path, record: dict[str, Any]) -> dict[str, Any]:
        """Add the derived fields the API exposes."""
        audio_file = record.get("audio_file")
        return {
            **record,
            "folder": str(folder),
            "audio_path": str(folder / audio_file) if audio_file else None,
        }

    def _update(self, meeting_id: str, **changes: Any) -> dict[str, Any] | None:
        with self._lock:
            folder = self._dir_for(meeting_id)
            if folder is None:
                return None
            record = self._read_record(folder)
            if record is None:
                return None
            record.update(changes)
            self._write_record(folder, record)
            return self._expand(folder, record)

    def get_meeting(self, meeting_id: str) -> dict[str, Any] | None:
        with self._lock:
            folder = self._dir_for(meeting_id)
            if folder is None:
                return None
            record = self._read_record(folder)
            return self._expand(folder, record) if record else None

    def finish_meeting(self, meeting_id: str, duration_ms: int, audio_path: str | None) -> None:
        audio_file = Path(audio_path).name if audio_path else None
        self._update(
            meeting_id,
            ended_at=_now(),
            status="recorded",
            duration_ms=duration_ms,
            audio_file=audio_file,
        )
        folder = self._dir_for(meeting_id)
        if folder is not None:
            self._render_markdown(folder)

    def set_status(self, meeting_id: str, status: str) -> None:
        self._update(meeting_id, status=status)

    def rename_meeting(self, meeting_id: str, title: str) -> None:
        """Retitle the meeting and move its folder to match."""
        with self._lock:
            folder = self._dir_for(meeting_id)
            if folder is None:
                return
            record = self._read_record(folder)
            if record is None:
                return
            record["title"] = title
            self._write_record(folder, record)

            prefix = folder.name.split("_")[0:2]
            wanted = f"{'_'.join(prefix)}_{_slug(title)}_{meeting_id[:8]}"
            if folder.name != wanted:
                try:
                    folder = folder.rename(self._unique_dir(wanted))
                    self._index[meeting_id] = folder
                except OSError:
                    logger.warning("Could not rename %s; the title was still saved.", folder)
            self._render_markdown(folder)

    def list_meetings(self, limit: int = 100, offset: int = 0, search: str = "") -> list[dict[str, Any]]:
        with self._lock:
            self._reindex()
            records: list[dict[str, Any]] = []
            for meeting_id, folder in self._index.items():
                record = self._read_record(folder)
                if record is None:
                    continue
                expanded = self._expand(folder, record)
                expanded["segment_count"] = record.get("segment_count", 0)
                if search and not self._matches(folder, expanded, search):
                    continue
                records.append(expanded)

            records.sort(key=lambda item: item.get("started_at") or "", reverse=True)
            return records[offset : offset + limit]

    def _matches(self, folder: Path, record: dict[str, Any], search: str) -> bool:
        needle = search.lower()
        if needle in str(record.get("title", "")).lower():
            return True
        transcript = folder / TRANSCRIPT_JSONL
        if not transcript.exists():
            return False
        try:
            with transcript.open("r", encoding="utf-8") as handle:
                return any(needle in line.lower() for line in handle)
        except OSError:
            return False

    # -- transcript ---------------------------------------------------------
    def add_segment(
        self,
        meeting_id: str,
        channel: str,
        start_ms: int,
        end_ms: int,
        text: str,
        confidence: float | None = None,
        speaker: str | None = None,
        embedding: Sequence[float] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            folder = self._dir_for(meeting_id)
            if folder is None:
                raise KeyError(f"Unknown meeting {meeting_id}")
            record = self._read_record(folder) or {}
            segment_id = int(record.get("segment_count", 0)) + 1

            segment = {
                "id": segment_id,
                "channel": channel,
                "speaker": speaker or (MIC_SPEAKER if channel == "me" else DEFAULT_PARTICIPANT),
                "start_ms": int(start_ms),
                "end_ms": int(end_ms),
                "text": text,
                "confidence": round(confidence, 4) if confidence is not None else None,
            }
            with (folder / TRANSCRIPT_JSONL).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(segment, ensure_ascii=False) + "\n")

            if embedding is not None:
                with (folder / SPEAKERS_JSONL).open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps({"segment": segment_id, "vector": [round(float(v), 5) for v in embedding]})
                        + "\n"
                    )

            record["segment_count"] = segment_id
            self._write_record(folder, record)

            with (folder / TRANSCRIPT_MD).open("a", encoding="utf-8") as handle:
                handle.write(self._markdown_line(segment, record.get("speakers", {})))

            return {**segment, "meeting_id": meeting_id}

    def list_segments(self, meeting_id: str) -> list[dict[str, Any]]:
        folder = self._dir_for(meeting_id)
        if folder is None:
            return []
        speakers = (self._read_record(folder) or {}).get("speakers", {})
        segments: list[dict[str, Any]] = []
        transcript = folder / TRANSCRIPT_JSONL
        if not transcript.exists():
            return []
        with transcript.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    segment = json.loads(line)
                except json.JSONDecodeError:
                    continue
                segment["meeting_id"] = meeting_id
                segment["speaker_name"] = self._display_name(segment, speakers)
                segments.append(segment)
        segments.sort(key=lambda item: (item.get("start_ms", 0), item.get("id", 0)))
        return segments

    def transcript_text(self, meeting_id: str) -> str:
        """Speaker-labelled transcript - this is what the LLM reads."""
        return "\n".join(
            f"[{format_timestamp(segment['start_ms'])}] {segment['speaker_name']}: {segment['text']}"
            for segment in self.list_segments(meeting_id)
        )

    def _display_name(self, segment: dict[str, Any], speakers: dict[str, str]) -> str:
        label = segment.get("speaker") or (MIC_SPEAKER if segment.get("channel") == "me" else DEFAULT_PARTICIPANT)
        return speakers.get(label, label)

    def _markdown_line(self, segment: dict[str, Any], speakers: dict[str, str]) -> str:
        name = self._display_name(segment, speakers)
        return f"**[{format_timestamp(segment['start_ms'])}] {name}:** {segment['text']}\n\n"

    def _render_markdown(self, folder: Path) -> None:
        """Rebuild transcript.md from the JSONL source of truth."""
        record = self._read_record(folder) or {}
        speakers = record.get("speakers", {})
        lines = [
            f"# {record.get('title', 'Meeting')}",
            "",
            f"- Started: {record.get('started_at')}",
            f"- Duration: {int(record.get('duration_ms') or 0) // 1000}s",
        ]
        if record.get("source_app"):
            lines.append(f"- Source: {record['source_app']}")
        lines += ["", "---", "", ""]

        meeting_id = record.get("id")
        body = ""
        if meeting_id:
            body = "".join(
                self._markdown_line(segment, speakers) for segment in self.list_segments(str(meeting_id))
            )
        (folder / TRANSCRIPT_MD).write_text("\n".join(lines) + body, encoding="utf-8")

    # -- speakers -----------------------------------------------------------
    def set_speaker_names(self, meeting_id: str, names: dict[str, str]) -> dict[str, Any] | None:
        """Map auto labels ("S1") to real names, then re-render the transcript."""
        with self._lock:
            folder = self._dir_for(meeting_id)
            if folder is None:
                return None
            record = self._read_record(folder)
            if record is None:
                return None
            speakers = dict(record.get("speakers", {}))
            for label, name in names.items():
                cleaned = name.strip()
                if cleaned:
                    speakers[label] = cleaned[:80]
                else:
                    speakers.pop(label, None)
            record["speakers"] = speakers
            self._write_record(folder, record)
            self._render_markdown(folder)
            return self._expand(folder, record)

    def speaker_labels(self, meeting_id: str) -> list[dict[str, Any]]:
        """Distinct voices in a meeting, with how much each of them spoke."""
        segments = self.list_segments(meeting_id)
        record = self.get_meeting(meeting_id) or {}
        names = record.get("speakers", {})
        totals: dict[str, dict[str, Any]] = {}
        for segment in segments:
            label = segment.get("speaker") or DEFAULT_PARTICIPANT
            entry = totals.setdefault(
                label,
                {"label": label, "name": names.get(label, label), "channel": segment.get("channel"),
                 "segments": 0, "speaking_ms": 0},
            )
            entry["segments"] += 1
            entry["speaking_ms"] += max(0, int(segment.get("end_ms", 0)) - int(segment.get("start_ms", 0)))
        return sorted(totals.values(), key=lambda item: item["speaking_ms"], reverse=True)

    def load_embeddings(self, meeting_id: str) -> list[tuple[int, list[float]]]:
        folder = self._dir_for(meeting_id)
        if folder is None:
            return []
        path = folder / SPEAKERS_JSONL
        if not path.exists():
            return []
        vectors: list[tuple[int, list[float]]] = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    vectors.append((int(row["segment"]), [float(v) for v in row["vector"]]))
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    continue
        return vectors

    def apply_speaker_assignment(self, meeting_id: str, assignment: dict[int, str]) -> int:
        """Rewrite the speaker label on the given segments (used by re-clustering)."""
        with self._lock:
            folder = self._dir_for(meeting_id)
            if folder is None:
                return 0
            segments = self.list_segments(meeting_id)
            changed = 0
            for segment in segments:
                label = assignment.get(int(segment["id"]))
                if label and segment.get("speaker") != label:
                    segment["speaker"] = label
                    changed += 1
            if changed:
                with (folder / TRANSCRIPT_JSONL).open("w", encoding="utf-8") as handle:
                    for segment in segments:
                        handle.write(
                            json.dumps(
                                {key: segment[key] for key in
                                 ("id", "channel", "speaker", "start_ms", "end_ms", "text", "confidence")},
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                # Names attached to labels that no longer exist would be misleading.
                record = self._read_record(folder) or {}
                live = {segment["speaker"] for segment in segments}
                record["speakers"] = {k: v for k, v in record.get("speakers", {}).items() if k in live}
                self._write_record(folder, record)
                self._render_markdown(folder)
            return changed

    # -- artifacts ----------------------------------------------------------
    def save_artifact(self, meeting_id: str, kind: str, content: str, model: str | None) -> dict[str, Any]:
        with self._lock:
            folder = self._dir_for(meeting_id)
            if folder is None:
                raise KeyError(f"Unknown meeting {meeting_id}")
            filename = ARTIFACT_FILES.get(kind, f"{_slug(kind)}.md")
            (folder / filename).write_text(content.rstrip() + "\n", encoding="utf-8")

            record = self._read_record(folder) or {}
            artifacts = dict(record.get("artifacts", {}))
            artifacts[kind] = {"file": filename, "model": model, "created_at": _now()}
            record["artifacts"] = artifacts
            self._write_record(folder, record)
            return {"meeting_id": meeting_id, "kind": kind, "content": content, "model": model}

    def list_artifacts(self, meeting_id: str) -> list[dict[str, Any]]:
        folder = self._dir_for(meeting_id)
        if folder is None:
            return []
        record = self._read_record(folder) or {}
        artifacts: list[dict[str, Any]] = []
        for kind, meta in (record.get("artifacts") or {}).items():
            path = folder / meta.get("file", ARTIFACT_FILES.get(kind, ""))
            if not path.exists():
                continue
            artifacts.append(
                {
                    "meeting_id": meeting_id,
                    "kind": kind,
                    "content": path.read_text(encoding="utf-8"),
                    "model": meta.get("model"),
                    "created_at": meta.get("created_at"),
                }
            )
        return sorted(artifacts, key=lambda item: item["kind"])

    # -- deletion -----------------------------------------------------------
    def delete_meeting(self, meeting_id: str) -> bool:
        with self._lock:
            folder = self._dir_for(meeting_id)
            if folder is None:
                return False
            if not self._inside_root(folder):
                logger.error("Refusing to delete %s: outside the storage folder", folder)
                return False
            shutil.rmtree(folder, ignore_errors=False)
            self._index.pop(meeting_id, None)
            return True

    def delete_meetings(self, meeting_ids: Iterable[str]) -> int:
        return sum(1 for meeting_id in meeting_ids if self.delete_meeting(meeting_id))

    def delete_all(self) -> int:
        with self._lock:
            self._reindex()
            return self.delete_meetings(list(self._index.keys()))

    def delete_audio_only(self, meeting_id: str | None = None) -> int:
        with self._lock:
            self._reindex()
            targets = [meeting_id] if meeting_id else list(self._index.keys())
            removed = 0
            for target in targets:
                folder = self._dir_for(str(target))
                if folder is None:
                    continue
                record = self._read_record(folder) or {}
                audio_file = record.get("audio_file")
                if not audio_file:
                    continue
                path = folder / Path(audio_file).name
                if path.exists() and self._inside_root(path):
                    path.unlink()
                    removed += 1
                record["audio_file"] = None
                self._write_record(folder, record)
            return removed

    def purge_expired(self, retention_days: int) -> int:
        if retention_days <= 0:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(timespec="seconds")
        with self._lock:
            self._reindex()
            expired = [
                meeting_id
                for meeting_id, folder in self._index.items()
                if str((self._read_record(folder) or {}).get("started_at", "")) < cutoff
            ]
        return self.delete_meetings(expired)

    def _inside_root(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.root.resolve())
            return True
        except ValueError:
            return False

    # -- stats and relocation ----------------------------------------------
    def storage_stats(self) -> dict[str, Any]:
        with self._lock:
            self._reindex()
            meetings = 0
            segments = 0
            artifacts = 0
            audio_bytes = 0
            text_bytes = 0
            oldest: str | None = None

            for folder in self._index.values():
                record = self._read_record(folder)
                if record is None:
                    continue
                meetings += 1
                segments += int(record.get("segment_count", 0))
                artifacts += len(record.get("artifacts") or {})
                started = record.get("started_at")
                if started and (oldest is None or started < oldest):
                    oldest = started
                for item in folder.rglob("*"):
                    if not item.is_file():
                        continue
                    size = item.stat().st_size
                    if item.suffix.lower() == ".wav":
                        audio_bytes += size
                    else:
                        text_bytes += size

            return {
                "meetings": meetings,
                "segments": segments,
                "artifacts": artifacts,
                "text_bytes": text_bytes,
                "audio_bytes": audio_bytes,
                "total_bytes": text_bytes + audio_bytes,
                "oldest_meeting_at": oldest,
                "storage_dir": str(self.root),
            }

    def relocate(self, destination: Path, move_existing: bool = True) -> dict[str, Any]:
        """Point the store at a new folder, optionally taking the meetings along."""
        destination = Path(destination).expanduser()
        with self._lock:
            if destination.resolve() == self.root.resolve():
                return {"moved": 0, "storage_dir": str(self.root)}

            try:
                destination.mkdir(parents=True, exist_ok=True)
                probe = destination / ".meetingscribe-write-test"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink()
            except OSError as exc:
                raise ValueError(f"Cannot write to {destination}: {exc}") from exc

            if self._is_within(destination, self.root):
                raise ValueError("The new folder cannot be inside the current storage folder.")

            moved = 0
            if move_existing:
                self._reindex()
                for folder in list(self._index.values()):
                    target = destination / folder.name
                    suffix = 2
                    while target.exists():
                        target = destination / f"{folder.name}-{suffix}"
                        suffix += 1
                    shutil.move(str(folder), str(target))
                    moved += 1

            self.root = destination
            self.root.mkdir(parents=True, exist_ok=True)
            self._reindex()
            return {"moved": moved, "storage_dir": str(self.root)}

    @staticmethod
    def _is_within(candidate: Path, parent: Path) -> bool:
        try:
            candidate.resolve().relative_to(parent.resolve())
            return True
        except ValueError:
            return False
