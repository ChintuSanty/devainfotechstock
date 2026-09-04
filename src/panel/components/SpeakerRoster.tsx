import { useEffect, useState } from 'react';
import { api } from '../../shared/api';
import { formatDuration } from '../../shared/format';
import type { Speaker } from '../../shared/types';

/**
 * The voices found in one meeting. Auto labels (S1, S2...) are stable ids; the
 * names typed here are stored alongside them, so re-running detection never
 * loses a name that still has a matching voice.
 */
export function SpeakerRoster({
  meetingId,
  speakers,
  canRedetect,
  onChanged,
}: {
  meetingId: string;
  speakers: Speaker[];
  canRedetect: boolean;
  onChanged: () => void;
}) {
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setDrafts(Object.fromEntries(speakers.map((speaker) => [speaker.label, speaker.name])));
  }, [speakers]);

  const identified = speakers.filter((speaker) => speaker.channel !== 'me');
  const totalMs = speakers.reduce((sum, speaker) => sum + speaker.speaking_ms, 0) || 1;
  const dirty = speakers.some((speaker) => (drafts[speaker.label] ?? speaker.name) !== speaker.name);

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const names = Object.fromEntries(
        speakers
          .filter((speaker) => (drafts[speaker.label] ?? '') !== speaker.name)
          // An emptied box clears the name and falls back to the auto label.
          .map((speaker) => [speaker.label, drafts[speaker.label] ?? '']),
      );
      await api.nameSpeakers(meetingId, names);
      setMessage('Names saved. The transcript and exports now use them.');
      window.setTimeout(() => setMessage(null), 3000);
      onChanged();
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const redetect = async () => {
    setBusy(true);
    setError(null);
    try {
      const result = await api.redetectSpeakers(meetingId);
      setMessage(
        result.changed > 0
          ? `Re-detected ${result.speakers.length} voices, relabelling ${result.changed} lines.`
          : 'Re-detection agreed with the existing labels.',
      );
      onChanged();
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (speakers.length === 0) return null;

  return (
    <div className="card" style={{ marginTop: 0 }}>
      <div className="row between">
        <div>
          <h2>Voices</h2>
          <p className="hint" style={{ marginBottom: 0 }}>
            {identified.length > 1
              ? 'Give each voice a name and it will be used in the transcript, minutes and exports.'
              : 'Speaker identification was off for this meeting, so everyone but you is one label.'}
          </p>
        </div>
        {canRedetect && (
          <button onClick={redetect} disabled={busy} title="Re-cluster the stored voice fingerprints">
            Re-detect speakers
          </button>
        )}
      </div>

      {error && <div className="error-banner" style={{ marginTop: 12 }}>{error}</div>}
      {message && <div className="info-banner" style={{ marginTop: 12 }}>{message}</div>}

      <div className="stack" style={{ marginTop: 14 }}>
        {speakers.map((speaker) => (
          <div key={speaker.label} className="speaker-row">
            <span className={`badge ${speaker.channel === 'me' ? 'ok' : ''}`}>{speaker.label}</span>
            <input
              value={drafts[speaker.label] ?? ''}
              placeholder={speaker.label}
              disabled={speaker.channel === 'me'}
              onChange={(event) => setDrafts({ ...drafts, [speaker.label]: event.target.value })}
              onKeyDown={(event) => event.key === 'Enter' && dirty && void save()}
            />
            <span className="meter" title={`${Math.round((speaker.speaking_ms / totalMs) * 100)}% of talk time`}>
              <span style={{ width: `${(speaker.speaking_ms / totalMs) * 100}%` }} />
            </span>
            <span className="speaker-time">
              {formatDuration(speaker.speaking_ms)} · {speaker.segments} lines
            </span>
          </div>
        ))}
      </div>

      {dirty && (
        <div className="row" style={{ marginTop: 14 }}>
          <button className="primary" onClick={save} disabled={busy}>
            Save names
          </button>
          <button
            className="ghost"
            onClick={() => setDrafts(Object.fromEntries(speakers.map((s) => [s.label, s.name])))}
            disabled={busy}
          >
            Cancel
          </button>
        </div>
      )}
    </div>
  );
}
