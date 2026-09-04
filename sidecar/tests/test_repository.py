import pytest

from app.storage.database import Database
from app.storage.repository import MeetingRepository


@pytest.fixture()
def repo(tmp_path):
    audio_dir = tmp_path / "recordings"
    audio_dir.mkdir()
    return MeetingRepository(Database(tmp_path / "test.db"), audio_dir)


def test_create_and_fetch_meeting(repo):
    meeting = repo.create_meeting("Sprint planning", "Zoom")
    assert meeting["status"] == "recording"
    assert repo.get_meeting(meeting["id"])["title"] == "Sprint planning"


def test_segments_are_returned_in_time_order(repo):
    meeting = repo.create_meeting("Standup")
    repo.add_segment(meeting["id"], "others", 5_000, 6_000, "second")
    repo.add_segment(meeting["id"], "me", 1_000, 2_000, "first")
    assert [s["text"] for s in repo.list_segments(meeting["id"])] == ["first", "second"]


def test_transcript_labels_the_two_channels(repo):
    meeting = repo.create_meeting("Review")
    repo.add_segment(meeting["id"], "me", 0, 1_000, "hello")
    repo.add_segment(meeting["id"], "others", 2_000, 3_000, "hi there")
    transcript = repo.transcript_text(meeting["id"])
    assert "[00:00:00] You: hello" in transcript
    assert "[00:00:02] Participant: hi there" in transcript


def test_saving_an_artifact_twice_replaces_it(repo):
    meeting = repo.create_meeting("Retro")
    repo.save_artifact(meeting["id"], "minutes", "draft", "qwen3:4b")
    repo.save_artifact(meeting["id"], "minutes", "final", "qwen3:4b")
    artifacts = repo.list_artifacts(meeting["id"])
    assert len(artifacts) == 1 and artifacts[0]["content"] == "final"


def test_deleting_a_meeting_removes_its_segments_and_audio(repo):
    meeting = repo.create_meeting("Confidential")
    repo.add_segment(meeting["id"], "me", 0, 1_000, "secret")
    audio = repo.audio_dir / f"{meeting['id']}.wav"
    audio.write_bytes(b"RIFF")
    repo.finish_meeting(meeting["id"], 60_000, str(audio))

    assert repo.delete_meeting(meeting["id"]) is True
    assert repo.get_meeting(meeting["id"]) is None
    assert repo.list_segments(meeting["id"]) == []
    assert not audio.exists()


def test_delete_all_clears_everything(repo):
    for index in range(3):
        repo.create_meeting(f"Meeting {index}")
    assert repo.delete_all() == 3
    assert repo.list_meetings() == []


def test_delete_audio_only_keeps_the_transcript(repo):
    meeting = repo.create_meeting("Kickoff")
    repo.add_segment(meeting["id"], "me", 0, 1_000, "keep me")
    audio = repo.audio_dir / f"{meeting['id']}.wav"
    audio.write_bytes(b"RIFF")
    repo.finish_meeting(meeting["id"], 1_000, str(audio))

    assert repo.delete_audio_only() == 1
    assert not audio.exists()
    assert repo.list_segments(meeting["id"])[0]["text"] == "keep me"
    assert repo.get_meeting(meeting["id"])["audio_path"] is None


def test_audio_outside_the_recordings_dir_is_never_deleted(repo, tmp_path):
    outside = tmp_path / "important.wav"
    outside.write_bytes(b"RIFF")
    meeting = repo.create_meeting("Odd")
    repo.finish_meeting(meeting["id"], 1_000, str(outside))

    repo.delete_meeting(meeting["id"])
    assert outside.exists()


def test_purge_expired_respects_keep_forever(repo):
    repo.create_meeting("Old one")
    assert repo.purge_expired(0) == 0
    assert len(repo.list_meetings()) == 1


def test_purge_expired_removes_older_meetings(repo):
    meeting = repo.create_meeting("Ancient")
    repo.db.execute("UPDATE meetings SET started_at = ? WHERE id = ?", ("2000-01-01T00:00:00+00:00", meeting["id"]))
    assert repo.purge_expired(30) == 1
    assert repo.list_meetings() == []


def test_search_matches_titles_and_transcript_text(repo):
    first = repo.create_meeting("Budget review")
    second = repo.create_meeting("Random chat")
    repo.add_segment(second["id"], "others", 0, 1_000, "we should discuss the budget")

    ids = {m["id"] for m in repo.list_meetings(search="budget")}
    assert ids == {first["id"], second["id"]}


def test_settings_round_trip_with_types(repo):
    updated = repo.save_settings({"store_audio": True, "retention_days": 30, "llm_model": "llama3.1:8b"})
    assert updated.store_audio is True
    assert updated.retention_days == 30
    assert updated.llm_model == "llama3.1:8b"
    assert repo.load_settings().retention_days == 30


def test_unknown_settings_keys_are_ignored(repo):
    updated = repo.save_settings({"definitely_not_a_setting": "x"})
    assert not hasattr(updated, "definitely_not_a_setting")


def test_reset_settings_restores_defaults(repo):
    repo.save_settings({"retention_days": 90})
    assert repo.reset_settings().retention_days == 0


def test_storage_stats_counts_rows(repo):
    meeting = repo.create_meeting("Stats")
    repo.add_segment(meeting["id"], "me", 0, 1_000, "one")
    repo.save_artifact(meeting["id"], "minutes", "text", "model")
    stats = repo.storage_stats()
    assert stats["meetings"] == 1 and stats["segments"] == 1 and stats["artifacts"] == 1
    assert stats["total_bytes"] > 0
