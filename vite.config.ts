import { defineConfig, type Plugin } from 'vite';
import react from '@vitejs/plugin-react';
import { resolve } from 'node:path';

const CSP = [
  "default-src 'self'",
  // The sidecar only ever listens on loopback.
  "connect-src 'self' http://127.0.0.1:* ws://127.0.0.1:*",
  "script-src 'self'",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data:",
].join('; ');

/**
 * Applied to built output only. The dev server needs inline scripts for React
 * refresh, and a dev build never ships.
 */
function contentSecurityPolicy(): Plugin {
  return {
    name: 'meetingscribe-csp',
    apply: 'build',
    transformIndexHtml(html) {
      return html.replace(
        '</head>',
        `  <meta http-equiv="Content-Security-Policy" content="${CSP}" />\n  </head>`,
      );
    },
  };
}

// Two renderer entry points:
//   index.html   -> the main window (dashboard, transcripts, admin panel)
//   overlay.html -> the frameless always-on-top floating icon
export default defineConfig({
  plugins: [react(), contentSecurityPolicy()],
  base: './',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    rollupOptions: {
      input: {
        panel: resolve(__dirname, 'index.html'),
        overlay: resolve(__dirname, 'overlay.html'),
      },
    },
  },
  server: { port: 5173, strictPort: true },
});
