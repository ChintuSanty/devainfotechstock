import { contextBridge, ipcRenderer } from 'electron';

/**
 * The only bridge between the renderer and the main process. The renderer gets
 * the loopback URL + per-launch token for the sidecar and a handful of window
 * commands - nothing else.
 */
const api = {
  getConnection: (): Promise<{ baseUrl: string; token: string } | null> =>
    ipcRenderer.invoke('sidecar:connection'),
  restartSidecar: (): Promise<{ baseUrl: string; token: string }> =>
    ipcRenderer.invoke('sidecar:restart'),

  openPanel: (tab?: string): Promise<void> => ipcRenderer.invoke('window:open-panel', tab),
  /** Native folder picker for the storage location. Resolves null if cancelled. */
  chooseFolder: (current?: string): Promise<string | null> =>
    ipcRenderer.invoke('dialog:choose-folder', current),
  /** Reveal a folder in Explorer. Resolves an error string, or null on success. */
  openPath: (target: string): Promise<string | null> => ipcRenderer.invoke('shell:open-path', target),
  setOverlayExpanded: (expanded: boolean): Promise<void> =>
    ipcRenderer.invoke('overlay:set-expanded', expanded),
  setOverlayInteractive: (interactive: boolean): Promise<void> =>
    ipcRenderer.invoke('overlay:set-interactive', interactive),
  quit: (): Promise<void> => ipcRenderer.invoke('app:quit'),

  /** Main process pushes 'sidecar-down' / 'navigate' style events here. */
  onAppEvent: (handler: (event: { type: string; payload?: unknown }) => void): (() => void) => {
    const listener = (_: unknown, value: { type: string; payload?: unknown }) => handler(value);
    ipcRenderer.on('app:event', listener);
    return () => {
      ipcRenderer.removeListener('app:event', listener);
    };
  },
} as const;

contextBridge.exposeInMainWorld('meetingscribe', api);

export type MeetingScribeBridge = typeof api;
