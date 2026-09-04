import { app, BrowserWindow, dialog, globalShortcut, ipcMain, Menu, shell, Tray, nativeImage } from 'electron';
import path from 'node:path';
import { Sidecar } from './sidecar';
import { createOverlayWindow, createPanelWindow, resizeOverlay } from './windows';

let overlay: BrowserWindow | null = null;
let panel: BrowserWindow | null = null;
let tray: Tray | null = null;
let sidecar: Sidecar | null = null;
let quitting = false;

// A single instance keeps one owner of the audio devices and the database.
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => showPanel());
  void app.whenReady().then(bootstrap);
}

async function bootstrap(): Promise<void> {
  app.setAppUserModelId('in.devainfotech.meetingscribe');

  sidecar = new Sidecar((code) => {
    broadcast({ type: 'sidecar-down', payload: { code } });
  });

  overlay = createOverlayWindow();
  registerIpc();
  createTray();

  globalShortcut.register('CommandOrControl+Shift+R', () => {
    broadcast({ type: 'shortcut', payload: { name: 'toggle-recording' } });
  });

  try {
    await sidecar.start();
    broadcast({ type: 'sidecar-up' });
  } catch (error) {
    dialog.showErrorBox(
      'MeetingScribe could not start',
      `The local recording service failed to launch.\n\n${(error as Error).message}\n\n` +
        'Run "npm run sidecar:install" and make sure Python 3.10+ is on your PATH.',
    );
  }

  app.on('activate', () => {
    if (!overlay) overlay = createOverlayWindow();
  });
}

function showPanel(tab?: string): void {
  if (!panel || panel.isDestroyed()) {
    panel = createPanelWindow();
    panel.on('closed', () => {
      panel = null;
    });
  }
  if (panel.isMinimized()) panel.restore();
  panel.focus();
  if (tab) {
    // The window may still be loading, so wait for the renderer to be live.
    const send = () => panel?.webContents.send('app:event', { type: 'navigate', payload: { tab } });
    if (panel.webContents.isLoading()) panel.webContents.once('did-finish-load', send);
    else send();
  }
}

function broadcast(event: { type: string; payload?: unknown }): void {
  for (const win of BrowserWindow.getAllWindows()) {
    if (!win.isDestroyed()) win.webContents.send('app:event', event);
  }
}

function registerIpc(): void {
  ipcMain.handle('sidecar:connection', () => sidecar?.connection ?? null);

  ipcMain.handle('sidecar:restart', async () => {
    await sidecar?.stop();
    sidecar = new Sidecar((code) => broadcast({ type: 'sidecar-down', payload: { code } }));
    const connection = await sidecar.start();
    broadcast({ type: 'sidecar-up' });
    return connection;
  });

  ipcMain.handle('window:open-panel', (_event, tab?: string) => showPanel(tab));

  ipcMain.handle('overlay:set-expanded', (_event, expanded: boolean) => {
    if (overlay && !overlay.isDestroyed()) resizeOverlay(overlay, Boolean(expanded));
  });

  // Lets clicks fall through the transparent corners of the overlay window.
  ipcMain.handle('overlay:set-interactive', (_event, interactive: boolean) => {
    overlay?.setIgnoreMouseEvents(!interactive, { forward: true });
  });

  // Used by Settings to pick where meetings are stored.
  ipcMain.handle('dialog:choose-folder', async (_event, current?: string) => {
    const parent = panel && !panel.isDestroyed() ? panel : undefined;
    const options = {
      title: 'Choose where MeetingScribe stores meetings',
      properties: ['openDirectory', 'createDirectory'] as const,
      defaultPath: current || app.getPath('documents'),
    };
    const result = parent
      ? await dialog.showOpenDialog(parent, { ...options, properties: [...options.properties] })
      : await dialog.showOpenDialog({ ...options, properties: [...options.properties] });
    return result.canceled || result.filePaths.length === 0 ? null : result.filePaths[0];
  });

  ipcMain.handle('shell:open-path', async (_event, target: string) => {
    const error = await shell.openPath(target);
    return error || null;
  });

  ipcMain.handle('app:quit', () => {
    quitting = true;
    app.quit();
  });
}

function createTray(): void {
  const icon = nativeImage.createFromPath(path.join(__dirname, '..', 'build', 'tray.png'));
  tray = new Tray(icon.isEmpty() ? nativeImage.createEmpty() : icon);
  tray.setToolTip('MeetingScribe');
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: 'Open MeetingScribe', click: () => showPanel('live') },
      { label: 'Meetings', click: () => showPanel('meetings') },
      { label: 'Privacy & Data', click: () => showPanel('admin') },
      { type: 'separator' },
      {
        label: 'Show floating icon',
        click: () => {
          if (!overlay || overlay.isDestroyed()) overlay = createOverlayWindow();
          else overlay.show();
        },
      },
      { type: 'separator' },
      {
        label: 'Quit',
        click: () => {
          quitting = true;
          app.quit();
        },
      },
    ]),
  );
  tray.on('double-click', () => showPanel('live'));
}

// Closing the main window leaves the overlay running; quitting is explicit.
app.on('window-all-closed', () => {
  if (quitting) app.quit();
});

app.on('before-quit', async (event) => {
  if (!sidecar) return;
  event.preventDefault();
  const pending = sidecar;
  sidecar = null;
  await pending.stop();
  globalShortcut.unregisterAll();
  app.quit();
});
