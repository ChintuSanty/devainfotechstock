# MeetingScribe

A local-first desktop meeting assistant for Windows. It records **both sides of a
meeting** — the system audio (everyone else) and your microphone (you) —
transcribes them on-device, tells the participants apart by voice, and writes
the **minutes of meeting plus practical suggestions** using a local LLM.

Nothing leaves the machine. No account, no cloud, no upload. Everything is
stored as **plain text** in a folder you choose, and you can wipe any of it from
the app's admin panel.

```
┌───────────────────────────────────────────────────────────────┐
│  Floating always-on-top icon        Main window               │
│  (record / pause / stop)            Live · Meetings · Admin   │
└──────────────────────┬────────────────────────────────────────┘
                       │  loopback HTTP + WebSocket (token-authed)
┌──────────────────────▼────────────────────────────────────────┐
│  Python sidecar                                               │
│                                                               │
│  WASAPI loopback ─┐         ┌─► voice ID ─┐                   │
│  (all meeting apps)├─► VAD ─►┤             ├─► text files ─┐   │
│  Microphone ───────┘         └─► Whisper ──┘               │   │
│                                                            ▼   │
│                                    Ollama ──► Minutes + Suggestions
└───────────────────────────────────────────────────────────────┘
```

## How it captures every meeting app

Windows mixes all playback through one audio endpoint. MeetingScribe opens a
**WASAPI loopback** stream on that endpoint, so it hears whatever the machine is
playing: Zoom, Microsoft Teams, Google Meet in a browser, Webex, Slack huddles,
Discord — including apps released after this was written. There is no per-app
integration, no virtual audio cable to install and no meeting bot to invite.

Your microphone is captured as a **separate stream**, so you are always
distinguishable from the room. Turning on speaker identification goes further
and splits the meeting audio into individual people (see below).

## Telling people apart

Whisper transcribes what was said, not who said it. MeetingScribe separates
speakers in two stages:

1. **Channel split, always on.** Your microphone is `You`; everything from the
   meeting audio is someone else. No model needed.
2. **Voice identification, opt-in.** Each utterance in the meeting audio gets a
   speaker embedding, and utterances whose voices match are grouped into `S1`,
   `S2` and so on. Name them once in the meeting view — the transcript, the
   minutes, the suggestions and every export use the names from then on.

Labels are assigned live, so they appear during the call. Live labelling is
order-dependent and can misjudge an early speaker, so the fingerprints are saved
next to the transcript and **Re-detect speakers** re-clusters the whole meeting
in one pass afterwards. That works from the saved vectors, so it needs no audio.

Voice identification needs one optional model:

```bash
pip install -r sidecar/requirements-speakers.txt   # pulls in PyTorch, ~2 GB
```

Without it everything else works exactly as before and the other participants
stay labelled `Participant`. Voice separation is good, not perfect — treat the
labels as a strong hint, not evidence.

## Storage: plain text, wherever you want it

Every meeting is a folder of files you can read, grep, sync or delete without
this app:

```
<your folder>/2026-09-04_1030_budget-review_a1b2c3d4/
  meeting.json      metadata: title, times, status, speaker names
  transcript.md     timestamped, speaker-labelled, human readable
  transcript.jsonl  one JSON object per line, append-only
  minutes.md        the generated minutes of meeting
  suggestions.md    follow-ups, gaps, risks, next-meeting prep
  notes.md          the intermediate notes the minutes were built from
  speakers.jsonl    voice fingerprints, only when speaker ID is on
  audio.wav         only if you opt in to keeping audio
```

`transcript.jsonl` is the source of truth and is appended one line at a time, so
a crash mid-meeting costs at most the last utterance; `transcript.md` is a
rendering of it that is rebuilt whenever speaker names change.

Pick the folder in **Settings → Storage** or **Privacy & Data**, with a native
folder picker. Point it at a synced drive, an encrypted volume or a backed-up
directory; moving it offers to bring the existing meetings along. Settings
themselves live in `settings.json` in the app directory, since the storage
location has to be readable before the store is opened.

## Requirements

| | |
|---|---|
| OS | Windows 10 (1903+) or Windows 11 |
| Python | 3.10 – 3.12, on `PATH` |
| Node.js | 18+ (only to build/run from source) |
| [Ollama](https://ollama.com) | For the minutes and suggestions |
| RAM | 8 GB (16 GB comfortable) |
| Disk | ~2 GB for the Whisper and LLM models |
| Optional | `sidecar/requirements-speakers.txt` for per-speaker identification |

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
5. Open the **Transcript** tab to name the voices it found, or re-run detection.
6. Export any meeting as Markdown, plain text or JSON — or just open the meeting
   folder, since it is already text.

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
- **Everything is plain text** in the folder you chose, one folder per meeting.
  Nothing is in a database or a proprietary format.
- **Delete anything, any time**: individual meetings, all saved audio while
  keeping transcripts, or every trace of every meeting.
- **Deletion is real.** The meeting's whole folder is removed from disk, and
  only paths inside the configured storage folder are ever touched.
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
| Storage folder | app directory | Any folder you can write to |
| Identify speakers | off | Needs the optional voice model |
| Voice match strictness | 0.62 | Lower merges voices, higher splits them |

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
  speakers/        Voice embeddings and online/offline speaker clustering
  llm/             Ollama client, prompts, map-reduce summariser
  pipeline/        The recording session that ties it all together
  storage/         Plain-text meeting store, settings file, retention
  detect/          Meeting-app detection (labelling only)
```

## Tests

```bash
npm run typecheck                 # renderer + main process
python -m pytest sidecar/tests -q # 106 tests
```

Covering resampling, VAD segmentation, the text store, settings, speaker
clustering, summarisation and the full HTTP API. They run anywhere — no sound
card, no models and no LLM needed.

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

**One person shows up as two speakers** (or two people as one) — adjust *Voice
match strictness* in Settings, then hit **Re-detect speakers** on the meeting.
Lower merges, higher splits.

**Speaker identification is greyed out** — install
`sidecar/requirements-speakers.txt` and restart the local service from Settings.

## Roadmap

- macOS support via ScreenCaptureKit
- Remembering voices across meetings, so names carry over
- Calendar hooks to name meetings and pre-fill attendees
