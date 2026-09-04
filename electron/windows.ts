import { BrowserWindow, screen, shell } from 'electron';
import path from 'node:path';

const DEV_SERVER = 'http://localhost:5173';
const isDev = process.env.NODE_ENV === 'development';

const preload = () => path.join(__dirname, 'preload.js');

function loadRoute(win: BrowserWindow, entry: 'index' | 'overlay'): void {
  if (isDev) {
    void win.loadURL(`${DEV_SERVER}/${entry === 'index' ? '' : 'overlay.html'}`);
  } else {
    void win.loadFile(path.join(__dirname, '..', 'dist', `${entry}.html`));
  }
}

/** Collapsed size of the floating icon; it grows when the mini-controls open. */
export const OVERLAY_COLLAPSED = { width: 72, height: 72 };
export const OVERLAY_EXPANDED = { width: 320, height: 188 };

/**
 * The always-on-top floating icon. Frameless, transparent and kept out of the
 * taskbar and the alt-tab list so it behaves like a widget rather than a window.
 */
export function createOverlayWindow(): BrowserWindow {
  const { workArea } = screen.getPrimaryDisplay();
  const win = new BrowserWindow({
    ...OVERLAY_COLLAPSED,
    x: workArea.x + workArea.width - OVERLAY_COLLAPSED.width - 24,
    y: workArea.y + Math.floor(workArea.height / 2),
    frame: false,
    transparent: true,
    backgroundColor: '#00000000',
    resizable: false,
    movable: true,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    skipTaskbar: true,
    alwaysOnTop: true,
    hasShadow: false,
    show: false,
    webPreferences: { preload: preload(), contextIsolation: true, nodeIntegration: false },
  });

  // 'screen-saver' is the highest normal level, so the icon stays visible over
  // full-screen Zoom/Teams/Meet windows.
  win.setAlwaysOnTop(true, 'screen-saver');
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  loadRoute(win, 'overlay');
  win.once('ready-to-show', () => win.show());
  return win;
}

/** The main window: live transcript, meeting library, settings and admin panel. */
export function createPanelWindow(): BrowserWindow {
  const win = new BrowserWindow({
    width: 1180,
    height: 780,
    minWidth: 900,
    minHeight: 600,
    backgroundColor: '#0f1115',
    show: false,
    autoHideMenuBar: true,
    webPreferences: { preload: preload(), contextIsolation: true, nodeIntegration: false },
  });

  loadRoute(win, 'index');
  win.once('ready-to-show', () => win.show());
  win.webContents.setWindowOpenHandler(({ url }) => {
    void shell.openExternal(url);
    return { action: 'deny' };
  });
  return win;
}

/** Resize the overlay in place, keeping its right edge anchored. */
export function resizeOverlay(win: BrowserWindow, expanded: boolean): void {
  const size = expanded ? OVERLAY_EXPANDED : OVERLAY_COLLAPSED;
  const [x, y] = win.getPosition();
  const [currentWidth] = win.getSize();
  win.setBounds({ x: x + (currentWidth - size.width), y, ...size }, false);
}
