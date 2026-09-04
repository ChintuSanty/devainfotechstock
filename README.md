# MeetingScribe

A local-first desktop meeting assistant for Windows. It records **both sides of a
meeting** — the system audio (everyone else) and your microphone (you) —
transcribes them on-device, and writes the **minutes of meeting plus practical
suggestions** using a local LLM.

Nothing leaves the machine. No account, no cloud, no upload. Everything is
stored in one folder that you can inspect and wipe from the app's admin panel.

```
┌───────────────────────────────────────────────────────────────┐
│  Floating always-on-top icon        Main window               │
│  (record / pause / stop)            Live · Meetings · Admin   │
└──────────────────────┬────────────────────────────────────────┘
                       │  loopback HTTP + WebSocket (token-authed)
┌──────────────────────▼────────────────────────────────────────┐
│  Python sidecar                                               │
│                                                               │
│  WASAPI loopback ─┐                                           │
│  (all meeting apps)├─► VAD ─► faster-whisper ─► SQLite ─┐     │
│  Microphone ───────┘                                    │     │
│                                                         ▼     │
│                                    Ollama ──► Minutes + Suggestions
└───────────────────────────────────────────────────────────────┘
```

## How it captures every meeting app

Windows mixes all playback through one audio endpoint. MeetingScribe opens a
**WASAPI loopback** stream on that endpoint, so it hears whatever the machine is
playing: Zoom, Microsoft Teams, Google Meet in a browser, Webex, Slack huddles,
Discord — including apps released after this was written. There is no per-app
integration, no virtual audio cable to install and no meeting bot to invite.

Your microphone is captured as a **separate stream**. That gives free speaker
attribution without a diarisation model: the mic channel is `You`, the loopback
channel is `Participant`.

## Requirements

| | |
|---|---|
| OS | Windows 10 (1903+) or Windows 11 |
| Python | 3.10 – 3.12, on `PATH` |
| Node.js | 18+ (only to build/run from source) |
| [Ollama](https://ollama.com) | For the minutes and suggestions |
| RAM | 8 GB (16 GB comfortable) |
| Disk | ~2 GB for the Whisper and LLM models |

A GPU is optional. On CPU the default `small.en` model transcribes roughly 3x
faster than realtime, which keeps up with a live meeting comfortably.

## Setup

```bash
# 1. Front end
npm install

# 2. Sidecar (a virtualenv inside sidecar/ is picked up automatically)
python -m venv sidecar/.venv
sidecar\.venv\Scripts\pip install -r sidecar/requirements.txt

# 3. Local LLM
ollama pull qwen3:4b        # ~2.5 GB, good quality/speed on CPU

# 4. Run
npm run dev
```

The first recording downloads the Whisper model (~470 MB for `small.en`) into
your Hugging Face cache. After that the app works fully offline.

Build a Windows installer with `npm run package`; the output lands in `release/`.

## Using it

1. The floating icon sits on top of every window, including full-screen Zoom
   and Teams. Drag it anywhere; click it to open the controls.
2. Hit **Record** when the meeting starts, or press <kbd>Ctrl</kbd> +
   <kbd>Shift</kbd> + <kbd>R</kbd> from anywhere.
3. The transcript appears live in the main window as people speak.
4. Hit **Stop**. The minutes and suggestions are written automatically and show
   up under **Meetings**.
5. Export any meeting as Markdown, plain text or JSON.

### What the LLM produces

**Minutes** — summary, key discussion points, decisions, an action-item table
with owners and due dates, open questions and next steps.

**Suggestions** — immediate follow-ups, gaps and unresolved items, risks to
watch, and recommendations for the next meeting.

Long meetings are map-reduced: the transcript is condensed chunk by chunk, then
written up once, so a two-hour call fits inside a small model's context window.

## Privacy and your data

Open **Privacy & Data** in the app to see exactly what is stored and to delete
any of it.

- **Audio is not kept by default.** Only the transcript and the generated
  documents are stored. Saving audio is an explicit opt-in.
- **Everything is in one SQLite file** under
  `%APPDATA%\MeetingScribe\data\meetingscribe.db`.
- **Delete anything, any time**: individual meetings, all saved audio while
  keeping transcripts, or every trace of every meeting.
- **Deletion is real.** Rows are removed and the database is `VACUUM`ed, so the
  text is not left sitting in free pages. Audio files are unlinked, and only
  files inside the app's own `recordings` folder are ever touched.
- **Automatic retention** can delete meetings older than 7/30/90/365 days. The
  sweep runs on every launch.
- **The local API is locked down**: the sidecar binds to `127.0.0.1` on a random
  port and mints a fresh bearer token each launch. Requests without it get a
  401, and the token is worthless once the app closes.

> Recording a meeting may require the consent of the other participants
> depending on where you and they are. MeetingScribe does not notify anyone that
> recording is happening — that is your call to make.

## Configuration worth knowing

| Setting | Default | Notes |
|---|---|---|
| Whisper model | `small.en` | `tiny.en`/`base.en` for old machines, `medium.en` if you have a GPU |
| Compute device | auto | CUDA float16 when available, otherwise CPU int8 |
| Silence before a line closes | 700 ms | Lower is snappier, higher avoids chopped sentences |
| LLM | `qwen3:4b` via Ollama | Any Ollama model, or any OpenAI-compatible server |
| Store audio | off | Transcript-only by default |
| Retention | keep forever | Or 7/30/90/365 days |

Swapping in `llama3.1:8b` or `mistral-nemo` gives noticeably better minutes if
you have the RAM; point **Settings → Local language model** at any
OpenAI-compatible server (llama.cpp, LM Studio, vLLM) to use something else.

## Layout

```
electron/          Main process: window management, sidecar supervision, tray
  main.ts          App lifecycle, IPC, global shortcut
  sidecar.ts       Spawns Python, reads the handshake, restarts on demand
  windows.ts       Overlay (frameless, always-on-top) and main window
  preload.ts       The only renderer↔main bridge
src/
  overlay/         The floating icon and its mini controls
  panel/           Live view, meeting library, settings, admin panel
  shared/          API client, event socket, formatting
sidecar/app/
  server.py        Loopback FastAPI app: REST + event WebSocket
  audio/           WASAPI loopback + microphone capture, resampling, VAD
  stt/             faster-whisper engine
  llm/             Ollama client, prompts, map-reduce summariser
  pipeline/        The recording session that ties it all together
  storage/         SQLite schema, repository, retention and deletion
  detect/          Meeting-app detection (labelling only)
```

## Tests

```bash
npm run typecheck                 # renderer + main process
python -m pytest sidecar/tests -q # 65 tests: audio, storage, LLM, HTTP API
```

The audio and API tests run anywhere — no sound card, no models and no LLM
needed.

## Troubleshooting

**"No WASAPI loopback device found"** — enable an output device in Windows sound
settings. If you use exclusive-mode audio software, release the endpoint first.

**Participants are transcribed but you are not** (or vice versa) — check the
device pickers in **Settings → Audio capture**; headsets often expose several
endpoints.

**"Cannot reach the model server"** — start Ollama (`ollama serve`) and pull the
model named in Settings.

**Transcription lags behind the meeting** — drop to `base.en`, or switch the
compute device to CUDA if you have an NVIDIA GPU.

## Roadmap

- macOS support via ScreenCaptureKit
- Speaker diarisation within the participant channel
- Calendar hooks to name meetings and pre-fill attendees
