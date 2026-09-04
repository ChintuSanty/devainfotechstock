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
from .storage import Database, MeetingRepository
from .stt import WhisperEngine

logger = logging.getLogger("meetingscribe")

TOKEN = secrets.token_urlsafe(32)


class AppState:
    """Everything the request handlers need, wired once at startup."""

    def __init__(self, config: AppConfig) -> None:
        config.ensure_dirs()
        self.config = config
        self.database = Database(config.db_path)
        self.repository = MeetingRepository(self.database, config.audio_dir)
        self.settings: Settings = self.repository.load_settings()
        self.events = EventBus()
        self.engine = WhisperEngine(
            model_size=self.settings.stt_model,
            device=self.settings.stt_device,
            compute_type=self.settings.stt_compute_type,
            language=self.settings.stt_language,
            beam_size=self.settings.stt_beam_size,
        )
        self.session = RecordingSession(
            self.repository, self.engine, self.events, config.audio_dir
        )
        self.watcher = MeetingAppWatcher(
            lambda apps: self.events.publish("meeting_apps", {"apps": apps})
        )
        self._summary_jobs: dict[str, threading.Thread] = {}

    def apply_settings(self, updated: Settings) -> None:
        """Reload the Whisper model only when its own settings actually moved."""
        stt_changed = (
            updated.stt_model != self.settings.stt_model
            or updated.stt_device != self.settings.stt_device
            or updated.stt_compute_type != self.settings.stt_compute_type
        )
        self.settings = updated
        self.engine.model_size = updated.stt_model
        self.engine.device = updated.stt_device
        self.engine.compute_type = updated.stt_compute_type
        self.engine.language = updated.stt_language
        self.engine.beam_size = updated.stt_beam_size
        if stt_changed:
            self.engine.unload()

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
        meeting = self.repository.get_meeting(meeting_id)
        if meeting is None:
            return
        self.repository.set_status(meeting_id, "summarising")
        self.events.publish("summary_progress", {"meeting_id": meeting_id, "stage": "starting", "progress": 0.0})
        try:
            summarizer = Summarizer(build_client(self.settings), chunk_chars=self.settings.llm_context_chars)
            result = summarizer.run(
                transcript=self.repository.transcript_text(meeting_id),
                date=meeting["started_at"],
                duration=format_duration(meeting.get("duration_ms") or 0),
                progress=lambda stage, value: self.events.publish(
                    "summary_progress", {"meeting_id": meeting_id, "stage": stage, "progress": value}
                ),
                want_title=meeting["title"].startswith("Meeting "),
            )
        except LLMError as exc:
            self.repository.set_status(meeting_id, "recorded")
            self.events.publish("summary_failed", {"meeting_id": meeting_id, "message": str(exc)})
            return
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Summarisation crashed")
            self.repository.set_status(meeting_id, "recorded")
            self.events.publish("summary_failed", {"meeting_id": meeting_id, "message": str(exc)})
            return

        self.repository.save_artifact(meeting_id, "minutes", result.minutes, result.model)
        self.repository.save_artifact(meeting_id, "suggestions", result.suggestions, result.model)
        self.repository.save_artifact(meeting_id, "notes", result.notes, result.model)
        if result.title:
            self.repository.rename_meeting(meeting_id, result.title)
        self.repository.set_status(meeting_id, "summarised")
        self.events.publish(
            "summary_ready",
            {"meeting_id": meeting_id, "meeting": self.repository.get_meeting(meeting_id)},
        )

    def shutdown(self) -> None:
        self.watcher.stop()
        if self.session.is_active:
            try:
                self.session.stop()
            except Exception:
                logger.exception("Failed to stop the recording cleanly")
        self.database.close()


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
        purged = state.repository.purge_expired(state.settings.retention_days)
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
            "stt_model_loaded": state.engine.is_loaded,
            "recording": state.session.status(),
        }

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
        updated = state.repository.save_settings(body.values)
        state.apply_settings(updated)
        state.events.publish("settings_changed", updated.to_dict())
        return updated.to_dict()

    @app.post("/settings/reset", dependencies=guard)
    def reset_settings() -> dict[str, Any]:
        updated = state.repository.reset_settings()
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
        return {"meetings": state.repository.list_meetings(limit, offset, search)}

    @app.get("/meetings/{meeting_id}", dependencies=guard)
    def get_meeting(meeting_id: str) -> dict[str, Any]:
        meeting = state.repository.get_meeting(meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail="Meeting not found.")
        return {
            "meeting": meeting,
            "segments": state.repository.list_segments(meeting_id),
            "artifacts": state.repository.list_artifacts(meeting_id),
        }

    @app.patch("/meetings/{meeting_id}", dependencies=guard)
    def rename_meeting(meeting_id: str, body: RenameRequest) -> dict[str, Any]:
        if state.repository.get_meeting(meeting_id) is None:
            raise HTTPException(status_code=404, detail="Meeting not found.")
        state.repository.rename_meeting(meeting_id, body.title)
        return state.repository.get_meeting(meeting_id) or {}

    @app.post("/meetings/{meeting_id}/summarise", dependencies=guard)
    def summarise_meeting(meeting_id: str, force: bool = Query(default=True)) -> dict[str, Any]:
        if state.repository.get_meeting(meeting_id) is None:
            raise HTTPException(status_code=404, detail="Meeting not found.")
        if not state.repository.list_segments(meeting_id):
            raise HTTPException(status_code=422, detail="This meeting has no transcript to summarise.")
        state.summarise(meeting_id, force=force)
        return {"status": "started", "meeting_id": meeting_id}

    @app.get("/meetings/{meeting_id}/export", dependencies=guard)
    def export_meeting(meeting_id: str, fmt: str = Query(default="md", pattern="^(md|txt|json)$")) -> Response:
        meeting = state.repository.get_meeting(meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail="Meeting not found.")
        content, media_type, extension = build_export(
            fmt,
            meeting,
            state.repository.list_segments(meeting_id),
            state.repository.list_artifacts(meeting_id),
        )
        filename = _safe_filename(meeting["title"], extension)
        return Response(
            content=content,
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.delete("/meetings/{meeting_id}", dependencies=guard)
    def delete_meeting(meeting_id: str) -> dict[str, Any]:
        if state.session.is_active and state.session.status().get("meeting_id") == meeting_id:
            raise HTTPException(status_code=409, detail="Stop the recording before deleting this meeting.")
        if not state.repository.delete_meeting(meeting_id):
            raise HTTPException(status_code=404, detail="Meeting not found.")
        state.events.publish("meeting_deleted", {"meeting_id": meeting_id})
        return {"deleted": 1}

    # -- admin: the user's own data controls --------------------------------
    @app.get("/admin/stats", dependencies=guard)
    def admin_stats() -> dict[str, Any]:
        return state.repository.storage_stats()

    @app.post("/admin/delete", dependencies=guard)
    def admin_delete_many(body: DeleteManyRequest) -> dict[str, Any]:
        active = state.session.status().get("meeting_id")
        if active and active in body.meeting_ids:
            raise HTTPException(status_code=409, detail="Stop the recording before deleting this meeting.")
        deleted = state.repository.delete_meetings(body.meeting_ids)
        state.events.publish("meetings_deleted", {"count": deleted})
        return {"deleted": deleted}

    @app.post("/admin/delete-all", dependencies=guard)
    def admin_delete_all() -> dict[str, Any]:
        if state.session.is_active:
            raise HTTPException(status_code=409, detail="Stop the recording before deleting everything.")
        deleted = state.repository.delete_all()
        state.events.publish("meetings_deleted", {"count": deleted, "all": True})
        return {"deleted": deleted}

    @app.post("/admin/delete-audio", dependencies=guard)
    def admin_delete_audio(meeting_id: str | None = Query(default=None)) -> dict[str, Any]:
        removed = state.repository.delete_audio_only(meeting_id)
        state.events.publish("audio_deleted", {"count": removed})
        return {"deleted": removed}

    @app.post("/admin/purge", dependencies=guard)
    def admin_purge(body: PurgeRequest) -> dict[str, Any]:
        deleted = state.repository.purge_expired(body.retention_days)
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
