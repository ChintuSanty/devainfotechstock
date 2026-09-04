import { bridge } from './bridge';
import type {
  Artifact,
  AppSettings,
  DeviceInfo,
  LlmHealth,
  Meeting,
  MeetingApp,
  RecordingStatus,
  Segment,
  StorageStats,
} from './types';

interface Connection {
  baseUrl: string;
  token: string;
}

let connection: Connection | null = null;

async function conn(): Promise<Connection> {
  if (connection) return connection;
  const api = bridge();
  if (!api) throw new Error('MeetingScribe must run inside the desktop app.');
  const value = await api.getConnection();
  if (!value) throw new Error('The local recording service is not running yet.');
  connection = value;
  return value;
}

/** Called when the sidecar restarts and mints a new token. */
export function resetConnection(next?: Connection): void {
  connection = next ?? null;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const { baseUrl, token } = await conn();
  const response = await fetch(`${baseUrl}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
      ...(init.headers ?? {}),
    },
  });
  if (!response.ok) {
    let detail = `Request failed with ${response.status}`;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      /* keep the status-code message */
    }
    throw new Error(detail);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  health: () => request<Record<string, unknown>>('/health'),

  // Recording
  status: () => request<RecordingStatus>('/recording/status'),
  start: (title?: string, sourceApp?: string) =>
    request<Meeting>('/recording/start', {
      method: 'POST',
      body: JSON.stringify({ title: title ?? null, source_app: sourceApp ?? null }),
    }),
  stop: () => request<Meeting>('/recording/stop', { method: 'POST' }),
  pause: () => request<RecordingStatus>('/recording/pause', { method: 'POST' }),
  resume: () => request<RecordingStatus>('/recording/resume', { method: 'POST' }),

  // Meetings
  listMeetings: (search = '') =>
    request<{ meetings: Meeting[] }>(`/meetings?search=${encodeURIComponent(search)}`).then((r) => r.meetings),
  getMeeting: (id: string) =>
    request<{ meeting: Meeting; segments: Segment[]; artifacts: Artifact[] }>(`/meetings/${id}`),
  renameMeeting: (id: string, title: string) =>
    request<Meeting>(`/meetings/${id}`, { method: 'PATCH', body: JSON.stringify({ title }) }),
  summarise: (id: string) => request<{ status: string }>(`/meetings/${id}/summarise`, { method: 'POST' }),
  deleteMeeting: (id: string) => request<{ deleted: number }>(`/meetings/${id}`, { method: 'DELETE' }),

  exportMeeting: async (id: string, fmt: 'md' | 'txt' | 'json'): Promise<{ text: string; filename: string }> => {
    const { baseUrl, token } = await conn();
    const response = await fetch(`${baseUrl}/meetings/${id}/export?fmt=${fmt}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!response.ok) throw new Error(`Export failed with ${response.status}`);
    const disposition = response.headers.get('Content-Disposition') ?? '';
    const match = /filename="([^"]+)"/.exec(disposition);
    return { text: await response.text(), filename: match?.[1] ?? `meeting.${fmt}` };
  },

  // Settings & environment
  getSettings: () => request<AppSettings>('/settings'),
  saveSettings: (values: Partial<AppSettings>) =>
    request<AppSettings>('/settings', { method: 'PUT', body: JSON.stringify({ values }) }),
  resetSettings: () => request<AppSettings>('/settings/reset', { method: 'POST' }),
  devices: () => request<{ loopback: DeviceInfo[]; input: DeviceInfo[] }>('/devices'),
  llmHealth: () => request<LlmHealth>('/llm/health'),
  meetingApps: () => request<{ apps: MeetingApp[] }>('/meeting-apps').then((r) => r.apps),

  // Admin / privacy
  stats: () => request<StorageStats>('/admin/stats'),
  deleteMany: (meetingIds: string[]) =>
    request<{ deleted: number }>('/admin/delete', {
      method: 'POST',
      body: JSON.stringify({ meeting_ids: meetingIds }),
    }),
  deleteAll: () => request<{ deleted: number }>('/admin/delete-all', { method: 'POST' }),
  deleteAudio: (meetingId?: string) =>
    request<{ deleted: number }>(
      `/admin/delete-audio${meetingId ? `?meeting_id=${encodeURIComponent(meetingId)}` : ''}`,
      { method: 'POST' },
    ),
  purge: (retentionDays: number) =>
    request<{ deleted: number }>('/admin/purge', {
      method: 'POST',
      body: JSON.stringify({ retention_days: retentionDays }),
    }),
};

export async function eventsUrl(): Promise<string> {
  const { baseUrl, token } = await conn();
  return `${baseUrl.replace('http://', 'ws://')}/events?token=${encodeURIComponent(token)}`;
}
