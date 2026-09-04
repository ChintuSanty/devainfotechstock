import { useCallback, useEffect, useState } from 'react';
import { api, resetConnection } from '../shared/api';
import { bridge } from '../shared/bridge';
import { useSidecarEvents } from '../shared/useSidecarEvents';
import type { RecordingStatus, Segment } from '../shared/types';
import { AdminView } from './views/AdminView';
import { LiveView } from './views/LiveView';
import { MeetingsView } from './views/MeetingsView';
import { SettingsView } from './views/SettingsView';

export type Tab = 'live' | 'meetings' | 'settings' | 'admin';

const TABS: { id: Tab; label: string }[] = [
  { id: 'live', label: 'Live' },
  { id: 'meetings', label: 'Meetings' },
  { id: 'settings', label: 'Settings' },
  { id: 'admin', label: 'Privacy & Data' },
];

const IDLE: RecordingStatus = {
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

export function App() {
  const [tab, setTab] = useState<Tab>('live');
  const [status, setStatus] = useState<RecordingStatus>(IDLE);
  const [liveSegments, setLiveSegments] = useState<Segment[]>([]);
  const [meetingsVersion, setMeetingsVersion] = useState(0);
  const [connectionError, setConnectionError] = useState<string | null>(null);

  const refreshStatus = useCallback(async () => {
    try {
      setStatus(await api.status());
      setConnectionError(null);
    } catch (cause) {
      setConnectionError((cause as Error).message);
    }
  }, []);

  useEffect(() => {
    void refreshStatus();
  }, [refreshStatus]);

  useEffect(() => {
    return bridge()?.onAppEvent((event) => {
      if (event.type === 'navigate') {
        const next = (event.payload as { tab?: Tab })?.tab;
        if (next) setTab(next);
      }
      if (event.type === 'sidecar-down') {
        setConnectionError('The local recording service stopped. Restart it from Settings.');
      }
      if (event.type === 'sidecar-up') {
        resetConnection();
        void refreshStatus();
      }
    });
  }, [refreshStatus]);

  useSidecarEvents((event) => {
    switch (event.type) {
      case 'hello':
        setStatus(event.payload as unknown as RecordingStatus);
        setConnectionError(null);
        break;
      case 'recording_started':
        setLiveSegments([]);
        void refreshStatus();
        setTab('live');
        break;
      case 'recording_stopped':
      case 'recording_paused':
      case 'recording_resumed':
        void refreshStatus();
        setMeetingsVersion((n) => n + 1);
        break;
      case 'segment':
        setLiveSegments((current) => [...current, event.payload as unknown as Segment]);
        break;
      case 'summary_ready':
      case 'meeting_deleted':
      case 'meetings_deleted':
        setMeetingsVersion((n) => n + 1);
        break;
      default:
        break;
    }
  });

  const recording = status.state === 'recording' || status.state === 'paused';

  return (
    <div className="app">
      <nav className="sidebar">
        <div className="brand">
          <span className="dot" />
          MeetingScribe
        </div>
        {TABS.map((item) => (
          <button
            key={item.id}
            className={`nav-item ${tab === item.id ? 'active' : ''}`}
            onClick={() => setTab(item.id)}
          >
            {item.label}
            {item.id === 'live' && recording && <span className="pulse" style={{ color: 'var(--danger)' }} />}
          </button>
        ))}
        <div className="sidebar-footer">
          Everything stays on this computer.
          <br />
          No account, no cloud, no upload.
        </div>
      </nav>

      <main className="content">
        {connectionError && <div className="error-banner">{connectionError}</div>}
        {tab === 'live' && (
          <LiveView status={status} segments={liveSegments} onStatusChange={refreshStatus} />
        )}
        {tab === 'meetings' && <MeetingsView version={meetingsVersion} />}
        {tab === 'settings' && <SettingsView />}
        {tab === 'admin' && <AdminView version={meetingsVersion} onChanged={() => setMeetingsVersion((n) => n + 1)} />}
      </main>
    </div>
  );
}
