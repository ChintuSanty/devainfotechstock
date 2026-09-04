import { useCallback, useEffect, useState } from 'react';
import { api } from '../../shared/api';
import { formatDate, formatDuration, formatTimestamp } from '../../shared/format';
import { useSidecarEvents } from '../../shared/useSidecarEvents';
import type { Artifact, Meeting, Segment, Speaker } from '../../shared/types';
import { Markdown } from '../components/Markdown';
import { SpeakerRoster } from '../components/SpeakerRoster';
import { StatusBadge } from './MeetingsView';

type DetailTab = 'minutes' | 'suggestions' | 'transcript';

export function MeetingDetail({ meetingId, onBack }: { meetingId: string; onBack: () => void }) {
  const [meeting, setMeeting] = useState<Meeting | null>(null);
  const [segments, setSegments] = useState<Segment[]>([]);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [speakers, setSpeakers] = useState<Speaker[]>([]);
  const [tab, setTab] = useState<DetailTab>('minutes');
  const [progress, setProgress] = useState<{ stage: string; value: number } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [draftTitle, setDraftTitle] = useState('');

  const load = useCallback(async () => {
    try {
      const data = await api.getMeeting(meetingId);
      setMeeting(data.meeting);
      setSegments(data.segments);
      setArtifacts(data.artifacts);
      setSpeakers(data.speakers ?? []);
      setDraftTitle(data.meeting.title);
      setError(null);
    } catch (cause) {
      setError((cause as Error).message);
    }
  }, [meetingId]);

  useEffect(() => {
    void load();
  }, [load]);

  useSidecarEvents((event) => {
    const payload = event.payload as { meeting_id?: string; stage?: string; progress?: number; message?: string };
    if (payload.meeting_id !== meetingId) return;
    if (event.type === 'summary_progress') {
      setProgress({ stage: payload.stage ?? '', value: payload.progress ?? 0 });
    }
    if (event.type === 'summary_ready') {
      setProgress(null);
      void load();
    }
    if (event.type === 'summary_failed') {
      setProgress(null);
      setError(payload.message ?? 'Could not generate the minutes.');
      void load();
    }
    if (event.type === 'speakers_changed') void load();
  });

  const byKind = Object.fromEntries(artifacts.map((artifact) => [artifact.kind, artifact]));
  const hasMinutes = Boolean(byKind.minutes);
  // Re-detection re-clusters stored fingerprints, which only exist when speaker
  // identification was on while the meeting was recorded.
  const hasVoicePrints = segments.some((segment) => /^S\d+$/.test(segment.speaker ?? ''));

  const regenerate = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.summarise(meetingId);
      setProgress({ stage: 'starting', value: 0 });
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const exportAs = async (fmt: 'md' | 'txt' | 'json') => {
    try {
      const { text, filename } = await api.exportMeeting(meetingId, fmt);
      const url = URL.createObjectURL(new Blob([text], { type: 'text/plain' }));
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = filename;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (cause) {
      setError((cause as Error).message);
    }
  };

  const remove = async () => {
    if (!window.confirm('Delete this meeting, its transcript and its minutes? This cannot be undone.')) return;
    try {
      await api.deleteMeeting(meetingId);
      onBack();
    } catch (cause) {
      setError((cause as Error).message);
    }
  };

  const saveTitle = async () => {
    const next = draftTitle.trim();
    if (!next || next === meeting?.title) {
      setRenaming(false);
      return;
    }
    try {
      await api.renameMeeting(meetingId, next);
      setRenaming(false);
      void load();
    } catch (cause) {
      setError((cause as Error).message);
    }
  };

  if (!meeting) {
    return (
      <>
        <button className="ghost" onClick={onBack}>&larr; Back</button>
        {error ? <div className="error-banner">{error}</div> : <div className="empty">Loading...</div>}
      </>
    );
  }

  return (
    <>
      <button className="ghost" onClick={onBack} style={{ marginBottom: 12 }}>
        &larr; All meetings
      </button>

      <div className="page-header">
        <div style={{ minWidth: 0 }}>
          {renaming ? (
            <div className="row">
              <input
                style={{ width: 380 }}
                value={draftTitle}
                autoFocus
                onChange={(event) => setDraftTitle(event.target.value)}
                onKeyDown={(event) => event.key === 'Enter' && void saveTitle()}
              />
              <button onClick={saveTitle}>Save</button>
              <button className="ghost" onClick={() => setRenaming(false)}>Cancel</button>
            </div>
          ) : (
            <h1 onDoubleClick={() => setRenaming(true)} title="Double-click to rename">
              {meeting.title}
            </h1>
          )}
          <p>
            {formatDate(meeting.started_at)} &middot; {formatDuration(meeting.duration_ms)} &middot; {segments.length} lines
            {meeting.source_app ? ` · ${meeting.source_app}` : ''}
          </p>
        </div>
        <div className="row">
          <StatusBadge status={meeting.status} />
          <button onClick={() => exportAs('md')}>Export .md</button>
          <button onClick={() => exportAs('json')}>.json</button>
          <button className="danger" onClick={remove}>Delete</button>
        </div>
      </div>

      {error && <div className="error-banner">{error}</div>}
      {progress && (
        <div className="info-banner">
          Generating with the local model - {progress.stage.replace(/_/g, ' ')} ({Math.round(progress.value * 100)}%)
        </div>
      )}

      <div className="tabs">
        {(['minutes', 'suggestions', 'transcript'] as DetailTab[]).map((item) => (
          <button key={item} className={`tab ${tab === item ? 'active' : ''}`} onClick={() => setTab(item)}>
            {item === 'minutes' ? 'Minutes' : item === 'suggestions' ? 'Suggestions' : 'Transcript'}
          </button>
        ))}
      </div>

      {tab !== 'transcript' && (
        <div className="card">
          {byKind[tab] ? (
            <>
              <Markdown source={byKind[tab].content} />
              <div className="row" style={{ marginTop: 18 }}>
                <button onClick={regenerate} disabled={busy || Boolean(progress)}>
                  Regenerate
                </button>
                <span className="badge">{byKind[tab].model ?? 'local model'}</span>
              </div>
            </>
          ) : (
            <div className="empty">
              <p>
                {segments.length === 0
                  ? 'This meeting has no transcript, so there is nothing to summarise.'
                  : hasMinutes
                    ? 'Not generated yet.'
                    : 'Minutes and suggestions have not been generated for this meeting.'}
              </p>
              {segments.length > 0 && (
                <button className="primary" onClick={regenerate} disabled={busy || Boolean(progress)}>
                  Generate now
                </button>
              )}
            </div>
          )}
        </div>
      )}

      {tab === 'transcript' && (
        <>
          <SpeakerRoster
            meetingId={meetingId}
            speakers={speakers}
            canRedetect={hasVoicePrints}
            onChanged={load}
          />
          <div className="card">
            {segments.length === 0 ? (
              <div className="empty">No speech was transcribed for this meeting.</div>
            ) : (
              <>
                <div className="row between" style={{ marginBottom: 14 }}>
                  <p className="hint" style={{ margin: 0 }}>
                    &ldquo;You&rdquo; is your microphone; everyone else was captured from the meeting audio.
                  </p>
                  <button onClick={() => exportAs('txt')}>Export transcript</button>
                </div>
                <div className="transcript">
                  {segments.map((segment) => (
                    <div key={segment.id} className={`turn ${segment.channel}`}>
                      <span className="time">{formatTimestamp(segment.start_ms)}</span>
                      <span>
                        <span className={`who ${isNamed(segment) ? 'named' : ''}`}>{speakerOf(segment)}</span>
                        {segment.text}
                      </span>
                    </div>
                  ))}
                </div>
              </>
            )}
          </div>
        </>
      )}
    </>
  );
}

function speakerOf(segment: Segment): string {
  return segment.speaker_name || segment.speaker || (segment.channel === 'me' ? 'You' : 'Participant');
}

function isNamed(segment: Segment): boolean {
  const name = speakerOf(segment);
  return name !== 'You' && name !== 'Participant';
}
