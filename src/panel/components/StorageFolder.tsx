import { useCallback, useEffect, useState } from 'react';
import { api } from '../../shared/api';
import { bridge } from '../../shared/bridge';
import type { StorageInfo } from '../../shared/types';

/**
 * Picks where meetings are written. Meetings are folders of plain text, so
 * moving them is a file move - the user can also point this at a synced folder
 * (OneDrive, a NAS, an encrypted volume) and MeetingScribe will read it there.
 */
export function StorageFolder({ onChanged }: { onChanged?: () => void }) {
  const [info, setInfo] = useState<StorageInfo | null>(null);
  const [draft, setDraft] = useState('');
  const [moveExisting, setMoveExisting] = useState(true);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const next = await api.storage();
      setInfo(next);
      setDraft(next.storage_dir);
      setError(null);
    } catch (cause) {
      setError((cause as Error).message);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const dirty = Boolean(info) && draft.trim() !== '' && draft.trim() !== info?.storage_dir;

  const browse = async () => {
    const chosen = await bridge()?.chooseFolder(info?.storage_dir);
    if (chosen) setDraft(chosen);
  };

  const apply = async () => {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const result = await api.setStorage(draft.trim(), moveExisting);
      setMessage(
        result.moved > 0
          ? `Moved ${result.moved} meeting${result.moved === 1 ? '' : 's'} to the new folder.`
          : 'Storage folder updated.',
      );
      await load();
      onChanged?.();
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="stack">
      {error && <div className="error-banner">{error}</div>}
      {message && <div className="info-banner">{message}</div>}

      <div className="field">
        <label>Meeting folder</label>
        <div className="row" style={{ flexWrap: 'nowrap' }}>
          <input value={draft} onChange={(event) => setDraft(event.target.value)} spellCheck={false} />
          <button onClick={browse} disabled={busy}>
            Browse
          </button>
          <button
            onClick={() => info && void bridge()?.openPath(info.storage_dir)}
            disabled={busy || !info}
            title="Open this folder in Explorer"
          >
            Open
          </button>
        </div>
        <p className="note">
          One folder per meeting, containing <code>meeting.json</code>, <code>transcript.md</code>,{' '}
          <code>minutes.md</code> and <code>suggestions.md</code>. Readable in any text editor.
        </p>
      </div>

      {dirty && (
        <>
          <label className="switch">
            <input
              type="checkbox"
              checked={moveExisting}
              onChange={(event) => setMoveExisting(event.target.checked)}
            />
            Move the existing meetings to the new folder
          </label>
          <div className="row">
            <button className="primary" onClick={apply} disabled={busy}>
              {busy ? 'Working...' : 'Save storage folder'}
            </button>
            <button className="ghost" onClick={() => info && setDraft(info.storage_dir)} disabled={busy}>
              Cancel
            </button>
            {!moveExisting && (
              <span className="badge warn">Existing meetings stay in the old folder</span>
            )}
          </div>
        </>
      )}

      {info && !info.is_default && !dirty && (
        <div className="row">
          <button className="ghost" onClick={() => setDraft(info.default_storage_dir)} disabled={busy}>
            Reset to the default folder
          </button>
        </div>
      )}
    </div>
  );
}
