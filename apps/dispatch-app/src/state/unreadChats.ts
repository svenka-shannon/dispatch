/**
 * Unread chat count — tiny in-memory pub/sub used for the Chats tab badge.
 *
 * NOTE: This module is a minimal reconstruction (2026-09-19). The original
 * src/state/*.ts files were never committed because the repo .gitignore's
 * `state/` pattern (meant for the daemon's state dir) also matched
 * `src/state/`, and the local copies were lost. Without these modules the
 * web export (`expo export --platform web`) failed at bundle time on every
 * daemon restart. Keep behavior conservative: count fan-out + iOS app icon
 * badge sync, nothing else.
 */
import { Platform } from "react-native";

// expo-notifications is iOS/Android only — conditionally import for web compat
let Notifications: typeof import("expo-notifications") | null = null;
if (Platform.OS !== "web") {
  try {
    Notifications = require("expo-notifications") as typeof import("expo-notifications");
  } catch {
    /* native module not available */
  }
}

type Listener = (count: number) => void;

let currentCount = 0;
const listeners = new Set<Listener>();

/**
 * Publish the current unread chat count (called from useChatList after each
 * conversations refresh). Also reconciles the iOS app icon badge.
 */
export function notifyUnreadChatCount(count: number): void {
  if (count === currentCount) return;
  currentCount = count;
  for (const listener of listeners) {
    try {
      listener(count);
    } catch {
      /* listener errors must not break the publisher */
    }
  }
  // Fire-and-forget badge sync; never throw into the caller.
  Notifications?.setBadgeCountAsync(count).catch(() => {});
}

/**
 * Subscribe to unread chat count changes. Invokes the callback immediately
 * with the current value. Returns an unsubscribe function.
 */
export function subscribeUnreadChatCount(listener: Listener): () => void {
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
