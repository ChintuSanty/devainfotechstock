"""Export a meeting as Markdown, plain text or JSON."""

from __future__ import annotations

import json
from typing import Any


def format_duration(ms: int) -> str:
    seconds = max(0, int(ms)) // 1000
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def format_timestamp(ms: int) -> str:
    seconds = max(0, int(ms)) // 1000
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def speaker_label(channel: str) -> str:
    return "You" if channel == "me" else "Participant"


def to_markdown(meeting: dict[str, Any], segments: list[dict[str, Any]], artifacts: list[dict[str, Any]]) -> str:
    by_kind = {a["kind"]: a["content"] for a in artifacts}
    lines = [
        f"# {meeting['title']}",
        "",
        f"- **Started:** {meeting['started_at']}",
        f"- **Duration:** {format_duration(meeting.get('duration_ms') or 0)}",
    ]
    if meeting.get("source_app"):
        lines.append(f"- **Source:** {meeting['source_app']}")
    lines.append("")

    if by_kind.get("minutes"):
        lines += [by_kind["minutes"], ""]
    if by_kind.get("suggestions"):
        lines += [by_kind["suggestions"], ""]

    lines += ["## Full Transcript", ""]
    for segment in segments:
        lines.append(
            f"**[{format_timestamp(segment['start_ms'])}] {speaker_label(segment['channel'])}:** {segment['text']}"
        )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def to_text(meeting: dict[str, Any], segments: list[dict[str, Any]]) -> str:
    lines = [meeting["title"], "=" * len(meeting["title"]), ""]
    for segment in segments:
        lines.append(
            f"[{format_timestamp(segment['start_ms'])}] {speaker_label(segment['channel'])}: {segment['text']}"
        )
    return "\n".join(lines) + "\n"


def to_json(meeting: dict[str, Any], segments: list[dict[str, Any]], artifacts: list[dict[str, Any]]) -> str:
    return json.dumps(
        {"meeting": meeting, "segments": segments, "artifacts": artifacts},
        indent=2,
        ensure_ascii=False,
    )


def build_export(
    fmt: str, meeting: dict[str, Any], segments: list[dict[str, Any]], artifacts: list[dict[str, Any]]
) -> tuple[str, str, str]:
    """Return (content, media_type, file_extension)."""
    if fmt == "json":
        return to_json(meeting, segments, artifacts), "application/json", "json"
    if fmt == "txt":
        return to_text(meeting, segments), "text/plain; charset=utf-8", "txt"
    return to_markdown(meeting, segments, artifacts), "text/markdown; charset=utf-8", "md"
