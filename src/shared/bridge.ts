import type { MeetingScribeBridge } from '../../electron/preload';

declare global {
  interface Window {
    meetingscribe?: MeetingScribeBridge;
  }
}

/** The preload bridge, or null when a page is opened outside Electron. */
export function bridge(): MeetingScribeBridge | null {
  return window.meetingscribe ?? null;
}
