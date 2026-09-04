"""End-to-end tests of the loopback API, with audio and the LLM stubbed out."""

import pytest
from fastapi.testclient import TestClient

from app import server
from app.config import AppConfig


@pytest.fixture()
def client(tmp_path):
    app = server.create_app(AppConfig(data_dir=tmp_path / "data"))
    with TestClient(app) as test_client:
        test_client.headers.update({"Authorization": f"Bearer {server.TOKEN}"})
        yield test_client


@pytest.fixture()
def state(client):
    return client.app.state.services


def seed_meeting(state, title="Weekly sync", lines=2):
    meeting = state.store.create_meeting(title, "Zoom")
    for index in range(lines):
        state.store.add_segment(
            meeting["id"],
            "me" if index % 2 == 0 else "others",
            index * 1_000,
            index * 1_000 + 900,
            f"line {index}",
        )
    state.store.finish_meeting(meeting["id"], lines * 1_000, None)
    return meeting


def test_requests_without_a_token_are_rejected(client):
    response = client.get("/health", headers={"Authorization": ""})
    assert response.status_code == 401


def test_a_stale_token_is_rejected(client):
    response = client.get("/health", headers={"Authorization": "Bearer not-the-token"})
    assert response.status_code == 401


def test_health_reports_an_idle_recorder_and_the_storage_folder(client, state):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["recording"]["state"] == "idle"
    assert body["storage_dir"] == str(state.store.root)


def test_settings_round_trip(client):
    assert client.get("/settings").json()["retention_days"] == 0
    updated = client.put("/settings", json={"values": {"retention_days": 30, "store_audio": True}}).json()
    assert updated["retention_days"] == 30 and updated["store_audio"] is True
    assert client.post("/settings/reset").json()["retention_days"] == 0


def test_changing_the_stt_model_updates_the_engine(client, state):
    client.put("/settings", json={"values": {"stt_model": "base.en"}})
    assert state.engine.model_size == "base.en"


def test_listing_and_reading_a_meeting(client, state):
    meeting = seed_meeting(state)
    listed = client.get("/meetings").json()["meetings"]
    assert [m["id"] for m in listed] == [meeting["id"]]
    assert listed[0]["segment_count"] == 2

    detail = client.get(f"/meetings/{meeting['id']}").json()
    assert detail["meeting"]["title"] == "Weekly sync"
    assert len(detail["segments"]) == 2


def test_unknown_meeting_is_a_404(client):
    assert client.get("/meetings/does-not-exist").status_code == 404


def test_renaming_a_meeting(client, state):
    meeting = seed_meeting(state)
    assert client.patch(f"/meetings/{meeting['id']}", json={"title": "Renamed"}).json()["title"] == "Renamed"


def test_search_filters_the_list(client, state):
    seed_meeting(state, "Budget review")
    seed_meeting(state, "Hiring sync")
    results = client.get("/meetings", params={"search": "budget"}).json()["meetings"]
    assert [m["title"] for m in results] == ["Budget review"]


@pytest.mark.parametrize("fmt,marker", [("md", "# Weekly sync"), ("txt", "You: line 0"), ("json", '"segments"')])
def test_export_formats(client, state, fmt, marker):
    meeting = seed_meeting(state)
    response = client.get(f"/meetings/{meeting['id']}/export", params={"fmt": fmt})
    assert response.status_code == 200
    assert marker in response.text
    assert "attachment" in response.headers["content-disposition"]


def test_export_rejects_an_unknown_format(client, state):
    meeting = seed_meeting(state)
    assert client.get(f"/meetings/{meeting['id']}/export", params={"fmt": "pdf"}).status_code == 422


def test_summarise_needs_a_transcript(client, state):
    empty = state.store.create_meeting("Silent")
    assert client.post(f"/meetings/{empty['id']}/summarise").status_code == 422


def test_summarise_runs_the_llm_and_stores_the_artifacts(client, state, monkeypatch):
    meeting = seed_meeting(state)

    class FakeSummarizer:
        def __init__(self, client, chunk_chars=0):
            self.model = "fake"

        def run(self, transcript, date, duration, progress=None, want_title=True, speakers=None):
            from app.llm.summarizer import SummaryResult

            if progress:
                progress("done", 1.0)
            return SummaryResult(
                minutes="# Minutes of Meeting",
                suggestions="## Suggestions",
                notes="TOPICS",
                title="Nice title" if want_title else None,
                model="fake",
            )

    monkeypatch.setattr(server, "Summarizer", FakeSummarizer)
    monkeypatch.setattr(server, "build_client", lambda settings: object())

    assert client.post(f"/meetings/{meeting['id']}/summarise").json()["status"] == "started"
    state._summary_jobs[meeting["id"]].join(timeout=10)

    detail = client.get(f"/meetings/{meeting['id']}").json()
    # Stored as files, so each ends with a trailing newline.
    kinds = {a["kind"]: a["content"].strip() for a in detail["artifacts"]}
    assert kinds["minutes"] == "# Minutes of Meeting"
    assert kinds["suggestions"] == "## Suggestions"
    assert detail["meeting"]["status"] == "summarised"
    assert detail["meeting"]["title"] == "Weekly sync"  # a user-set title is kept


