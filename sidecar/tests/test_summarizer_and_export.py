import json

import pytest

from app.export import build_export, format_duration, format_timestamp
from app.llm.client import LLMError, _strip_reasoning
from app.llm.summarizer import Summarizer


class FakeClient:
    """Stands in for Ollama: records prompts, returns canned answers."""

    model = "fake-model"

    def __init__(self):
        self.calls = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if "Summarise this portion" in user:
            return "TOPICS: budget\nDECISIONS: None\nACTIONS: None"
        if "Write the minutes" in user:
            return "# Minutes of Meeting\n\n## Summary\nWe talked about the budget."
        if "Suggestions & Follow-ups" in user:
            return "## Suggestions & Follow-ups\n\n### Immediate Follow-ups\n- Send the budget."
        return "Budget discussion"


def transcript(lines: int) -> str:
    return "\n".join(f"[00:00:{i:02d}] You: line number {i}" for i in range(lines))


def test_short_transcript_is_a_single_chunk():
    summarizer = Summarizer(FakeClient(), chunk_chars=10_000)
    assert len(summarizer.chunk(transcript(10))) == 1


def test_long_transcript_is_split_into_chunks():
    summarizer = Summarizer(FakeClient(), chunk_chars=2_000)
    chunks = summarizer.chunk(transcript(400))
    assert len(chunks) > 1
    assert all(len(chunk) <= 2_600 for chunk in chunks)  # chunk size plus overlap


def test_chunking_never_splits_a_line():
    summarizer = Summarizer(FakeClient(), chunk_chars=2_000)
    for chunk in summarizer.chunk(transcript(400)):
        for line in chunk.splitlines():
            assert line.startswith("[00:00:")


def test_run_produces_minutes_suggestions_and_title():
    client = FakeClient()
    result = Summarizer(client, chunk_chars=10_000).run(
        transcript(20), date="2026-09-04T10:00:00+00:00", duration="15m 0s"
    )
    assert result.minutes.startswith("# Minutes of Meeting")
    assert "Immediate Follow-ups" in result.suggestions
    assert result.title == "Budget discussion"
    assert result.model == "fake-model"


def test_run_reports_progress_up_to_one():
    seen = []
    Summarizer(FakeClient(), chunk_chars=10_000).run(
        transcript(20), date="d", duration="1m", progress=lambda stage, value: seen.append((stage, value))
    )
    assert seen[-1] == ("done", 1.0)
    assert [value for _, value in seen] == sorted(value for _, value in seen)


def test_empty_transcript_is_rejected():
    with pytest.raises(LLMError):
        Summarizer(FakeClient()).run("   ", date="d", duration="0s")


def test_reasoning_blocks_are_stripped():
    assert _strip_reasoning("<think>hmm</think>Answer") == "Answer"
    assert _strip_reasoning("plain") == "plain"


@pytest.mark.parametrize(
    "ms,expected",
    [(0, "0s"), (45_000, "45s"), (90_000, "1m 30s"), (3_725_000, "1h 2m 5s")],
)
def test_format_duration(ms, expected):
    assert format_duration(ms) == expected


def test_format_timestamp():
    assert format_timestamp(3_661_000) == "01:01:01"


def _fixture():
    meeting = {
        "id": "abc",
        "title": "Budget review",
        "started_at": "2026-09-04T10:00:00+00:00",
        "duration_ms": 90_000,
        "source_app": "Zoom",
    }
    segments = [
        {"channel": "me", "start_ms": 0, "end_ms": 2_000, "text": "Shall we start?"},
        {"channel": "others", "start_ms": 3_000, "end_ms": 6_000, "text": "Yes, go ahead."},
    ]
    artifacts = [
        {"kind": "minutes", "content": "# Minutes of Meeting\n\n## Summary\nBudget."},
        {"kind": "suggestions", "content": "## Suggestions & Follow-ups"},
    ]
    return meeting, segments, artifacts


def test_markdown_export_contains_minutes_and_transcript():
    content, media_type, extension = build_export("md", *_fixture())
    assert extension == "md" and "markdown" in media_type
    assert "# Budget review" in content
    assert "## Summary" in content
    assert "**[00:00:03] Participant:** Yes, go ahead." in content


def test_text_export_is_transcript_only():
    content, _, extension = build_export("txt", *_fixture())
    assert extension == "txt"
    assert "[00:00:00] You: Shall we start?" in content
    assert "Minutes of Meeting" not in content


def test_json_export_round_trips():
    content, media_type, _ = build_export("json", *_fixture())
    assert media_type == "application/json"
    parsed = json.loads(content)
    assert parsed["meeting"]["title"] == "Budget review"
    assert len(parsed["segments"]) == 2


# -- named speakers in exports -------------------------------------------
def _named_fixture():
    meeting, segments, artifacts = _fixture()
    segments[0]["speaker_name"] = "You"
    segments[1]["speaker_name"] = "Priya"
    return meeting, segments, artifacts


def test_markdown_export_uses_speaker_names():
    meeting, segments, artifacts = _named_fixture()
    segments.append({"channel": "others", "start_ms": 8_000, "end_ms": 9_000,
                     "text": "One more thing.", "speaker_name": "Sam"})
    content, _, _ = build_export("md", meeting, segments, artifacts)
    assert "**[00:00:03] Priya:** Yes, go ahead." in content
    assert "**Voices heard:** You, Priya, Sam" in content


def test_text_export_uses_speaker_names():
    content, _, _ = build_export("txt", *_named_fixture())
    assert "[00:00:03] Priya: Yes, go ahead." in content


def test_exports_fall_back_to_channel_labels_without_speaker_ids():
    content, _, _ = build_export("md", *_fixture())
    assert "**[00:00:03] Participant:**" in content
    # One undifferentiated participant channel is not a speaker roster.
    assert "Voices heard" not in content


def test_channel_note_switches_when_speakers_are_known():
    from app.llm.prompts import MIXED_CHANNEL_NOTE, channel_note

    assert channel_note([]) == MIXED_CHANNEL_NOTE
    assert channel_note(["Participant"]) == MIXED_CHANNEL_NOTE

    note = channel_note(["You", "Priya", "S2"])
    assert "'Priya'" in note and "S1" in note


def test_the_summariser_passes_the_roster_into_the_prompt():
    client = FakeClient()
    Summarizer(client, chunk_chars=10_000).run(
        transcript(10), date="d", duration="1m", speakers=["You", "Priya", "Sam"]
    )
    assert any("'Priya'" in system for system, _ in client.calls)
