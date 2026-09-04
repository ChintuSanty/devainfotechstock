"""The plain-text meeting store: files on disk, readable without this app."""

import json

import pytest

from app.storage import MeetingStore


@pytest.fixture()
def store(tmp_path):
    return MeetingStore(tmp_path / "meetings")


def test_a_meeting_is_a_readable_folder(store):
    meeting = store.create_meeting("Budget review", "Zoom")
    folder = store.root / next(iter(p.name for p in store.root.iterdir()))

    assert folder.is_dir()
    assert "budget-review" in folder.name
    record = json.loads((folder / "meeting.json").read_text())
    assert record["id"] == meeting["id"]
    assert record["title"] == "Budget review"
    assert (folder / "transcript.jsonl").exists()


def test_segments_append_to_jsonl_and_markdown(store):
    meeting = store.create_meeting("Standup")
    store.add_segment(meeting["id"], "me", 0, 1_000, "morning all")
    store.add_segment(meeting["id"], "others", 2_000, 3_000, "morning")

    folder = store._dir_for(meeting["id"])
    lines = [json.loads(line) for line in (folder / "transcript.jsonl").read_text().splitlines() if line]
    assert [line["text"] for line in lines] == ["morning all", "morning"]
    assert [line["id"] for line in lines] == [1, 2]

    markdown = (folder / "transcript.md").read_text()
    assert "**[00:00:00] You:** morning all" in markdown
    assert "**[00:00:02] Participant:** morning" in markdown


def test_segments_come_back_in_time_order(store):
    meeting = store.create_meeting("Review")
    store.add_segment(meeting["id"], "others", 5_000, 6_000, "second")
    store.add_segment(meeting["id"], "me", 1_000, 2_000, "first")
    assert [s["text"] for s in store.list_segments(meeting["id"])] == ["first", "second"]


def test_transcript_text_labels_speakers(store):
    meeting = store.create_meeting("Review")
    store.add_segment(meeting["id"], "me", 0, 1_000, "hello")
    store.add_segment(meeting["id"], "others", 2_000, 3_000, "hi there", speaker="S1")
    text = store.transcript_text(meeting["id"])
    assert "[00:00:00] You: hello" in text
    assert "[00:00:02] S1: hi there" in text


def test_renaming_moves_the_folder_and_keeps_the_id(store):
    meeting = store.create_meeting("Untitled")
    store.rename_meeting(meeting["id"], "Q3 planning")
    folder = store._dir_for(meeting["id"])
    assert "q3-planning" in folder.name
    assert store.get_meeting(meeting["id"])["title"] == "Q3 planning"


def test_artifacts_are_written_as_markdown_files(store):
    meeting = store.create_meeting("Retro")
    store.save_artifact(meeting["id"], "minutes", "# Minutes of Meeting", "qwen3:4b")
    store.save_artifact(meeting["id"], "suggestions", "## Suggestions", "qwen3:4b")

    folder = store._dir_for(meeting["id"])
    assert (folder / "minutes.md").read_text().startswith("# Minutes of Meeting")
    kinds = {a["kind"]: a for a in store.list_artifacts(meeting["id"])}
    assert kinds["minutes"]["model"] == "qwen3:4b"
    assert kinds["suggestions"]["content"].strip() == "## Suggestions"


def test_saving_an_artifact_twice_replaces_it(store):
    meeting = store.create_meeting("Retro")
    store.save_artifact(meeting["id"], "minutes", "draft", "m")
    store.save_artifact(meeting["id"], "minutes", "final", "m")
    artifacts = store.list_artifacts(meeting["id"])
    assert len(artifacts) == 1 and artifacts[0]["content"].strip() == "final"


def test_deleting_a_meeting_removes_the_whole_folder(store):
    meeting = store.create_meeting("Confidential")
    store.add_segment(meeting["id"], "me", 0, 1_000, "secret")
    folder = store._dir_for(meeting["id"])

    assert store.delete_meeting(meeting["id"]) is True
    assert not folder.exists()
    assert store.get_meeting(meeting["id"]) is None
    assert store.list_segments(meeting["id"]) == []


def test_delete_all_clears_the_store(store):
    for index in range(3):
        store.create_meeting(f"Meeting {index}")
    assert store.delete_all() == 3
    assert store.list_meetings() == []


def test_delete_audio_only_keeps_the_transcript(store):
    meeting = store.create_meeting("Kickoff")
    store.add_segment(meeting["id"], "me", 0, 1_000, "keep me")
    folder = store._dir_for(meeting["id"])
    (folder / "audio.wav").write_bytes(b"RIFF")
    store.finish_meeting(meeting["id"], 1_000, str(folder / "audio.wav"))

    assert store.delete_audio_only() == 1
    assert not (folder / "audio.wav").exists()
    assert store.list_segments(meeting["id"])[0]["text"] == "keep me"
    assert store.get_meeting(meeting["id"])["audio_path"] is None


def test_purge_respects_keep_forever(store):
    store.create_meeting("Old one")
    assert store.purge_expired(0) == 0
    assert len(store.list_meetings()) == 1