def test_an_auto_generated_title_replaces_the_placeholder(client, state, monkeypatch):
    meeting = seed_meeting(state, "Meeting 04 Sep 2026, 10:00")

    class FakeSummarizer:
        def __init__(self, client, chunk_chars=0):
            pass

        def run(self, transcript, date, duration, progress=None, want_title=True, speakers=None):
            from app.llm.summarizer import SummaryResult

            return SummaryResult("m", "s", "n", "Nice title" if want_title else None, "fake")

    monkeypatch.setattr(server, "Summarizer", FakeSummarizer)
    monkeypatch.setattr(server, "build_client", lambda settings: object())

    client.post(f"/meetings/{meeting['id']}/summarise")
    state._summary_jobs[meeting["id"]].join(timeout=10)
    assert client.get(f"/meetings/{meeting['id']}").json()["meeting"]["title"] == "Nice title"


def test_a_failing_llm_leaves_the_meeting_recorded(client, state, monkeypatch):
    meeting = seed_meeting(state)

    def explode(settings):
        from app.llm.client import LLMError

        raise LLMError("Ollama is not running")

    monkeypatch.setattr(server, "build_client", explode)
    client.post(f"/meetings/{meeting['id']}/summarise")
    state._summary_jobs[meeting["id"]].join(timeout=10)

    assert client.get(f"/meetings/{meeting['id']}").json()["meeting"]["status"] == "recorded"


def test_deleting_a_meeting(client, state):
    meeting = seed_meeting(state)
    assert client.delete(f"/meetings/{meeting['id']}").json() == {"deleted": 1}
    assert client.delete(f"/meetings/{meeting['id']}").status_code == 404


def test_admin_stats_and_bulk_delete(client, state):
    first = seed_meeting(state, "One")
    second = seed_meeting(state, "Two")
    assert client.get("/admin/stats").json()["meetings"] == 2

    assert client.post("/admin/delete", json={"meeting_ids": [first["id"]]}).json()["deleted"] == 1
    assert client.post("/admin/delete-all").json()["deleted"] == 1
    assert client.get("/meetings").json()["meetings"] == []
    assert second["id"] not in [m["id"] for m in client.get("/meetings").json()["meetings"]]


def test_admin_delete_audio_keeps_transcripts(client, state):
    meeting = seed_meeting(state)
    audio = state.store._dir_for(meeting["id"]) / "audio.wav"
    audio.write_bytes(b"RIFF")
    state.store.finish_meeting(meeting["id"], 2_000, str(audio))

    assert client.post("/admin/delete-audio").json()["deleted"] == 1
    assert not audio.exists()
    assert len(client.get(f"/meetings/{meeting['id']}").json()["segments"]) == 2


def test_admin_purge_uses_the_retention_window(client, state):
    import json

    meeting = seed_meeting(state, "Ancient")
    folder = state.store._dir_for(meeting["id"])
    record = json.loads((folder / "meeting.json").read_text())
    record["started_at"] = "2000-01-01T00:00:00+00:00"
    (folder / "meeting.json").write_text(json.dumps(record))

    assert client.post("/admin/purge", json={"retention_days": 30}).json()["deleted"] == 1


def test_purge_rejects_a_negative_window(client):
    assert client.post("/admin/purge", json={"retention_days": -1}).status_code == 422


def test_stopping_when_nothing_is_recording_is_a_conflict(client):
    assert client.post("/recording/stop").status_code == 409


def test_starting_without_any_channel_enabled_is_rejected(client):
    client.put("/settings", json={"values": {"capture_system_audio": False, "capture_microphone": False}})
    response = client.post("/recording/start", json={})
    assert response.status_code == 409
    assert "at least one" in response.json()["detail"]


def test_the_event_socket_needs_the_token(client):
    with pytest.raises(Exception):
        with client.websocket_connect("/events?token=wrong"):
            pass


