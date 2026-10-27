/**
 * Unread agent-session tracking — in-memory read markers + count pub/sub
 * used for the Agents tab badge.
 *
 * NOTE: This module is a minimal reconstruction (2026-09-19). The original
 * src/state/*.ts files were never committed because the repo .gitignore's
 * `state/` pattern (meant for the daemon's state dir) also matched
 * `src/state/`, and the local copies were lost. Without these modules the
 * web export (`expo export --platform web`) failed at bundle time on every
 * daemon restart.
 *
 * Semantics: a session is unread when its last message is newer than the
 * last time the user opened it AND the last message is not from the user.
 * The first snapshot after app launch is treated as the read baseline so a
 * fresh launch doesn't badge every session (state is in-memory only).
 */
import type { AgentSession } from "../api/types";

type Listener = (count: number) => void;

/** session id → ISO timestamp of the last message seen as "read" */
const lastReadAt = new Map<string, string>();
let latestSessions: AgentSession[] = [];
let baselineSeeded = false;
let currentCount = 0;
const listeners = new Set<Listener>();

function isUnread(session: AgentSession): boolean {
  if (!session.last_message_time) return false;
  if (session.last_message_is_from_me) return false;
  const readAt = lastReadAt.get(session.id);
  if (readAt === undefined) return true;
  return session.last_message_time > readAt;
}

function recompute(): void {
  const count = latestSessions.filter(isUnread).length;
  if (count === currentCount) return;
  currentCount = count;
  for (const listener of listeners) {
    try {
      listener(count);
    } catch {
      /* listener errors must not break the publisher */
    }
  }
}

/**
 * Feed the latest session list (called from useAgentSessions after each
 * poll) so unread counts stay current.
 */
export function updateAgentReadTracking(sessions: AgentSession[]): void {
  latestSessions = sessions;
  if (!baselineSeeded) {
    // First snapshot = read baseline (no persistence across launches).
    for (const s of sessions) {
      if (s.last_message_time) lastReadAt.set(s.id, s.last_message_time);
    }
    baselineSeeded = true;
  }
  recompute();
}

/** Mark one agent session as read (called when the user opens it). */
export function markAgentSessionAsRead(sessionId: string): void {
  const session = latestSessions.find((s) => s.id === sessionId);
  const stamp = session?.last_message_time ?? new Date().toISOString();
  lastReadAt.set(sessionId, stamp);
  recompute();
}

/**
 * Subscribe to unread agent-session count changes. Invokes the callback
 * immediately with the current value. Returns an unsubscribe function.
 */
export function subscribeUnreadAgentCount(listener: Listener): () => void {
  listeners.add(listener);
  try {
    listener(currentCount);
  } catch {
    /* ignore */
  }
  return () => {
    listeners.delete(listener);
  };
}
