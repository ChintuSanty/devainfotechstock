"""Loopback API for the Electron front end.

Binds to 127.0.0.1 on an OS-assigned port and prints a one-line JSON handshake
containing the port and a per-launch bearer token. Nothing is reachable from
outside the machine, and a stale token from a previous run is useless.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import signal
import socket
import sys
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, Field

from . import __version__
from .audio.devices import list_devices
from .config import AppConfig, Settings
from .detect import MeetingAppWatcher
from .events import EventBus
from .export import build_export, format_duration
from .llm import LLMError, Summarizer, build_client
from .pipeline import RecordingSession
from .speakers import SpeakerEmbedder, cluster_offline, embedder_status
from .storage import MeetingStore, SettingsStore
from .stt import WhisperEngine

logger = logging.getLogger("meetingscribe")

TOKEN = secrets.token_urlsafe(32)


class AppState:
    """Everything the request handlers need, wired once at startup."""

    def __init__(self, config: AppConfig) -> None:
        config.ensure_dirs()
        self.config = config
        self.settings_store = SettingsStore(config.settings_path)
        self.settings: Settings = self.settings_store.load()
        self.store = MeetingStore(config.resolve_storage_dir(self.settings))
        self.events = EventBus()
        self.engine = WhisperEngine(
            model_size=self.settings.stt_model,
            device=self.settings.stt_device,
            compute_type=self.settings.stt_compute_type,
            language=self.settings.stt_language,
            beam_size=self.settings.stt_beam_size,
        )
        self.embedder = SpeakerEmbedder(self.settings.speaker_embedding_model)
        self.session = RecordingSession(self.store, self.engine, self.events, self.embedder)
        self.watcher = MeetingAppWatcher(
            lambda apps: self.events.publish("meeting_apps", {"apps": apps})
        )
        self._summary_jobs: dict[str, threading.Thread] = {}

    def apply_settings(self, updated: Settings) -> None:
        """Reload models only when the settings they depend on actually moved."""
        stt_changed = (
            updated.stt_model != self.settings.stt_model
            or updated.stt_device != self.settings.stt_device
            or updated.stt_compute_type != self.settings.stt_compute_type
        )
        embedder_changed = updated.speaker_embedding_model != self.settings.speaker_embedding_model
        storage_changed = updated.storage_dir != self.settings.storage_dir
        self.settings = updated
        self.engine.model_size = updated.stt_model
        self.engine.device = updated.stt_device
        self.engine.compute_type = updated.stt_compute_type
        self.engine.language = updated.stt_language
        self.engine.beam_size = updated.stt_beam_size
        if stt_changed:
            self.engine.unload()
        if embedder_changed:
            self.embedder.unload()
            self.embedder.preferred = updated.speaker_embedding_model
        if storage_changed and not self.session.is_active:
            # The path was set directly rather than through /admin/storage, so
            # just follow it; existing meetings stay where they are.
            self.store.relocate(self.config.resolve_storage_dir(updated), move_existing=False)

    def change_storage(self, path: str, move_existing: bool) -> dict[str, Any]:
        if self.session.is_active:
            raise RuntimeError("Stop the recording before moving the storage folder.")
        result = self.store.relocate(Path(path).expanduser(), move_existing=move_existing)
        self.settings = self.settings_store.save({"storage_dir": result["storage_dir"]})
        self.events.publish("storage_changed", result)
        return result

    def summarise(self, meeting_id: str, force: bool = False) -> None:
        """Run minutes + suggestions off the request thread."""
        existing = self._summary_jobs.get(meeting_id)
        if existing is not None and existing.is_alive() and not force:
            return
        thread = threading.Thread(
            target=self._summarise_worker, args=(meeting_id,), name=f"summarise-{meeting_id[:8]}", daemon=True
        )
        self._summary_jobs[meeting_id] = thread
        thread.start()

    def _summarise_worker(self, meeting_id: str) -> None:
        meeting = self.store.get_meeting(meeting_id)
        if meeting is None:
            return
        self.store.set_status(meeting_id, "summarising")
        self.events.publish("summary_progress", {"meeting_id": meeting_id, "stage": "starting", "progress": 0.0})
        try:
            summarizer = Summarizer(build_client(self.settings), chunk_chars=self.settings.llm_context_chars)
            result = summarizer.run(
                transcript=self.store.transcript_text(meeting_id),
                date=meeting["started_at"],
                duration=format_duration(meeting.get("duration_ms") or 0),
                progress=lambda stage, value: self.events.publish(
                    "summary_progress", {"meeting_id": meeting_id, "stage": stage, "progress": value}
                ),
                want_title=meeting["title"].startswith("Meeting "),
                speakers=[speaker["name"] for speaker in self.store.speaker_labels(meeting_id)],
            )
        except LLMError as exc:
            self.store.set_status(meeting_id, "recorded")
            self.events.publish("summary_failed", {"meeting_id": meeting_id, "message": str(exc)})
            return
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Summarisation crashed")
            self.store.set_status(meeting_id, "recorded")
            self.events.publish("summary_failed", {"meeting_id": meeting_id, "message": str(exc)})
            return

        self.store.save_artifact(meeting_id, "minutes", result.minutes, result.model)
        self.store.save_artifact(meeting_id, "suggestions", result.suggestions, result.model)
        self.store.save_artifact(meeting_id, "notes", result.notes, result.model)
        if result.title:
            self.store.rename_meeting(meeting_id, result.title)
        self.store.set_status(meeting_id, "summarised")
        self.events.publish(
            "summary_ready",
            {"meeting_id": meeting_id, "meeting": self.store.get_meeting(meeting_id)},
        )

    def shutdown(self) -> None:
        self.watcher.stop()
        if self.session.is_active:
            try:
                self.session.stop()
            except Exception:
                logger.exception("Failed to stop the recording cleanly")


# --------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------
class StartRequest(BaseModel):
    title: str | None = None
    source_app: str | None = None


class RenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class SettingsRequest(BaseModel):
    values: dict[str, Any]


class PurgeRequest(BaseModel):
    retention_days: int = Field(ge=0, le=3650)


class StorageRequest(BaseModel):
    path: str = Field(min_length=1)
    move_existing: bool = True


class SpeakerNamesRequest(BaseModel):
    names: dict[str, str]


class ReclusterRequest(BaseModel):
    threshold: float | None = Field(default=None, ge=0.1, le=0.99)


class DeleteManyRequest(BaseModel):
    meeting_ids: list[str]


# --------------------------------------------------------------------------
# App factory
# --------------------------------------------------------------------------
def create_app(config: AppConfig) -> FastAPI:
    state = AppState(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        state.events.bind_loop(asyncio.get_running_loop())
        if state.settings.auto_detect_meeting_apps:
            state.watcher.start()
        # Retention is enforced at every launch, not only on a timer.
        purged = state.store.purge_expired(state.settings.retention_days)
        if purged:
            logger.info("Retention policy removed %d meeting(s)", purged)
        yield
        state.shutdown()

    app = FastAPI(title="MeetingScribe Sidecar", version=__version__, lifespan=lifespan)
    app.state.services = state

    def require_token(request: Request) -> None:
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer ") or not secrets.compare_digest(header[7:], TOKEN):
            raise HTTPException(status_code=401, detail="Invalid or missing token.")

    guard = [Depends(require_token)]

    # -- health / meta ------------------------------------------------------
    @app.get("/health", dependencies=guard)
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "platform": sys.platform,
            "data_dir": str(config.data_dir),
            "storage_dir": str(state.store.root),
            "stt_model_loaded": state.engine.is_loaded,
            "recording": state.session.status(),
        }

    @app.get("/speakers/health", dependencies=guard)
    def speakers_health() -> dict[str, Any]:
        status = embedder_status(state.settings.speaker_embedding_model)
        status["enabled"] = state.settings.identify_speakers
        status["loaded"] = state.embedder.is_loaded
        return status

    @app.get("/devices", dependencies=guard)
    def devices() -> dict[str, Any]:
        try:
            return list_devices()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/llm/health", dependencies=guard)
    def llm_health() -> dict[str, Any]:
        return build_client(state.settings).health()

    @app.get("/meeting-apps", dependencies=guard)
    def meeting_apps() -> dict[str, Any]:
        return {"apps": state.watcher.current}

    # -- settings -----------------------------------------------------------
    @app.get("/settings", dependencies=guard)
    def get_settings() -> dict[str, Any]:
        return state.settings.to_dict()

    @app.put("/settings", dependencies=guard)
    def put_settings(body: SettingsRequest) -> dict[str, Any]:
        updated = state.settings_store.save(body.values)
        state.apply_settings(updated)
        state.events.publish("settings_changed", updated.to_dict())
        return updated.to_dict()

    @app.post("/settings/reset", dependencies=guard)
    def reset_settings() -> dict[str, Any]:
        updated = state.settings_store.reset()
        state.apply_settings(updated)
        state.events.publish("settings_changed", updated.to_dict())
        return updated.to_dict()

    # -- recording ----------------------------------------------------------
    @app.get("/recording/status", dependencies=guard)
    def recording_status() -> dict[str, Any]:
        return state.session.status()

    @app.post("/recording/start", dependencies=guard)
    def recording_start(body: StartRequest) -> dict[str, Any]:
        source_app = body.source_app
        if source_app is None and state.watcher.current:
            source_app = ", ".join(a["app"] for a in state.watcher.current)
        try:
            meeting = state.session.start(state.settings, body.title, source_app)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return meeting

    @app.post("/recording/stop", dependencies=guard)
    def recording_stop() -> dict[str, Any]:
        meeting = state.session.stop()
        if meeting is None:
            raise HTTPException(status_code=409, detail="No meeting is being recorded.")
        if state.settings.auto_summarise_on_stop:
            state.summarise(meeting["id"])
        return meeting

    @app.post("/recording/pause", dependencies=guard)
    def recording_pause() -> dict[str, Any]:
        state.session.pause()
        return state.session.status()

    @app.post("/recording/resume", dependencies=guard)
    def recording_resume() -> dict[str, Any]:
        state.session.resume()
        return state.session.status()

    # -- meetings -----------------------------------------------------------
    @app.get("/meetings", dependencies=guard)
    def list_meetings(
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        search: str = Query(default=""),
    ) -> dict[str, Any]:
        return {"meetings": state.store.list_meetings(limit, offset, search)}

    @app.get("/meetings/{meeting_id}", dependencies=guard)
    def get_meeting(meeting_id: str) -> dict[str, Any]:
        meeting = state.store.get_meeting(meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail="Meeting not found.")
        return {
            "meeting": meeting,
            "segments": state.store.list_segments(meeting_id),
            "artifacts": state.store.list_artifacts(meeting_id),
            "speakers": state.store.speaker_labels(meeting_id),
        }

    @app.patch("/meetings/{meeting_id}", dependencies=guard)
    def rename_meeting(meeting_id: str, body: RenameRequest) -> dict[str, Any]:
        if state.store.get_meeting(meeting_id) is None:
            raise HTTPException(status_code=404, detail="Meeting not found.")
        state.store.rename_meeting(meeting_id, body.title)
        return state.store.get_meeting(meeting_id) or {}

    @app.post("/meetings/{meeting_id}/summarise", dependencies=guard)
    def summarise_meeting(meeting_id: str, force: bool = Query(default=True)) -> dict[str, Any]:
        if state.store.get_meeting(meeting_id) is None:
            raise HTTPException(status_code=404, detail="Meeting not found.")
        if not state.store.list_segments(meeting_id):
            raise HTTPException(status_code=422, detail="This meeting has no transcript to summarise.")
        state.summarise(meeting_id, force=force)
        return {"status": "started", "meeting_id": meeting_id}

    @app.get("/meetings/{meeting_id}/export", dependencies=guard)
    def export_meeting(meeting_id: str, fmt: str = Query(default="md", pattern="^(md|txt|json)$")) -> Response:
        meeting = state.store.get_meeting(meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail="Meeting not found.")
        content, media_type, extension = build_export(
            fmt,
            meeting,
            state.store.list_segments(meeting_id),
            state.store.list_artifacts(meeting_id),
        )
        filename = _safe_filename(meeting["title"], extension)
        return Response(
            content=content,
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/meetings/{meeting_id}/speakers", dependencies=guard)
    def get_speakers(meeting_id: str) -> dict[str, Any]:
        if state.store.get_meeting(meeting_id) is None:
            raise HTTPException(status_code=404, detail="Meeting not found.")
        return {"speakers": state.store.speaker_labels(meeting_id)}

    @app.patch("/meetings/{meeting_id}/speakers", dependencies=guard)
    def name_speakers(meeting_id: str, body: SpeakerNamesRequest) -> dict[str, Any]:
        meeting = state.store.set_speaker_names(meeting_id, body.names)
        if meeting is None:
            raise HTTPException(status_code=404, detail="Meeting not found.")
        state.events.publish("speakers_changed", {"meeting_id": meeting_id})
        return {"meeting": meeting, "speakers": state.store.speaker_labels(meeting_id)}

    @app.post("/meetings/{meeting_id}/speakers/redetect", dependencies=guard)
    def redetect_speakers(meeting_id: str, body: ReclusterRequest) -> dict[str, Any]:
        """Re-cluster the stored voice fingerprints, fixing early mislabels."""
        if state.store.get_meeting(meeting_id) is None:
            raise HTTPException(status_code=404, detail="Meeting not found.")
        vectors = state.store.load_embeddings(meeting_id)
        if not vectors:
            raise HTTPException(
                status_code=422,
                detail="This meeting has no voice fingerprints. Speaker identification was off while it was recorded.",
            )
        labels = cluster_offline(
            [vector for _, vector in vectors],
            threshold=body.threshold if body.threshold is not None else state.settings.speaker_similarity,
            max_speakers=state.settings.speaker_max_count,
        )
        changed = state.store.apply_speaker_assignment(
            meeting_id, {segment_id: label for (segment_id, _), label in zip(vectors, labels)}
        )
        state.events.publish("speakers_changed", {"meeting_id": meeting_id})
        return {"changed": changed, "speakers": state.store.speaker_labels(meeting_id)}

    @app.delete("/meetings/{meeting_id}", dependencies=guard)
    def delete_meeting(meeting_id: str) -> dict[str, Any]:
        if state.session.is_active and state.session.status().get("meeting_id") == meeting_id:
            raise HTTPException(status_code=409, detail="Stop the recording before deleting this meeting.")
        if not state.store.delete_meeting(meeting_id):
            raise HTTPException(status_code=404, detail="Meeting not found.")
        state.events.publish("meeting_deleted", {"meeting_id": meeting_id})
        return {"deleted": 1}

    # -- admin: the user's own data controls --------------------------------
    @app.get("/admin/stats", dependencies=guard)
    def admin_stats() -> dict[str, Any]:
        return state.store.storage_stats()

    @app.post("/admin/delete", dependencies=guard)
    def admin_delete_many(body: DeleteManyRequest) -> dict[str, Any]:
        active = state.session.status().get("meeting_id")
        if active and active in body.meeting_ids:
            raise HTTPException(status_code=409, detail="Stop the recording before deleting this meeting.")
        deleted = state.store.delete_meetings(body.meeting_ids)
        state.events.publish("meetings_deleted", {"count": deleted})
        return {"deleted": deleted}

    @app.post("/admin/delete-all", dependencies=guard)
    def admin_delete_all() -> dict[str, Any]:
        if state.session.is_active:
            raise HTTPException(status_code=409, detail="Stop the recording before deleting everything.")
        deleted = state.store.delete_all()
        state.events.publish("meetings_deleted", {"count": deleted, "all": True})
        return {"deleted": deleted}

    @app.post("/admin/delete-audio", dependencies=guard)
    def admin_delete_audio(meeting_id: str | None = Query(default=None)) -> dict[str, Any]:
        removed = state.store.delete_audio_only(meeting_id)
        state.events.publish("audio_deleted", {"count": removed})
        return {"deleted": removed}

    @app.get("/admin/storage", dependencies=guard)
    def get_storage() -> dict[str, Any]:
        return {
            "storage_dir": str(state.store.root),
            "default_storage_dir": str(config.default_storage_dir),
            "is_default": str(state.store.root) == str(config.default_storage_dir),
        }

    @app.post("/admin/storage", dependencies=guard)
    def set_storage(body: StorageRequest) -> dict[str, Any]:
        try:
            return state.change_storage(body.path, body.move_existing)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=422, detail=f"Could not use that folder: {exc}") from exc

    @app.post("/admin/purge", dependencies=guard)
    def admin_purge(body: PurgeRequest) -> dict[str, Any]:
        deleted = state.store.purge_expired(body.retention_days)
        state.events.publish("meetings_deleted", {"count": deleted})
        return {"deleted": deleted}

    # -- live events --------------------------------------------------------
    @app.websocket("/events")
    async def events_socket(websocket: WebSocket, token: str = Query(default="")) -> None:
        if not secrets.compare_digest(token, TOKEN):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        queue = state.events.subscribe()
        try:
            await websocket.send_json({"type": "hello", "payload": state.session.status()})
            while True:
                event = await queue.get()
                await websocket.send_json(event)
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.debug("Event socket closed", exc_info=True)
        finally:
            state.events.unsubscribe(queue)

    @app.get("/", response_class=PlainTextResponse, dependencies=guard)
    def root() -> str:
        return f"MeetingScribe sidecar {__version__}"

    return app


def _safe_filename(title: str, extension: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in " -_" else "_" for char in title).strip()
    cleaned = cleaned or "meeting"
    stamp = datetime.now().strftime("%Y%m%d")
    return f"{cleaned[:60]}-{stamp}.{extension}"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("MEETINGSCRIBE_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    import uvicorn

    config = AppConfig(port=int(os.environ.get("MEETINGSCRIBE_PORT", "0")) or _free_port())
    app = create_app(config)

    # The Electron supervisor blocks on this line.
    print(json.dumps({"event": "ready", "port": config.port, "token": TOKEN}), flush=True)

    server = uvicorn.Server(
        uvicorn.Config(app, host=config.host, port=config.port, log_level="warning", access_log=False)
    )

    def handle_signal(_signum: int, _frame: Any) -> None:
        server.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handle_signal)
        except (ValueError, OSError):  # pragma: no cover - non-main thread
            pass

    server.run()


if __name__ == "__main__":
    main()