def test_the_event_socket_greets_with_the_current_status(client):
    with client.websocket_connect(f"/events?token={server.TOKEN}") as socket:
        hello = socket.receive_json()
        assert hello["type"] == "hello" and hello["payload"]["state"] == "idle"


# -- storage location -----------------------------------------------------
def test_storage_defaults_to_the_app_folder(client, state):
    body = client.get("/admin/storage").json()
    assert body["is_default"] is True
    assert body["storage_dir"] == str(state.config.default_storage_dir)


def test_changing_the_storage_folder_moves_the_meetings(client, state, tmp_path):
    meeting = seed_meeting(state, "Portable")
    destination = tmp_path / "Documents" / "MeetingScribe"

    body = client.post("/admin/storage", json={"path": str(destination), "move_existing": True}).json()
    assert body["moved"] == 1
    assert body["storage_dir"] == str(destination)

    # The meeting is readable from the new location, and the setting persisted.
    assert client.get(f"/meetings/{meeting['id']}").json()["meeting"]["title"] == "Portable"
    assert client.get("/settings").json()["storage_dir"] == str(destination)
    assert client.get("/admin/storage").json()["is_default"] is False
    assert (destination).exists() and any(destination.iterdir())


def test_changing_the_storage_folder_without_moving(client, state, tmp_path):
    seed_meeting(state, "Left behind")
    client.post("/admin/storage", json={"path": str(tmp_path / "empty"), "move_existing": False})
    assert client.get("/meetings").json()["meetings"] == []


def test_a_nested_storage_folder_is_rejected(client, state):
    nested = state.store.root / "inside"
    assert client.post("/admin/storage", json={"path": str(nested)}).status_code == 422


def test_the_storage_folder_cannot_move_mid_recording(client, state, tmp_path, monkeypatch):
    monkeypatch.setattr(type(state.session), "is_active", property(lambda self: True))
    assert client.post("/admin/storage", json={"path": str(tmp_path / "nope")}).status_code == 409


# -- speakers -------------------------------------------------------------
def test_speaker_health_reports_whether_a_model_is_installed(client):
    body = client.get("/speakers/health").json()
    assert body["enabled"] is False
    assert set(body["installed"]) == {"speechbrain", "resemblyzer"}


def test_listing_and_naming_speakers(client, state):
    meeting = state.store.create_meeting("Panel")
    state.store.add_segment(meeting["id"], "others", 0, 3_000, "hello", speaker="S1")
    state.store.add_segment(meeting["id"], "others", 4_000, 5_000, "hi", speaker="S2")

    speakers = client.get(f"/meetings/{meeting['id']}/speakers").json()["speakers"]
    assert {s["label"] for s in speakers} == {"S1", "S2"}

    body = client.patch(f"/meetings/{meeting['id']}/speakers", json={"names": {"S1": "Priya"}}).json()
    assert body["meeting"]["speakers"] == {"S1": "Priya"}
    assert "Priya: hello" in state.store.transcript_text(meeting["id"])


def test_the_meeting_detail_carries_the_speaker_roster(client, state):
    meeting = state.store.create_meeting("Panel")
    state.store.add_segment(meeting["id"], "others", 0, 3_000, "hello", speaker="S1")
    assert client.get(f"/meetings/{meeting['id']}").json()["speakers"][0]["label"] == "S1"


def test_redetect_needs_stored_fingerprints(client, state):
    meeting = seed_meeting(state)
    response = client.post(f"/meetings/{meeting['id']}/speakers/redetect", json={})
    assert response.status_code == 422
    assert "voice fingerprints" in response.json()["detail"]


def test_redetect_reclusters_from_the_stored_vectors(client, state):
    import numpy as np

    from tests.test_speakers import utterance, voice

    meeting = state.store.create_meeting("Panel")
    alice, bob = voice(41), voice(42)
    # Online labelling got the first line wrong; the vectors say otherwise.
    plan = [(alice, "S1"), (bob, "S2"), (bob, "S2"), (alice, "S3"), (alice, "S3")]
    for index, (person, label) in enumerate(plan):
        state.store.add_segment(
            meeting["id"], "others", index * 1_000, index * 1_000 + 900, f"line {index}",
            speaker=label, embedding=np.asarray(utterance(person)).tolist(),
        )

    body = client.post(f"/meetings/{meeting['id']}/speakers/redetect", json={"threshold": 0.6}).json()
    assert body["changed"] > 0
    labels = [s["speaker"] for s in state.store.list_segments(meeting["id"])]
    assert labels[0] == labels[3] == labels[4]
    assert len(set(labels)) == 2
