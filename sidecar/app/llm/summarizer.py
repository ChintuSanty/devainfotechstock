"""Turns a transcript into minutes and suggestions.

Long meetings do not fit in a small local model's context window, so the
transcript is map-reduced: each chunk is condensed into structured notes, then
the notes are written up once. The intermediate notes are also what the
suggestions pass reads, so a two-hour call costs one pass over the transcript
plus two short passes over the notes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from . import prompts
from .client import LLMClient, LLMError

logger = logging.getLogger(__name__)

ProgressFn = Callable[[str, float], None]


@dataclass
class SummaryResult:
    minutes: str
    suggestions: str
    notes: str
    title: str | None
    model: str


class Summarizer:
    def __init__(self, client: LLMClient, chunk_chars: int = 12_000, overlap_chars: int = 400) -> None:
        self.client = client
        self.chunk_chars = max(2_000, chunk_chars)
        self.overlap_chars = overlap_chars

    def run(
        self,
        transcript: str,
        date: str,
        duration: str,
        progress: ProgressFn | None = None,
        want_title: bool = True,
        speakers: list[str] | None = None,
    ) -> SummaryResult:
        transcript = transcript.strip()
        if not transcript:
            raise LLMError("There is nothing to summarise - the transcript is empty.")

        def report(stage: str, value: float) -> None:
            if progress:
                progress(stage, value)

        chunks = self.chunk(transcript)
        report("analysing", 0.05)

        note = prompts.channel_note(speakers)
        chunk_system = prompts.CHUNK_SYSTEM.format(channel_note=note)

        notes_parts: list[str] = []
        for index, chunk in enumerate(chunks, start=1):
            notes_parts.append(
                self.client.complete(
                    chunk_system,
                    prompts.CHUNK_USER.format(index=index, total=len(chunks), chunk=chunk),
                )
            )
            report("analysing", 0.05 + 0.55 * index / len(chunks))
        notes = "\n\n".join(notes_parts)

        report("writing_minutes", 0.65)
        minutes = self.client.complete(
            prompts.MINUTES_SYSTEM.format(channel_note=note),
            prompts.MINUTES_USER.format(date=date, duration=duration, notes=notes),
        )

        report("writing_suggestions", 0.85)
        suggestions = self.client.complete(
            prompts.SUGGESTIONS_SYSTEM, prompts.SUGGESTIONS_USER.format(notes=notes)
        )

        title = None
        if want_title:
            try:
                title = _clean_title(
                    self.client.complete(prompts.TITLE_SYSTEM, prompts.TITLE_USER.format(notes=notes[:4_000]))
                )
            except LLMError:
                logger.warning("Title generation failed; keeping the existing title.")

        report("done", 1.0)
        return SummaryResult(
            minutes=minutes.strip(),
            suggestions=suggestions.strip(),
            notes=notes.strip(),
            title=title,
            model=self.client.model,
        )

    def chunk(self, transcript: str) -> list[str]:
        """Split on line boundaries so a speaker turn is never cut in half."""
        lines = transcript.splitlines()
        chunks: list[str] = []
        current: list[str] = []
        size = 0
        for line in lines:
            if size + len(line) + 1 > self.chunk_chars and current:
                chunks.append("\n".join(current))
                carry = _tail_lines(current, self.overlap_chars)
                current = list(carry)
                size = sum(len(item) + 1 for item in carry)
            current.append(line)
            size += len(line) + 1
        if current:
            chunks.append("\n".join(current))
        return chunks or [transcript]


def _tail_lines(lines: list[str], max_chars: int) -> list[str]:
    carry: list[str] = []
    total = 0
    for line in reversed(lines):
        if total + len(line) > max_chars:
            break
        carry.insert(0, line)
        total += len(line) + 1
    return carry


def _clean_title(raw: str) -> str:
    title = raw.strip().strip('"').strip("'").splitlines()[0].strip()
    return title[:120]
