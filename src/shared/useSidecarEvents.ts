import { useEffect, useRef } from 'react';
import { eventsUrl, resetConnection } from './api';
import { bridge } from './bridge';
import type { SidecarEvent } from './types';

const RETRY_MS = 2_000;

/**
 * Keeps a WebSocket open to the sidecar and re-dials whenever it drops - which
 * happens on a sidecar restart, when the token changes with it.
 */
export function useSidecarEvents(onEvent: (event: SidecarEvent) => void): void {
  const handler = useRef(onEvent);
  handler.current = onEvent;

  useEffect(() => {
    let socket: WebSocket | null = null;
    let timer: number | undefined;
    let cancelled = false;

    const connect = async () => {
      if (cancelled) return;
      try {
        socket = new WebSocket(await eventsUrl());
      } catch {
        timer = window.setTimeout(connect, RETRY_MS);
        return;
      }
      socket.onmessage = (message) => {
        try {
          handler.current(JSON.parse(message.data as string) as SidecarEvent);
        } catch {
          /* ignore malformed frames */
        }
      };
      socket.onclose = () => {
        if (cancelled) return;
        resetConnection();
        timer = window.setTimeout(connect, RETRY_MS);
      };
      socket.onerror = () => socket?.close();
    };

    void connect();

    // A sidecar restart hands out a new port and token.
    const unsubscribe = bridge()?.onAppEvent((event) => {
      if (event.type === 'sidecar-up' || event.type === 'sidecar-down') {
        resetConnection();
        socket?.close();
      }
    });

    return () => {
      cancelled = true;
      unsubscribe?.();
      window.clearTimeout(timer);
      socket?.close();
    };
  }, []);
}
