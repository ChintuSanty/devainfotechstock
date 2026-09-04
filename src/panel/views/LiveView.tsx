import { useEffect, useRef, useState } from 'react';
import { api } from '../../shared/api';
import { formatDuration, formatTimestamp } from '../../shared/format';
import type { LlmHealth, MeetingApp, RecordingStatus, Segment } from '../../shared/types';

interface Props {
  status: RecordingStatus;
  segments: Segment[];
  onStatusChange: () => void;
}

export function LiveView({ status, segments, onStatusChange }: Props) {
  const [title, setTitle] = useState('');
  const [apps, setApps] = useState<MeetingApp[]>([]);
  const [llm, setLlm] = useState<LlmHealth | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [, tick] = useState(0);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    void api.meetingApps().then(setApps).catch(() => undefined);
    void api.llmHealth().then(setLlm).catch(() => undefined);
  }, []);

  useEffect(() => {
    if (status.state !== 'recording') return;
    const timer = window.setInterval(() => tick((n) => n + 1), 1000);
    return () => window.clearInterval(timer);
  }, [status.state]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [segments.length]);

  const recording = status.state === 'recording' || status.state === 'paused';

  const run = async (action: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await action();
      onStatusChange();
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Live</h1>
          <p>
            {recording
              ? 'Capturing meeting audio and your microphone.'
              : 'Start recording when your meeting begins. Everything runs on this machine.'}
          </p>
        </div>
        <div className="row">
          {llm && (
            <span className={`badge ${llm.available && llm.model_installed ? 'ok' : 'warn'}`}>
              {llm.available
                ? llm.model_installed
                  ? `${llm.model} ready`
                  : `${llm.model} not pulled`
                : 'Local model offline'}
            </span>
          )}
        </div>
      </div>

      {error && <div className="error-banner">{error}</div>}
      {status.error && <div className="error-banner">{status.error}</div>}

      <div className="card">
        <div className="row between">
          <div className="row" style={{ gap: 18 }}>
            <span className={`badge ${recording ? 'live' : ''}`}>
              {status.state === 'recording' && <span className="pulse" />}
              {stateLabel(status.state)}
            </span>
            <span className="timer">{formatDuration(status.elapsed_ms)}</span>
          </div>
          <div className="row">
            {!recording ? (
              <>
                <input
                  className="no-drag"
                  style={{ width: 260 }}
                  placeholder="Meeting title (optional)"
                  value={title}
                  onChange={(event) => setTitle(event.target.value)}
                />
                <button
                  className="primary"
                  disabled={busy}
                  onClick={() =>
                    run(async () => {
                      await api.start(title.trim() || undefined, apps.map((a) => a.app).join(', ') || undefined);
                      setTitle('');
                    })
                  }
                >
                  Start recording
                </button>
              </>
            ) : (
              <>
                <button disabled={busy} onClick={() => run(status.state === 'paused' ? api.resume : api.pause)}>
                  {status.state === 'paused' ? 'Resume' : 'Pause'}
                </button>
                <button className="danger" disabled={busy} onClick={() => run(api.stop)}>
                  Stop &amp; generate minutes
                </button>
              </>
            )}
          </div>
        </div>

        <div className="grid-2" style={{ marginTop: 18 }}>
          <ChannelMeter
            label="Meeting audio (everyone else)"
            hint="System sound from Zoom, Teams, Meet, Webex - any app"
            channel={status.channels.others}
          />
          <ChannelMeter label="Your microphone" hint="Captured separately so you are labelled apart" channel={status.channels.me} mic />
        </div>

        {apps.length > 0 && (
          <div className="info-banner" style={{ marginTop: 14, marginBottom: 0 }}>
            Detected: {apps.map((app) => app.app).join(', ')}
          </div>
        )}
      </div>

      <div className="card">
        <div className="row between">
          <div>
            <h2>Live transcript</h2>
            <p className="hint">
              {segments.length} line{segments.length === 1 ? '' : 's'}
              {status.pending_utterances > 0 && ` - ${status.pending_utterances} waiting to transcribe`}
            </p>
          </div>
        </div>

        {segments.length === 0 ? (
          <div className="empty">
            {recording ? 'Listening. Transcribed speech will appear here.' : 'No transcript yet.'}
          </div>
        ) : (
          <div className="transcript">
            {segments.map((segment) => (
              <div key={segment.id} className={`turn ${segment.channel}`}>
                <span className="time">{formatTimestamp(segment.start_ms)}</span>
                <span>
                  <span className="who">
                    {segment.speaker_name || segment.speaker || (segment.channel === 'me' ? 'You' : 'Participant')}
                  </span>
                  {segment.text}
                </span>
              </div>
            ))}
            <div ref={endRef} />
          </div>
        )}
      </div>
    </>
  );
}

function ChannelMeter({
  label,
  hint,
  channel,
  mic = false,
}: {
  label: string;
  hint: string;
  channel?: { active: boolean; level: number; error: string | null };
  mic?: boolean;
}) {
  const level = channel?.level ?? 0;
  const db = level > 0 ? 20 * Math.log10(level) : -Infinity;
  const width = Number.isFinite(db) ? Math.max(0, Math.min(100, ((db + 60) / 60) * 100)) : 0;

  return (
    <div className="stat">
      <div className="row between" style={{ marginBottom: 8 }}>
        <strong style={{ fontSize: 13 }}>{label}</strong>
        <span className={`badge ${channel?.active ? 'ok' : ''}`}>{channel?.active ? 'Live' : 'Idle'}</span>
      </div>
      <span className={`meter ${mic ? 'mic' : ''}`} style={{ display: 'block' }}>
        <span style={{ width: `${width}%` }} />
      </span>
      <div className="label" style={{ marginTop: 8 }}>{channel?.error ?? hint}</div>
    </div>
  );
}

function stateLabel(state: RecordingStatus['state']): string {
  const labels: Record<RecordingStatus['state'], string> = {
    idle: 'Ready',
    starting: 'Starting',
    recording: 'Recording',
    paused: 'Paused',
    stopping: 'Finishing',
    error: 'Error',
  };
  return labels[state];
}
