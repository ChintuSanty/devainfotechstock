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


def speaker_label(segment: dict[str, Any]) -> str:
    """Prefer the resolved name, falling back to the channel's generic label."""
    name = segment.get("speaker_name") or segment.get("speaker")
    if name:
        return str(name)
    return "You" if segment.get("channel") == "me" else "Participant"


def participants(segments: list[dict[str, Any]]) -> list[str]:
    """Distinct voices, in the order they were first heard."""
    seen: list[str] = []
    for segment in segments:
        name = speaker_label(segment)
        if name not in seen:
            seen.append(name)
    return seen


def _has_identified_speakers(segments: list[dict[str, Any]]) -> bool:
    """True once the meeting-audio channel has been split into individuals."""
    others = {speaker_label(s) for s in segments if s.get("channel") != "me"}
    return len(others) > 1


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
    if _has_identified_speakers(segments):
        lines.append(f"- **Voices heard:** {', '.join(participants(segments))}")
    lines.append("")

    if by_kind.get("minutes"):
        lines += [by_kind["minutes"], ""]
    if by_kind.get("suggestions"):
        lines += [by_kind["suggestions"], ""]

    lines += ["## Full Transcript", ""]
    for segment in segments:
        lines.append(
            f"**[{format_timestamp(segment['start_ms'])}] {speaker_label(segment)}:** {segment['text']}"
        )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def to_text(meeting: dict[str, Any], segments: list[dict[str, Any]]) -> str:
    lines = [meeting["title"], "=" * len(meeting["title"]), ""]
    for segment in segments:
        lines.append(f"[{format_timestamp(segment['start_ms'])}] {speaker_label(segment)}: {segment['text']}")
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
