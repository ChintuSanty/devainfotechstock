import { useCallback, useEffect, useState } from 'react';
import { api } from '../../shared/api';
import { formatDate, formatDuration } from '../../shared/format';
import { useSidecarEvents } from '../../shared/useSidecarEvents';
import type { Meeting } from '../../shared/types';
import { MeetingDetail } from './MeetingDetail';

export function MeetingsView({ version }: { version: number }) {
  const [meetings, setMeetings] = useState<Meeting[]>([]);
  const [search, setSearch] = useState('');
  const [selected, setSelected] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (term: string) => {
    setLoading(true);
    try {
      setMeetings(await api.listMeetings(term));
      setError(null);
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(search), search ? 250 : 0);
    return () => window.clearTimeout(timer);
  }, [search, version, load]);

  useSidecarEvents((event) => {
    if (event.type === 'summary_ready' || event.type === 'summary_failed') void load(search);
  });

  if (selected) {
    return (
      <MeetingDetail
        meetingId={selected}
        onBack={() => {
          setSelected(null);
          void load(search);
        }}
      />
    );
  }

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Meetings</h1>
          <p>Every recording, transcript and set of minutes stored on this computer.</p>
        </div>
        <input
          style={{ width: 280 }}
          placeholder="Search titles and transcripts"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
      </div>

      {error && <div className="error-banner">{error}</div>}

      {loading && meetings.length === 0 ? (
        <div className="empty">Loading...</div>
      ) : meetings.length === 0 ? (
        <div className="empty">
          {search ? `Nothing matches "${search}".` : 'No meetings yet. Hit record when your next call starts.'}
        </div>
      ) : (
        <div className="list">
          {meetings.map((meeting) => (
            <div key={meeting.id} className="list-item">
              <div className="grow">
                <div className="title">{meeting.title}</div>
                <div className="meta">
                  {formatDate(meeting.started_at)} &middot; {formatDuration(meeting.duration_ms)} &middot;{' '}
                  {meeting.segment_count ?? 0} lines
                  {meeting.source_app ? ` · ${meeting.source_app}` : ''}
                </div>
              </div>
              <StatusBadge status={meeting.status} />
              <button onClick={() => setSelected(meeting.id)}>Open</button>
            </div>
          ))}
        </div>
      )}
    </>
  );
}

export function StatusBadge({ status }: { status: Meeting['status'] }) {
  const map: Record<Meeting['status'], { text: string; className: string }> = {
    recording: { text: 'Recording', className: 'live' },
    recorded: { text: 'Transcript only', className: '' },
    summarising: { text: 'Writing minutes', className: 'warn' },
    summarised: { text: 'Minutes ready', className: 'ok' },
    failed: { text: 'Failed', className: 'warn' },
  };
  const value = map[status] ?? { text: status, className: '' };
  return <span className={`badge ${value.className}`}>{value.text}</span>;
}
