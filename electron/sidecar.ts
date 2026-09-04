import { spawn, ChildProcessWithoutNullStreams } from 'node:child_process';
import { app } from 'electron';
import path from 'node:path';
import fs from 'node:fs';
import readline from 'node:readline';

export interface SidecarHandshake {
  /** http://127.0.0.1:<port> */
  baseUrl: string;
  /** Bearer token minted per-launch; the sidecar rejects requests without it. */
  token: string;
}

const HANDSHAKE_TIMEOUT_MS = 60_000;

/**
 * Owns the lifetime of the Python sidecar that does audio capture, speech
 * recognition, summarisation and local storage.
 *
 * The sidecar binds to a random loopback port and prints a single JSON
 * handshake line on stdout. Nothing is ever exposed off 127.0.0.1.
 */
export class Sidecar {
  private child: ChildProcessWithoutNullStreams | null = null;
  private handshake: SidecarHandshake | null = null;
  private stopping = false;
  private readonly logStream: fs.WriteStream;

  constructor(private readonly onExit: (code: number | null) => void) {
    const logDir = path.join(app.getPath('userData'), 'logs');
    fs.mkdirSync(logDir, { recursive: true });
    this.logStream = fs.createWriteStream(path.join(logDir, 'sidecar.log'), { flags: 'a' });
  }

  get connection(): SidecarHandshake | null {
    return this.handshake;
  }

  async start(): Promise<SidecarHandshake> {
    if (this.handshake) return this.handshake;

    const { command, args, cwd } = resolveSidecarCommand();
    this.child = spawn(command, args, {
      cwd,
      env: {
        ...process.env,
        MEETINGSCRIBE_DATA_DIR: path.join(app.getPath('userData'), 'data'),
        PYTHONUNBUFFERED: '1',
        PYTHONUTF8: '1',
      },
      windowsHide: true,
    });

    this.child.stderr.on('data', (buf: Buffer) => this.logStream.write(buf));
    this.child.on('exit', (code) => {
      this.handshake = null;
      this.child = null;
      if (!this.stopping) this.onExit(code);
    });

    const handshake = await this.readHandshake(this.child);
    this.handshake = handshake;
    return handshake;
  }

  private readHandshake(child: ChildProcessWithoutNullStreams): Promise<SidecarHandshake> {
    return new Promise((resolve, reject) => {
      const rl = readline.createInterface({ input: child.stdout });
      const timer = setTimeout(() => {
        rl.close();
        reject(new Error('Timed out waiting for the sidecar handshake.'));
      }, HANDSHAKE_TIMEOUT_MS);

      rl.on('line', (line) => {
        this.logStream.write(line + '\n');
        let parsed: unknown;
        try {
          parsed = JSON.parse(line);
        } catch {
          return; // Not the handshake line - keep reading.
        }
        const message = parsed as { event?: string; port?: number; token?: string };
        if (message.event !== 'ready' || !message.port || !message.token) return;
        clearTimeout(timer);
        rl.removeAllListeners('line');
        resolve({ baseUrl: `http://127.0.0.1:${message.port}`, token: message.token });
      });

      child.on('exit', (code) => {
        clearTimeout(timer);
        reject(new Error(`Sidecar exited before it was ready (code ${code}). See logs/sidecar.log.`));
      });
    });
  }

  async stop(): Promise<void> {
    this.stopping = true;
    const child = this.child;
    if (!child) return;
    // Ask nicely so in-flight meetings are flushed to disk, then force it.
    child.kill('SIGTERM');
    await new Promise<void>((resolve) => {
      const timer = setTimeout(() => {
        child.kill('SIGKILL');
        resolve();
      }, 5_000);
      child.once('exit', () => {
        clearTimeout(timer);
        resolve();
      });
    });
  }
}

function resolveSidecarCommand(): { command: string; args: string[]; cwd: string } {
  const packagedRoot = path.join(process.resourcesPath ?? '', 'sidecar');
  const devRoot = path.join(app.getAppPath(), 'sidecar');
  const root = app.isPackaged && fs.existsSync(packagedRoot) ? packagedRoot : devRoot;

  // A frozen PyInstaller build wins when present; otherwise fall back to the
  // interpreter, which is what happens during development.
  const frozen = path.join(root, process.platform === 'win32' ? 'meetingscribe-sidecar.exe' : 'meetingscribe-sidecar');
  if (fs.existsSync(frozen)) return { command: frozen, args: [], cwd: root };

  const venvPython = path.join(
    root,
    '.venv',
    process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python',
  );
  const python = fs.existsSync(venvPython)
    ? venvPython
    : process.env.MEETINGSCRIBE_PYTHON ?? (process.platform === 'win32' ? 'python' : 'python3');

  return { command: python, args: ['-m', 'app.server'], cwd: root };
}
