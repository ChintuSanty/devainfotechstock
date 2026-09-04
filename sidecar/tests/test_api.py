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
    meeting = state.repository.create_meeting(title, "Zoom")
    for index in range(lines):
        state.repository.add_segment(
            meeting["id"],
            "me" if index % 2 == 0 else "others",
            index * 1_000,
            index * 1_000 + 900,
            f"line {index}",
        )
    state.repository.finish_meeting(meeting["id"], lines * 1_000, None)
    return meeting


def test_requests_without_a_token_are_rejected(client):
    response = client.get("/health", headers={"Authorization": ""})
    assert response.status_code == 401


def test_a_stale_token_is_rejected(client):
    response = client.get("/health", headers={"Authorization": "Bearer not-the-token"})
    assert response.status_code == 401


def test_health_reports_an_idle_recorder(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["recording"]["state"] == "idle"


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
    empty = state.repository.create_meeting("Silent")
    assert client.post(f"/meetings/{empty['id']}/summarise").status_code == 422


def test_summarise_runs_the_llm_and_stores_the_artifacts(client, state, monkeypatch):
    meeting = seed_meeting(state)

    class FakeSummarizer:
        def __init__(self, client, chunk_chars=0):
            self.model = "fake"

        def run(self, transcript, date, duration, progress=None, want_title=True):
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
    kinds = {a["kind"]: a["content"] for a in detail["artifacts"]}
    assert kinds["minutes"] == "# Minutes of Meeting"
    assert kinds["suggestions"] == "## Suggestions"
    assert detail["meeting"]["status"] == "summarised"
    assert detail["meeting"]["title"] == "Weekly sync"  # a user-set title is kept


def test_an_auto_generated_title_replaces_the_placeholder(client, state, monkeypatch):
    meeting = seed_meeting(state, "Meeting 04 Sep 2026, 10:00")

    class FakeSummarizer:
        def __init__(self, client, chunk_chars=0):
            pass

        def run(self, transcript, date, duration, progress=None, want_title=True):
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
    audio = state.config.audio_dir / f"{meeting['id']}.wav"
    audio.write_bytes(b"RIFF")
    state.repository.finish_meeting(meeting["id"], 2_000, str(audio))

    assert client.post("/admin/delete-audio").json()["deleted"] == 1
    assert not audio.exists()
    assert len(client.get(f"/meetings/{meeting['id']}").json()["segments"]) == 2


def test_admin_purge_uses_the_retention_window(client, state):
    meeting = seed_meeting(state, "Ancient")
    state.database.execute(
        "UPDATE meetings SET started_at = ? WHERE id = ?", ("2000-01-01T00:00:00+00:00", meeting["id"])
    )
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