def test_purge_removes_older_meetings(store):
    meeting = store.create_meeting("Ancient")
    folder = store._dir_for(meeting["id"])
    record = json.loads((folder / "meeting.json").read_text())
    record["started_at"] = "2000-01-01T00:00:00+00:00"
    (folder / "meeting.json").write_text(json.dumps(record))

    assert store.purge_expired(30) == 1
    assert store.list_meetings() == []


def test_search_matches_titles_and_transcripts(store):
    first = store.create_meeting("Budget review")
    second = store.create_meeting("Random chat")
    store.add_segment(second["id"], "others", 0, 1_000, "we should discuss the budget")

    ids = {m["id"] for m in store.list_meetings(search="budget")}
    assert ids == {first["id"], second["id"]}


def test_a_corrupt_meeting_folder_is_skipped_not_fatal(store):
    good = store.create_meeting("Fine")
    broken = store.root / "2026-01-01_0900_broken_deadbeef"
    broken.mkdir()
    (broken / "meeting.json").write_text("{ not json")

    assert [m["id"] for m in store.list_meetings()] == [good["id"]]


def test_storage_stats_separates_text_from_audio(store):
    meeting = store.create_meeting("Stats")
    store.add_segment(meeting["id"], "me", 0, 1_000, "one")
    store.save_artifact(meeting["id"], "minutes", "text", "model")
    (store._dir_for(meeting["id"]) / "audio.wav").write_bytes(b"0" * 2048)

    stats = store.storage_stats()
    assert stats["meetings"] == 1 and stats["segments"] == 1 and stats["artifacts"] == 1
    assert stats["audio_bytes"] == 2048
    assert stats["text_bytes"] > 0
    assert stats["storage_dir"] == str(store.root)


# -- speakers -------------------------------------------------------------
def test_speaker_labels_report_talk_time(store):
    meeting = store.create_meeting("Panel")
    store.add_segment(meeting["id"], "others", 0, 4_000, "a lot", speaker="S1")
    store.add_segment(meeting["id"], "others", 5_000, 6_000, "a little", speaker="S2")
    store.add_segment(meeting["id"], "me", 7_000, 8_000, "me too")

    # Ordered by talk time, ties keeping the order they were first heard.
    speakers = store.speaker_labels(meeting["id"])
    assert [s["label"] for s in speakers] == ["S1", "S2", "You"]
    assert speakers[0]["speaking_ms"] == 4_000
    assert speakers[1]["speaking_ms"] == speakers[2]["speaking_ms"] == 1_000


def test_naming_a_speaker_rewrites_the_transcript(store):
    meeting = store.create_meeting("Panel")
    store.add_segment(meeting["id"], "others", 0, 2_000, "hello", speaker="S1")

    store.set_speaker_names(meeting["id"], {"S1": "Priya"})
    assert "Priya: hello" in store.transcript_text(meeting["id"])
    assert "**[00:00:00] Priya:**" in (store._dir_for(meeting["id"]) / "transcript.md").read_text()
    assert store.list_segments(meeting["id"])[0]["speaker"] == "S1"  # the label is stable


def test_embeddings_are_stored_separately_from_the_transcript(store):
    meeting = store.create_meeting("Panel")
    store.add_segment(meeting["id"], "others", 0, 2_000, "hello", speaker="S1", embedding=[0.1, 0.2, 0.3])

    folder = store._dir_for(meeting["id"])
    assert "vector" not in (folder / "transcript.jsonl").read_text()
    assert store.load_embeddings(meeting["id"]) == [(1, [0.1, 0.2, 0.3])]


def test_reassigning_speakers_rewrites_labels_and_drops_stale_names(store):
    meeting = store.create_meeting("Panel")
    store.add_segment(meeting["id"], "others", 0, 2_000, "one", speaker="S1")
    store.add_segment(meeting["id"], "others", 3_000, 4_000, "two", speaker="S2")
    store.set_speaker_names(meeting["id"], {"S1": "Priya", "S2": "Sam"})

    # Re-clustering decides both lines were the same voice.
    assert store.apply_speaker_assignment(meeting["id"], {1: "S1", 2: "S1"}) == 1
    assert [s["speaker"] for s in store.list_segments(meeting["id"])] == ["S1", "S1"]
    assert store.get_meeting(meeting["id"])["speakers"] == {"S1": "Priya"}


# -- relocation -----------------------------------------------------------
def test_relocating_moves_the_meeting_folders(store, tmp_path):
    meeting = store.create_meeting("Travelling")
    store.add_segment(meeting["id"], "me", 0, 1_000, "still here")
    destination = tmp_path / "elsewhere"

    result = store.relocate(destination, move_existing=True)
    assert result["moved"] == 1
    assert store.root == destination
    assert store.list_segments(meeting["id"])[0]["text"] == "still here"
    assert not any((tmp_path / "meetings").iterdir())


def test_relocating_without_moving_leaves_the_old_data_behind(store, tmp_path):
    store.create_meeting("Stays put")
    result = store.relocate(tmp_path / "fresh", move_existing=False)
    assert result["moved"] == 0
    assert store.list_meetings() == []


def test_relocating_into_the_current_folder_is_rejected(store):
    with pytest.raises(ValueError):
        store.relocate(store.root / "nested")


def test_relocating_to_the_same_folder_is_a_no_op(store):
    store.create_meeting("Here")
    assert store.relocate(store.root)["moved"] == 0
    assert len(store.list_meetings()) == 1
