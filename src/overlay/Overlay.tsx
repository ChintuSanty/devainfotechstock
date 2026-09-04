import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../shared/api';
import { bridge } from '../shared/bridge';
import { formatDuration } from '../shared/format';
import { useSidecarEvents } from '../shared/useSidecarEvents';
import type { MeetingApp, RecordingStatus, Segment } from '../shared/types';

const IDLE_STATUS: RecordingStatus = {
  state: 'idle',
  meeting_id: null,
  title: null,
  started_at: null,
  elapsed_ms: 0,
  segment_count: 0,
  pending_utterances: 0,
  error: null,
  channels: {},
};

/**
 * The always-on-top widget. Collapsed it is a single draggable orb; clicking it
 * expands the recording controls in place, and the main window is only ever a
 * click away.
 */
export function Overlay() {
  const [status, setStatus] = useState<RecordingStatus>(IDLE_STATUS);
  const [expanded, setExpanded] = useState(false);
  const [levels, setLevels] = useState<{ me: number; others: number }>({ me: 0, others: 0 });
  const [lastLine, setLastLine] = useState('');
  const [apps, setApps] = useState<MeetingApp[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const elapsedBase = useRef({ at: 0, ms: 0 });
  const [, forceTick] = useState(0);

  const refresh = useCallback(async () => {
    try {
      const next = await api.status();
      setStatus(next);
      elapsedBase.current = { at: Date.now(), ms: next.elapsed_ms };
      setError(null);
    } catch (cause) {
      setError((cause as Error).message);
    }
  }, []);

  useEffect(() => {
    void refresh();
    void api.meetingApps().then(setApps).catch(() => undefined);
  }, [refresh]);

  useSidecarEvents((event) => {
    switch (event.type) {
      case 'hello':
      case 'recording_started':
      case 'recording_stopped':
      case 'recording_paused':
      case 'recording_resumed':
        void refresh();
        break;
      case 'level': {
        const { channel, level } = event.payload as { channel: 'me' | 'others'; level: number };
        setLevels((current) => ({ ...current, [channel]: level }));
        break;
      }
      case 'segment': {
        const segment = event.payload as unknown as Segment;
        const who = segment.speaker_name || segment.speaker || (segment.channel === 'me' ? 'You' : 'Participant');
        setLastLine(`${who}: ${segment.text}`);
        break;
      }
      case 'meeting_apps':
        setApps((event.payload as { apps: MeetingApp[] }).apps);
        break;
      case 'recording_error':
        setError(String((event.payload as { message?: string }).message ?? 'Recording error'));
        break;
      default:
        break;
    }
  });

  // Local clock so the timer ticks without polling the sidecar every second.
  useEffect(() => {
    if (status.state !== 'recording') return;
    const timer = window.setInterval(() => forceTick((n) => n + 1), 1000);
    return () => window.clearInterval(timer);
  }, [status.state]);

  // The global shortcut is handled by the main process and relayed here.
  useEffect(() => {
    return bridge()?.onAppEvent((event) => {
      if (event.type === 'shortcut' && (event.payload as { name?: string })?.name === 'toggle-recording') {
        void toggle();
      }
    });
  });

  const setPanel = (open: boolean) => {
    setExpanded(open);
    void bridge()?.setOverlayExpanded(open);
  };

  const elapsedMs =
    status.state === 'recording'
      ? elapsedBase.current.ms + (Date.now() - elapsedBase.current.at)
      : status.elapsed_ms;

  const toggle = async () => {
    setBusy(true);
    setError(null);
    try {
      if (status.state === 'idle' || status.state === 'error') {
        await api.start(undefined, apps.map((app) => app.app).join(', ') || undefined);
      } else {
        await api.stop();
        setLevels({ me: 0, others: 0 });
        setLastLine('');
      }
      await refresh();
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const togglePause = async () => {
    setBusy(true);
    try {
      await (status.state === 'paused' ? api.resume() : api.pause());
      await refresh();
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const recording = status.state === 'recording' || status.state === 'paused';
  const orbClass = status.state === 'paused' ? 'paused' : recording ? 'recording' : 'idle';

  if (!expanded) {
    return (
      <div className="overlay">
        <div className={`orb ${orbClass}`}>
          {status.state === 'recording' && <span className="ring" />}
          <button
            className="hit"
            title={recording ? `Recording - ${formatDuration(elapsedMs)}` : 'MeetingScribe - click to open'}
            onClick={() => setPanel(true)}
            onDoubleClick={() => bridge()?.openPanel('live')}
          >
            <span className="glyph" />
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="overlay">
      <div className="panel">
        <div className="drag-title">
          <span>MeetingScribe</span>
          <button className="ghost no-drag" style={{ padding: '0 6px' }} onClick={() => setPanel(false)}>
            &minus;
          </button>
        </div>

        <div className="status-line">
          <span className={`badge ${recording ? 'live' : ''}`}>
            {status.state === 'recording' && <span className="pulse" />}
            {label(status.state)}
          </span>
          <span className="elapsed">{formatDuration(elapsedMs)}</span>
        </div>

        {error && <div className="error-banner" style={{ fontSize: 12 }}>{error}</div>}

        {!recording && apps.length > 0 && (
          <div className="info-banner" style={{ fontSize: 12 }}>
            {apps.map((app) => app.app).join(', ')} detected
          </div>
        )}

        <div className="levels">
          <div className="level-row">
            <span className="tag">Meeting</span>
            <span className="meter">
              <span style={{ width: `${meterWidth(levels.others)}%` }} />
            </span>
          </div>
          <div className="level-row">
            <span className="tag">You</span>
            <span className="meter mic">
              <span style={{ width: `${meterWidth(levels.me)}%` }} />
            </span>
          </div>
        </div>

        <div className="actions">
          <button className={recording ? 'danger' : 'primary'} onClick={toggle} disabled={busy}>
            {recording ? 'Stop' : 'Record'}
          </button>
          {recording && (
            <button onClick={togglePause} disabled={busy}>
              {status.state === 'paused' ? 'Resume' : 'Pause'}
            </button>
          )}
          <button onClick={() => bridge()?.openPanel(recording ? 'live' : 'meetings')}>Open</button>
        </div>

        {lastLine && <div className="last-line">{lastLine}</div>}
      </div>
    </div>
  );
}

function label(state: RecordingStatus['state']): string {
  switch (state) {
    case 'recording':
      return 'Recording';
    case 'paused':
      return 'Paused';
    case 'starting':
      return 'Starting';
    case 'stopping':
      return 'Finishing';
    case 'error':
      return 'Error';
    default:
      return 'Ready';
  }
}

/** RMS is tiny for speech, so scale it logarithmically to read like a VU meter. */
function meterWidth(level: number): number {
  if (level <= 0) return 0;
  const db = 20 * Math.log10(level);
  return Math.max(0, Math.min(100, ((db + 60) / 60) * 100));
}
