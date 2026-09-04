import { useCallback, useEffect, useState } from 'react';
import { api } from '../../shared/api';
import { formatBytes, formatDate, formatDuration } from '../../shared/format';
import type { AppSettings, Meeting, StorageStats } from '../../shared/types';

/**
 * The admin panel: what is stored, and every way to delete it. Deletion is
 * permanent - rows are removed and the database is VACUUMed so the text is not
 * left behind in free pages.
 */
export function AdminView({ version, onChanged }: { version: number; onChanged: () => void }) {
  const [stats, setStats] = useState<StorageStats | null>(null);
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [meetings, setMeetings] = useState<Meeting[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [nextStats, nextSettings, nextMeetings] = await Promise.all([
        api.stats(),
        api.getSettings(),
        api.listMeetings(),
      ]);
      setStats(nextStats);
      setSettings(nextSettings);
      setMeetings(nextMeetings);
      setSelected(new Set());
      setError(null);
    } catch (cause) {
      setError((cause as Error).message);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load, version]);

  const run = async (action: () => Promise<string>) => {
    setBusy(true);
    setError(null);
    try {
      setMessage(await action());
      await load();
      onChanged();
      window.setTimeout(() => setMessage(null), 4000);
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const toggle = (id: string) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Privacy &amp; Data</h1>
          <p>Everything MeetingScribe knows lives in one folder on this computer. You can wipe any of it here.</p>
        </div>
      </div>

      {error && <div className="error-banner">{error}</div>}
      {message && <div className="info-banner">{message}</div>}

      <div className="card">
        <h2>What is stored</h2>
        <p className="hint" style={{ wordBreak: 'break-all' }}>{stats?.data_dir ?? ''}</p>
        <div className="grid-2">
          <div className="stat">
            <div className="value">{stats?.meetings ?? 0}</div>
            <div className="label">Meetings</div>
          </div>
          <div className="stat">
            <div className="value">{stats?.segments ?? 0}</div>
            <div className="label">Transcript lines</div>
          </div>
          <div className="stat">
            <div className="value">{formatBytes(stats?.database_bytes ?? 0)}</div>
            <div className="label">Database</div>
          </div>
          <div className="stat">
            <div className="value">{formatBytes(stats?.audio_bytes ?? 0)}</div>
            <div className="label">Audio recordings</div>
          </div>
        </div>
        {stats?.oldest_meeting_at && (
          <p className="hint" style={{ marginTop: 12, marginBottom: 0 }}>
            Oldest meeting kept: {formatDate(stats.oldest_meeting_at)}
          </p>
        )}
      </div>

      <div className="card">
        <h2>Retention</h2>
        <p className="hint">
          Audio is not saved by default - only the transcript and the minutes. Retention is applied every time the app
          starts.
        </p>
        <div className="stack">
          <label className="switch">
            <input
              type="checkbox"
              checked={settings?.store_audio ?? false}
              disabled={!settings}
              onChange={(event) =>
                void run(async () => {
                  setSettings(await api.saveSettings({ store_audio: event.target.checked }));
                  return event.target.checked
                    ? 'Audio will be saved with new recordings.'
                    : 'Audio will no longer be saved.';
                })
              }
            />
            Keep the audio recording as well as the transcript
          </label>

          <div className="field" style={{ maxWidth: 320 }}>
            <label>Delete meetings older than</label>
            <select
              value={settings?.retention_days ?? 0}
              disabled={!settings}
              onChange={(event) =>
                void run(async () => {
                  const days = Number(event.target.value);
                  setSettings(await api.saveSettings({ retention_days: days }));
                  const deleted = (await api.purge(days)).deleted;
                  return days === 0
                    ? 'Meetings will be kept until you delete them.'
                    : `Retention set to ${days} days. ${deleted} old meeting(s) removed.`;
                })
              }
            >
              <option value={0}>Keep forever</option>
              <option value={7}>7 days</option>
              <option value={30}>30 days</option>
              <option value={90}>90 days</option>
              <option value={365}>1 year</option>
            </select>
          </div>
        </div>
      </div>

      <div className="card">
        <h2>Delete individual meetings</h2>
        <p className="hint">Select the meetings you want gone, then delete them permanently.</p>
        {meetings.length === 0 ? (
          <div className="empty">There is nothing stored.</div>
        ) : (
          <>
            <div className="list" style={{ maxHeight: 320, overflowY: 'auto' }}>
              {meetings.map((meeting) => (
                <label key={meeting.id} className="list-item" style={{ cursor: 'pointer' }}>
                  <input type="checkbox" checked={selected.has(meeting.id)} onChange={() => toggle(meeting.id)} />
                  <div className="grow">
                    <div className="title">{meeting.title}</div>
                    <div className="meta">
                      {formatDate(meeting.started_at)} &middot; {formatDuration(meeting.duration_ms)} &middot;{' '}
                      {meeting.segment_count ?? 0} lines
                      {meeting.audio_path ? ' · audio saved' : ''}
                    </div>
                  </div>
                </label>
              ))}
            </div>
            <div className="row" style={{ marginTop: 14 }}>
              <button
                className="danger"
                disabled={busy || selected.size === 0}
                onClick={() =>
                  void run(async () => {
                    if (!window.confirm(`Permanently delete ${selected.size} meeting(s)?`)) return 'Cancelled.';
                    const { deleted } = await api.deleteMany([...selected]);
                    return `Deleted ${deleted} meeting(s).`;
                  })
                }
              >
                Delete selected ({selected.size})
              </button>
              <button
                className="ghost"
                disabled={meetings.length === 0}
                onClick={() => setSelected(new Set(meetings.map((meeting) => meeting.id)))}
              >
                Select all
              </button>
            </div>
          </>
        )}
      </div>

      <div className="card danger-zone">
        <h2>Erase everything</h2>
        <p className="hint">
          These actions cannot be undone. Nothing is backed up anywhere, so deleted data is gone for good.
        </p>
        <div className="row">
          <button
            className="danger"
            disabled={busy}
            onClick={() =>
              void run(async () => {
                if (!window.confirm('Delete all saved audio recordings? Transcripts and minutes are kept.'))
                  return 'Cancelled.';
                const { deleted } = await api.deleteAudio();
                return `Deleted ${deleted} audio file(s).`;
              })
            }
          >
            Delete all audio recordings
          </button>
          <button
            className="danger"
            disabled={busy}
            onClick={() =>
              void run(async () => {
                if (!window.confirm('Delete every meeting, transcript, set of minutes and recording?'))
                  return 'Cancelled.';
                if (!window.confirm('This is permanent and cannot be undone. Erase everything?')) return 'Cancelled.';
                const { deleted } = await api.deleteAll();
                return `Erased ${deleted} meeting(s). Nothing is left.`;
              })
            }
          >
            Erase all meeting data
          </button>
        </div>
      </div>
    </>
  );
}
