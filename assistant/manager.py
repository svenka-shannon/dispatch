#!/usr/bin/env python3
"""
Claude Assistant Manager (SDK Backend)

Orchestrates the SMS-based personal assistant system:
- Polls Messages.app for new texts
- Routes messages to appropriate SDK sessions based on contact tier
- Manages session lifecycle (spawn, monitor, restart)
- Ignores messages from unknown contacts (not in any tier group)

Tier hierarchy: admin > partner > family > favorite
"""

import os
import sys
import time
import json
import re
import signal
import sqlite3
import subprocess
import logging
import socket
import tempfile
import threading
import queue
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from collections import deque
from enum import Enum
from typing import Optional, Dict, Any, List


# Load ~/dispatch/.env (which sources ~/.secrets.env) before any module
# reads os.environ. Implementation lives in common.py so the CLI can also
# call it without pulling in the daemon's heavy imports.
from assistant.common import load_dotenv as _load_dotenv

_load_dotenv()


from assistant.common import (
    HOME,
    ASSISTANT_DIR,
    STATE_DIR,
    LOGS_DIR,
    SESSION_REGISTRY_FILE,
    MESSAGES_DB,
    SKILLS_DIR,
    TRANSCRIPTS_DIR,
    CLAUDE,
    CLAUDE_ASSISTANT_CLI,
    UV,
    BUN,
    MASTER_SESSION,
    MASTER_TRANSCRIPT_DIR,
    SIGNAL_CLI,
    SIGNAL_SOCKET,
    signal_account,
    SIGNAL_DIR,
    normalize_chat_id,
    is_group_chat_id,
    get_session_name,
    get_group_session_name_from_participants,
    wrap_sms,
    wrap_admin,
    wrap_group_message,
    format_message_body,
    get_reply_chain,
    ensure_transcript_dir,
)
from assistant.sdk_backend import SDKBackend, SessionRegistry, _fire_and_forget
from assistant import perf
from assistant.resources import ResourceRegistry, ManagedSQLiteReader
from assistant.bus_helpers import (
    produce_event, produce_session_event,
    sanitize_msg_for_bus, sanitize_reaction_for_bus,
    health_check_payload, service_restarted_payload, service_spawned_payload,
    reminder_payload, healme_payload,
    compaction_triggered_payload, message_sent_payload, session_injected_payload,
    quota_alert_payload,
    produce_read_receipt,
)

# Import SignalDB for message persistence (lazy import to avoid startup errors)
_signal_db = None
_signal_db_lock = threading.Lock()
def get_signal_db():
    """Lazy-load SignalDB to avoid import errors if signal skill not set up."""
    global _signal_db
    if _signal_db is not None:
        return _signal_db
    with _signal_db_lock:
        if _signal_db is None:
            try:
                sys.path.insert(0, str(Path.home() / ".claude/skills/signal/scripts"))
                from signal_db import SignalDB  # type: ignore[import-not-found]
                _signal_db = SignalDB()
            except Exception as e:
                logging.getLogger(__name__).warning(f"Failed to load SignalDB: {e}")
                return None
    return _signal_db

# Paths
STATE_FILE = STATE_DIR / "last_rowid.txt"
CONSOLIDATION_STATE_FILE = STATE_DIR / "last_consolidation_date.txt"

def _backend_disabled(backend: str) -> bool:
    """Check if a backend is in the disabled_backends list (hot-reloaded from config)."""
    from assistant import config
    config.reload()  # Always re-read from disk for hot-reload
    disabled = config.get("disabled_backends", [])
    return backend in (disabled or [])

def _signal_enabled() -> bool:
    """Check if Signal is enabled (checks disabled_backends list, then legacy env var)."""
    if _backend_disabled("signal"):
        return False
    # Legacy env var fallback
    return os.environ.get("DISABLE_SIGNAL", "").lower() not in ("1", "true", "yes")

SIGNAL_ENABLED = _signal_enabled()

# Dispatch API config - lives in dispatch/services/dispatch-api
DISPATCH_API_DIR = ASSISTANT_DIR / "services" / "dispatch-api"
DISPATCH_API_SCRIPT = DISPATCH_API_DIR / "server.py"
DISPATCH_API_PORT = 9091

# Expo Metro dev server config - serves JS bundles to the mobile app
DISPATCH_APP_DIR = ASSISTANT_DIR / "apps" / "dispatch-app"
try:
    from . import config as _cfg
    METRO_PORT = _cfg.get("metro.port", 8081)
except Exception:
    METRO_PORT = 8081

# macOS epoch offset (2001-01-01 to 1970-01-01)
MACOS_EPOCH_OFFSET = 978307200

# Polling interval
POLL_INTERVAL = 0.1  # seconds (100ms for near-instant response)

# Setup logging (stdout only - cli.py redirects stdout to manager.log)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# Separate lifecycle logger for session events
lifecycle_log = logging.getLogger("lifecycle")
lifecycle_handler = logging.FileHandler(LOGS_DIR / "session_lifecycle.log")
lifecycle_handler.setFormatter(logging.Formatter('%(asctime)s | %(message)s'))
lifecycle_log.addHandler(lifecycle_handler)
lifecycle_log.setLevel(logging.INFO)


class ContactsManager:
    """Interface to contacts with in-memory caching for fast lookups.

    Uses ContactsCache from contacts_core.py for O(1) phone lookups after initial load.
    Cache is loaded once on first use (~1 second), then lookups are microseconds.
    """

    CONTACTS_CLI = HOME / ".claude/skills/contacts/scripts/contacts"

    def __init__(self):
        import sys
        # contacts_core.py is in the skills folder
        sys.path.insert(0, str(HOME / "dispatch/skills/contacts/scripts"))
        from contacts_core import lookup_phone_sqlite, lookup_email_sqlite, list_contacts_sqlite
        self._lookup_phone = lookup_phone_sqlite
        self._lookup_email = lookup_email_sqlite
        self._list_contacts = list_contacts_sqlite

    def lookup_phone(self, phone: str) -> Optional[Dict[str, str]]:
        """Lookup contact by phone number via SQLite."""
        return self._lookup_phone(phone)

    def lookup_email(self, email: str) -> Optional[Dict[str, str]]:
        """Lookup contact by email address via SQLite."""
        return self._lookup_email(email)

    def lookup_identifier(self, identifier: str) -> Optional[Dict[str, str]]:
        """Lookup contact by phone, email, OR Signal UUID via SQLite."""
        with perf.timed("contact_lookup_ms", component="daemon"):
            contact = self._lookup_phone(identifier)
            if contact:
                return contact
            if '@' in identifier:
                return self._lookup_email(identifier)
            # Try resolving Signal UUID → phone number via signal-cli recipient DB
            if len(identifier) == 36 and identifier.count('-') == 4:
                phone = self._resolve_signal_uuid_to_phone(identifier)
                if phone:
                    contact = self._lookup_phone(phone)
                    if contact:
                        return contact
            return None

    @staticmethod
    def _resolve_signal_uuid_to_phone(uuid: str) -> Optional[str]:
        """Resolve a Signal UUID to a phone number via signal-cli's recipient DB."""
        import sqlite3 as _sqlite3
        SIGNAL_DB = HOME / ".local/share/signal-cli/data/218538.d/account.db"
        try:
            conn = _sqlite3.connect(f"file:{SIGNAL_DB}?mode=ro", uri=True, timeout=2)
            row = conn.execute(
                "SELECT number FROM recipient WHERE aci = ?", (uuid,)
            ).fetchone()
            conn.close()
            if row and row[0]:
                return row[0]
        except Exception:
            pass
        return None

    def list_blessed_contacts(self) -> list:
        """Get all contacts with blessed tiers (admin, partner, family, favorite)."""
        contacts = self._list_contacts()
        return [c for c in contacts if c.get("tier") in ("admin", "partner", "family", "favorite")]

    def lookup_phone_by_name(self, name: str) -> Optional[Dict[str, str]]:
        """Lookup contact by name via SQLite."""
        contacts = self._list_contacts()
        name_lower = name.lower()
        for c in contacts:
            if c["name"].lower() == name_lower:
                return c
        return None


class MessagesReader:
    """Reads messages from macOS Messages.app chat.db."""

    # WAL checkpoint interval — avoid checkpointing every 100ms poll
    WAL_CHECKPOINT_INTERVAL = 5.0  # seconds

    def __init__(self, contacts_manager=None):
        self.db_path = MESSAGES_DB
        self._contacts = contacts_manager
        self._conn = None  # Persistent read connection (lazy init)
        self._managed_conn = False  # True if connection is managed by ResourceRegistry
        self._last_checkpoint = 0.0  # Track last WAL checkpoint time

    def set_managed_connection(self, conn: sqlite3.Connection):
        """Inject a connection managed by ResourceRegistry.

        When set, _get_conn() returns this connection instead of creating its own.
        The connection lifecycle is handled by the registry — close() becomes a no-op.
        """
        # Close any existing unmanaged connection first
        if self._conn is not None and not self._managed_conn:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = conn
        self._managed_conn = True

    def _get_conn(self) -> sqlite3.Connection:
        """Get or create a persistent read connection with optimal settings.

        If a managed connection was injected via set_managed_connection(),
        returns that instead of creating a new one.
        """
        if self._conn is not None:
            if getattr(self, '_managed_conn', False):
                return self._conn
            try:
                # Verify connection is still valid
                self._conn.execute("SELECT 1")
                return self._conn
            except Exception:
                # Connection broken, recreate
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

        conn = sqlite3.connect(self.db_path, timeout=5, check_same_thread=False)
        # WAL mode allows concurrent reads while Messages.app writes
        conn.execute("PRAGMA journal_mode=WAL")
        # Read uncommitted allows reading without shared locks — avoids
        # blocking on Messages.app WAL writes
        conn.execute("PRAGMA read_uncommitted=1")
        # Don't hold transactions open
        conn.isolation_level = None
        self._conn = conn
        return conn

    def _maybe_checkpoint(self, cursor):
        """Run WAL checkpoint at most every WAL_CHECKPOINT_INTERVAL seconds."""
        now = time.time()
        if now - self._last_checkpoint < self.WAL_CHECKPOINT_INTERVAL:
            return
        try:
            checkpoint_result = cursor.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            perf.gauge("wal_checkpoint_status", checkpoint_result[0] if checkpoint_result else -1,
                       component="daemon", source="imessage")
        except Exception:
            perf.gauge("wal_checkpoint_status", -2, component="daemon", source="imessage")
        self._last_checkpoint = now

    def close(self):
        """Close the persistent connection.

        No-op if connection is managed by ResourceRegistry (lifecycle handled there).
        """
        if getattr(self, '_managed_conn', False):
            return  # Registry handles cleanup
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def run_wal_checkpoint(self):
        """Run WAL checkpoint as a standalone operation (not in poll path).

        Called periodically from a separate async task to avoid blocking
        the 100ms message poll cycle when Messages.app holds write locks.
        """
        conn = self._get_conn()
        cursor = conn.cursor()
        self._maybe_checkpoint(cursor)

    def get_new_messages(self, since_rowid: int) -> list:
        """Get messages newer than the given ROWID."""
        conn = self._get_conn()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                message.ROWID,
                message.date,
                handle.id as phone,
                message.is_from_me,
                message.text,
                message.attributedBody,
                message.cache_has_attachments,
                message.is_audio_message,
                chat.style,
                chat.display_name,
                chat.chat_identifier,
                message.thread_originator_guid,
                message.guid
            FROM message
            LEFT JOIN handle ON message.handle_id = handle.ROWID
            LEFT JOIN chat_message_join ON message.ROWID = chat_message_join.message_id
            LEFT JOIN chat ON chat_message_join.chat_id = chat.ROWID
            WHERE message.ROWID > ?
              AND message.is_from_me = 0
            ORDER BY message.date ASC
        """, (since_rowid,))

        rows = cursor.fetchall()

        messages = []
        for row in rows:
            rowid, date, phone, is_from_me, text, attributed_body, has_attachments, is_audio_message, chat_style, chat_display_name, chat_identifier, thread_originator_guid, message_guid = row

            # Race condition fix: If chat_style is NULL, the chat_message_join row might not
            # have been written yet. Wait 50ms and re-query this specific message.
            if chat_style is None:
                race_start = time.time()
                log.info(f"[RACE_TELEMETRY] rowid={rowid} chat_style=NULL on initial query, waiting 50ms")
                time.sleep(0.05)  # 50ms
                cursor.execute("""
                    SELECT chat.style, chat.display_name, chat.chat_identifier
                    FROM message
                    LEFT JOIN chat_message_join ON message.ROWID = chat_message_join.message_id
                    LEFT JOIN chat ON chat_message_join.chat_id = chat.ROWID
                    WHERE message.ROWID = ?
                """, (rowid,))
                requery_row = cursor.fetchone()
                race_elapsed_ms = (time.time() - race_start) * 1000
                if requery_row:
                    chat_style, chat_display_name, chat_identifier = requery_row
                    if chat_style is not None:
                        log.info(f"[RACE_TELEMETRY] rowid={rowid} SUCCESS after {race_elapsed_ms:.1f}ms - chat_style={chat_style} chat_identifier={chat_identifier}")
                    else:
                        log.warning(f"[RACE_TELEMETRY] rowid={rowid} STILL_NULL after {race_elapsed_ms:.1f}ms - join row may not exist yet")
                else:
                    log.warning(f"[RACE_TELEMETRY] rowid={rowid} NO_ROW after {race_elapsed_ms:.1f}ms - message may have been deleted")

            # Extract text from attributedBody if needed
            msg_text = text
            if not msg_text and attributed_body:
                msg_text = self._parse_attributed_body(attributed_body)

            # Extract audio transcription if this is an audio message
            audio_transcription = None
            if is_audio_message and attributed_body:
                audio_transcription = self._extract_audio_transcription(attributed_body)

            # Clean up attachment placeholder character
            if msg_text == '\ufffc':
                msg_text = None

            # Get attachments if present
            attachments = []
            if has_attachments:
                attachments = self._get_attachments(cursor, rowid)

            # Skip if no text AND no attachments
            if not msg_text and not attachments:
                continue

            # Skip if phone is None (can happen briefly when message is being written to DB)
            if not phone:
                continue

            # Detect group chat (style 43 = group, 45 = 1:1)
            is_group = chat_style == 43

            # Get group name - use display_name or generate from participants
            group_name = None
            if is_group:
                if chat_display_name:
                    group_name = chat_display_name
                else:
                    group_name = self._generate_group_name(cursor, chat_identifier, self._contacts)

            timestamp = self._macos_to_datetime(date)
            messages.append({
                "rowid": rowid,
                "timestamp": timestamp,
                "phone": phone,
                "text": msg_text,
                "attachments": attachments,
                "is_group": is_group,
                "group_name": group_name,
                "chat_identifier": chat_identifier,
                "is_audio_message": bool(is_audio_message),
                "audio_transcription": audio_transcription,
                "thread_originator_guid": thread_originator_guid,
                "guid": message_guid,
            })

        return messages

    def get_new_reactions(self, since_rowid: int) -> list:
        """Get reactions newer than the given ROWID.

        Reactions are messages with associated_message_type in 2000-2999 (add) or 3000-3999 (remove).
        Standard types: 2000=❤️, 2001=👍, 2002=👎, 2003=😂, 2004=‼️, 2005=❓
        iOS 17+ uses 2006+ for custom emoji reactions (emoji stored in associated_message_emoji).
        Removals mirror: 3000=remove ❤️, etc.
        """
        conn = self._get_conn()
        cursor = conn.cursor()

        # Checkpoint handled by _maybe_checkpoint in get_new_messages (same connection)

        # Query reactions and join to get the target message text
        cursor.execute("""
            SELECT
                r.ROWID,
                r.date,
                h.id as phone,
                r.associated_message_type,
                r.associated_message_emoji,
                r.associated_message_guid,
                target.text as target_text,
                target.is_from_me as target_is_from_me,
                chat.style,
                chat.chat_identifier
            FROM message r
            LEFT JOIN handle h ON r.handle_id = h.ROWID
            LEFT JOIN message target ON substr(r.associated_message_guid, 5) = target.guid
            LEFT JOIN chat_message_join ON r.ROWID = chat_message_join.message_id
            LEFT JOIN chat ON chat_message_join.chat_id = chat.ROWID
            WHERE r.ROWID > ?
              AND r.is_from_me = 0
              AND (
                  r.associated_message_type BETWEEN 2000 AND 2999
                  OR r.associated_message_type BETWEEN 3000 AND 3999
              )
            ORDER BY r.date ASC
        """, (since_rowid,))

        rows = cursor.fetchall()
        reactions = []

        # Map reaction types to emoji (fallback if associated_message_emoji is null)
        REACTION_EMOJI = {
            2000: "❤️", 2001: "👍", 2002: "👎", 2003: "😂", 2004: "‼️", 2005: "❓",
            3000: "❤️", 3001: "👍", 3002: "👎", 3003: "😂", 3004: "‼️", 3005: "❓",
        }

        for row in rows:
            (rowid, date, phone, reaction_type, reaction_emoji, target_guid,
             target_text, target_is_from_me, chat_style, chat_identifier) = row

            if not phone:
                continue

            # Use associated_message_emoji if available (iOS 17+ custom emoji),
            # otherwise fall back to the type mapping
            emoji = reaction_emoji or REACTION_EMOJI.get(reaction_type, "💬")

            # Determine if this is a removal (3000+ series)
            is_removal = reaction_type >= 3000

            timestamp = self._macos_to_datetime(date)
            is_group = chat_style == 43

            reactions.append({
                "rowid": rowid,
                "timestamp": timestamp,
                "phone": phone,
                "emoji": emoji,
                "is_removal": is_removal,
                "target_guid": target_guid,
                "target_text": target_text,
                "target_is_from_me": bool(target_is_from_me),
                "is_group": is_group,
                "chat_identifier": chat_identifier,
                "type": "reaction",  # Mark as reaction for process_message routing
            })

        return reactions

    def _generate_group_name(self, cursor, chat_identifier: str, contacts_manager=None) -> str | None:
        """Generate a group name from participant names.

        Uses ContactsManager for fast in-memory lookups instead of spawning
        a subprocess per participant (bug #10 fix).
        """
        # Get participant phone numbers
        cursor.execute("""
            SELECT h.id
            FROM handle h
            JOIN chat_handle_join chj ON h.ROWID = chj.handle_id
            JOIN chat c ON chj.chat_id = c.ROWID
            WHERE c.chat_identifier = ?
        """, (chat_identifier,))

        phones = [row[0] for row in cursor.fetchall()]

        # Look up contact names using in-memory contacts cache
        names = []
        for phone in phones:
            if contacts_manager:
                contact = contacts_manager.lookup_identifier(phone)
                if contact:
                    first_name = contact["name"].split()[0].lower()
                    names.append(first_name)
                else:
                    names.append(phone[-4:])
            else:
                names.append(phone[-4:])

        # Sort alphabetically and join
        names.sort()
        return "-".join(names) if names else None

    def _group_has_blessed_participant(self, chat_identifier: str, contacts_manager) -> bool:
        """Check if a group chat has any blessed contacts (admin, partner, family, favorite) as participants.

        This is used to allow messages from unknown senders (e.g., alternate email identifiers)
        in groups where a blessed contact participates. Without this, messages from the admin's alternate
        identifiers (email vs phone) would be ignored.

        Args:
            chat_identifier: The unique identifier for the group chat
            contacts_manager: A ContactsManager instance for looking up contacts
        """
        # Reuse the persistent connection to avoid FD churn.
        # This is called via run_in_executor so thread safety matters —
        # but _get_conn() returns a connection with check_same_thread=False.
        conn = self._get_conn()
        cursor = conn.cursor()

        try:
            # Get all participant identifiers for this chat
            cursor.execute("""
                SELECT h.id
                FROM handle h
                JOIN chat_handle_join chj ON h.ROWID = chj.handle_id
                JOIN chat c ON chj.chat_id = c.ROWID
                WHERE c.chat_identifier = ?
            """, (chat_identifier,))

            participants = [row[0] for row in cursor.fetchall()]

            # Check if any participant is a blessed contact (supports both phone and email identifiers)
            for participant_id in participants:
                contact = contacts_manager.lookup_identifier(participant_id)
                if contact and contact.get("tier") in ("admin", "partner", "family", "favorite"):
                    return True

            return False
        except Exception as e:
            # Log the error but don't reset self._conn — that races with
            # other executor threads using the same connection. Let _get_conn()
            # handle reconnection on the next call via its SELECT 1 health check.
            log.warning(f"_group_has_blessed_participant error: {e}")
            raise

    def _get_attachments(self, cursor, message_rowid: int) -> list:
        """Get attachments for a message."""
        cursor.execute("""
            SELECT
                attachment.filename,
                attachment.mime_type,
                attachment.transfer_name,
                attachment.total_bytes
            FROM attachment
            JOIN message_attachment_join ON attachment.ROWID = message_attachment_join.attachment_id
            WHERE message_attachment_join.message_id = ?
        """, (message_rowid,))

        attachments = []
        for row in cursor.fetchall():
            filename, mime_type, transfer_name, total_bytes = row
            if filename:
                # Expand ~ to full path
                full_path = filename.replace("~/", str(Path.home()) + "/")
                attachments.append({
                    "path": full_path,
                    "mime_type": mime_type or "unknown",
                    "name": transfer_name or Path(filename).name,
                    "size": total_bytes or 0
                })
        return attachments

    def get_latest_rowid(self) -> int:
        """Get the most recent message ROWID."""
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            # Try PASSIVE checkpoint to improve WAL visibility (won't fail if locked)
            try:
                cursor.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except Exception:
                pass  # Checkpoint failed, continue anyway - non-critical
            cursor.execute("SELECT MAX(ROWID) FROM message")
            result = cursor.fetchone()[0]
            return result or 0
        except Exception:
            if not getattr(self, '_managed_conn', False):
                self._conn = None
            raise

    def _parse_attributed_body(self, data: bytes) -> Optional[str]:
        """Extract text from NSAttributedString binary data."""
        if not data:
            return None
        try:
            parts = data.split(b"NSString")
            if len(parts) < 2:
                return None
            content = parts[1][5:]
            if content[0] == 0x81:
                length = int.from_bytes(content[1:3], "little")
                text_start = 3
            else:
                length = content[0]
                text_start = 1
            return content[text_start:text_start + length].decode("utf-8", errors="ignore")
        except Exception:
            return None

    def _extract_audio_transcription(self, data: bytes) -> Optional[str]:
        """Extract Apple's audio transcription from attributedBody."""
        if not data:
            return None
        try:
            import re
            text = data.decode('utf-8', errors='ignore')
            # Look for IMAudioTranscription followed by the text
            match = re.search(r'IMAudioTranscription.(.+?)(?:__kIM|$)', text, re.DOTALL)
            if match:
                raw = match.group(1)
                # Clean up: remove non-printable characters except spaces
                cleaned = ''.join(c for c in raw if c.isprintable() or c == ' ')
                cleaned = cleaned.strip()
                if cleaned and cleaned[0].isdigit():
                    # Skip leading digit (length indicator)
                    cleaned = cleaned[1:].strip()
                # Remove trailing artifacts
                cleaned = cleaned.rstrip('&').strip()
                return cleaned if cleaned else None
            return None
        except Exception:
            return None

    def _macos_to_datetime(self, ts: int) -> datetime:
        """Convert macOS nanosecond timestamp to datetime."""
        unix_ts = ts / 1_000_000_000 + MACOS_EPOCH_OFFSET
        return datetime.fromtimestamp(unix_ts)


class SignalListener(threading.Thread):
    """Listens to signal-cli daemon socket and queues incoming messages.

    Runs in a background thread, connects to the signal-cli JSON-RPC socket,
    subscribes to receive notifications, and pushes incoming messages to a
    thread-safe queue for processing by the main loop.
    """

    def __init__(self, message_queue: queue.Queue):
        super().__init__(daemon=True, name="SignalListener")
        self.message_queue = message_queue
        self.socket_path = str(SIGNAL_SOCKET)
        self.running = False
        self.sock = None
        self._seen_timestamps: set[int] = set()  # Track recent timestamps to avoid duplicates
        self._seen_timestamps_max = 1000  # Prune when set gets too large

    def stop(self):
        """Stop the listener thread."""
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass

    def run(self):
        """Main listener loop - connects and processes messages."""
        self.running = True
        while self.running:
            try:
                self._connect_and_listen()
            except Exception as e:
                log.error(f"SignalListener error: {e}")
                time.sleep(5)  # Backoff on error

    def _connect_and_listen(self):
        """Connect to socket and process incoming messages."""
        if not SIGNAL_SOCKET.exists():
            log.debug("Signal socket not ready, waiting...")
            time.sleep(1)
            return

        log.info(f"SignalListener connecting to {self.socket_path}")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.sock.connect(self.socket_path)
            # Set timeout so recv() doesn't block forever if signal-cli hangs
            # without closing the socket. Allows periodic health checks.
            self.sock.settimeout(60)
            log.info("SignalListener connected")

            # Subscribe to receive messages
            subscribe_req = json.dumps({
                "jsonrpc": "2.0",
                "method": "subscribeReceive",
                "id": 1,
                "params": {}
            }) + "\n"
            self.sock.sendall(subscribe_req.encode())

            buffer = b""
            MAX_BUFFER_SIZE = 10 * 1024 * 1024  # 10MB — corrupted stream protection
            while self.running:
                try:
                    chunk = self.sock.recv(4096)
                    if not chunk:
                        log.warning("SignalListener: socket closed")
                        break
                    buffer += chunk

                    # Guard against corrupted JSON stream without newlines
                    if len(buffer) > MAX_BUFFER_SIZE:
                        log.error(f"SignalListener: buffer exceeded {MAX_BUFFER_SIZE} bytes, resetting")
                        buffer = b""
                        continue

                    # Process complete JSON lines
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        if not line.strip():
                            continue
                        self._process_message(line.decode())

                except socket.timeout:
                    continue
                except Exception as e:
                    log.error(f"SignalListener recv error: {e}")
                    break
        finally:
            if self.sock:
                self.sock.close()
                self.sock = None

    def _process_message(self, line: str):
        """Parse a JSON-RPC message and queue it if it's an incoming message."""
        try:
            data = json.loads(line)

            # We only care about receive notifications
            if data.get("method") != "receive":
                return

            params = data.get("params", {})
            # Handle both direct params and nested result
            result = params.get("result", params)
            envelope = result.get("envelope", {})

            # Extract message data
            # Use phone number if available, fall back to UUID for users who haven't shared their number
            source_number = envelope.get("sourceNumber") or envelope.get("sourceUuid")
            source_name = envelope.get("sourceName")
            data_msg = envelope.get("dataMessage", {})
            body = data_msg.get("message")
            timestamp = data_msg.get("timestamp", 0)

            # Extract attachments early so we can accept attachment-only messages
            attachments = self._extract_attachments(data_msg)

            # Skip if no text message AND no attachments
            if not body and not attachments:
                return

            # For attachment-only messages, use placeholder text
            if not body and attachments:
                body = "(image)" if any(a["mime_type"].startswith("image/") for a in attachments) else "(attachment)"

            # Skip duplicates (use set to handle out-of-order messages)
            if timestamp in self._seen_timestamps:
                return
            self._seen_timestamps.add(timestamp)
            # Prune timestamps older than 5 minutes to prevent unbounded growth
            # Time-based expiry is safer than size-based: avoids duplicate
            # processing after long idle periods followed by a burst
            if len(self._seen_timestamps) > self._seen_timestamps_max:
                cutoff_ms = int(time.time() * 1000) - (5 * 60 * 1000)  # 5 minutes ago
                self._seen_timestamps = {ts for ts in self._seen_timestamps if ts > cutoff_ms}

            # Check for group message
            group_info = data_msg.get("groupInfo", {})
            group_id = group_info.get("groupId")

            # Build message dict matching MessagesReader format
            msg = {
                "rowid": timestamp,  # Use timestamp as unique ID
                "timestamp": datetime.fromtimestamp(timestamp / 1000),
                "phone": source_number,
                "text": body,
                "attachments": attachments,
                "is_group": bool(group_id),
                "group_name": group_info.get("groupName"),
                "chat_identifier": group_id if group_id else source_number,
                "is_audio_message": False,
                "audio_transcription": None,
                "thread_originator_guid": None,
                "source": "signal",  # Mark as Signal message
                "sender_name": source_name,  # Signal profile name (may be None)
                "source_uuid": envelope.get("sourceUuid"),  # Signal UUID for recipient lookup
            }

            log.info(f"SignalListener: queued message from {source_number}: {body[:50]}...")
            self.message_queue.put(msg)

            # Store in database for history
            try:
                db = get_signal_db()
                if db and not db.message_exists(timestamp, msg["chat_identifier"], source_number):
                    db.store_message(
                        timestamp=timestamp,
                        chat_id=msg["chat_identifier"],
                        sender=source_number,
                        text=body,
                        is_from_me=False,
                        attachments=msg["attachments"] if msg["attachments"] else None,
                        group_name=group_info.get("groupName"),
                    )
                    log.debug(f"SignalListener: stored message in DB")
            except Exception as e:
                log.warning(f"SignalListener: failed to store message in DB: {e}")

        except json.JSONDecodeError as e:
            log.debug(f"SignalListener: invalid JSON: {e}")
        except Exception as e:
            log.error(f"SignalListener: error processing message: {e}")

    def _extract_attachments(self, data_msg: dict) -> List[dict]:
        """Extract attachment info from a Signal message.

        signal-cli daemon stores downloaded attachments at
        ~/.local/share/signal-cli/attachments/<id> but does NOT include
        a ``file`` path in the JSON-RPC notification.  We construct the
        path from the attachment ``id`` field instead.
        """
        attachments = []
        for att in data_msg.get("attachments", []):
            # Prefer explicit file path (future-proofing), fall back to
            # constructing from id in the signal-cli attachments dir.
            path = att.get("file", "")
            if not path:
                att_id = att.get("id", "")
                if att_id:
                    candidate = Path.home() / ".local/share/signal-cli/attachments" / att_id
                    if candidate.exists():
                        path = str(candidate)
            attachments.append({
                "path": path,
                "mime_type": att.get("contentType", "unknown"),
                "name": att.get("filename", "attachment"),
                "size": att.get("size", 0),
            })
        return attachments


class TestMessageWatcher(threading.Thread):
    """Watches test message directory and queues test messages for processing.

    Enables testing without iMessage/Signal by dropping JSON files into a directory.
    File format:
    {
        "from": "+15555551234",
        "text": "message text",
        "is_group": false,
        "chat_id": "+15555551234",  // optional, defaults to "from"
        "group_name": "Test Group",  // optional
        "attachments": ["/path/to/file"],  // optional
        "reply_to": "guid"  // optional
    }
    """

    TEST_DIR = Path(HOME) / ".claude/test-messages"

    def __init__(self, message_queue: queue.Queue):
        super().__init__(daemon=True, name="TestMessageWatcher")
        self.message_queue = message_queue
        self.running = False
        self.TEST_DIR.mkdir(parents=True, exist_ok=True)

    def stop(self):
        """Stop the watcher thread."""
        self.running = False

    def run(self):
        """Main watcher loop - polls directory for .json files."""
        self.running = True
        log.info(f"TestMessageWatcher started, watching {self.TEST_DIR}")

        while self.running:
            try:
                # Look for .json files
                for file_path in sorted(self.TEST_DIR.glob("*.json")):
                    try:
                        with open(file_path) as f:
                            raw_msg = json.load(f)

                        # Normalize to message contract
                        normalized = self._normalize_message(raw_msg)

                        # Queue for processing
                        self.message_queue.put(normalized)
                        log.info(f"Queued test message from {file_path.name}: from={normalized['phone']} is_group={normalized['is_group']}")

                        # Delete file after reading
                        file_path.unlink()

                    except Exception as e:
                        log.error(f"Error processing test message {file_path}: {e}")
                        # Move to error directory instead of leaving in place
                        error_dir = self.TEST_DIR / "errors"
                        error_dir.mkdir(exist_ok=True)
                        try:
                            file_path.rename(error_dir / file_path.name)
                        except Exception:
                            file_path.unlink()  # Delete if can't move

                time.sleep(0.1)  # Poll every 100ms

            except Exception as e:
                log.error(f"TestMessageWatcher error: {e}")
                time.sleep(1)

    def _normalize_message(self, raw: dict) -> dict:
        """Convert test message format to internal message contract."""
        from_phone = raw.get("from", "+15555550005")
        is_group = raw.get("is_group", False)
        chat_id = raw.get("chat_id", from_phone)

        # Normalize chat_id
        chat_id = normalize_chat_id(chat_id)

        # Build message matching MessagesReader/SignalListener format
        msg = {
            "rowid": int(time.time() * 1000),  # Fake ROWID for test messages
            "date": int(time.time()),
            "phone": from_phone,
            "is_from_me": 0,
            "text": raw.get("text", ""),
            "attachments": [],
            "is_group": is_group,
            "group_name": raw.get("group_name"),
            "chat_identifier": chat_id,
            "chat_style": 43 if is_group else 45,
            "reply_to_guid": raw.get("reply_to"),
            "source": "test",  # Tag as test message
        }

        # Handle attachments
        if "attachments" in raw:
            for path in raw["attachments"]:
                msg["attachments"].append({
                    "path": path,
                    "mime_type": "unknown",
                    "name": Path(path).name,
                    "size": 0,
                })

        return msg


class ReminderPoller:
    """
    Native reminder poller using JSON-based storage.

    Replaces the old Reminders.app polling approach with a native system:
    - Cron runs in local time (handles DST automatically)
    - Internal storage in UTC for comparison
    - Retry with exponential backoff
    - Catch-up on startup with 24h limit
    - Admin alerts for dead reminders

    Design: v6 (9.2/10 review score)
    """

    def __init__(self, backend: SDKBackend, contacts_manager: ContactsManager):
        self.backend = backend
        self.contacts = contacts_manager
        self.reminders = []
        self.config = {}
        self._caught_up = False

    def _produce_event(self, topic: str, event_type: str, payload: dict,
                       key: str | None = None, source: str = "reminder",
                       headers: dict[str, str] | None = None):
        """Delegate event production to bus via backend's producer."""
        producer = getattr(self.backend, '_producer', None)
        produce_event(producer, topic, event_type, payload, key=key, source=source,
                      headers=headers)

    def _produce_session_injected(self, session_id: str, contact: str,
                                   tier: str, r: dict):
        """Produce session.injected event for reminder injection."""
        from assistant.bus_helpers import produce_session_event, session_injected_payload
        producer = getattr(self.backend, '_producer', None)
        produce_session_event(producer, session_id, "session.injected",
            session_injected_payload(session_id, "reminder", contact, tier,
                                     reminder_id=r.get("id"), target=r.get("target", "fg")),
            source="reminder")

    def _resolve_reminder_contact(self, r: dict) -> tuple[str, str]:
        """Resolve reminder contact to (chat_id, tier).

        Returns (chat_id, tier) or raises ValueError if contact not found.
        """
        contact = r.get("contact")
        if not contact:
            raise ValueError(f"Reminder has no contact: {r.get('id')}")

        if re.match(r'^[0-9a-f]{32}$', contact) or contact.startswith('+'):
            return contact, "admin"

        contact_info = self.contacts.lookup_phone_by_name(contact)
        if not contact_info:
            raise ValueError(f"Contact not found: {contact}")
        chat_id = contact_info.get("phone")
        if not chat_id:
            raise ValueError(f"No phone for contact: {contact}")
        return chat_id, contact_info.get("tier", "admin")

    def _load_reminders(self):
        """Load reminders from JSON file."""
        from assistant.reminders import reminders_lock, load_reminders, REMINDERS_FILE
        with reminders_lock():
            data = load_reminders()
            self.reminders = data.get("reminders", [])
            self.config = data.get("config", {})
        # Track file mtime for change detection
        try:
            self._reminders_mtime = REMINDERS_FILE.stat().st_mtime
        except (OSError, AttributeError):
            self._reminders_mtime = 0

    def _load_reminders_if_changed(self):
        """Reload reminders only if the file was modified externally (e.g., by CLI)."""
        from assistant.reminders import REMINDERS_FILE
        try:
            current_mtime = REMINDERS_FILE.stat().st_mtime
        except OSError:
            return  # File doesn't exist, nothing to reload
        if current_mtime != getattr(self, '_reminders_mtime', 0):
            self._load_reminders()

    def _save_reminders(self):
        """Save reminders to JSON file."""
        from assistant.reminders import reminders_lock, save_reminders
        with reminders_lock():
            save_reminders({
                "version": 1,
                "config": self.config,
                "reminders": self.reminders
            })

    def _get_reminder_timezone(self, r: dict) -> str:
        """Get effective timezone for a reminder."""
        return r.get("schedule", {}).get("timezone") or self.config.get("default_timezone", "America/New_York")

    def _should_fire(self, r: dict, now_utc: datetime) -> bool:
        """Check if a reminder should fire now."""
        from datetime import timezone

        fire_time_str = r.get("next_fire")
        if not fire_time_str:
            return False

        fire_time = datetime.fromisoformat(fire_time_str.replace('Z', '+00:00'))
        if fire_time > now_utc:
            return False

        max_retries = self.config.get("max_retries", 3)
        if r.get("retry_count", 0) >= max_retries:
            return False  # Dead, needs manual intervention

        # Check backoff if there was an error
        if r.get("last_error") and r.get("last_fired"):
            backoff_list = self.config.get("backoff_seconds", [60, 120, 240])
            retry_count = r.get("retry_count", 0)
            idx = min(max(0, retry_count - 1), len(backoff_list) - 1)
            backoff_secs = backoff_list[idx]
            last_attempt = datetime.fromisoformat(r["last_fired"].replace('Z', '+00:00'))
            if (now_utc - last_attempt).total_seconds() < backoff_secs:
                return False

        return True

    async def catch_up_missed_reminders(self):
        """Called once on daemon start to fire missed reminders."""
        from datetime import timezone, timedelta

        if self._caught_up:
            return

        self._load_reminders()
        now_utc = datetime.now(timezone.utc)
        catch_up_max_hours = self.config.get("catch_up_max_hours", 24)
        catch_up_max = timedelta(hours=catch_up_max_hours)
        modified = False

        for r in list(self.reminders):
            fire_time_str = r.get("next_fire")
            if not fire_time_str:
                continue

            fire_time = datetime.fromisoformat(fire_time_str.replace('Z', '+00:00'))
            if fire_time >= now_utc:
                continue

            age = now_utc - fire_time

            if age > catch_up_max:
                log.warning(f"REMINDER_SKIPPED | id={r['id']} | {age.total_seconds()/3600:.1f}h late")
                if r["schedule"]["type"] == "once":
                    self.reminders.remove(r)
                else:
                    # Advance cron to next fire time
                    from assistant.reminders import next_cron_fire
                    tz = self._get_reminder_timezone(r)
                    r["next_fire"] = next_cron_fire(r["schedule"]["value"], tz)
                modified = True
                continue

            log.info(f"REMINDER_CATCHUP | id={r['id']} | {age.total_seconds()/60:.0f}m late")
            await self._fire_reminder(r, late=True)
            modified = True

        if modified:
            self._save_reminders()

        self._caught_up = True

    async def process_due_reminders(self):
        """Check for due reminders and fire them. Called every poll cycle."""
        from datetime import timezone

        # Global kill switch — check config.local.yaml reminders_enabled flag.
        # When false, all recurring reminders are skipped (one-shot preserved).
        # Uses reload() so changes take effect without daemon restart.
        from . import config as _cfg
        _cfg.reload()
        if not _cfg.get("reminders_enabled", True):
            if not getattr(self, '_reminders_disabled_logged', False):
                log.info("REMINDERS_DISABLED | reminders_enabled=false in config — skipping all reminders")
                self._reminders_disabled_logged = True
            return
        if getattr(self, '_reminders_disabled_logged', False):
            log.info("REMINDERS_ENABLED | reminders_enabled=true in config — reminders re-enabled")
            self._reminders_disabled_logged = False

        # Skip reminders when quota is degraded (>=90%) to conserve tokens.
        # Cron reminders will catch up on next fire; one-shot reminders are preserved.
        qm = getattr(self.backend, 'quota_manager', None)
        if qm and qm.state == "degraded":
            if not getattr(self, '_reminders_paused_logged', False):
                log.info("REMINDERS_PAUSED | Quota degraded — skipping reminders to conserve tokens")
                self._reminders_paused_logged = True
            return
        # Reset the log-once flag when quota recovers
        if getattr(self, '_reminders_paused_logged', False):
            log.info("REMINDERS_RESUMED | Quota recovered — reminders re-enabled")
            self._reminders_paused_logged = False

        # Only reload if file changed on disk (avoids unnecessary I/O)
        self._load_reminders_if_changed()

        now_utc = datetime.now(timezone.utc)
        modified = False

        for r in list(self.reminders):
            if self._should_fire(r, now_utc):
                await self._fire_reminder(r)
                modified = True

        # Only save if reminders were actually modified
        if modified:
            self._save_reminders()

    async def _fire_reminder(self, r: dict, late: bool = False):
        """Fire a reminder: produce to bus + inject directly (dual path).

        Two paths based on reminder type:
        1. Generalized (has 'event' field): produce the stored event template to bus.
           No direct inject — the bus consumer handles routing/execution.
        2. Legacy (no 'event' field): produce reminder.due + direct inject (dual path).

        TODO: Remove direct _inject_to_session() once reminder consumer is live.
        See plans/reminder-bus-producer.md "Definition of Done for Dual Path Removal".
        """
        from datetime import timezone
        from assistant.reminders import next_cron_fire
        import uuid

        try:
            now_utc = datetime.now(timezone.utc)
            tz = self._get_reminder_timezone(r)

            if "event" in r and r["event"]:
                # ── Generalized path: produce the stored event template ──
                evt = r["event"]
                trace_id = f"trace-{str(uuid.uuid4())[:8]}"
                self._produce_event(
                    topic=evt["topic"],
                    event_type=evt["type"],
                    payload=evt["payload"],  # user payload passed through unchanged
                    key=evt.get("key"),
                    source="reminder-scheduler",
                    headers={
                        "trace_id": trace_id,
                        "reminder_id": r["id"],
                        "reminder_title": r.get("title", ""),
                        "fired_at": now_utc.isoformat().replace('+00:00', 'Z'),
                        "schedule_type": r["schedule"]["type"],
                        "fired_count": str(r.get("fired_count", 0) + 1),
                    },
                )
                log.info(f"REMINDER_FIRED | id={r['id']} | trace={trace_id} | "
                         f"event={evt['topic']}/{evt['type']} | mode=generalized")
            else:
                # ── Legacy path: produce reminder.due + direct inject ──
                chat_id, tier = self._resolve_reminder_contact(r)

                scheduled_time = r.get("next_fire", now_utc.isoformat())
                minutes_late = 0
                if late:
                    fire_time = datetime.fromisoformat(scheduled_time.replace('Z', '+00:00'))
                    minutes_late = (now_utc - fire_time).total_seconds() / 60

                # 1. Produce to bus (fire-and-forget)
                self._produce_event(
                    "reminders", "reminder.due",
                    {
                        "reminder_id": r["id"],
                        "title": r.get("title", ""),
                        "contact": r.get("contact", ""),
                        "chat_id": chat_id,
                        "tier": tier,
                        "target": r.get("target", "fg"),
                        "schedule_type": r["schedule"]["type"],
                        "schedule_value": r["schedule"]["value"],
                        "timezone": tz,
                        "scheduled_fire_time": scheduled_time,
                        "actual_fire_time": now_utc.isoformat().replace('+00:00', 'Z'),
                        "is_late": late,
                        "minutes_late": round(minutes_late, 1),
                        "fired_count": r.get("fired_count", 0) + 1,
                    },
                    key=chat_id,
                    source="reminder-poller"
                )

                # 2. Direct inject — PRIMARY delivery path
                await self._inject_to_session(r, late, resolved_chat_id=chat_id, resolved_tier=tier)
                log.info(f"REMINDER_FIRED | id={r['id']} | late={late} | "
                         f"target={r.get('target', 'fg')} | mode=legacy")

            # Success — update reminder state
            r["last_fired"] = now_utc.isoformat().replace('+00:00', 'Z')
            r["fired_count"] = r.get("fired_count", 0) + 1
            r["last_error"] = None
            r["retry_count"] = 0

            if r["schedule"]["type"] == "once":
                self.reminders.remove(r)
                log.info(f"REMINDER_DELETED | id={r['id']} | reason=completed")
            else:
                r["next_fire"] = next_cron_fire(r["schedule"]["value"], tz)
                log.info(f"REMINDER_SCHEDULED | id={r['id']} | next={r['next_fire']}")

        except Exception as e:
            r["retry_count"] = r.get("retry_count", 0) + 1
            r["last_error"] = str(e)
            r["last_fired"] = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
            log.error(f"REMINDER_FAILED | id={r['id']} | attempt={r['retry_count']} | {e}")

            max_retries = self.config.get("max_retries", 3)
            if r["retry_count"] >= max_retries:
                log.error(f"REMINDER_DEAD | id={r['id']}")
                await self._alert_admin(r)

    async def _inject_to_session(self, r: dict, late: bool = False,
                                 resolved_chat_id: str | None = None,
                                 resolved_tier: str | None = None):
        """Inject reminder into contact's session.

        Args:
            resolved_chat_id: Pre-resolved chat_id (skips contact resolution).
            resolved_tier: Pre-resolved tier (skips contact resolution).
        """
        from datetime import timezone
        from assistant.reminders import format_for_display

        contact: str = r.get("contact") or ""
        if resolved_chat_id and resolved_tier:
            chat_id = resolved_chat_id
            tier = resolved_tier
        else:
            if not contact:
                raise ValueError(f"Reminder has no contact: {r.get('id')}")

            # Resolve contact to chat_id
            if re.match(r'^[0-9a-f]{32}$', contact) or contact.startswith('+'):
                chat_id = contact
                tier = "admin"
            else:
                contact_info = self.contacts.lookup_phone_by_name(contact)
                if not contact_info:
                    raise ValueError(f"Contact not found: {contact}")
                chat_id = contact_info.get("phone")
                if not chat_id:
                    raise ValueError(f"No phone for contact: {contact}")
                tier = contact_info.get("tier", "admin")

        # Build injection message
        tz = self._get_reminder_timezone(r)
        now_local = datetime.now(timezone.utc).astimezone()
        ts_display = now_local.strftime("%Y-%m-%d %I:%M %p %Z")

        msg = f"---REMINDER [{ts_display}]---\n{r['title']}\n---END REMINDER---\n"

        if late:
            fire_time = datetime.fromisoformat(r["next_fire"].replace('Z', '+00:00'))
            mins_late = (datetime.now(timezone.utc) - fire_time).total_seconds() / 60
            fire_display = format_for_display(r["next_fire"], tz)
            msg += f"\n[LATE: {mins_late:.0f} minutes after scheduled time ({fire_display})]\n"

        if r["schedule"]["type"] == "cron":
            msg += f"\n[Schedule: {r['schedule']['value']} in {tz}]\n"

        target = r.get("target", "fg")
        msg += "\nACTION REQUIRED:\n1. TEXT the user: \"Reminder: [task]. Working on it now...\"\n2. EXECUTE the task\n3. TEXT the user the results when done"

        # Inject into session based on target
        normalized = normalize_chat_id(chat_id)

        if target == "spawn":
            # Create a fresh agent session for this task
            # Use a unique session name for the spawn
            spawn_id = f"{normalized}-spawn-{r['id']}"
            await self.backend.create_session(contact, spawn_id, tier)
            session = self.backend.sessions.get(spawn_id)
            if session:
                await session.inject(msg)
                log.info(f"REMINDER_SPAWN | id={r['id']} | session={spawn_id}")
                self._produce_session_injected(spawn_id, contact, tier, r)
            else:
                raise RuntimeError(f"Failed to spawn session for {contact}")
        else:
            # fg (default) - inject into foreground session
            session = self.backend.sessions.get(normalized)
            if session and session.is_alive():
                await session.inject(msg)
                self._produce_session_injected(normalized, contact, tier, r)
            else:
                # Create session and inject
                await self.backend.create_session(contact, normalized, tier)
                session = self.backend.sessions.get(normalized)
                if session:
                    await session.inject(msg)
                    self._produce_session_injected(normalized, contact, tier, r)
                else:
                    raise RuntimeError(f"Failed to create session for {contact}")

    async def _alert_admin(self, r: dict):
        """Notify admin of dead reminder.

        NOTE: This injects directly into the admin session, NOT through the bus.
        This is intentional — system alerts must not depend on a consumer being alive.
        """
        from assistant import config
        admin_phone = config.get("owner.phone")
        if not admin_phone:
            return

        msg = f"⚠️ Reminder failed (3 attempts):\n{r['title']}\nError: {r.get('last_error', 'Unknown')}\n\nRetry: claude-assistant remind retry {r['id']}"

        try:
            normalized = normalize_chat_id(admin_phone)
            session = self.backend.sessions.get(normalized)
            if session and session.is_alive():
                await session.inject(msg)
        except Exception as e:
            log.error(f"Failed to alert admin about dead reminder: {e}")


class IPCServer:
    """Unix socket IPC server for CLI commands."""

    IPC_SOCKET = Path("/tmp/claude-assistant.sock")

    def __init__(self, backend: SDKBackend, registry: SessionRegistry, contacts: ContactsManager):
        self.backend = backend
        self.registry = registry
        self.contacts = contacts
        self._server = None
        self.restart_api_callback: Optional[Any] = None

    async def start(self):
        # Clean up stale socket
        self.IPC_SOCKET.unlink(missing_ok=True)
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(self.IPC_SOCKET)
        )
        self.IPC_SOCKET.chmod(0o600)  # Owner-only access
        log.info(f"IPC server listening on {self.IPC_SOCKET}")

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        self.IPC_SOCKET.unlink(missing_ok=True)

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            data = await asyncio.wait_for(reader.readline(), timeout=30)
            if not data:
                return

            request = json.loads(data.decode())
            response = await self._dispatch(request)
            writer.write((json.dumps(response) + "\n").encode())
            await writer.drain()
        except Exception as e:
            try:
                writer.write((json.dumps({"ok": False, "error": str(e)}) + "\n").encode())
                await writer.drain()
            except Exception:
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _dispatch(self, request: dict) -> dict:
        cmd = request.get("cmd")

        if cmd == "inject":
            return await self._cmd_inject(request)
        elif cmd == "kill_session":
            chat_id = request.get("chat_id")
            if not chat_id:
                return {"ok": False, "error": "Missing chat_id"}
            ok = await self.backend.kill_session(chat_id)
            return {"ok": ok, "message": f"Killed {chat_id}" if ok else "Session not found"}
        elif cmd == "restart_session":
            chat_id = request.get("chat_id")
            tier = request.get("tier")  # Optional tier override
            clean = request.get("clean", False)  # Clean restart (no resume)
            if not chat_id:
                return {"ok": False, "error": "Missing chat_id"}
            session = await self.backend.restart_session(chat_id, tier_override=tier, clean=clean)
            return {"ok": session is not None, "message": f"Restarted {chat_id}" if session else "Failed to restart"}
        elif cmd == "set_global_model":
            model = request.get("model")
            if not model:
                return {"ok": False, "error": "Missing model"}
            valid = ("opus", "sonnet", "haiku", "--clear", "clear")
            if model not in valid:
                return {"ok": False, "error": f"Invalid model: {model}. Use: opus, sonnet, haiku, or --clear"}
            qm = self.backend.quota_manager
            trigger = request.get("trigger", "manual_cli")
            qm.set_global_model(model, trigger=trigger)
            state = qm.state
            override = qm.get_override_info()
            # Notify admin (skip if triggered from the app — user already sees the result)
            if not trigger.startswith("manual_app"):
                from assistant import config
                admin_phone = config.get("owner.phone")
                if admin_phone:
                    if model in ("--clear", "clear"):
                        self._send_sms(admin_phone, "[SVEN] ⚡ Global model override cleared — sessions will use per-session defaults (opus)")
                    else:
                        self._send_sms(admin_phone, f"[SVEN] ⚡ Global model switched to {model} — new sessions will use {model}")
            return {
                "ok": True,
                "state": state,
                "override": override,
                "message": f"Global model set to {model}" if model not in ("--clear", "clear") else "Global model override cleared",
            }

        elif cmd == "get_global_model":
            qm = self.backend.quota_manager
            state = qm.state
            override = qm.get_override_info()
            cb = self.backend.haiku_circuit_breaker
            # Read from quota cache only — never triggers an API call
            try:
                from assistant.health import get_quota_cached
                usage, quota_updated = get_quota_cached()
                quota_5h = (usage.get("five_hour") or {}).get("utilization") if usage else None
                quota_7d_opus = (usage.get("seven_day_opus") or {}).get("utilization") if usage else None
            except Exception:
                quota_5h = quota_7d_opus = None
                quota_updated = None
            return {
                "ok": True,
                "state": state,
                "override": override,
                "circuit_breaker": cb.state,
                "quota_5h_pct": quota_5h,
                "quota_7d_opus_pct": quota_7d_opus,
                "quota_updated_at": quota_updated,
            }

        elif cmd == "set_model":
            chat_id = request.get("chat_id")
            model = request.get("model")
            if not chat_id:
                return {"ok": False, "error": "Missing chat_id"}
            if model not in ("opus", "sonnet", "haiku"):
                return {"ok": False, "error": f"Invalid model: {model}"}
            # Update registry with new model
            existing = self.registry.get(chat_id)
            if not existing:
                # Session not started yet — store model preference so it's picked up on first message
                # Derive session_name from chat_id (e.g. "dispatch-app:uuid" → "dispatch-app/uuid")
                session_name = chat_id.replace(":", "/", 1)
                self.registry.register(chat_id=chat_id, session_name=session_name, model=model)
                return {"ok": True, "message": f"Stored model preference {model} for {chat_id} (session not yet active)"}
            existing["model"] = model
            self.registry.register(**existing)
            # Restart session to pick up new model
            session = await self.backend.restart_session(chat_id)
            return {"ok": session is not None, "message": f"Set model to {model} for {chat_id}"}
        elif cmd == "kill_all_sessions":
            count = await self.backend.kill_all_sessions()
            return {"ok": True, "message": f"Killed {count} sessions"}
        elif cmd == "status":
            sessions = await self.backend.get_all_sessions()
            # Enrich with registry data (session_name, etc.)
            for s in sessions:
                s_chat_id = s.get("chat_id")
                reg = self.registry.get(s_chat_id) if s_chat_id else None
                if reg:
                    s["session_name"] = reg.get("session_name", "")
                    if not s.get("tier"):
                        s["tier"] = reg.get("tier", "")
            return {"ok": True, "sessions": sessions}
        elif cmd == "restart_api":
            if hasattr(self, 'restart_api_callback') and self.restart_api_callback:
                return self.restart_api_callback()
            return {"ok": False, "error": "restart_api not available"}
        elif cmd == "investigation_dispatch":
            return await self._cmd_dispatch_investigation(request)
        elif cmd == "investigation_status":
            return await self._cmd_investigation_status(request)
        elif cmd == "investigation_list":
            return await self._cmd_investigation_list(request)
        elif cmd == "investigation_cancel":
            return await self._cmd_investigation_cancel(request)
        else:
            return {"ok": False, "error": f"Unknown command: {cmd}"}

    async def _cmd_dispatch_investigation(self, req: dict) -> dict:
        from_session = req.get("from_session")
        prompt = req.get("prompt", "")
        allow_mutations = bool(req.get("allow_mutations", False))
        timeout_minutes = int(req.get("timeout_minutes", 15))
        if not from_session:
            return {"ok": False, "error": "from_session required"}
        if not prompt:
            return {"ok": False, "error": "prompt required"}

        if from_session not in self.backend.sessions:
            return {"ok": False,
                    "error": f"Originating session not found: {from_session}"}

        from assistant import investigations
        producer = getattr(self.backend, "_producer", None)
        try:
            task_id = investigations.dispatch(
                producer, prompt, from_session,
                allow_mutations=allow_mutations,
                timeout_minutes=timeout_minutes,
            )
        except investigations.RecursiveDispatchError as e:
            return {"ok": False, "error": str(e), "code": "recursive"}
        except investigations.RateLimitExceeded as e:
            return {"ok": False, "error": str(e), "code": "rate_limited"}
        except investigations.InvestigationError as e:
            return {"ok": False, "error": str(e), "code": "invalid"}
        return {"ok": True, "task_id": task_id,
                "message": f"Investigation dispatched: {task_id}"}

    async def _cmd_investigation_status(self, req: dict) -> dict:
        from assistant import investigations
        task_id = req.get("task_id")
        if not task_id:
            return {"ok": False, "error": "task_id required"}
        entry = investigations.get_status(task_id)
        if entry is None:
            return {"ok": False, "error": f"Investigation not found: {task_id}"}
        return {"ok": True, "investigation": entry}

    async def _cmd_investigation_list(self, req: dict) -> dict:
        from assistant import investigations
        from_session = req.get("from_session")
        entries = investigations.list_inflight(from_session=from_session)
        return {"ok": True, "investigations": entries}

    async def _cmd_investigation_cancel(self, req: dict) -> dict:
        from assistant import investigations
        task_id = req.get("task_id")
        if not task_id:
            return {"ok": False, "error": "task_id required"}
        ok = investigations.cancel(task_id)
        if not ok:
            return {"ok": False, "error": "Investigation not cancellable "
                                          "(missing or already terminal)"}
        session_key = f"ephemeral-{task_id}"
        if session_key in self.backend.sessions:
            try:
                await self.backend.kill_ephemeral_session(task_id)
            except Exception as e:
                return {"ok": True,
                        "message": f"Marked cancelled but kill failed: {e}"}
        return {"ok": True, "message": f"Cancelled {task_id}"}

    async def _cmd_inject(self, req: dict) -> dict:
        chat_id = req.get("chat_id")
        prompt = req.get("prompt", "")
        is_sms = req.get("sms", False)
        is_admin = req.get("admin", False)
        is_app = req.get("app", False)
        contact_name = req.get("contact_name")
        tier = req.get("tier")
        source = req.get("source", "imessage")
        reply_to = req.get("reply_to")
        attachment = req.get("attachment")  # Optional: {"path": "/path/to/image.jpg"}

        if not chat_id or not prompt:
            return {"ok": False, "error": "chat_id and prompt required"}

        # Wrap prompt if needed
        final_prompt = prompt
        if is_sms and contact_name and tier:
            final_prompt = wrap_sms(final_prompt, contact_name, tier, chat_id, reply_to_guid=reply_to, source=source, app=is_app)
        if is_admin:
            final_prompt = wrap_admin(final_prompt)

        # Append tier-specific rules reminder suffix (only for tiers with rules files)
        if tier in ["admin", "partner", "family", "favorite", "bots", "unknown"]:
            rules_file = f"~/.claude/skills/sms-assistant/{tier}-rules.md"
            suffix = f"\n\nREMINDER: If you haven't already, read {rules_file} for important behavioral guidelines for {tier} tier contacts."
            final_prompt = final_prompt + suffix

        # Determine if this is a group
        is_group = is_group_chat_id(chat_id)

        # Build attachments list if attachment provided
        attachments = None
        message_timestamp = None
        if attachment:
            attachments = [attachment]
            # Use current time as message timestamp for CLI injections
            from datetime import datetime
            message_timestamp = datetime.now()

        try:
            if is_group:
                await self.backend.inject_group_message(
                    chat_id=chat_id,
                    display_name=contact_name or "Group",
                    sender_name=contact_name or "Admin",
                    sender_tier=tier or "admin",
                    text=final_prompt,
                    attachments=attachments,
                    source=source,
                    message_timestamp=message_timestamp,
                )
            else:
                await self.backend.inject_message(
                    contact_name or "Unknown", chat_id, final_prompt, tier or "admin",
                    attachments=attachments,
                    source=source,
                    message_timestamp=message_timestamp,
                )

            reg_data = self.registry.get(chat_id)
            session_name = reg_data.get("session_name", chat_id) if reg_data else chat_id
            return {"ok": True, "message": f"Injected into {session_name}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}


class StartupResult(Enum):
    """Result of a child process startup attempt."""
    READY = "ready"          # /health returned 200 within timeout
    SLOW_START = "slow"      # Process alive but no /health after timeout
    FAILED = "failed"        # Process exited during probe or spawn failed


class ChildSupervisor:
    """Supervises a single child process with readiness probes and auto-restart.

    Owns the full lifecycle of one child process:
    - Spawns the process and verifies it starts healthy (readiness probe)
    - Monitors process liveness every POLL_INTERVAL seconds
    - Auto-restarts on crash with exponential backoff
    - Enters degraded mode after MAX_FAST_RESTARTS in RESTART_WINDOW
    - Coordinates with the 300s deep health check via asyncio.Lock

    Usage:
        supervisor = ChildSupervisor("dispatch_api", spawn_fn, "http://localhost:9091/health")
        result = await supervisor.start()          # In Manager.run()
        task = asyncio.create_task(supervisor.run_forever())  # Background supervision
        await supervisor.stop()                    # On shutdown
    """

    MAX_FAST_RESTARTS = 5        # Max restarts in rolling window before degraded
    RESTART_WINDOW = 300         # Rolling window (seconds)
    POLL_INTERVAL = 10           # Liveness check interval (seconds)
    READINESS_TIMEOUT = 15       # Max wait for /health after spawn (seconds)
    READINESS_POLL = 0.5         # Poll interval during readiness check (seconds)
    BACKOFF_SEQUENCE = [0, 10, 30, 60]  # Seconds, capped at last value

    def __init__(self, name: str, spawn_fn, health_url: str,
                 alert_fn=None, producer=None, health_timeout: float = 2.0):
        """
        Args:
            name: Identifier for logging (e.g. "dispatch_api", "metro")
            spawn_fn: Callable that returns subprocess.Popen or None
            health_url: HTTP endpoint to probe for readiness (e.g. "http://localhost:9091/health")
            alert_fn: Optional callable(str) to alert admin (e.g. send SMS). Called via to_thread.
            producer: Optional bus producer for emitting lifecycle events
            health_timeout: HTTP request timeout for health probes (seconds)
        """
        self.name = name
        self._spawn_fn = spawn_fn
        self._health_url = health_url
        self._health_timeout = health_timeout
        self._alert_fn = alert_fn
        self._producer = producer
        self._proc: Optional[subprocess.Popen] = None
        self._lock = asyncio.Lock()
        self._restart_timestamps: deque = deque(maxlen=50)
        self._degraded = False

    @property
    def proc(self) -> Optional[subprocess.Popen]:
        """Current child process (read-only access for external code)."""
        return self._proc

    @property
    def degraded(self) -> bool:
        """Whether the supervisor is in degraded mode."""
        return self._degraded

    # ── Health checks ──

    def _check_health_sync(self) -> bool:
        """Sync HTTP health check — only called via asyncio.to_thread."""
        try:
            import urllib.request
            with urllib.request.urlopen(self._health_url, timeout=self._health_timeout) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def _check_health_async(self) -> bool:
        """Non-blocking HTTP health check."""
        try:
            return await asyncio.to_thread(self._check_health_sync)
        except Exception:
            return False

    # ── Process lifecycle ──

    async def _cleanup_process(self):
        """Ensure old process is fully stopped and port is released.

        MUST be called WITHOUT holding self._lock (contains async sleeps).
        """
        if self._proc is None:
            return
        try:
            if self._proc.poll() is None:
                # Try graceful SIGTERM to process group first
                try:
                    pgid = os.getpgid(self._proc.pid)
                    os.killpg(pgid, signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    try:
                        self._proc.terminate()
                    except (ProcessLookupError, OSError):
                        pass
                try:
                    await asyncio.to_thread(self._proc.wait, timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        pgid = os.getpgid(self._proc.pid)
                        os.killpg(pgid, signal.SIGKILL)
                    except (ProcessLookupError, OSError):
                        try:
                            self._proc.kill()
                        except (ProcessLookupError, OSError):
                            pass
                    try:
                        await asyncio.to_thread(self._proc.wait, timeout=2)
                    except Exception:
                        pass
        except (ProcessLookupError, OSError):
            pass
        self._proc = None
        # Port release grace period
        await asyncio.sleep(0.5)

    def _recent_restarts(self) -> list:
        """Restarts within the rolling window."""
        cutoff = time.time() - self.RESTART_WINDOW
        return [t for t in self._restart_timestamps if t > cutoff]

    # ── Startup ──

    async def start(self) -> StartupResult:
        """Spawn the child process and wait for it to become healthy.

        Returns StartupResult indicating whether the process started successfully.
        """
        # Cleanup any existing process (lock-free, contains sleeps)
        await self._cleanup_process()

        # Spawn under lock (brief, no I/O)
        async with self._lock:
            try:
                self._proc = self._spawn_fn()
            except Exception as e:
                lifecycle_log.error(f"{self.name} | SPAWN_ERROR | {e}")
                self._proc = None
                return StartupResult.FAILED
            if self._proc is None:
                lifecycle_log.error(f"{self.name} | SPAWN_FAILED | spawn_fn returned None")
                return StartupResult.FAILED
            lifecycle_log.info(f"{self.name} | SPAWNED | pid={self._proc.pid}")
            produce_event(self._producer, "system", "health.service_spawned",
                service_spawned_payload(self.name, self._proc.pid), source="daemon")

        # Readiness probe (lock-free)
        result = await self._readiness_probe()
        if result == StartupResult.READY:
            # Fresh initial start — clear restart history
            self._restart_timestamps.clear()
            self._degraded = False
        return result

    async def _readiness_probe(self) -> StartupResult:
        """Poll /health until the process is ready, exits, or times out."""
        start_time = time.monotonic()
        for _ in range(int(self.READINESS_TIMEOUT / self.READINESS_POLL)):
            await asyncio.sleep(self.READINESS_POLL)
            if self._proc is None or self._proc.poll() is not None:
                exit_code = self._proc.returncode if self._proc else -1
                lifecycle_log.info(f"{self.name} | STARTUP_CRASH | exit_code={exit_code}")
                return StartupResult.FAILED
            if await self._check_health_async():
                elapsed = time.monotonic() - start_time
                lifecycle_log.info(f"{self.name} | READY | startup_ms={int(elapsed * 1000)}")
                return StartupResult.READY
        lifecycle_log.warning(f"{self.name} | STARTUP_TIMEOUT | waited={self.READINESS_TIMEOUT}s")
        # Process alive but slow — let run_forever monitor it.
        # The 300s deep health check will detect if it never becomes healthy.
        return StartupResult.SLOW_START

    # ── Main supervision loop ──

    async def run_forever(self):
        """Main supervision loop — runs as asyncio.create_task.

        Checks process liveness every POLL_INTERVAL seconds. On crash,
        restarts with exponential backoff. Enters degraded mode after
        MAX_FAST_RESTARTS in RESTART_WINDOW to prevent restart storms.
        """
        while True:
            await asyncio.sleep(self.POLL_INTERVAL)

            # Quick liveness check (no lock, no HTTP, just .poll())
            if self._proc is None:
                continue
            if self._proc.poll() is None:
                continue  # Process alive

            # Process exited — handle crash
            await self._handle_crash()

    async def _handle_crash(self):
        """Handle a detected child process crash with backoff and restart."""
        exit_code = self._proc.returncode if self._proc else -1
        lifecycle_log.info(json.dumps({
            "event": "child_exited",
            "process": self.name,
            "exit_code": exit_code,
            "recent_restarts": len(self._recent_restarts()),
        }))
        produce_event(self._producer, "system", "health.service_restarted",
            service_restarted_payload(self.name, f"exited(rc={exit_code})"), source="health")

        if self._degraded:
            lifecycle_log.info(f"{self.name} | DEGRADED_SKIP | waiting for deep health check")
            return

        # Phase 1: Backoff (lock-free, no state mutation)
        recent = self._recent_restarts()
        idx = min(len(recent), len(self.BACKOFF_SEQUENCE) - 1)
        backoff = self.BACKOFF_SEQUENCE[idx]
        if backoff > 0:
            lifecycle_log.info(f"{self.name} | BACKOFF_WAIT | {backoff}s before restart")
            await asyncio.sleep(backoff)

        # Phase 2: Cleanup old process (lock-free, contains sleeps)
        await self._cleanup_process()

        # Phase 3: Restart (under lock, brief, no blocking I/O)
        alert_msg = None
        async with self._lock:
            # Re-check: maybe someone else already restarted
            if self._proc is not None and self._proc.poll() is None:
                return

            self._restart_timestamps.append(time.time())
            recent = self._recent_restarts()
            if len(recent) > self.MAX_FAST_RESTARTS:
                self._degraded = True
                alert_msg = (
                    f"🚨 {self.name} crashed {len(recent)}x in "
                    f"{self.RESTART_WINDOW}s — entering degraded mode. "
                    f"Check logs/{self.name.replace('_', '-')}.log"
                )
                lifecycle_log.error(f"{self.name} | DEGRADED | {len(recent)} restarts in window")
            else:
                try:
                    self._proc = self._spawn_fn()
                except Exception as e:
                    lifecycle_log.error(f"{self.name} | RESTART_SPAWN_ERROR | {e}")
                    self._proc = None
                if self._proc:
                    lifecycle_log.info(
                        f"{self.name} | RESTARTED | pid={self._proc.pid} "
                        f"attempt={len(recent)}"
                    )

        # Phase 4: Post-lock actions (alert + readiness probe)
        if alert_msg and self._alert_fn:
            try:
                await asyncio.to_thread(self._alert_fn, alert_msg)
            except Exception:
                pass  # Best-effort alert

        if self._proc and not self._degraded:
            await self._readiness_probe()

    # ── Deep health check integration ──

    def clear_degraded(self):
        """Clear degraded mode — called by the 300s deep health check.

        Does NOT restart the process. The next run_forever iteration will
        detect the dead process and restart with the rolling window budget.
        Timestamp history is preserved so a persistent crasher re-enters
        degraded mode quickly.

        Thread-safety: This is called from an async task on the same event loop.
        Since asyncio is cooperative and _degraded is a boolean, no lock needed.
        The supervisor's _handle_crash re-checks _degraded under the lock.
        """
        if self._degraded:
            self._degraded = False
            lifecycle_log.info(f"{self.name} | DEGRADED_CLEARED | by deep health check")

    # ── Shutdown ──

    async def stop(self):
        """Graceful shutdown — stop the child process."""
        await self._cleanup_process()
        lifecycle_log.info(f"{self.name} | STOPPED")


class Manager:
    """Main manager that orchestrates everything."""

    def __init__(self):
        self.contacts = ContactsManager()
        self.messages = MessagesReader(contacts_manager=self.contacts)
        self.registry = SessionRegistry(SESSION_REGISTRY_FILE)
        self._resource_registry = None  # Set in run() via async with

        # Initialize event bus
        from bus.bus import Bus
        self._bus = Bus(db_path=str(STATE_DIR / "bus.db"))
        self._bus.create_topic("messages", retention_ms=168 * 3600 * 1000)  # 7 days
        self._bus.create_topic("sessions", retention_ms=168 * 3600 * 1000)
        self._bus.create_topic("system", retention_ms=168 * 3600 * 1000)
        self._bus.create_topic("reminders", retention_ms=168 * 3600 * 1000)
        self._bus.create_topic("tasks", retention_ms=168 * 3600 * 1000)  # 7 days
        self._bus.create_topic("messages.dlq", retention_ms=30 * 24 * 3600 * 1000)  # 30 days
        self._bus.create_topic("facts", retention_ms=30 * 24 * 3600 * 1000)  # 30 days
        self._bus.create_topic("imessage.ui", retention_ms=24 * 3600 * 1000)  # 1 day — tapback reactions, typing indicators
        self._bus.create_topic("email", retention_ms=168 * 3600 * 1000)  # 7 days — gmail email events
        self._producer = self._bus.producer()
        self._dlq_retry_counts: dict[str, int] = {}  # offset_key -> retry_count

        # Seed quota cache from disk so get_quota_cached() works immediately
        try:
            quota_file = STATE_DIR / "quota_cache.json"
            if quota_file.exists():
                raw = json.loads(quota_file.read_text())
                from assistant.health import seed_quota_cache
                seed_quota_cache(raw.get("data"), raw.get("updated_at"))
        except Exception as e:
            log.warning("QUOTA | Failed to seed from disk: %s", e)

        self.sessions = SDKBackend(
            registry=self.registry,
            contacts_manager=self.contacts,
            producer=self._producer,
        )
        self.reminders = ReminderPoller(self.sessions, self.contacts)
        self.ipc = IPCServer(self.sessions, self.registry, self.contacts)
        self.ipc.restart_api_callback = self._restart_dispatch_api

        # Child process supervisors (spawned in run(), not here — needs event loop)
        self.dispatch_api_supervisor = ChildSupervisor(
            name="dispatch_api",
            spawn_fn=self._create_dispatch_api_process,
            health_url=f"http://localhost:{DISPATCH_API_PORT}/health",
            alert_fn=self._alert_admin,
            producer=self._producer,
        )
        self.metro_supervisor = ChildSupervisor(
            name="metro",
            spawn_fn=self._create_metro_process,
            health_url=f"http://localhost:{METRO_PORT}/status",
            alert_fn=self._alert_admin,
            producer=self._producer,
        )
        # Consumer restart counters (used by _on_consumer_done / _on_task_consumer_done)
        self._message_consumer_restarts: int = 0
        self._task_consumer_restarts: int = 0

        # Track which disabled chats/backends have been notified (one-time per daemon run)
        self._disabled_notice_sent: set[str] = set()

        # Backward compat: expose process refs for resource registry / external code
        self.dispatch_api_daemon = None  # Set after start() in run()
        self.metro_daemon = None
        # Supervisor background tasks (set in run())
        self._dispatch_api_supervisor_task: Optional[asyncio.Task] = None
        self._metro_supervisor_task: Optional[asyncio.Task] = None

        # Signal integration
        self.signal_queue = queue.Queue()
        self.signal_daemon = None
        self.signal_listener = None

        # Test message integration
        self.test_queue = queue.Queue()
        self.test_watcher = None

        # Discord integration
        self.discord_queue = queue.Queue()
        self.discord_listener = None

        # Load last processed ROWID
        self.last_rowid = self._load_state()

        # Shutdown flag
        self._shutdown_flag = False
        self._start_time = time.time()

        # Health check background task flag (prevents overlapping runs)
        self._health_check_running = False

        # Unified dependency health & recovery framework (self-healing-resilience
        # plan §4.1). Holds the registry of external-dependency checks
        # (chrome_control, signal_cli, bus_consumers, disk, fd_leak) — each with
        # probe/recover/backoff/escalate. Built in run() once the manager is wired
        # up; ticked from the poll loop. Replaces the ad-hoc chrome-control check.
        self._dep_health_runner = None  # set in _run_with_registry()

        # Blocked-session watchdog: prevents a session from idling forever with an
        # outstanding commitment ("I'll do X" / "waiting on you"). See
        # _run_stuck_session_check(). Guard against overlapping runs + per-session
        # backoff so we nudge once, then escalate-via-the-session — never spam.
        self._stuck_check_running = False
        self._last_stuck_nudge_at: Dict[str, float] = {}  # session_name -> monotonic-ish time.time()

        # Message consumer: asyncio.Event for near-zero-latency notification
        self._consumer_notify = asyncio.Event()
        self._consumer_executor = ThreadPoolExecutor(1, thread_name_prefix="bus-consumer")
        self._consumer_task: asyncio.Task | None = None

        # Task consumer: handles task.requested events from bus
        self._task_consumer_notify = asyncio.Event()
        self._task_consumer_executor = ThreadPoolExecutor(1, thread_name_prefix="task-consumer")
        self._task_consumer_task: asyncio.Task | None = None
        self._ephemeral_tasks: Dict[str, dict] = {}  # task_id -> tracking info
        self._running_script_tasks: Dict[str, asyncio.Task] = {}  # task_id -> asyncio.Task
        # Replay-guard: recently-completed trace_ids -> completion ts.
        # Keyed on trace_id (NOT task_id) so legitimate scheduler re-fires of
        # the same task_id (e.g. */30 cron) each get a fresh trace and pass
        # through, while a genuine consumer replay of the same event (same
        # trace_id) is caught.
        self._completed_task_traces: Dict[str, float] = {}

        # Initialize bus consumers
        self._consumer_runner = self._init_consumers()

    def _init_consumers(self):
        """Initialize ConsumerRunner with audit consumers for all 3 topics.

        These consumers log events for observability and validate the bus
        is working end-to-end. Future consumers will add real processing
        (e.g., vision indexing, reminder audit trails, consolidation coordination).
        """
        from bus.consumers import ConsumerRunner, ConsumerConfig, actions
        from assistant.fact_reminder_consumer import handle_fact_event
        from assistant.tweet_consumer import handle_tweet_scheduled

        configs = [
            # Audit consumer for messages topic — tracks all message flow
            ConsumerConfig(
                topic="messages",
                group="audit-messages",
                action=actions.call_function(
                    lambda records: log.info(
                        f"BUS_CONSUMER | messages | {len(records)} record(s): "
                        + ", ".join(f"{r.type}[{r.key}]" for r in records[:5])
                        + ("..." if len(records) > 5 else "")
                    )
                ),
                commit_interval_s=10,  # Batch commits to reduce write lock contention
            ),
            # Audit consumer for sessions topic — tracks session lifecycle
            ConsumerConfig(
                topic="sessions",
                group="audit-sessions",
                action=actions.call_function(
                    lambda records: log.info(
                        f"BUS_CONSUMER | sessions | {len(records)} record(s): "
                        + ", ".join(f"{r.type}[{r.key}]" for r in records[:5])
                        + ("..." if len(records) > 5 else "")
                    )
                ),
                commit_interval_s=10,  # Batch commits to reduce write lock contention
            ),
            # Audit consumer for system topic — tracks health, consolidation, vision, etc.
            ConsumerConfig(
                topic="system",
                group="audit-system",
                action=actions.call_function(
                    lambda records: log.info(
                        f"BUS_CONSUMER | system | {len(records)} record(s): "
                        + ", ".join(f"{r.type}[{r.source}]" for r in records[:5])
                        + ("..." if len(records) > 5 else "")
                    )
                ),
                commit_interval_s=10,  # Batch commits to reduce write lock contention
            ),
            # Fact → Reminder consumer: watches facts topic for temporal facts,
            # creates reminders at key moments (check-in, pre-departure, landing, daily intel)
            ConsumerConfig(
                topic="facts",
                group="fact-reminder-creator",
                filter=lambda r: r.type in ("fact.created", "fact.updated"),
                action=actions.call_function(handle_fact_event),
                max_retries=2,
                error_action=actions.dead_letter(self._bus, "dead-letters"),
                commit_interval_s=5,
            ),
            # Compaction consumer: handles conditional "done compacting" notifications.
            # When post-compact-hook produces compaction.completed, the manager checks
            # if the user was notified during compaction and sends "done" if applicable.
            ConsumerConfig(
                topic="system",
                group="compaction-handler",
                filter=lambda r: r.type == "compaction.completed",
                action=actions.call_function(self._handle_compaction_completed_records),
                commit_interval_s=5,
            ),
            # iMessage UI automation consumer: serializes all Messages.app UI actions
            # (tapback reactions, typing indicators) through a single consumer to prevent contention.
            ConsumerConfig(
                topic="imessage.ui",
                group="imessage-ui-worker",
                action=actions.call_function(self._handle_imessage_ui_records),
                max_retries=1,
                error_action=actions.dead_letter(self._bus, "messages.dlq"),
                commit_interval_s=1,
            ),
            # Tweet consumer: posts scheduled tweets from the midnight tweet planner.
            # Listens for tweet.scheduled events, waits until the scheduled time, then posts.
            ConsumerConfig(
                topic="tweets",
                group="tweet-poster",
                filter=lambda r: r.type == "tweet.scheduled",
                action=actions.call_function(handle_tweet_scheduled),
                max_retries=2,
                error_action=actions.dead_letter(self._bus, "dead-letters"),
                commit_interval_s=5,
            ),
            # Quota handler: processes quota.fetched events from health check.
            # Writes cache file, checks thresholds for SMS alerts, runs model degradation.
            ConsumerConfig(
                topic="system",
                group="quota-handler",
                filter=lambda r: r.type == "quota.fetched",
                action=actions.call_function(self._handle_quota_event),
                commit_interval_s=5,
            ),
        ]
        return ConsumerRunner(self._bus, configs)

    def _start_consumer_thread(self):
        """Start ConsumerRunner in a background thread with auto-restart.

        Exactly ONE consumer thread may drive the runner. Two instances fence
        each other forever: each rebuild joins the groups, bumping generations,
        which raises StaleGenerationError in the other instance, which rebuilds
        and fences back — an infinite crash loop (8.6k crashes, July 2026).
        Guards:
          - a superseded thread (manager._consumer_thread no longer itself)
            exits instead of competing;
          - run_forever() returning cleanly (deliberate stop()) ends the
            thread instead of respawning the runner.
        """
        def _run():
            import time
            while True:
                if getattr(self, "_consumer_thread", None) is not threading.current_thread():
                    log.info("ConsumerRunner thread superseded — exiting")
                    return
                try:
                    log.info("ConsumerRunner started (background thread)")
                    self._consumer_runner.run_forever(poll_interval_ms=500)
                    # Clean return = deliberate stop() — do NOT respawn.
                    log.info("ConsumerRunner stopped cleanly — consumer thread exiting")
                    return
                except Exception as e:
                    log.error(f"ConsumerRunner crashed: {e} — restarting in 5s")
                    produce_event(self._producer, "system", "consumer.crashed",
                        {"error": str(e)}, source="consumer")
                    time.sleep(5)
                    if getattr(self, "_consumer_thread", None) is not threading.current_thread():
                        log.info("ConsumerRunner thread superseded during backoff — exiting")
                        return
                    # Rebuild consumer runner so it gets fresh DB connections
                    try:
                        self._consumer_runner = self._init_consumers()
                        log.info("ConsumerRunner rebuilt after crash, restarting loop")
                    except Exception as rebuild_err:
                        log.error(f"ConsumerRunner rebuild failed: {rebuild_err} — retrying in 30s")
                        time.sleep(30)

        thread = threading.Thread(
            target=_run,
            name="bus-consumer-runner",
            daemon=True,
        )
        # Publish the thread as the one-and-only BEFORE it starts so the
        # supersession check can never race a fresh thread against itself.
        self._consumer_thread = thread
        thread.start()
        return thread

    # Constants for iMessage UI consumer (hoisted to class level)
    _IMESSAGE_UI_GUID_PATTERN = re.compile(r'^(p:\d+/|s:\d+/|iMessage;)')
    _TAPBACK_STALENESS_MS = 30_000  # 30s
    _TYPING_STALENESS_MS = 5_000   # 5s

    # Emoji → tapback reaction name mapping for Messages.app UI automation
    _EMOJI_TO_TAPBACK = {
        "❤️": "heart", "♥️": "heart", "❤": "heart",
        "👍": "thumbsup", "👍🏻": "thumbsup", "👍🏼": "thumbsup",
        "👍🏽": "thumbsup", "👍🏾": "thumbsup", "👍🏿": "thumbsup",
        "👎": "thumbsdown", "👎🏻": "thumbsdown", "👎🏼": "thumbsdown",
        "👎🏽": "thumbsdown", "👎🏾": "thumbsdown", "👎🏿": "thumbsdown",
        "😂": "haha", "😆": "haha",
        "❗": "exclamation", "‼️": "exclamation", "!!": "exclamation",
        "❓": "question", "?": "question",
    }

    # Track the currently visible chat to avoid redundant navigation
    _current_chat_id: Optional[str] = None

    def _navigate_to_chat(self, chat_id: str) -> bool:
        """Navigate Messages.app to the specified chat.

        Uses `open imessage://` URL scheme for individual chats.
        For group chats, uses Messages sidebar search to find and select the chat.
        Skips navigation if already on the target chat.
        Returns True if navigation succeeded (or was unnecessary).
        """
        if self._current_chat_id == chat_id:
            log.debug(f"IMESSAGE_UI | already on chat {chat_id}, skipping navigation")
            return True

        is_group = not chat_id.startswith("+")
        if is_group:
            return self._navigate_to_group_chat(chat_id)

        open_result = subprocess.run(
            ["open", f"imessage://{chat_id}"],
            capture_output=True, text=True, timeout=5
        )
        if open_result.returncode != 0:
            log.error(f"IMESSAGE_UI | failed to open chat for {chat_id}: {open_result.stderr}")
            return False

        # Wait for Messages to navigate to the chat
        time.sleep(1.0)
        self._current_chat_id = chat_id
        return True

    def _navigate_to_group_chat(self, chat_id: str) -> bool:
        """Navigate Messages.app to a group chat using sidebar search.

        Looks up the group's display_name from chat.db, then uses AppleScript
        to search the Messages sidebar and click the matching chat button.
        Returns True if navigation succeeded.
        """
        # Look up group display name from chat.db
        try:
            conn = sqlite3.connect(f"file:{MESSAGES_DB}?mode=ro", uri=True)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT display_name FROM chat WHERE chat_identifier = ?",
                (chat_id,)
            )
            row = cursor.fetchone()
            conn.close()
            if not row or not row[0]:
                log.error(f"IMESSAGE_UI | no display_name found for group {chat_id}")
                return False
            group_name = row[0]
        except Exception as e:
            log.error(f"IMESSAGE_UI | failed to look up group name for {chat_id}: {e}")
            return False

        log.info(f"IMESSAGE_UI | navigating to group chat '{group_name}' ({chat_id})")

        # Use AppleScript to search sidebar and click the matching chat button.
        # Steps: Escape any state → find search field → set search text →
        # find button matching group name in results → click it → escape search.
        # The search field description varies ("Search" or "search text field"),
        # so we match with "contains earch".
        script = f'''
            tell application "Messages"
                activate
            end tell

            delay 0.3

            tell application "System Events"
                tell process "Messages"
                    -- Escape any existing state
                    key code 53
                    delay 0.3

                    -- Find the search text field (description varies between OS versions)
                    set allElements to entire contents of window 1
                    set searchField to missing value
                    repeat with elem in allElements
                        try
                            if class of elem is text field then
                                set d to description of elem
                                if d contains "earch" then
                                    set searchField to elem
                                    exit repeat
                                end if
                            end if
                        end try
                    end repeat

                    if searchField is missing value then
                        return "ERROR: Search field not found"
                    end if

                    -- Click search field and enter group name
                    click searchField
                    delay 0.5
                    set value of searchField to "{group_name}"
                    delay 1.5

                    -- Find and click the button matching the group name in search results
                    set allElements to entire contents of window 1
                    repeat with elem in allElements
                        try
                            if class of elem is button then
                                set d to description of elem
                                if d is "{group_name}" then
                                    click elem
                                    delay 0.5
                                    -- Clear the search with Escape
                                    key code 53
                                    delay 0.3
                                    return name of window 1
                                end if
                            end if
                        end try
                    end repeat

                    -- Fallback: escape and report failure
                    key code 53
                    delay 0.3
                    return "ERROR: Group chat button not found in search results"
                end tell
            end tell
        '''

        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=20
            )
            output = result.stdout.strip()

            if result.returncode != 0 or output.startswith("ERROR:"):
                log.error(f"IMESSAGE_UI | group chat navigation failed: {result.stderr or output}")
                return False

            # Verify window title matches the group name
            if group_name.lower() in output.lower():
                log.info(f"IMESSAGE_UI | navigated to group chat '{output}'")
                self._current_chat_id = chat_id
                return True
            else:
                log.warning(
                    f"IMESSAGE_UI | window title mismatch: got '{output}', "
                    f"expected '{group_name}' — proceeding anyway"
                )
                self._current_chat_id = chat_id
                return True

        except subprocess.TimeoutExpired:
            log.error(f"IMESSAGE_UI | group chat navigation timed out for {chat_id}")
            return False
        except Exception as e:
            log.error(f"IMESSAGE_UI | group chat navigation error: {e}")
            return False

    def _execute_tapback(self, chat_id: str, reaction: str, guid: str) -> bool:
        """Execute a tapback reaction via Messages.app UI automation.

        Returns True if tapback was sent successfully.
        """
        # Map emoji to reaction name (also accept raw names like "thumbsup")
        reaction_name = self._EMOJI_TO_TAPBACK.get(reaction, reaction.lower().strip())
        valid_names = {"heart", "thumbsup", "thumbsdown", "haha", "exclamation", "question"}
        if reaction_name not in valid_names:
            log.warning(f"IMESSAGE_UI | unknown reaction '{reaction}' (mapped to '{reaction_name}'), skipping")
            return False

        tapback_script = Path.home() / ".claude/skills/sms-assistant/scripts/tapback.scpt"
        if not tapback_script.exists():
            log.error(f"IMESSAGE_UI | tapback.scpt not found at {tapback_script}")
            return False

        # GUID verification: Cmd+T reacts to the most recent incoming message.
        # If a new message arrived between queueing and execution, the tapback
        # would land on the wrong message. Query chat.db to verify the target
        # GUID is among recent messages. We check the last 10 messages (not just
        # the latest) to handle group chats where messages arrive rapidly and
        # our own replies interleave with incoming messages.
        if guid:
            try:
                conn = sqlite3.connect(f"file:{MESSAGES_DB}?mode=ro", uri=True)
                conn.execute("PRAGMA busy_timeout=2000")
                rows = conn.execute(
                    """
                    SELECT m.guid FROM message m
                    JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                    JOIN chat c ON c.ROWID = cmj.chat_id
                    WHERE c.chat_identifier = ?
                    ORDER BY m.date DESC LIMIT 10
                    """,
                    (chat_id,),
                ).fetchall()
                conn.close()
                # Normalize GUIDs for comparison: chat.db stores raw UUIDs,
                # but the prompt injects p:0/UUID format. Strip the prefix.
                def _strip_guid_prefix(g: str) -> str:
                    import re
                    return re.sub(r'^[ps]:\d+/', '', g)
                target_stripped = _strip_guid_prefix(guid)
                recent_guids = [_strip_guid_prefix(r[0]) for r in rows]
                if recent_guids and target_stripped not in recent_guids:
                    log.warning(
                        f"IMESSAGE_UI | tapback GUID not found in recent messages for {chat_id}: "
                        f"target={guid}, skipping to avoid wrong-message reaction"
                    )
                    return False
                # Warn if target is not the most recent (Cmd+T will react to latest)
                if recent_guids and target_stripped != recent_guids[0]:
                    log.warning(
                        f"IMESSAGE_UI | tapback target is not the most recent message "
                        f"(target={guid}, latest={rows[0][0]}). Cmd+T may react to wrong message."
                    )
            except Exception as e:
                log.warning(f"IMESSAGE_UI | GUID verification failed ({e}), proceeding anyway")

        if not self._navigate_to_chat(chat_id):
            return False

        result = subprocess.run(
            ["osascript", str(tapback_script), reaction_name],
            capture_output=True, text=True, timeout=10
        )

        if result.returncode == 0 and "TAPBACK" in result.stdout:
            log.info(f"IMESSAGE_UI | tapback {reaction_name} sent successfully for {chat_id}")
            return True
        else:
            log.error(f"IMESSAGE_UI | tapback failed: rc={result.returncode} stdout={result.stdout.strip()} stderr={result.stderr.strip()}")
            return False

    def _execute_read_receipt(self, chat_id: str) -> bool:
        """Mark a chat as read by navigating to it in Messages.app.

        When Messages.app displays a chat, it automatically marks messages
        as read and sends read receipts to the sender. So simply navigating
        to the chat is sufficient.

        Returns True if navigation (and thus read receipt) succeeded.
        """
        if not self._navigate_to_chat(chat_id):
            return False

        log.info(f"IMESSAGE_UI | read receipt sent for {chat_id} (navigated to chat)")
        return True

    def _execute_typing(self, chat_id: str, start: bool) -> bool:
        """Send typing indicator by simulating keystroke in Messages.app.

        For typing.start: navigate to chat, type a space to trigger the
        typing indicator, then immediately delete it.
        For typing.stop: just delete any leftover characters (no-op if clean).

        Returns True if the typing indicator was sent successfully.
        """
        if not self._navigate_to_chat(chat_id):
            return False

        if start:
            # Type a character to trigger the typing indicator, then delete it
            script = '''
            tell application "System Events"
                tell process "Messages"
                    keystroke " "
                    delay 0.1
                    key code 51
                end tell
            end tell
            return "TYPING|start"
            '''
        else:
            # Press backspace to clear any leftover and stop typing indicator
            script = '''
            tell application "System Events"
                tell process "Messages"
                    key code 51
                end tell
            end tell
            return "TYPING|stop"
            '''

        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10
        )

        if result.returncode == 0:
            log.info(f"IMESSAGE_UI | typing {'start' if start else 'stop'} sent for {chat_id}")
            return True
        else:
            log.error(f"IMESSAGE_UI | typing failed: rc={result.returncode} stderr={result.stderr.strip()}")
            return False

    _READ_STALENESS_MS = 10_000  # 10s — read receipts are less time-sensitive

    def _handle_imessage_ui_records(self, records):
        """Bus consumer handler for imessage.ui events (tapback, read, typing).

        Serializes all Messages.app UI automation through a single consumer
        to prevent contention. One topic per UI-exclusive resource.

        Events are batched by chat_id to minimize chat switching — all actions
        for the same chat are executed together before moving to the next.

        NOTE: Per-action try/except catches and logs execution errors to
        prevent a single failed action from aborting the remaining actions
        in the batch. The ConsumerRunner framework handles record-level
        retry (max_retries=1) and DLQ for unhandled exceptions.
        """
        now_ms = int(time.time() * 1000)

        # Phase 1: Filter stale records and group valid ones by chat_id
        # Use list to preserve insertion order within each chat
        chat_actions: Dict[str, list] = {}
        for record in records:
            payload = record.payload if isinstance(record.payload, dict) else json.loads(record.payload)
            age_ms = now_ms - record.timestamp if record.timestamp else 0
            chat_id = payload.get("chat_id", "")

            if record.type == "tapback":
                if age_ms > self._TAPBACK_STALENESS_MS:
                    log.info(f"IMESSAGE_UI | discarding stale tapback ({age_ms}ms old): {record.key}")
                    continue
                guid = payload.get("message_guid", "")
                reaction = payload.get("reaction", "")
                if not guid or not self._IMESSAGE_UI_GUID_PATTERN.match(guid):
                    log.warning(f"IMESSAGE_UI | invalid/missing GUID, discarding tapback: {guid}")
                    continue
                if not reaction or not chat_id:
                    log.warning(f"IMESSAGE_UI | missing reaction or chat_id, discarding")
                    continue
                chat_actions.setdefault(chat_id, []).append(
                    ("tapback", {"reaction": reaction, "guid": guid})
                )

            elif record.type == "read":
                if age_ms > self._READ_STALENESS_MS:
                    log.debug(f"IMESSAGE_UI | discarding stale read ({age_ms}ms old): {record.key}")
                    continue
                if not chat_id:
                    log.warning(f"IMESSAGE_UI | missing chat_id for read event, discarding")
                    continue
                chat_actions.setdefault(chat_id, []).append(("read", {}))

            elif record.type in ("typing.start", "typing.stop"):
                if age_ms > self._TYPING_STALENESS_MS:
                    log.debug(f"IMESSAGE_UI | discarding stale {record.type} ({age_ms}ms old)")
                    continue
                if not chat_id:
                    log.warning(f"IMESSAGE_UI | missing chat_id for typing event, discarding")
                    continue
                is_start = record.type == "typing.start"
                chat_actions.setdefault(chat_id, []).append(
                    ("typing", {"start": is_start})
                )

            else:
                log.warning(f"IMESSAGE_UI | unknown event type: {record.type}")

        # Phase 2: Execute actions grouped by chat_id
        for chat_id, actions in chat_actions.items():
            log.info(f"IMESSAGE_UI | processing {len(actions)} action(s) for {chat_id}")
            for action_type, params in actions:
                try:
                    if action_type == "read":
                        self._execute_read_receipt(chat_id)
                    elif action_type == "tapback":
                        success = self._execute_tapback(chat_id, params["reaction"], params["guid"])
                        if not success:
                            log.warning(f"IMESSAGE_UI | tapback failed for {chat_id}")
                    elif action_type == "typing":
                        self._execute_typing(chat_id, params["start"])
                except Exception as e:
                    log.error(f"IMESSAGE_UI | {action_type} execution error for {chat_id}: {e}")

    def _handle_compaction_completed_records(self, records):
        """Bus consumer handler for compaction.completed events.

        Delegates to SDKBackend.handle_compaction_completed() which checks
        session state and conditionally sends 'done compacting' SMS.
        """
        for record in records:
            try:
                payload = json.loads(record.value) if isinstance(record.value, str) else record.value
                session_name = payload.get("session_name", "")
                duration_s = payload.get("duration_s", 0)
                compaction_epoch = payload.get("compaction_epoch")
                if session_name:
                    self.sessions.handle_compaction_completed(session_name, duration_s,
                                                              compaction_epoch=compaction_epoch)
            except Exception as e:
                log.error(f"COMPACTION_CONSUMER | error processing record: {e}")

    # ── Quota cache file staleness threshold ──────────────────────────
    # One fetch interval (15 min).  If the file is older than this, the
    # self-heal path in _run_health_checks() will rewrite it from memory.
    _QUOTA_CACHE_STALE_S = 15 * 60

    @staticmethod
    def _write_quota_cache(data: dict, source: str = "unknown") -> bool:
        """Atomically write quota_cache.json.  Returns True on success.

        Used by three callers (most → least reliable):
          1. Health check direct-write   — PRIMARY path
          2. Self-heal in health check   — BACKSTOP for persistent failures
          3. Bus consumer                — NON-CRITICAL safety net

        Concurrent writers are safe: os.replace() is atomic on POSIX, and
        quota data changes at ~15-min granularity so any valid write within
        a cycle is acceptable (last-write-wins).
        """
        quota_path = STATE_DIR / "quota_cache.json"
        try:
            payload = json.dumps({
                "data": data,
                "updated_at": datetime.now().isoformat(),
                "error": None,
            })
            fd, tmp = tempfile.mkstemp(dir=STATE_DIR, suffix=".tmp")
            try:
                os.write(fd, payload.encode())
                os.close(fd)
                os.replace(tmp, str(quota_path))
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            log.debug("QUOTA_CACHE | Written via %s to %s", source, quota_path)
            return True
        except Exception as e:
            log.error("QUOTA_CACHE | Write via %s failed: %s", source, e)
            return False

    def _handle_quota_event(self, records):
        """Bus consumer handler for quota.fetched events.

        Processes quota data in 4 independently error-isolated steps:
        1. Write quota_cache.json (redundant with health check direct-write — safety net)
        2. Check thresholds → SMS alerts if crossed
        3. QuotaManager model degradation transitions
        4. Perf gauge metrics
        """
        from assistant.health import check_quota_thresholds, format_quota_alert
        from assistant import config

        for record in records:
            try:
                usage = json.loads(record.value) if isinstance(record.value, str) else record.value
            except Exception as e:
                log.error("QUOTA_HANDLER | Failed to parse record: %s", e)
                continue

            admin_phone = config.get("owner.phone")

            # Step 1: Write quota_cache.json — redundant with health check
            # direct-write, kept as safety net
            cache_data = {k: v for k, v in usage.items()
                          if k not in ("backoff_seconds", "consecutive_failures")}
            self._write_quota_cache(cache_data, source="consumer")

            # Step 2: Threshold alerts (80/90/95% → SMS + bus event)
            try:
                alerts = check_quota_thresholds(cache_data)
                if alerts:
                    for alert in alerts:
                        produce_event(self._producer, "system", "health.quota_alert",
                            quota_alert_payload(alert), source="health")
                    if admin_phone:
                        lines = [format_quota_alert(a) for a in alerts]
                        msg = "[SVEN] Usage alert:\n" + "\n".join(lines)
                        self._send_sms(admin_phone, msg)
                    log.warning("QUOTA_ALERT | Sent %d alert(s) to admin", len(alerts))
            except Exception as e:
                log.error("QUOTA_ALERT | Failed: %s", e)

            # Step 3: Model degradation (QuotaManager state machine)
            try:
                quota_5h = (usage.get("five_hour") or {}).get("utilization", 0)
                quota_7d_opus = (usage.get("seven_day_opus") or {}).get("utilization", 0)
                qm = self.sessions.quota_manager
                qm_actions = qm.check_and_transition(quota_5h, quota_7d_opus)
                if admin_phone:
                    for action in qm_actions:
                        if action == "sms_degraded":
                            override = qm.get_override_info()
                            trigger = override.get("trigger", "unknown") if override else "unknown"
                            self._send_sms(admin_phone,
                                f"[SVEN] ⚠️ Quota at danger level — auto-downgrading all sessions to sonnet.\n"
                                f"Trigger: {trigger}\n"
                                f"5h={quota_5h:.0f}% | 7d-opus={quota_7d_opus:.0f}%\n"
                                f"New sessions will use sonnet. Existing sessions switch on next restart.\n"
                                f"Reminders auto-paused to conserve tokens.\n"
                                f"Manual override: claude-assistant set-global-model opus")
                        elif action == "sms_recovered":
                            self._send_sms(admin_phone,
                                f"[SVEN] ✅ Quota recovered — back to opus.\n"
                                f"5h={quota_5h:.0f}% | 7d-opus={quota_7d_opus:.0f}%\n"
                                f"New sessions will use opus. Reminders re-enabled.")
                        elif action == "sms_still_degraded":
                            hours = getattr(qm, "last_degraded_hours", None)
                            duration = f" ({hours:.1f}h)" if hours else ""
                            self._send_sms(admin_phone,
                                f"[SVEN] ⚠️ Still in degraded mode (sonnet){duration}.\n"
                                f"5h={quota_5h:.0f}% | 7d-opus={quota_7d_opus:.0f}%\n"
                                f"Run: claude-assistant set-global-model --clear  to force opus")
                perf.gauge("quota_state_degraded", 1 if qm.state == "degraded" else 0, component="daemon")
                log.debug("QUOTA_DEGRADE | state=%s", qm.state)
            except Exception as e:
                log.error("QUOTA_DEGRADE | Failed: %s", e)

            # Step 4: Perf gauges
            try:
                for key in ("five_hour", "seven_day"):
                    util = (usage.get(key) or {}).get("utilization")
                    if util is not None:
                        perf.gauge(f"quota_{key}_pct", util, component="daemon")
            except Exception:
                pass

    @staticmethod
    def _is_transient_error(error: Exception) -> bool:
        """Classify whether an error is transient (worth retrying) or permanent.

        Transient: session timeouts, temporary connection issues, resource exhaustion.
        Permanent: malformed messages, missing data, programming errors.
        """
        error_str = f"{type(error).__name__}: {error}".lower()
        transient_patterns = [
            "timeout", "control request timeout", "connection refused",
            "resource temporarily unavailable", "too many open files",
            "session not ready", "initialize",
        ]
        permanent_patterns = [
            "keyerror", "valueerror", "typeerror", "attributeerror",
            "json", "decode", "malformed", "missing required",
            "exceeds maximum size", "payload too large", "size limit",
        ]
        # Check permanent first — if it matches, don't retry
        for pattern in permanent_patterns:
            if pattern in error_str:
                return False
        # Check transient patterns
        for pattern in transient_patterns:
            if pattern in error_str:
                return True
        # Default: treat unknown errors as transient (safer to retry)
        return True

    # Maximum rejoin attempts before giving up on StaleGenerationError recovery.
    # After this many failures, the consumer stops and the watchdog handles restart.
    MAX_REJOIN_ATTEMPTS = 5

    async def _handle_stale_generation(
        self, consumer, executor, label: str, consecutive_errors: int
    ) -> int:
        """Handle StaleGenerationError by rejoining the consumer group.

        Returns the updated consecutive_errors count.
        Raises RuntimeError if max rejoin attempts exceeded.
        """
        from bus import StaleGenerationError  # noqa: F811
        log.warning(f"{label} fenced by rebalance. Rejoining group...")
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(executor, consumer._join_group)
            log.info(f"{label} rejoined group successfully")
            return 0  # Reset only on success
        except Exception as rejoin_error:
            consecutive_errors += 1
            log.error(f"{label} rejoin failed ({consecutive_errors}/{self.MAX_REJOIN_ATTEMPTS}): {rejoin_error}")
            if consecutive_errors >= self.MAX_REJOIN_ATTEMPTS:
                log.critical(f"{label} exceeded max rejoin attempts ({self.MAX_REJOIN_ATTEMPTS}), stopping consumer")
                # Emit bus event so the incident is recorded
                try:
                    produce_event(self._producer, "system", "consumer.rejoin_exhausted", {
                        "label": label,
                        "attempts": consecutive_errors,
                    }, source="daemon")
                except Exception:
                    pass  # Best-effort — producer may be broken too
                raise RuntimeError(f"{label} exceeded max rejoin attempts") from rejoin_error
            backoff = min(30, 2 ** consecutive_errors)
            await asyncio.sleep(backoff)
            return consecutive_errors

    async def _run_message_consumer(self):
        """Consume message.received events from bus and route to process_message.

        Near-zero latency via asyncio.Event notification from poll loops.
        Falls back to periodic 5s poll as safety net for missed signals.

        Hybrid retry/DLQ strategy:
        - Transient errors: re-produce to messages topic (up to 2 retries)
        - Permanent errors or exhausted retries: send to messages.dlq topic
        - Always commit offset immediately (never block on poison messages)

        Threading note: consumer.poll() runs on a dedicated single-thread executor
        because it uses time.sleep() internally. consumer.commit() runs on the event
        loop thread — it's a quick SQLite UPDATE (<0.1ms) and is always called after
        poll() returns, so no concurrent access to the consumer's connection.
        """
        from assistant.bus_helpers import reconstruct_msg_from_bus
        from bus import StaleGenerationError

        # Close any previous consumer BEFORE creating new one to prevent
        # partition splitting. If old consumer joins group simultaneously
        # with new one, rebalance splits partitions and the old (dead)
        # consumer holds partitions it will never process.
        if self._resource_registry:
            self._resource_registry.close_and_remove("message-router-consumer")

        consumer = self._bus.consumer(
            group_id="message-router",
            topics=["messages"],
            auto_commit=False,
            auto_offset_reset="latest",  # Skip history on first start (already processed)
            exclusive=True,  # Single consumer — purge zombies to prevent partition split
        )

        # Verify we got all partitions — if not, a zombie consumer is holding some
        # (This is a safety check; exclusive=True above should prevent this)
        n_assigned = len(consumer.assigned_partitions)
        try:
            n_expected = self._bus.topic_partition_count("messages")
        except ValueError:
            n_expected = n_assigned  # topic lookup failed — skip check, don't crash
            log.warning("Could not look up partition count for 'messages' topic")
        if n_assigned < n_expected:
            msg = (f"Message consumer only assigned {n_assigned}/{n_expected} partitions! "
                   f"Zombie consumer may be holding the rest. Check consumer_members table.")
            log.error(msg)
            self._alert_admin(f"[SVEN] ⚠️ {msg}")

        # Register consumer for clean shutdown
        if self._resource_registry:
            self._resource_registry.register(
                "message-router-consumer", consumer, consumer.close)

        loop = asyncio.get_event_loop()
        consecutive_errors = 0

        log.info("Message consumer started (group=message-router)")

        while not self._shutdown_flag:
            try:
                # Phase 1: Wait for notification or periodic fallback (5s safety net)
                try:
                    await asyncio.wait_for(self._consumer_notify.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass  # Safety net — catches missed signals, e.g. after restart

                # Phase 2: Read available records via dedicated thread
                # 50ms timeout gives writer thread time to flush if notify fired before write
                records = await loop.run_in_executor(
                    self._consumer_executor, consumer.poll, 50
                )

                if not records:
                    # Don't clear event — if it's still set, we retry immediately
                    # This handles the write-queue race (notify before flush)
                    continue

                # Got records — clear the notification
                self._consumer_notify.clear()

                processed = 0
                failed = 0
                for record in records:
                    if record.type != "message.received":
                        log.debug(f"Consumer: skipping {record.type} at offset {record.offset}")
                        continue

                    try:
                        msg = reconstruct_msg_from_bus(record.payload)
                        await self.process_message(msg)
                        processed += 1
                    except asyncio.CancelledError:
                        # Graceful shutdown: commit what we've processed, then exit
                        consumer.commit()
                        raise
                    except Exception as e:
                        failed += 1
                        error_str = str(e)
                        # Use event_id (or content hash) so retries track per-message,
                        # not per-offset (offsets change when re-produced for retry)
                        event_id = record.payload.get("_original_offset", record.offset)
                        retry_key = f"{record.topic}:{record.key}:{event_id}"
                        retry_count = self._dlq_retry_counts.get(retry_key, record.payload.get('_retry_count', 0))
                        retry_count = min(retry_count, 10)  # Hard cap to prevent infinite retries

                        # Classify error using structured classifier
                        is_transient = self._is_transient_error(e)
                        max_retries = 2 if is_transient else 0

                        if retry_count < max_retries:
                            # Transient error, retry later — re-produce to messages topic
                            self._dlq_retry_counts[retry_key] = retry_count + 1
                            log.warning(
                                f"Consumer: transient failure "
                                f"(offset={record.offset}, key={record.key}, "
                                f"retry={retry_count + 1}/{max_retries}): {e}"
                            )
                            produce_event(self._producer, "messages", "message.received", {
                                **record.payload,
                                "_retry_count": retry_count + 1,
                                "_original_offset": record.payload.get("_original_offset", record.offset),
                                "_last_error": error_str,
                            }, key=record.key, source="consumer-retry")
                        else:
                            # Exhausted retries or permanent error — send to DLQ
                            error_class = "transient_exhausted" if is_transient else "permanent"
                            log.error(
                                f"Consumer: sending to DLQ "
                                f"(offset={record.offset}, key={record.key}, "
                                f"class={error_class}, retries={retry_count}): {e}"
                            )
                            produce_event(self._producer, "messages.dlq", "dead_letter", {
                                "original_topic": record.topic,
                                "original_offset": record.offset,
                                "original_key": record.key,
                                "original_type": record.type,
                                "original_payload": record.payload,
                                "error": error_str,
                                "error_class": error_class,
                                "retry_count": retry_count,
                            }, key=record.key, source="consumer")
                            # Also emit processing_failed for telemetry
                            produce_event(self._producer, "messages", "message.processing_failed", {
                                "chat_id": record.key,
                                "error": error_str,
                                "error_class": error_class,
                                "original_offset": record.offset,
                                "sent_to_dlq": True,
                            }, key=record.key, source="consumer")
                            # Clean up retry tracker
                            self._dlq_retry_counts.pop(retry_key, None)

                # ALWAYS commit after batch — never block on poison messages
                consumer.commit()

                if processed or failed:
                    log.debug(f"Consumer batch: {processed} ok, {failed} failed")

                consecutive_errors = 0

            except asyncio.CancelledError:
                log.info("Message consumer shutting down")
                break
            except StaleGenerationError as e:
                try:
                    consecutive_errors = await self._handle_stale_generation(
                        consumer, self._consumer_executor,
                        "Message consumer", consecutive_errors,
                    )
                except RuntimeError:
                    break  # Max rejoin attempts exceeded, let watchdog handle it
            except Exception as e:
                # Catch StaleGenerationError that may not match the imported class
                if "StaleGenerationError" in type(e).__name__ or "stale" in str(e).lower():
                    log.warning(f"Message consumer stale generation (caught as Exception): {e}")
                    try:
                        consecutive_errors = await self._handle_stale_generation(
                            consumer, self._consumer_executor,
                            "Message consumer", consecutive_errors,
                        )
                    except RuntimeError:
                        break
                    continue
                consecutive_errors += 1
                log.error(f"Consumer error ({consecutive_errors}): {e}")
                backoff = min(30, 2 ** consecutive_errors)
                await asyncio.sleep(backoff)

        log.info("Message consumer stopped")

    def _on_consumer_done(self, task: asyncio.Task):
        """Detect message consumer task death and auto-restart (max 5 restarts)."""
        if self._shutdown_flag:
            return
        try:
            exc = task.exception()
            log.error(f"Message consumer task died: {exc}. Restarting in 5s...")
        except asyncio.CancelledError:
            return

        self._message_consumer_restarts += 1
        MAX_RESTARTS = 5
        if self._message_consumer_restarts > MAX_RESTARTS:
            msg = (f"Message consumer exceeded {MAX_RESTARTS} restarts, giving up. "
                   "Manual daemon restart required.")
            log.error(msg)
            self._alert_admin(f"[SVEN] 🚨 {msg}")
            return

        async def _restart():
            await asyncio.sleep(5)
            if not self._shutdown_flag:
                self._consumer_task = asyncio.create_task(
                    self._run_message_consumer(), name="message-consumer"
                )
                self._consumer_task.add_done_callback(self._on_consumer_done)
                log.info(f"Message consumer task restarted "
                         f"({self._message_consumer_restarts}/{MAX_RESTARTS})")

        _fire_and_forget(_restart(), name="consumer-restart")

    # ──────────────────────────────────────────────────────────────
    # Task consumer + ephemeral task infrastructure
    # ──────────────────────────────────────────────────────────────

    async def _run_task_consumer(self):
        """Consume task.requested events from bus and spin up ephemeral agents.

        Similar architecture to _run_message_consumer but for the "tasks" topic.
        Handles task lifecycle: creation, timeout supervision, completion routing.

        TODO: Extract shared async consumer base with _run_message_consumer to
        eliminate ~40 lines of duplicated poll loop + backoff + shutdown boilerplate.
        """
        from assistant.bus_helpers import (
            task_started_payload, task_completed_payload,
            task_failed_payload, task_timeout_payload,
        )
        from bus import StaleGenerationError

        # Close any previous consumer BEFORE creating new one (same fix as message consumer)
        if self._resource_registry:
            self._resource_registry.close_and_remove("task-runner-consumer")

        consumer = self._bus.consumer(
            group_id="task-runner",
            topics=["tasks"],
            auto_commit=False,
            auto_offset_reset="latest",
            exclusive=True,  # Single consumer — purge zombies to prevent partition split
        )

        # Register for clean shutdown
        if self._resource_registry:
            self._resource_registry.register(
                "task-runner-consumer", consumer, consumer.close)

        loop = asyncio.get_event_loop()
        consecutive_errors = 0

        log.info("Task consumer started (group=task-runner)")

        while not self._shutdown_flag:
            try:
                # Wait for notification or periodic fallback
                try:
                    await asyncio.wait_for(self._task_consumer_notify.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass

                records = await loop.run_in_executor(
                    self._task_consumer_executor, consumer.poll, 50
                )

                if not records:
                    continue

                self._task_consumer_notify.clear()

                for record in records:
                    if record.type != "task.requested":
                        log.debug(f"Task consumer: skipping {record.type} at offset {record.offset}")
                        continue

                    try:
                        await self._handle_task_requested(record.payload, record.headers or {})
                    except asyncio.CancelledError:
                        consumer.commit()
                        raise
                    except Exception as e:
                        log.error(f"Task consumer: failed to handle task.requested "
                                  f"(offset={record.offset}): {e}")
                        produce_event(self._producer, "tasks", "task.failed", {
                            "task_id": record.payload.get("task_id", "unknown"),
                            "error": str(e),
                            "original_offset": record.offset,
                        }, key=record.key, source="task-runner")

                consumer.commit()
                consecutive_errors = 0

            except asyncio.CancelledError:
                log.info("Task consumer shutting down")
                break
            except StaleGenerationError as e:
                try:
                    consecutive_errors = await self._handle_stale_generation(
                        consumer, self._task_consumer_executor,
                        "Task consumer", consecutive_errors,
                    )
                except RuntimeError:
                    break  # Max rejoin attempts exceeded, let watchdog handle it
            except Exception as e:
                # Catch StaleGenerationError that may not match the imported class
                # (e.g., due to module reload or class identity mismatch)
                if "StaleGenerationError" in type(e).__name__ or "stale" in str(e).lower():
                    log.warning(f"Task consumer stale generation (caught as Exception): {e}")
                    try:
                        consecutive_errors = await self._handle_stale_generation(
                            consumer, self._task_consumer_executor,
                            "Task consumer", consecutive_errors,
                        )
                    except RuntimeError:
                        break
                    continue
                consecutive_errors += 1
                log.error(f"Task consumer error ({consecutive_errors}): {e}")
                backoff = min(30, 2 ** consecutive_errors)
                await asyncio.sleep(backoff)

        log.info("Task consumer stopped")

    async def _handle_task_requested(self, payload: dict, headers: dict):
        """Handle a task.requested event: spin up an ephemeral agent session.

        Validates the payload, creates the session, starts timeout supervision,
        and optionally notifies the requester.
        """
        from assistant.bus_helpers import (
            task_started_payload, task_failed_payload, task_skipped_payload,
        )

        task_id = payload.get("task_id")
        title = payload.get("title", "Untitled task")
        requested_by = payload.get("requested_by")
        instructions = payload.get("instructions", "")
        notify = payload.get("notify", True)
        timeout_minutes = payload.get("timeout_minutes", 30)

        # Support nested execution.prompt format
        execution = payload.get("execution", {})
        mode = execution.get("mode", "agent")
        if not instructions and execution.get("prompt"):
            instructions = execution["prompt"]

        if not task_id:
            log.error("task.requested missing task_id, skipping")
            return

        if not requested_by:
            log.error(f"task.requested {task_id} missing requested_by, skipping")
            return

        if not instructions:
            log.error(f"task.requested {task_id} missing instructions, skipping")
            return

        is_investigation = bool(payload.get("investigation"))
        if is_investigation:
            looks_ephemeral = (
                requested_by.startswith("ephemeral-")
                or requested_by in self._ephemeral_tasks
                or f"ephemeral-{requested_by}" in self.sessions.sessions
            )
            if looks_ephemeral:
                log.warning(
                    f"investigation.rejected_recursive | task_id={task_id} | "
                    f"requested_by={requested_by} (ephemeral session may not "
                    f"dispatch investigations)"
                )
                produce_event(self._producer, "system",
                    "investigation.rejected_recursive", {
                        "task_id": task_id,
                        "requested_by": requested_by,
                        "reason": "requested_by is ephemeral",
                    }, key=requested_by, source="task-runner")
                produce_event(self._producer, "tasks", "task.failed",
                    task_failed_payload(task_id, title, requested_by,
                        "Recursive investigation dispatch rejected"),
                    key=requested_by, source="task-runner")
                return

        # Global kill switch — check tasks_enabled in config.local.yaml.
        # Hot-reloaded on every task so changes take effect without a restart.
        from . import config as _cfg
        _cfg.reload()
        if not _cfg.get("tasks_enabled", True):
            log.info(f"TASKS_DISABLED | tasks_enabled=false in config — skipping task {task_id}")
            return

        # Dedup: skip if already running (covers both agent and script tasks)
        session_key = f"ephemeral-{task_id}"
        agent_running = (session_key in self.sessions.sessions
                         and self.sessions.sessions[session_key].is_alive())
        script_running = (task_id in self._running_script_tasks
                          and not self._running_script_tasks[task_id].done())
        # Also reject events that we've already fully processed — same event
        # (same trace_id) re-appearing means the bus consumer failed to commit
        # its offset and is replaying. Keyed on trace_id (not task_id) so a
        # legitimate scheduler re-fire of the same task (e.g. chrome-housekeep
        # on */30) with a FRESH trace_id doesn't trip the guard.
        # Short cooldown: 60s is enough to swallow a burst of consumer retries
        # without ever overlapping real scheduled runs.
        COMPLETED_COOLDOWN = 60
        trace_id = headers.get("trace_id") if headers else None
        recently_completed = (
            trace_id is not None
            and trace_id in self._completed_task_traces
            and (time.time() - self._completed_task_traces[trace_id]) < COMPLETED_COOLDOWN
        )
        if agent_running or script_running or recently_completed:
            reason = ("recently_completed" if recently_completed
                      else "already_running")
            log.warning(f"Task {task_id} {reason}, skipping duplicate "
                        f"(trace_id={trace_id})")
            produce_event(self._producer, "tasks", "task.skipped",
                task_skipped_payload(task_id, reason),
                key=requested_by, source="task-runner")
            # Alert admin when the SAME event is being replayed — this means
            # the consumer failed to commit offsets (sev0 restart loop signal).
            # Rate-limited: one alert per trace_id per hour.
            if recently_completed:
                alert_key = f"_replay_alerted_{trace_id}"
                last_alert = getattr(self, alert_key, 0)
                if time.time() - last_alert > 3600:
                    setattr(self, alert_key, time.time())
                    from assistant import config
                    admin_phone = config.get("owner.phone")
                    if admin_phone:
                        self._send_sms(
                            admin_phone,
                            f"🚨 Sev0 signal: task '{task_id}' replayed after "
                            f"completion (cooldown blocked restart). Consumer "
                            f"offset commit may be failing.",
                        )
            return

        log.info(f"TASK_START | task_id={task_id} | title={title} | "
                 f"requested_by={requested_by} | mode={mode} | timeout={timeout_minutes}m")

        if mode == "script":
            # Script tasks: run as subprocess, no Claude session needed
            # Track in _running_script_tasks for dedup (cleared in finally)
            task = asyncio.create_task(
                self._run_script_task(payload, headers),
                name=f"script-task-{task_id}",
            )
            self._running_script_tasks[task_id] = task
            task.add_done_callback(
                lambda _t, _tid=task_id: self._running_script_tasks.pop(_tid, None)
            )
            return

        # Agent tasks: create ephemeral Claude session
        ephemeral_kwargs: dict[str, Any] = {}
        if is_investigation:
            from assistant.investigations import (
                INVESTIGATOR_SYSTEM_PROMPT,
                bash_deny_patterns,
                mark_status,
            )
            allow_mutations = bool(payload.get("allow_mutations", False))
            ephemeral_kwargs["system_prompt_override"] = (
                INVESTIGATOR_SYSTEM_PROMPT + "\n\n" + instructions
            )
            ephemeral_kwargs["disallowed_tools"] = (
                () if allow_mutations else ("Edit", "Write", "NotebookEdit")
            )
            ephemeral_kwargs["bash_deny_regex"] = bash_deny_patterns(allow_mutations)
            mark_status(task_id, "running")

        try:
            session = await self.sessions.create_ephemeral_session(
                task_id=task_id,
                title=title,
                instructions=instructions,
                requested_by=requested_by,
                timeout_minutes=timeout_minutes,
                notify=notify,
                **ephemeral_kwargs,
            )
        except Exception as e:
            log.error(f"Failed to create ephemeral session for task {task_id}: {e}")
            produce_event(self._producer, "tasks", "task.failed",
                task_failed_payload(task_id, title, requested_by, str(e)),
                key=requested_by, source="task-runner")
            if is_investigation:
                from assistant.investigations import (
                    format_result_for_injection, parse_result_block, mark_status,
                )
                notify_session = payload.get("notify_session") or requested_by
                target = self.sessions.sessions.get(notify_session)
                if target and target.is_alive():
                    parsed = parse_result_block("")
                    parsed["raw"] = f"Investigator failed to start: {e}"
                    wrapped = format_result_for_injection(task_id, "failed", parsed)
                    try:
                        await target.inject(wrapped)
                    except Exception as inj_e:
                        log.warning(
                            f"investigation: failed to inject failure to "
                            f"{notify_session}: {inj_e}"
                        )
                mark_status(task_id, "failed", error=str(e))
            return

        # Track for timeout supervision
        self._ephemeral_tasks[task_id] = {
            "session_key": session_key,
            "started_at": time.time(),
            "timeout_minutes": timeout_minutes,
            "requested_by": requested_by,
            "title": title,
            "notify": notify,
            "investigation": is_investigation,
            "notify_session": payload.get("notify_session") if is_investigation else None,
            # Keep the originating event's trace_id so the completion path can
            # record it in the replay-guard cache (see _completed_task_traces).
            "trace_id": trace_id,
        }

        # Produce task.started event
        produce_event(self._producer, "tasks", "task.started",
            task_started_payload(
                task_id=task_id, title=title, requested_by=requested_by,
                session_name=session_key, timeout_minutes=timeout_minutes,
                execution_mode=mode,
            ),
            key=requested_by, source="task-runner",
            headers=headers,
        )

        # Notify requester if requested
        if notify:
            _fire_and_forget(
                self._notify_task_event(requested_by, f"⚙️ Task started: {title}"),
                name=f"task-notify-start-{task_id}",
            )

    async def _run_script_task(self, payload: dict, headers: dict):
        """Run a script task as a subprocess (no Claude session).

        For simple, well-defined tasks that don't need LLM reasoning.
        """
        from assistant.bus_helpers import task_completed_payload, task_failed_payload

        task_id = payload["task_id"]
        title = payload.get("title", "Untitled script task")
        requested_by = payload["requested_by"]
        notify = payload.get("notify", True)
        execution = payload.get("execution", {})
        command = execution.get("command", [])

        if not command:
            log.error(f"Script task {task_id} missing execution.command")
            produce_event(self._producer, "tasks", "task.failed",
                task_failed_payload(task_id, title, requested_by, "missing execution.command"),
                key=requested_by, source="task-runner")
            return

        start_time = time.time()
        log.info(f"SCRIPT_TASK_START | task_id={task_id} | command={command}")

        proc = None
        try:
            import os as _os
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,  # New process group for clean kill
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=payload.get("timeout_minutes", 30) * 60,
            )

            duration = time.time() - start_time
            from assistant.bus_helpers import redact_pii

            if proc.returncode == 0:
                produce_event(self._producer, "tasks", "task.completed",
                    task_completed_payload(task_id, title, requested_by, duration,
                        stdout=redact_pii(stdout.decode()) if stdout else "",
                        stderr=redact_pii(stderr.decode()) if stderr else "",
                    ),
                    key=requested_by, source="task-runner", headers=headers)
                log.info(f"SCRIPT_TASK_DONE | task_id={task_id} | duration={duration:.1f}s")

                if notify:
                    _fire_and_forget(
                        self._notify_task_event(requested_by, f"✅ Script task done: {title}"),
                        name=f"task-notify-done-{task_id}",
                    )
            else:
                error_msg = (stderr.decode() if stderr else f"exit code {proc.returncode}")
                produce_event(self._producer, "tasks", "task.failed",
                    task_failed_payload(task_id, title, requested_by, redact_pii(error_msg)),
                    key=requested_by, source="task-runner", headers=headers)
                log.error(f"SCRIPT_TASK_FAILED | task_id={task_id} | error={error_msg}")

                if notify:
                    _fire_and_forget(
                        self._notify_task_event(requested_by, f"❌ Script task failed: {title}"),
                        name=f"task-notify-fail-{task_id}",
                    )

        except asyncio.TimeoutError:
            from assistant.bus_helpers import task_timeout_payload
            duration = time.time() - start_time
            produce_event(self._producer, "tasks", "task.timeout",
                task_timeout_payload(task_id, title, requested_by,
                    payload.get("timeout_minutes", 30)),
                key=requested_by, source="task-runner", headers=headers)
            log.error(f"SCRIPT_TASK_TIMEOUT | task_id={task_id} | duration={duration:.1f}s")
            # Kill entire process group (catches child processes too)
            if proc is not None:
                try:
                    _os.killpg(_os.getpgid(proc.pid), 9)  # SIGKILL the group
                except (ProcessLookupError, OSError):
                    proc.kill()  # Fallback to killing just the process
                # Reap zombie process (with timeout to avoid hanging on D-state)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=10)
                except asyncio.TimeoutError:
                    log.warning(f"SCRIPT_TASK_REAP_TIMEOUT | task_id={task_id} | "
                                "process did not exit after SIGKILL + 10s")

            if notify:
                _fire_and_forget(
                    self._notify_task_event(
                        requested_by,
                        f"⏰ Script task timed out: {title}"
                    ),
                    name=f"task-notify-timeout-{task_id}",
                )

        except Exception as e:
            produce_event(self._producer, "tasks", "task.failed",
                task_failed_payload(task_id, title, requested_by, str(e)),
                key=requested_by, source="task-runner", headers=headers)
            log.error(f"SCRIPT_TASK_ERROR | task_id={task_id} | error={e}")
            # Clean up the process if it's still running
            try:
                if proc is not None and proc.returncode is None:
                    try:
                        _os.killpg(_os.getpgid(proc.pid), 9)
                    except (ProcessLookupError, OSError):
                        proc.kill()
                    await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                pass  # Best-effort cleanup
        finally:
            # Record completion trace_id to prevent restart loops when the bus
            # consumer re-reads the same task.requested event (sev0 fix).
            trace_id = headers.get("trace_id") if headers else None
            if trace_id:
                self._completed_task_traces[trace_id] = time.time()

    async def _imessage_tickle(self):
        """Wake Messages.app/IMDPersistenceAgent so pending iMessages flush to chat.db.

        macOS aggressively throttles backgrounded apps (App Nap + IMDPersistenceAgent's
        own scheduling), which can defer chat.db writes for minutes. The push arrives,
        but the disk row is delayed until something pokes the app. A cheap Apple Event
        is enough — we don't care about the result, only the side effect of forcing
        Messages.app to do a unit of work, which lets the agent flush its queue.
        """
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", 'tell application "Messages" to count of chats',
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            log.warning("IMESSAGE_TICKLE | osascript timed out after 5s")
            if proc is not None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        except Exception as e:
            log.warning(f"IMESSAGE_TICKLE | failed: {e}")

    async def _imessage_tickle_escalated(self):
        """Heavy tickle used when the light osascript ping isn't waking the stack.

        Observed 2026-07-31: inbound iMessage arrived via push, but the chat.db
        write was deferred ~8 minutes because IMDPersistenceAgent had App-Napped
        after Messages.app sat idle for 17h. The 30s `count of chats` tickle
        wasn't enough to wake it. This escalation kickstarts imagent (which owns
        the persistence path) and brings Messages.app to the foreground briefly
        so IMDPersistenceAgent flushes its queue.

        Gated by `_last_escalated_tickle_ts` in the caller — do NOT loop-kick.
        """
        uid = os.getuid()
        # 1) Kickstart imagent — the daemon that ferries messages to chat.db.
        try:
            proc = await asyncio.create_subprocess_exec(
                "launchctl", "kickstart", "-k", f"gui/{uid}/com.apple.imagent",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=10)
            log.warning("IMESSAGE_TICKLE_ESCALATED | launchctl kickstart imagent OK")
        except asyncio.TimeoutError:
            log.warning("IMESSAGE_TICKLE_ESCALATED | launchctl kickstart imagent timed out")
        except Exception as e:
            log.warning(f"IMESSAGE_TICKLE_ESCALATED | launchctl kickstart failed: {e}")

        # 2) Briefly foreground Messages.app so it services its RunLoop.
        #    `open -a` won't steal focus persistently — user can immediately
        #    switch back. This is the strongest signal to macOS that the app
        #    should exit App Nap.
        try:
            proc = await asyncio.create_subprocess_exec(
                "open", "-a", "Messages",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=5)
            log.warning("IMESSAGE_TICKLE_ESCALATED | open -a Messages OK")
        except asyncio.TimeoutError:
            log.warning("IMESSAGE_TICKLE_ESCALATED | open -a Messages timed out")
        except Exception as e:
            log.warning(f"IMESSAGE_TICKLE_ESCALATED | open failed: {e}")

    async def _periodic_wal_checkpoint(self):
        """Run WAL checkpoint on a separate timer, outside the poll cycle.

        Decoupled from get_new_messages() to avoid blocking the 100ms poll
        when Messages.app holds write locks (observed up to 5.4s stalls).
        Runs every 5 seconds in a dedicated executor (NOT the poll executor,
        since checkpoint can block and would starve the poll thread).
        """
        loop = asyncio.get_event_loop()
        wal_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wal-checkpoint")
        while not self._shutdown_flag:
            try:
                await asyncio.sleep(5)
                await loop.run_in_executor(
                    wal_executor, self.messages.run_wal_checkpoint
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.debug(f"WAL checkpoint error: {e}")
        wal_executor.shutdown(wait=False)

    async def _supervise_ephemeral_tasks(self):
        """Periodic supervision of ephemeral tasks: check for timeouts and completion.

        Runs every 30 seconds. Checks:
        1. Timed-out tasks → force-kill and produce task.timeout
        2. Completed tasks (session died) → produce task.completed and clean up
        """
        from assistant.bus_helpers import task_completed_payload, task_timeout_payload

        HARD_MAX_LIFETIME = 14400  # 4 hours absolute max

        while not self._shutdown_flag:
            try:
                await asyncio.sleep(30)

                # Prune expired entries from completed-trace replay guard.
                # Threshold matches the dedup window in _handle_task_requested
                # (COMPLETED_COOLDOWN = 60s) — kept slightly longer here so we
                # don't drop an entry a request is about to consult.
                now = time.time()
                expired = [tr for tr, ts in self._completed_task_traces.items()
                           if now - ts > 300]
                for tr in expired:
                    del self._completed_task_traces[tr]

                if not self._ephemeral_tasks:
                    continue

                # Snapshot to avoid mutation during iteration
                tasks_snapshot = dict(self._ephemeral_tasks)

                for task_id, info in tasks_snapshot.items():
                    elapsed = time.time() - info["started_at"]
                    timeout_secs = info["timeout_minutes"] * 60
                    session_key = info["session_key"]
                    session = self.sessions.sessions.get(session_key)

                    # Check completion FIRST — if session already died, it completed
                    # (or crashed). Don't let elapsed > timeout fire on a dead session
                    # that finished legitimately before the timeout.
                    if session is None or not session.is_alive():
                        log.info(f"TASK_COMPLETED | task_id={task_id} | "
                                 f"elapsed={elapsed:.0f}s | session_gone")

                        produce_event(self._producer, "tasks", "task.completed",
                            task_completed_payload(task_id, info["title"],
                                info["requested_by"], elapsed),
                            key=info["requested_by"], source="task-runner")

                        await self._handle_investigation_completed(
                            task_id, info, session, "completed"
                        )

                        # Clean up — record trace_id to prevent restart loops
                        # (see _completed_task_traces / COMPLETED_COOLDOWN).
                        _tr = info.get("trace_id")
                        if _tr:
                            self._completed_task_traces[_tr] = time.time()
                        await self.sessions.kill_ephemeral_session(task_id)
                        self._ephemeral_tasks.pop(task_id, None)

                        if info["notify"]:
                            _fire_and_forget(
                                self._notify_task_event(
                                    info["requested_by"],
                                    f"✅ Task completed: {info['title']}"
                                ),
                                name=f"task-notify-done-{task_id}",
                            )
                        continue

                    # Check if ephemeral task is idle after completing its work.
                    # Only clean up if the session is truly idle — not busy with
                    # pending queries (e.g. subagent tool calls that don't update
                    # last_activity). Use a generous threshold since agent tasks
                    # often have gaps between tool calls.
                    EPHEMERAL_IDLE_SECS = 120
                    if session is not None and hasattr(session, 'last_activity'):
                        idle_secs = (datetime.now() - session.last_activity).total_seconds()
                        is_busy = getattr(session, 'is_busy', False)
                        has_pending = getattr(session, '_pending_queries', 0) > 0
                        if (idle_secs >= EPHEMERAL_IDLE_SECS
                                and elapsed > EPHEMERAL_IDLE_SECS
                                and not is_busy
                                and not has_pending):
                            log.info(f"TASK_IDLE_CLEANUP | task_id={task_id} | "
                                     f"idle={idle_secs:.0f}s | elapsed={elapsed:.0f}s")

                            produce_event(self._producer, "tasks", "task.completed",
                                task_completed_payload(task_id, info["title"],
                                    info["requested_by"], elapsed),
                                key=info["requested_by"], source="task-runner")

                            await self._handle_investigation_completed(
                                task_id, info, session, "completed"
                            )

                            _tr = info.get("trace_id")
                            if _tr:
                                self._completed_task_traces[_tr] = time.time()
                            await self.sessions.kill_ephemeral_session(task_id)
                            self._ephemeral_tasks.pop(task_id, None)

                            if info["notify"]:
                                _fire_and_forget(
                                    self._notify_task_event(
                                        info["requested_by"],
                                        f"✅ Task completed: {info['title']}"
                                    ),
                                    name=f"task-notify-idle-{task_id}",
                                )
                            continue

                    # Check timeout (configured or hard max) — only for ALIVE sessions
                    if elapsed > timeout_secs or elapsed > HARD_MAX_LIFETIME:
                        log.warning(f"TASK_TIMEOUT | task_id={task_id} | "
                                    f"elapsed={elapsed:.0f}s | timeout={timeout_secs}s")

                        produce_event(self._producer, "tasks", "task.timeout",
                            task_timeout_payload(task_id, info["title"],
                                info["requested_by"], info["timeout_minutes"]),
                            key=info["requested_by"], source="task-runner")

                        await self._handle_investigation_completed(
                            task_id, info, session, "timeout"
                        )

                        # Kill session and clean up
                        _tr = info.get("trace_id")
                        if _tr:
                            self._completed_task_traces[_tr] = time.time()
                        await self.sessions.kill_ephemeral_session(task_id)
                        self._ephemeral_tasks.pop(task_id, None)

                        if info["notify"]:
                            _fire_and_forget(
                                self._notify_task_event(
                                    info["requested_by"],
                                    f"⏰ Task timed out after {info['timeout_minutes']}min: {info['title']}"
                                ),
                                name=f"task-notify-timeout-{task_id}",
                            )
                        continue

            except asyncio.CancelledError:
                log.info("Task supervisor shutting down")
                break
            except Exception as e:
                log.error(f"Task supervisor error: {e}")
                await asyncio.sleep(5)

        log.info("Task supervisor stopped")

    async def _handle_investigation_completed(
        self, task_id: str, info: dict, session: Any, status: str,
    ) -> None:
        """Deliver an investigation result to the originating session.

        Pulls last_assistant_text from the ephemeral session, parses the
        === INVESTIGATION RESULT === block, and injects the wrapped result
        into notify_session. Must run BEFORE kill_ephemeral_session because
        the ephemeral cwd (and the SDKSession in-memory state) is destroyed
        on kill.
        """
        if not info.get("investigation"):
            return

        notify_session = info.get("notify_session")
        if not notify_session:
            return

        from assistant.investigations import (
            parse_result_block, format_result_for_injection, mark_status,
        )

        text = ""
        if session is not None:
            text = getattr(session, "last_assistant_text", "") or ""

        if not text and status == "timeout":
            text = "Investigation timed out before completing."

        parsed = parse_result_block(text)
        wrapped = format_result_for_injection(task_id, status, parsed)

        target = self.sessions.sessions.get(notify_session)
        if not target or not target.is_alive():
            log.warning(
                f"investigation: notify_session {notify_session} is dead, "
                f"dropping result for task_id={task_id}"
            )
            mark_status(task_id, status,
                        delivered=False, parse_status=parsed["parse_status"])
            return

        try:
            await target.inject(wrapped)
            log.info(
                f"investigation.delivered | task_id={task_id} | "
                f"status={status} | parse_status={parsed['parse_status']} | "
                f"notify_session={notify_session}"
            )
            mark_status(task_id, status,
                        delivered=True, parse_status=parsed["parse_status"])
        except Exception as e:
            log.warning(
                f"investigation: failed to inject result for task_id={task_id} "
                f"into {notify_session}: {e}"
            )
            mark_status(task_id, status,
                        delivered=False, parse_status=parsed["parse_status"],
                        delivery_error=str(e))

    async def _notify_task_event(self, chat_id: str, message: str):
        """Send a task notification to the requester's session.

        Injects the message into the requester's existing chat session
        so the agent has full context and can decide whether/how to
        forward it to the user. Falls back to direct SMS if session is dead.
        """
        session = self.sessions.sessions.get(chat_id)
        if session and session.is_alive():
            try:
                await session.inject(f"[TASK NOTIFICATION] {message}")
            except Exception as e:
                log.warning(f"Failed to inject task notification to {chat_id}: {e}")
        else:
            # Fallback: send via the correct backend
            try:
                reg = self.sessions.registry.get(chat_id)
                source = reg.get("source", "imessage") if reg else "imessage"
                if source == "signal":
                    send_cmd = str(SKILLS_DIR / "signal" / "scripts" / "send-signal")
                else:
                    send_cmd = str(SKILLS_DIR / "sms-assistant" / "scripts" / "send-sms")
                proc = await asyncio.create_subprocess_exec(
                    send_cmd, chat_id, message,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.communicate()
            except Exception as e:
                log.warning(f"Failed to send task notification to {chat_id}: {e}")

    def _on_task_consumer_done(self, task: asyncio.Task):
        """Detect task consumer death and auto-restart (max 5 restarts)."""
        if self._shutdown_flag:
            return
        try:
            exc = task.exception()
            log.error(f"Task consumer task died: {exc}. Restarting in 5s...")
        except asyncio.CancelledError:
            return

        self._task_consumer_restarts += 1
        MAX_RESTARTS = 5
        if self._task_consumer_restarts > MAX_RESTARTS:
            msg = (f"Task consumer exceeded {MAX_RESTARTS} restarts, giving up. "
                   "Manual daemon restart required.")
            log.error(msg)
            self._alert_admin(f"[SVEN] 🚨 {msg}")
            return

        async def _restart():
            await asyncio.sleep(5)
            if not self._shutdown_flag:
                self._task_consumer_task = asyncio.create_task(
                    self._run_task_consumer(), name="task-consumer"
                )
                self._task_consumer_task.add_done_callback(self._on_task_consumer_done)
                log.info(f"Task consumer task restarted "
                         f"({self._task_consumer_restarts}/{MAX_RESTARTS})")

        _fire_and_forget(_restart(), name="task-consumer-restart")

    async def _cleanup_orphaned_ephemeral_sessions(self):
        """Clean up ephemeral sessions and cwds left from a previous daemon run.

        Called on startup to prevent resource leaks.
        """
        import shutil

        ephemeral_base = HOME / "dispatch" / "state" / "ephemeral"
        if not ephemeral_base.exists():
            return

        # Clean up leftover ephemeral cwds
        cleaned = 0
        for task_dir in ephemeral_base.iterdir():
            if task_dir.is_dir():
                try:
                    shutil.rmtree(task_dir)
                    cleaned += 1
                except Exception as e:
                    log.warning(f"Failed to clean orphaned ephemeral dir {task_dir}: {e}")

        if cleaned:
            log.info(f"Cleaned up {cleaned} orphaned ephemeral task directories")

        # Kill any orphaned ephemeral sessions
        orphaned = [
            key for key in self.sessions.sessions
            if key.startswith("ephemeral-")
        ]
        for key in orphaned:
            session = self.sessions.sessions.pop(key, None)
            if session:
                try:
                    await session.stop()
                except Exception as e:
                    log.warning(f"Failed to stop orphaned ephemeral session {key}: {e}")

        if orphaned:
            log.info(f"Cleaned up {len(orphaned)} orphaned ephemeral sessions")

    def _load_state(self) -> int:
        """Load the last processed message ROWID."""
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        if STATE_FILE.exists():
            return int(STATE_FILE.read_text().strip())
        # Start from current position (don't process old messages)
        return self.messages.get_latest_rowid()

    def _save_state(self, rowid: int):
        """Update in-memory rowid. File is persisted by _flush_state()."""
        self.last_rowid = rowid
        self._state_dirty = True

    def _flush_state(self):
        """Persist last_rowid to disk if changed. Called once per poll cycle."""
        if getattr(self, '_state_dirty', False):
            STATE_FILE.write_text(str(self.last_rowid))
            self._state_dirty = False

    def _close_log_fh(self, attr_name: str):
        """Safely close a log file handle stored as an instance attribute."""
        fh = getattr(self, attr_name, None)
        if fh:
            try:
                fh.close()
            except Exception:
                pass
            setattr(self, attr_name, None)

    def _alert_admin(self, message: str):
        """Send an alert SMS to admin. Sync — called via asyncio.to_thread by supervisors."""
        try:
            from assistant import config
            admin_phone = config.get("owner.phone")
            if admin_phone:
                self._send_sms(admin_phone, message)
        except Exception as e:
            log.error(f"Failed to alert admin: {e}")

    def _create_dispatch_api_process(self) -> Optional[subprocess.Popen]:
        """Create the Dispatch API server process (Popen only, no lifecycle management).

        Called by ChildSupervisor.start() and ChildSupervisor._handle_crash().
        Returns the Popen object or None if spawn failed.
        """
        if not DISPATCH_API_SCRIPT.exists():
            log.warning(f"Dispatch API script not found at {DISPATCH_API_SCRIPT}")
            return None

        dispatch_api_log_path = LOGS_DIR / "dispatch-api.log"
        self._close_log_fh('_dispatch_api_log_fh')
        self._dispatch_api_log_fh = open(dispatch_api_log_path, "a")

        # Kill any orphaned process on port 9091 to prevent bind failures
        killed_any = False
        try:
            import subprocess as _sp
            result = _sp.run(["lsof", "-ti", "tcp:9091", "-sTCP:LISTEN"], capture_output=True, text=True)
            for pid_str in result.stdout.strip().split('\n'):
                if pid_str.strip():
                    try:
                        import os as _os
                        _os.kill(int(pid_str.strip()), 9)
                        log.info(f"Killed orphaned process on port 9091: PID {pid_str.strip()}")
                        killed_any = True
                    except (ProcessLookupError, ValueError):
                        pass
        except Exception:
            pass  # Best-effort cleanup
        if killed_any:
            time.sleep(1)  # Sync sleep OK — called from supervisor which handles async context

        # Ensure dispatch-app web build exists and kick off background rebuild on every restart.
        # If dist is missing: blocking build first (prevent broken placeholder).
        # Always: fire a background rebuild so the served app stays fresh after restarts.
        _web_index = DISPATCH_APP_DIR / "dist" / "index.html"
        if not _web_index.is_file():
            log.info("Dispatch app web build missing — running blocking expo export")
            try:
                subprocess.run(
                    ["npx", "expo", "export", "--platform", "web"],
                    cwd=str(DISPATCH_APP_DIR),
                    capture_output=True,
                    timeout=120,
                )
                if _web_index.is_file():
                    log.info("Dispatch app web build completed successfully")
                else:
                    log.warning("Dispatch app web build ran but index.html still missing")
            except Exception as e:
                log.warning(f"Dispatch app web build failed: {e}")

        # Background rebuild — always run on restart so the build stays current.
        # Non-blocking: daemon starts immediately, fresh dist lands ~60s later.
        _expo_log = LOGS_DIR / "expo-export.log"
        try:
            with open(_expo_log, "a") as _expo_fh:
                subprocess.Popen(
                    ["npx", "expo", "export", "--platform", "web"],
                    cwd=str(DISPATCH_APP_DIR),
                    stdout=_expo_fh,
                    stderr=_expo_fh,
                    start_new_session=True,
                )
            log.info("Dispatch app background web build triggered")
        except Exception as e:
            log.warning(f"Dispatch app background web build failed to start: {e}")

        try:
            proc = subprocess.Popen(
                [str(UV), "run", str(DISPATCH_API_SCRIPT)],
                stdout=self._dispatch_api_log_fh,
                stderr=self._dispatch_api_log_fh,
                cwd=str(DISPATCH_API_DIR),
                start_new_session=True,
            )
        except Exception:
            self._close_log_fh('_dispatch_api_log_fh')
            raise

        log.info(f"Spawned Dispatch API process (PID: {proc.pid})")
        return proc

    def _check_dispatch_api_health(self) -> bool:
        """Check if Dispatch API is responding."""
        try:
            import urllib.request
            url = f"http://localhost:{DISPATCH_API_PORT}/health"
            with urllib.request.urlopen(url, timeout=2) as resp:
                return resp.status == 200
        except Exception:
            return False

    def _check_signal_health(self):
        """Check Signal daemon health and restart if needed."""
        if self.signal_daemon is None:
            return
        if self.signal_daemon.poll() is not None:
            log.warning("Signal daemon died, restarting...")
            lifecycle_log.info("SIGNAL_DAEMON | DIED | restarting")
            produce_event(self._producer, "system", "health.service_restarted",
                service_restarted_payload("signal_daemon", "died"), source="health")
            self.signal_daemon = self._spawn_signal_daemon()
            if self.signal_daemon:
                self._start_signal_listener()
        elif not SIGNAL_SOCKET.exists():
            log.warning("Signal socket missing, restarting daemon...")
            lifecycle_log.info("SIGNAL_DAEMON | SOCKET_MISSING | restarting")
            produce_event(self._producer, "system", "health.service_restarted",
                service_restarted_payload("signal_daemon", "socket_missing"), source="health")
            self.signal_daemon.terminate()
            try:
                self.signal_daemon.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.signal_daemon.kill()
            self.signal_daemon = self._spawn_signal_daemon()
            if self.signal_daemon:
                self._start_signal_listener()

    def _check_discord_health(self):
        """Check Discord listener health and restart if needed."""
        if self.discord_listener is None:
            return
        if not self.discord_listener.is_alive():
            log.warning("Discord listener died, restarting...")
            lifecycle_log.info("DISCORD_LISTENER | DIED | restarting")
            produce_event(self._producer, "system", "health.service_restarted",
                service_restarted_payload("discord_listener", "died"), source="health")
            self._start_discord_listener()

    # chrome-control health is now a framework-registered dependency
    # ("chrome_control" in assistant/dependency_checks.py). The old standalone
    # _run_chrome_control_check() / _chrome_check_running / _chrome_last_status
    # were removed in the Phase 2 unification — recovery used to run twice.
    # The DependencyHealthRunner now emits `health.dependency` events instead of
    # `health.chrome_check` (see CLAUDE.md "Diagnostic Events" table).

    # ── Blocked-session watchdog ────────────────────────────────────────
    #
    # Default idle threshold (minutes) before a session that went quiet with an
    # outstanding commitment gets nudged. Overridable via
    # config.local.yaml: `watchdog.stuck_session_idle_minutes`.
    STUCK_SESSION_IDLE_MINUTES_DEFAULT = 5.0

    def _stuck_session_idle_minutes(self) -> float:
        """Idle threshold N (minutes), from config (default 5)."""
        from assistant import config
        try:
            val = config.get("watchdog.stuck_session_idle_minutes",
                              self.STUCK_SESSION_IDLE_MINUTES_DEFAULT)
            return float(val)
        except (TypeError, ValueError):
            return self.STUCK_SESSION_IDLE_MINUTES_DEFAULT

    async def _run_stuck_session_check(self):
        """Nudge any session that's gone idle with an outstanding commitment.

        The 2026-05-11 outage: a session hit a wedged tool, tried the documented
        recovery once, asked the human to do something, then parked idle for ~1h.
        "Ask the human and wait" is not a recovery strategy. This backstop catches
        a quiet-but-committed session within ~N min and injects a system nudge
        telling it to re-check the blocker / escalate with a specific ask.

        Detection (two ways, marker preferred):
          - marker: the session set a `pending_commitment` via
            `claude-assistant commitment set ...` (a small file under
            state/commitments/). Authoritative; the session is *expected* to set
            this when it tells the user "I'll do X".
          - heuristic: no marker → scan the session's last outbound text for a
            small, conservative set of commitment phrases ("I'll …", "working on
            it", "waiting on you …", "once you …", "let me know … and I'll …").

        Idle = not currently in a turn (`not is_busy`), empty message queue, and
        `last_activity` older than N min (config: watchdog.stuck_session_idle_minutes,
        default 5). Don't-spam: per-session backoff of 2×N — one nudge, then the
        session itself escalates (it has send-sms + the "Self-Heal Before
        Escalating" rule); we re-arm only after the session does another turn.

        Emits a `session.stuck_nudge` bus event (sessions topic) + a manager-log
        line on each nudge. Non-blocking: all reads are in-process / cheap; the
        commitment-marker GC touches a few tiny files off the hot path.
        """
        from datetime import datetime as _dt, timezone as _tz
        from assistant.commitments import (
            get_commitment, clear_commitment, commitment_age_seconds,
            looks_like_commitment, COMMITMENT_MAX_AGE_SECONDS,
        )
        from assistant.bus_helpers import stuck_nudge_payload

        n_minutes = self._stuck_session_idle_minutes()
        idle_threshold_s = n_minutes * 60.0
        backoff_s = max(idle_threshold_s * 2.0, 120.0)  # 2×N, floor 2 min
        now_wall = time.time()

        # Snapshot the live sessions (dict can mutate concurrently).
        try:
            sessions_snapshot = list(self.sessions.sessions.items())
        except Exception as e:
            log.debug(f"STUCK_CHECK | could not snapshot sessions: {e}")
            return

        for chat_id, session in sessions_snapshot:
            if chat_id == MASTER_SESSION:
                continue
            try:
                session_name = getattr(session, "_session_name", None) or get_session_name(
                    getattr(session, "chat_id", chat_id), getattr(session, "source", "imessage"))

                # ── Idle gate ──
                if getattr(session, "is_busy", False):
                    continue
                try:
                    if session._message_queue.qsize() > 0:
                        continue
                except Exception:
                    continue
                last_activity = getattr(session, "last_activity", None)
                if not isinstance(last_activity, _dt):
                    continue
                idle_s = (_dt.now() - last_activity).total_seconds()
                if idle_s < idle_threshold_s:
                    # Active recently — clear any stale nudge cooldown so the next
                    # quiet-with-commitment period gets a fresh nudge.
                    self._last_stuck_nudge_at.pop(session_name, None)
                    continue

                # ── Outstanding-commitment gate ──
                detection: str | None = None
                committed_text: str | None = None

                commitment = get_commitment(session_name)
                if commitment is not None:
                    age = commitment_age_seconds(commitment)
                    if age is not None and age > COMMITMENT_MAX_AGE_SECONDS:
                        # Stale marker — GC it, don't nudge on it.
                        clear_commitment(session_name)
                        commitment = None
                    else:
                        detection = "marker"
                        committed_text = str(commitment.get("text", ""))[:280]

                if detection is None:
                    snippet = looks_like_commitment(getattr(session, "last_assistant_text", None))
                    if snippet:
                        detection = "heuristic"
                        committed_text = snippet[:280]

                if detection is None:
                    continue  # Normally-idle session with no commitment — leave it.

                # ── Don't-spam gate ──
                last_nudge = self._last_stuck_nudge_at.get(session_name)
                if last_nudge is not None and (now_wall - last_nudge) < backoff_s:
                    continue

                idle_minutes = idle_s / 60.0
                contact_name = getattr(session, "contact_name", "the user") or "the user"

                # Build the nudge message injected into the session's queue.
                ts_display = _dt.now().strftime("%Y-%m-%d %I:%M %p")
                what = f' (you said: "{committed_text}")' if committed_text else ""
                nudge = (
                    f"---SYSTEM NUDGE [{ts_display}]---\n"
                    f"You've been idle for ~{idle_minutes:.0f} min with an open commitment to "
                    f"{contact_name}{what}.\n\n"
                    "Do NOT just wait. Right now:\n"
                    "1. Re-check whether the blocker has cleared (re-run the documented recovery / retry the blocked step once).\n"
                    "2. If it cleared, continue the task and update the user.\n"
                    "3. If it's still blocked, escalate to the admin with a SPECIFIC ask (per the 'Self-Heal Before Escalating' rule in CLAUDE.md) AND schedule a re-check (e.g. `claude-assistant remind add \"re-check the blocked step\" --contact \"<this chat_id>\" --in 2m`) so the blocker self-resolves if the dependency comes back.\n"
                    "4. When done (or genuinely stuck and escalated), clear the marker: `claude-assistant commitment clear`.\n"
                    "---END SYSTEM NUDGE---"
                )

                try:
                    if session.is_alive():
                        await session.inject(nudge)
                    else:
                        log.warning(f"STUCK_CHECK | {session_name} idle+committed but not alive — skipping nudge")
                        continue
                except Exception as e:
                    log.error(f"STUCK_CHECK | failed to inject nudge into {session_name}: {e}")
                    continue

                self._last_stuck_nudge_at[session_name] = now_wall
                log.warning(
                    f"STUCK_CHECK | nudged {session_name} | idle={idle_minutes:.1f}min | "
                    f"detection={detection} | committed={committed_text!r}"
                )
                produce_session_event(
                    self._producer, getattr(session, "chat_id", chat_id), "session.stuck_nudge",
                    stuck_nudge_payload(session_name, getattr(session, "chat_id", chat_id),
                                        idle_minutes, detection,
                                        contact_name=contact_name, committed_text=committed_text),
                    source="watchdog",
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error(f"STUCK_CHECK | error checking {chat_id}: {e}")

    def _check_dispatch_api(self):
        """Deep health check for Dispatch API — diagnostic only, no restarts.

        The ChildSupervisor handles all restarts. This check:
        - Clears degraded mode if the process is now healthy
        - Clears degraded mode if the process is dead (lets supervisor retry)
        """
        sv = self.dispatch_api_supervisor
        proc = sv.proc
        if proc and proc.poll() is None:
            if sv._check_health_sync():
                # Healthy — clear degraded if set
                sv.clear_degraded()
            else:
                log.warning("DISPATCH_API | deep check: process alive but /health failed")
        elif sv.degraded:
            # Process dead + degraded — clear so supervisor can retry
            lifecycle_log.info("DISPATCH_API | DEGRADED_RECOVERY | clearing for supervisor retry")
            sv.clear_degraded()

    def _check_metro(self):
        """Deep health check for Metro — diagnostic only, no restarts."""
        sv = self.metro_supervisor
        proc = sv.proc
        if proc and proc.poll() is None:
            if not self._check_metro_health():
                log.warning("METRO | deep check: process alive but /status failed")
        elif sv.degraded:
            lifecycle_log.info("METRO | DEGRADED_RECOVERY | clearing for supervisor retry")
            sv.clear_degraded()

    def _create_metro_process(self) -> Optional[subprocess.Popen]:
        """Create the Metro dev server process (Popen only, no lifecycle management).

        Called by ChildSupervisor. Returns the Popen object or None.
        """
        if not DISPATCH_APP_DIR.exists():
            log.warning(f"Dispatch app dir not found at {DISPATCH_APP_DIR}")
            return None

        metro_log_path = LOGS_DIR / "metro.log"
        self._close_log_fh('_metro_log_fh')
        self._metro_log_fh = open(metro_log_path, "a")

        # Ensure node_modules are in sync before starting metro
        try:
            log.info("Running bun install to sync node_modules...")
            install_result = subprocess.run(
                ["bun", "install"],
                cwd=str(DISPATCH_APP_DIR),
                capture_output=True, text=True, timeout=120,
            )
            if install_result.returncode != 0:
                log.warning(f"bun install failed (rc={install_result.returncode}): {install_result.stderr[:500]}")
            else:
                log.info("bun install completed successfully")
        except FileNotFoundError:
            try:
                log.info("bun not found, falling back to npm install...")
                subprocess.run(
                    ["npm", "install"],
                    cwd=str(DISPATCH_APP_DIR),
                    capture_output=True, text=True, timeout=120,
                )
            except Exception as e:
                log.warning(f"npm install fallback failed: {e}")
        except Exception as e:
            log.warning(f"bun install failed: {e}")

        # Kill any orphaned process on the metro port
        try:
            import subprocess as _sp
            result = _sp.run(["lsof", "-ti", f"tcp:{METRO_PORT}", "-sTCP:LISTEN"], capture_output=True, text=True)
            for pid_str in result.stdout.strip().split('\n'):
                if pid_str.strip():
                    try:
                        import os as _os
                        _os.kill(int(pid_str.strip()), 9)
                        log.info(f"Killed orphaned process on port {METRO_PORT}: PID {pid_str.strip()}")
                    except (ProcessLookupError, ValueError):
                        pass
        except Exception:
            pass

        try:
            proc = subprocess.Popen(
                ["npx", "expo", "start", "--port", str(METRO_PORT)],
                stdout=self._metro_log_fh,
                stderr=self._metro_log_fh,
                cwd=str(DISPATCH_APP_DIR),
                start_new_session=True,
            )
        except Exception:
            self._close_log_fh('_metro_log_fh')
            raise

        log.info(f"Spawned Metro process (PID: {proc.pid})")
        return proc

    def _check_metro_health(self) -> bool:
        """Check if Metro dev server is responding."""
        try:
            import urllib.request
            url = f"http://localhost:{METRO_PORT}/status"
            with urllib.request.urlopen(url, timeout=2) as resp:
                return resp.status == 200
        except Exception:
            return False

    def _stop_metro(self):
        """Stop the Metro dev server — sync fallback for shutdown path."""
        proc = self.metro_supervisor.proc
        if proc:
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    pgid = os.getpgid(proc.pid)
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    try:
                        proc.kill()
                    except (ProcessLookupError, OSError):
                        pass
            self.metro_supervisor._proc = None
            self.metro_daemon = None
            log.info("Stopped Metro dev server")
        self._close_log_fh('_metro_log_fh')

    def _kill_existing_signal_daemons(self):
        """Kill any existing signal-cli daemon processes to avoid pile-up."""
        try:
            result = subprocess.run(
                ["pkill", "-f", "signal-cli.*daemon.*--socket"],
                capture_output=True, timeout=5,
            )
            if result.returncode == 0:
                log.info("Killed existing signal-cli daemon processes")
                time.sleep(1)  # Let processes die
        except Exception as e:
            log.debug(f"pkill signal-cli: {e}")

    def _spawn_signal_daemon(self) -> Optional[subprocess.Popen]:
        """Spawn the signal-cli daemon as a child process.

        Returns the Popen object or None if spawn failed.
        """
        # Kill any orphaned signal-cli processes first
        self._kill_existing_signal_daemons()

        # Clean up any stale socket
        if SIGNAL_SOCKET.exists():
            SIGNAL_SOCKET.unlink()

        signal_log_path = LOGS_DIR / "signal-daemon.log"
        self._close_log_fh('_signal_log_fh')
        self._signal_log_fh = open(signal_log_path, "a")

        try:
            proc = subprocess.Popen(
                [str(SIGNAL_CLI), "-a", signal_account(), "daemon", "--socket", str(SIGNAL_SOCKET), "--receive-mode", "on-connection", "--no-receive-stdout"],
                stdout=self._signal_log_fh,
                stderr=self._signal_log_fh,
            )
        except Exception:
            self._close_log_fh('_signal_log_fh')
            raise

        try:
            log.info(f"Spawned signal-cli daemon (PID: {proc.pid})")
            lifecycle_log.info(f"SIGNAL_DAEMON | SPAWNED | pid={proc.pid}")
            produce_event(getattr(self, '_producer', None), "system", "health.service_spawned",
                service_spawned_payload("signal_daemon", proc.pid), source="daemon")

            # Wait for socket to be ready (up to 30s - Java is slow to start)
            for _ in range(300):
                if SIGNAL_SOCKET.exists():
                    log.info("Signal daemon socket ready")
                    return proc
                time.sleep(0.1)

            log.warning("Signal daemon started but socket not ready after 30s")
            return proc
        except Exception as e:
            log.error(f"Failed to spawn signal daemon: {e}")
            return None

    def _start_signal_listener(self):
        """Start the Signal listener thread."""
        if self.signal_listener is not None and self.signal_listener.is_alive():
            log.debug("Signal listener already running")
            return

        self.signal_listener = SignalListener(self.signal_queue)
        self.signal_listener.start()
        log.info("Started Signal listener thread")
        lifecycle_log.info("SIGNAL_LISTENER | STARTED")

    def _start_discord_listener(self):
        """Start the Discord listener thread."""
        from assistant import config as app_config

        # Check config-driven enable/disable (disabled_backends list or legacy discord.enabled)
        if _backend_disabled("discord"):
            log.info("Discord disabled via disabled_backends config")
            return
        if not app_config.get("discord.enabled", True):
            log.info("Discord disabled via config (discord.enabled: false)")
            return

        if self.discord_listener is not None and self.discord_listener.is_alive():
            log.debug("Discord listener already running")
            return
        discord_channels = app_config.get("discord.channel_ids", [])
        if not discord_channels:
            log.info("Discord not configured — skipping (set discord.channel_ids in config)")
            return

        # Token resolution: keychain first (matches send-discord CLI), then config
        discord_token = None
        import subprocess
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", "discord_bot_token", "-w"],
                capture_output=True, text=True
            )
            if result.returncode == 0 and result.stdout.strip():
                discord_token = result.stdout.strip()
        except Exception:
            pass

        if not discord_token:
            discord_token = app_config.get("discord.bot_token")

        if not discord_token:
            log.warning("Discord token not found in keychain or config — skipping")
            return

        try:
            from assistant.discord_listener import DiscordListener
            bot_role_ids = app_config.get("discord.bot_role_ids", [])
            bot_names = app_config.get("discord.bot_names", ["sven"])
            self.discord_listener = DiscordListener(
                self.discord_queue, discord_channels, discord_token,
                bot_role_ids=bot_role_ids,
                bot_names=bot_names,
            )
            self.discord_listener.start()
            log.info(f"Started Discord listener for channels: {discord_channels} (roles={bot_role_ids}, names={bot_names})")
            lifecycle_log.info(f"DISCORD_LISTENER | STARTED | channels={discord_channels}")
        except ImportError as e:
            log.warning(f"Discord not available (discord.py not installed): {e}")
        except Exception as e:
            log.error(f"Failed to start Discord listener: {e}")

    def _stop_signal(self):
        """Stop Signal daemon and listener."""
        if self.signal_listener:
            self.signal_listener.stop()
            self.signal_listener = None

        if self.signal_daemon:
            self.signal_daemon.terminate()
            try:
                self.signal_daemon.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.signal_daemon.kill()
            self.signal_daemon = None
            log.info("Stopped signal daemon")

        self._close_log_fh('_signal_log_fh')

        if SIGNAL_SOCKET.exists():
            SIGNAL_SOCKET.unlink()

    def _restart_dispatch_api(self) -> dict:
        """Restart the dispatch API server. Called via IPC (sync context).

        Uses asyncio.run_coroutine_threadsafe if called from IPC thread,
        or falls back to sync spawn for backward compat.
        """
        log.info("DISPATCH_API | restarting via CLI")
        sv = self.dispatch_api_supervisor
        # Stop existing process synchronously
        if sv.proc and sv.proc.poll() is None:
            try:
                pgid = os.getpgid(sv.proc.pid)
                os.killpg(pgid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
            try:
                sv.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    sv.proc.kill()
                except (ProcessLookupError, OSError):
                    pass
        sv._proc = None
        time.sleep(0.5)
        # Spawn new process
        try:
            sv._proc = self._create_dispatch_api_process()
        except Exception as e:
            log.error(f"DISPATCH_API | restart spawn error: {e}")
            sv._proc = None
        self.dispatch_api_daemon = sv.proc  # Update compat ref
        ok = sv.proc is not None
        pid = sv.proc.pid if sv.proc else None
        msg = f"Dispatch API restarted (PID: {pid})" if ok else "Failed to restart Dispatch API"
        log.info(f"DISPATCH_API | {msg}")
        return {"ok": ok, "message": msg}

    async def _run_health_checks(self):
        """Run all health checks in background (non-blocking).

        This runs as a separate async task so health checks don't block
        the main message processing loop. Includes:
        - Session health_check_all (liveness)
        - Tier 2 deep Haiku analysis
        - Discord listener / Dispatch API / Metro health
        - Quota fetch + cache write
        - perf gauges for FDs / disk

        NOTE: signal-cli, disk recovery/escalation, and FD-leak escalation are
        owned by the DependencyHealthRunner (assistant/dependency_checks.py) —
        this method only emits the perf gauges for FDs/disk, not the alarms.
        """
        try:
            log.info("Running session health check (background)...")

            # Session liveness check
            await self.sessions.health_check_all()

            # Tier 2: Haiku deep analysis (skip recently healed)
            try:
                recently_healed = set(self.sessions._recently_healed.keys())
                await self.sessions.deep_health_check(skip_chat_ids=recently_healed)
            except Exception as e:
                log.error(f"Deep health check failed: {e}")

            # --- Service health checks (each isolated so one failure can't block others) ---
            #
            # NOTE: signal-cli health + restart is now owned by the
            # DependencyHealthRunner's "signal_cli" check (probe socket → restart
            # with --receive-mode on-connection → escalate after 3). We no longer
            # call _check_signal_health() here — that would recover twice.

            try:
                self._check_discord_health()
            except Exception as e:
                log.error(f"Discord health check failed: {e}")

            try:
                self._check_dispatch_api()
            except Exception as e:
                log.error(f"Dispatch API health check failed: {e}")

            try:
                self._check_metro()
            except Exception as e:
                log.error(f"Metro health check failed: {e}")

            # Proactive FD monitoring via ResourceRegistry — calibrated leak detection
            try:
                if self._resource_registry:
                    status = self._resource_registry.get_status()
                    perf.gauge("open_fds", status['fd_actual'], component="daemon")
                    perf.gauge("tracked_resources", status['total'], component="daemon")
                    perf.gauge("fd_delta",
                               status['fd_actual'] - status['fd_baseline'] - status['fd_tracked'],
                               component="daemon")

                    # Untracked-FD leak detection + escalation is now owned by the
                    # DependencyHealthRunner's "fd_leak" check (no auto-recovery —
                    # a real FD leak needs a code fix → escalate). We still record
                    # the calibrated leak warnings to the log here for the
                    # authoritative trail / grep, but the alarm path lives in the
                    # framework.
                    for w in self._resource_registry.check_fd_leaks(threshold=40):
                        log.warning(f"FD_LEAK | {w}")
                else:
                    # Fallback if registry not yet initialized
                    fd_count = len(os.listdir('/dev/fd'))
                    perf.gauge("open_fds", fd_count, component="daemon")
                    if fd_count > 200:
                        log.warning(f"FD_WARNING | open_fds={fd_count} | approaching system limit")
            except Exception:
                pass

            # Disk space — perf gauges only. Detection / recovery (clean temp
            # dirs) / escalation is owned by the DependencyHealthRunner's "disk"
            # check (it both warns and, if critical, attempts a cleanup before
            # escalating). We keep emitting the gauges here for the perf trail.
            try:
                from assistant.health import check_disk_space
                disk = check_disk_space()
                perf.gauge("disk_used_pct", disk["used_pct"], component="daemon")
                perf.gauge("disk_free_gb", disk["free_gb"], component="daemon")
                if disk["message"]:
                    log.warning(f"DISK | {disk['message']}")
            except Exception as e:
                log.error(f"Disk space check failed: {e}")

            # Quota monitoring — fetch, write cache, emit bus event
            #
            # Quota cache write hierarchy (most → least reliable):
            # 1. Direct write here after fetch_quota()  — PRIMARY path
            # 2. Self-heal below if file mtime stale    — BACKSTOP
            # 3. Bus consumer _handle_quota_event        — NON-CRITICAL for freshness
            #    (its real job: alerts, model degradation, perf gauges)
            #
            # Quota data changes at ~15-min granularity.  Any valid write
            # within a cycle is acceptable.  Concurrent atomic writes are
            # safe (last-write-wins, no locking needed).
            try:
                from assistant.health import fetch_quota, get_quota_backoff_state, get_quota_cached
                from assistant.bus_helpers import produce_event
                usage, _quota_ts, is_fresh = fetch_quota()
                if usage and is_fresh:
                    # Primary cache write — this alone prevents the 22h stale-cache incident
                    cache_data = {k: v for k, v in usage.items()
                                  if k not in ("backoff_seconds", "consecutive_failures")}
                    if not self._write_quota_cache(cache_data, source="direct"):
                        log.error("QUOTA_CACHE | Direct write failed — self-heal will backstop")
                    # Bus event for consumer (alerts, degradation, perf gauges)
                    backoff = get_quota_backoff_state()
                    produce_event(self._producer, "system", "quota.fetched",
                                  payload={**usage, **backoff},
                                  source="daemon")
                    log.debug("QUOTA | Emitted quota.fetched bus event")
                elif not is_fresh:
                    # Check if this was a soft API failure (not just a cache hit)
                    backoff = get_quota_backoff_state()
                    if backoff["consecutive_failures"] > 0:
                        produce_event(self._producer, "system", "quota.fetch_failed",
                                      payload={"error": "API call failed (soft)", **backoff},
                                      source="daemon")
                        log.debug("QUOTA | Emitted quota.fetch_failed (soft failure)")
            except Exception as e:
                log.error("QUOTA | Fetch failed: %s", e)
                try:
                    produce_event(self._producer, "system", "quota.fetch_failed",
                                  payload={"error": str(e), **get_quota_backoff_state()},
                                  source="daemon")
                except Exception:
                    pass

            # Self-heal: detect persistent write failures from prior cycles.
            # A single stat() call checks if the file is stale; if so, rewrite
            # from in-memory data.  This catches cases where BOTH the direct-write
            # above AND the consumer have been failing across multiple cycles.
            try:
                quota_path = STATE_DIR / "quota_cache.json"
                if quota_path.exists():
                    file_age_s = time.time() - quota_path.stat().st_mtime
                    if file_age_s > self._QUOTA_CACHE_STALE_S:
                        cached_data, _cached_ts = get_quota_cached()
                        if cached_data:
                            log.warning("QUOTA_CACHE | File stale by %dm, self-healing from memory",
                                        int(file_age_s / 60))
                            if not self._write_quota_cache(cached_data, source="self-heal"):
                                log.error("QUOTA_CACHE | Self-heal write also failed")
                        else:
                            # First boot or fetcher hasn't run yet — expected, not alarming
                            log.debug("QUOTA_CACHE | File stale by %dm but no in-memory data yet",
                                      int(file_age_s / 60))
                # else: first boot, no file — will be created on first successful fetch
            except Exception as e:
                log.error("QUOTA_CACHE | Staleness check failed: %s", e)

            # Process circuit breaker actions from deep_health_check
            try:
                from assistant import config
                admin_phone = config.get("owner.phone")
                cb_actions = getattr(self.sessions, '_circuit_breaker_actions', [])
                if cb_actions and admin_phone:
                    for action in cb_actions:
                        if action == "sms_circuit_open":
                            self._send_sms(admin_phone,
                                "[SVEN] 🚨 Deep heal circuit breaker OPEN — Haiku calls failing.\n"
                                "API may be down. Haiku health checks paused for 5 min.")
                    self.sessions._circuit_breaker_actions = []
            except Exception as e:
                log.error(f"Circuit breaker notification failed: {e}")

            log.info("Health check completed (background)")

        except Exception as e:
            log.error(f"Background health check failed: {e}")

    def _resolve_signal_uuid(self, uuid: str) -> Optional[str]:
        """Resolve a Signal UUID to a profile display name via signal-cli's recipient DB.

        Returns the profile name (e.g. "Josiah Roberts") or None if not found.
        Fast — single SQLite query against signal-cli's local data.
        """
        import sqlite3 as _sqlite3
        SIGNAL_DB = HOME / ".local/share/signal-cli/data/218538.d/account.db"
        try:
            conn = _sqlite3.connect(f"file:{SIGNAL_DB}?mode=ro", uri=True, timeout=2)
            row = conn.execute(
                "SELECT profile_given_name, profile_family_name, nick_name_given_name, nick_name_family_name FROM recipient WHERE aci = ?",
                (uuid,)
            ).fetchone()
            conn.close()
            if row:
                # Prefer nickname, fall back to profile name
                nick = f"{row[2] or ''} {row[3] or ''}".strip()
                profile = f"{row[0] or ''} {row[1] or ''}".strip()
                return nick or profile or None
        except Exception as e:
            log.warning(f"Failed to resolve Signal UUID {uuid}: {e}")
        return None

    def _maybe_send_disabled_notice(self, chat_id: str, source: str, phone: str, is_group: bool):
        """Send a one-time notice that a chat/backend is disabled. Only via iMessage."""
        notice_key = f"{source}:{chat_id}"
        if notice_key in self._disabled_notice_sent:
            return
        self._disabled_notice_sent.add(notice_key)

        # Only send notice via iMessage (we can't/shouldn't reply on disabled backends)
        # For disabled backends, the sender phone might be a Discord ID etc — skip those
        if source != "imessage":
            log.info(f"Disabled notice skipped for non-iMessage source '{source}' chat '{chat_id}'")
            return

        target = chat_id if is_group else phone
        self._send_sms(target, "[Sven is currently offline for this chat. Messages won't be processed until re-enabled.]")
        log.info(f"Sent disabled notice to {target}")

    def _send_sms(self, phone: str, message: str) -> bool:
        """Send an SMS message via the send-sms CLI.

        Returns True on success, False on failure.
        Used for daemon-level control responses (RESTART, HEALING failures).
        """
        try:
            result = subprocess.run(
                [str(HOME / ".claude/skills/sms-assistant/scripts/send-sms"), phone, message],
                capture_output=True,
                text=True,
                timeout=30
            )
            success = result.returncode == 0
            if not success:
                log.error(f"Failed to send SMS to {phone}: {result.stderr}")
            produce_event(self._producer, "messages", "message.sent" if success else "message.failed",
                message_sent_payload(phone, message, is_group=False, success=success,
                                     source="daemon-control"),
                key=f"imessage/{phone}", source="daemon")
            return success
        except Exception as e:
            log.error(f"Error sending SMS to {phone}: {e}")
            produce_event(self._producer, "messages", "message.failed",
                message_sent_payload(phone, message, is_group=False, success=False,
                                     error=str(e), source="daemon-control"),
                key=f"imessage/{phone}", source="daemon")
            return False

    def _send_sms_image(self, phone: str, image_path: str, caption: str | None = None) -> bool:
        """Send an image via SMS using the send-sms CLI.

        Returns True on success, False on failure.
        """
        try:
            cmd = [str(HOME / ".claude/skills/sms-assistant/scripts/send-sms"), phone]
            if caption:
                cmd.append(caption)
            cmd.extend(["--image", image_path])

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60
            )
            if result.returncode != 0:
                log.error(f"Failed to send image to {phone}: {result.stderr}")
                return False
            return True
        except Exception as e:
            log.error(f"Error sending image to {phone}: {e}")
            return False

    def _send_signal(self, chat_id: str, message: str) -> bool:
        """Send a Signal message via signal-cli.

        Args:
            chat_id: Either a phone number (+1...) or group ID (base64 string)
            message: The message text to send

        Returns True on success, False on failure.
        """
        try:
            # Determine if this is a group or individual chat
            if is_group_chat_id(chat_id):
                # Group message: use --group-id flag
                cmd = [
                    str(SIGNAL_CLI), "-a", signal_account(),
                    "send", "-g", chat_id, "--message-from-stdin"
                ]
            else:
                # Individual message: recipient is positional arg
                # Strip signal: prefix if present
                recipient = chat_id.replace("signal:", "")
                cmd = [
                    str(SIGNAL_CLI), "-a", signal_account(),
                    "send", recipient, "--message-from-stdin"
                ]

            result = subprocess.run(
                cmd,
                input=message,
                capture_output=True,
                text=True,
                timeout=60  # Increased timeout for group messages
            )
            if result.returncode != 0:
                log.error(f"Failed to send Signal to {chat_id}: {result.stderr}")
                return False
            log.info(f"Sent Signal message to {chat_id}")
            return True
        except Exception as e:
            log.error(f"Error sending Signal to {chat_id}: {e}")
            return False

    async def _spawn_healing_session(self, admin_name: str, admin_phone: str, custom_prompt: str | None = None):
        """Spawn a healing Claude session to diagnose and fix system issues.

        Uses asyncio.create_subprocess_exec with claude -p for a one-shot session.
        """
        # Build the healing prompt
        custom_context = custom_prompt if custom_prompt else "None provided"
        session_name = get_session_name(admin_name)

        healing_prompt = f'''EMERGENCY HEALING MODE

CRITICAL FIRST STEP - VERIFY SENDER:
1. Check who sent HEALME:
   ~/.claude/skills/sms-assistant/scripts/read-sms --chat "{admin_phone}" --limit 5

2. Look up their tier:
   ~/.claude/skills/contacts/scripts/contacts lookup "{admin_phone}"

The sender MUST be in the "admin" tier. If NOT admin tier, this is unauthorized:
~/.claude/skills/sms-assistant/scripts/send-sms "{admin_phone}" "[HEALING] ABORTED - unauthorized sender (not admin tier)"
Then STOP immediately.

Only proceed if sender is verified as admin tier.

---

HEALME was triggered from {admin_phone}.

Custom context: {custom_context}

Your job is to diagnose and fix the issue. Follow these steps:

1. FIRST (after verification): Send an SMS to let them know you're on it:
   ~/.claude/skills/sms-assistant/scripts/send-sms "{admin_phone}" "[HEALING] Starting diagnosis..."

2. Take and send a screenshot of the current screen state:
   screencapture -x /tmp/healme-screenshot.png && ~/.claude/skills/sms-assistant/scripts/send-sms "{admin_phone}" /tmp/healme-screenshot.png

3. Check system resources:
   uv run ~/.claude/skills/system-info/scripts/sysinfo.py

4. Check active SDK sessions:
   claude-assistant status

5. Check recent logs for errors:
   tail -100 ~/dispatch/logs/manager.log | grep -iE "(error|fail|exception)"
   tail -50 ~/dispatch/logs/session_lifecycle.log

6. Check recent SMS history with admin:
   ~/.claude/skills/sms-assistant/scripts/read-sms --chat "{admin_phone}" --limit 20

7. Check the admin transcript for context:
   Look at ~/transcripts/{session_name}/ if it exists

8. Send [HEALING] updates as you find issues:
   ~/.claude/skills/sms-assistant/scripts/send-sms "{admin_phone}" "[HEALING] Found: <issue>"

9. Fix what you can:
   - Kill stuck Claude processes: kill <pid>
   - Close stale Chrome tabs: ~/.claude/skills/chrome-control/scripts/chrome close <tab_id>
   - Kill broken sessions: claude-assistant kill-session <name>

10. Restart the daemon:
    claude-assistant restart

11. Restart the admin session:
    claude-assistant restart-session {session_name}

12. Send completion message:
    ~/.claude/skills/sms-assistant/scripts/send-sms "{admin_phone}" "[HEALING] Complete - <summary of what you found and fixed>"

If you CANNOT fix the issue, send:
~/.claude/skills/sms-assistant/scripts/send-sms "{admin_phone}" "[HEALING] FAILED - manual intervention needed: <reason>"

You have 15 minutes. Work efficiently.
'''

        # Write prompt to temp file to avoid shell escaping issues
        prompt_file = Path("/tmp/healing_prompt.txt")
        prompt_file.write_text(healing_prompt)

        try:
            proc = await asyncio.create_subprocess_exec(
                str(CLAUDE), "--dangerously-skip-permissions", "-p", healing_prompt,
                cwd=str(HOME),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            log.info(f"Spawned healing session for {admin_name} (PID: {proc.pid})")
            lifecycle_log.info(f"HEALME | SPAWNED | pid={proc.pid}")

            # Track the healing process so it gets awaited (prevents zombies)
            async def _await_healing(p, producer):
                try:
                    await p.wait()
                    lifecycle_log.info(f"HEALME | COMPLETED | pid={p.pid} returncode={p.returncode}")
                    produce_event(producer, "system", "healme.completed",
                        healme_payload(admin_phone, admin_name, "completed"),
                        source="healme")
                except Exception as e:
                    log.error(f"Healing session error: {e}")
            asyncio.create_task(_await_healing(proc, self._producer))
        except Exception as e:
            log.error(f"Failed to spawn healing session: {e}")
            # Try to notify admin
            self._send_sms(admin_phone, "[HEALING] FAILED to start healing session - manual intervention needed")
            produce_event(self._producer, "system", "healme.completed",
                healme_payload(admin_phone, admin_name, "failed", error=str(e)),
                source="healme")

    async def process_message(self, msg: dict):
        """Process a single incoming message."""
        phone = msg["phone"]
        text = msg["text"]
        rowid = msg["rowid"]
        attachments = msg.get("attachments", [])
        is_group = msg.get("is_group", False)
        group_name = msg.get("group_name")
        chat_identifier = msg.get("chat_identifier")
        audio_transcription = msg.get("audio_transcription")
        thread_originator_guid = msg.get("thread_originator_guid")
        message_guid = msg.get("guid")  # iMessage GUID for tapback reactions
        source = msg.get("source", "imessage")  # Default to iMessage for backwards compat
        message_timestamp = msg.get("timestamp")  # datetime for Gemini vision context

        # Log message preview
        text_preview = (text[:50] + "...") if text else "(attachment only)"
        attachment_info = f" + {len(attachments)} attachment(s)" if attachments else ""
        group_info = f" [GROUP: {group_name or chat_identifier}]" if is_group else ""
        log.info(f"Processing message {rowid}{group_info}: {text_preview}{attachment_info}")

        # --- Hot-reloaded disabled_backends / disabled_chats check ---
        from assistant import config as _dyn_config
        _dyn_config.reload()  # Re-read config from disk (no daemon restart needed)
        _disabled_backends = _dyn_config.get("disabled_backends", []) or []
        _disabled_chats = _dyn_config.get("disabled_chats", []) or []

        # Check disabled backends
        if source in _disabled_backends:
            chat_key = chat_identifier if is_group else phone
            log.info(f"Dropping message from disabled backend '{source}' (chat: {chat_key})")
            self._maybe_send_disabled_notice(chat_key, source, phone, is_group)
            return

        # Check disabled chats (by chat_id — phone for individual, chat_identifier for groups)
        chat_id_for_check = chat_identifier if is_group else phone
        if chat_id_for_check in _disabled_chats:
            log.info(f"Dropping message from disabled chat '{chat_id_for_check}'")
            self._maybe_send_disabled_notice(chat_id_for_check, source, phone, is_group)
            return

        # NOTE: produce_event("message.received") is called by the poll loop,
        # not here. This method is invoked by the bus consumer after reading
        # the event, so producing here would create duplicates.

        # Lookup contact (sender) - supports both phone and email identifiers
        contact = self.contacts.lookup_identifier(phone)

        if contact:
            sender_name = contact["name"]
            sender_tier = contact["tier"]
            log.info(f"Sender: {sender_name} (tier: {sender_tier})")
        else:
            sender_name = None
            sender_tier = None

        # Discord users don't have phone numbers — use config-based tier lookup
        if not contact and source == "discord":
            from assistant import config as app_config
            discord_users = app_config.get("discord.users", {})
            discord_user = discord_users.get(phone)  # phone = discord user ID
            if discord_user:
                sender_name = discord_user.get("name", phone)
                sender_tier = discord_user.get("tier", "unknown")
                contact = {"name": sender_name, "tier": sender_tier}
                log.info(f"Discord user resolved: {sender_name} (tier: {sender_tier})")
            else:
                # Unmapped Discord users get the default tier from config (defaults to "unknown")
                default_tier = app_config.get("discord.default_tier", "unknown")
                sender_name = msg.get("sender_name", phone)
                sender_tier = default_tier
                if default_tier != "unknown":
                    contact = {"name": sender_name, "tier": sender_tier}
                    log.info(f"[discord] Unmapped user: {sender_name} ({phone}) — using default tier '{default_tier}'")
                else:
                    log.info(f"[discord] Unmapped user: {sender_name} ({phone}) — ignoring (add to discord.users config or set discord.default_tier for access)")

        # Signal UUID resolution — when contact lookup fails and phone is a UUID,
        # resolve the display name from signal-cli's recipient database
        if not contact and source == "signal":
            signal_sender_name = msg.get("sender_name")  # Signal profile name from envelope
            source_uuid = msg.get("source_uuid")
            if signal_sender_name:
                sender_name = signal_sender_name
                log.info(f"[signal] Using Signal profile name for UUID sender: {sender_name} ({phone})")
            elif source_uuid:
                # Try resolving from signal-cli's recipient DB
                resolved = self._resolve_signal_uuid(source_uuid)
                if resolved:
                    sender_name = resolved
                    log.info(f"[signal] Resolved UUID {source_uuid} to profile name: {sender_name}")

        # HEALME intercept - works even if contacts lookup fails!
        # This is critical because HEALME is needed most when systems are broken.
        # Hardcoded admin phone as emergency fallback.
        from assistant import config
        ADMIN_PHONE = config.require("owner.phone")
        is_admin = (sender_tier == "admin") or (phone == ADMIN_PHONE)

        if text and text.strip().startswith("HEALME") and is_admin:
            admin_name = sender_name or "Admin"
            custom_prompt = text.strip()[6:].strip() or None
            log.info(f"HEALME triggered by {admin_name} ({phone}) with custom prompt: {custom_prompt}")
            lifecycle_log.info(f"HEALME | TRIGGERED | by={admin_name} custom={custom_prompt is not None}")
            produce_event(self._producer, "system", "healme.triggered",
                healme_payload(phone, admin_name, "triggered", custom_prompt),
                source="daemon")
            await self._spawn_healing_session(admin_name, phone, custom_prompt)
            return

        # MASTER intercept - routes to persistent master session
        if text and text.strip().startswith("MASTER") and is_admin:
            master_prompt = text.strip()[6:].strip()  # Strip "MASTER" prefix
            if master_prompt:
                log.info(f"MASTER command from {phone}: {master_prompt[:50]}...")
                lifecycle_log.info(f"MASTER | TRIGGERED | prompt_len={len(master_prompt)}")
                produce_event(self._producer, "system", "master.triggered", {
                    "admin_phone": phone, "prompt_length": len(master_prompt),
                }, source="daemon")
                await self.sessions.inject_master_prompt(phone, master_prompt)
            else:
                log.warning(f"MASTER command with empty prompt, ignoring")
            return

        # RESTART intercept - daemon restarts the session for this chat
        if text and text.strip() == "RESTART" and is_admin:
            # Determine which chat this is (group vs individual)
            target_chat_id = chat_identifier if is_group else phone
            if not target_chat_id:
                log.warning("RESTART: No chat_id available")
                return

            # Look up session from registry
            session_data = self.registry.get(target_chat_id)
            if session_data:
                session_name = session_data.get("session_name")
                if session_name:
                    log.info(f"RESTART command for session: {session_name} (chat_id: {target_chat_id})")
                    lifecycle_log.info(f"RESTART | TRIGGERED | session={session_name} chat_id={target_chat_id}")
                    produce_event(self._producer, "sessions", "command.restart", {
                        "chat_id": target_chat_id, "session_name": session_name,
                        "source": "sms",
                    }, key=target_chat_id, source="daemon")

                    result_session = await self.sessions.restart_session(target_chat_id)

                    # Send SMS confirmation
                    if result_session:
                        self._send_sms(phone, f"[RESTART] {session_name} restarted")
                    else:
                        self._send_sms(phone, f"[RESTART] Failed to restart {session_name}")
                else:
                    log.warning(f"RESTART: No session_name in registry for {target_chat_id}")
                    self._send_sms(phone, f"[RESTART] No session found for this chat")
            else:
                log.warning(f"RESTART: No registry entry for chat_id {target_chat_id}")
                self._send_sms(phone, f"[RESTART] No session found for this chat")
            return

        # Route based on group vs individual
        if is_group:
            # Group chat - route to group session
            # chat_identifier is the canonical chat_id for groups
            if not chat_identifier:
                log.error(f"Missing chat_identifier for group message from {phone}")
                return

            # Check if a session already exists for this group
            existing_session = self.registry.get(chat_identifier)

            # Allow messages from unknown senders if:
            # 1. Sender is in a blessed tier, OR
            # 2. A session already exists for this group (meaning we've already engaged with it), OR
            # 3. A blessed contact is a participant in this group (handles email identifier case)
            if sender_tier in ("admin", "partner", "family", "favorite"):
                await self.sessions.inject_group_message(
                    chat_id=chat_identifier,
                    display_name=group_name,
                    sender_name=sender_name or phone,  # Fallback to phone/email if name unknown
                    sender_tier=sender_tier,
                    text=text,
                    attachments=attachments,
                    audio_transcription=audio_transcription,
                    thread_originator_guid=thread_originator_guid,
                    source=source,
                    message_timestamp=message_timestamp,
                    message_guid=message_guid,
                )
            elif existing_session:
                # Unknown sender but we've already engaged with this group
                log.info(f"Unknown sender {phone} in existing group session, allowing message")
                await self.sessions.inject_group_message(
                    chat_id=chat_identifier,
                    display_name=group_name,
                    sender_name=phone,  # Use the identifier as name (email or phone)
                    sender_tier="unknown",  # Mark as unknown tier for ACL purposes
                    text=text,
                    attachments=attachments,
                    audio_transcription=audio_transcription,
                    thread_originator_guid=thread_originator_guid,
                    source=source,
                    message_timestamp=message_timestamp,
                    message_guid=message_guid,
                )
            elif await asyncio.get_event_loop().run_in_executor(
                self._chat_reader._executor, self.messages._group_has_blessed_participant, chat_identifier, self.contacts
            ):
                # Unknown sender BUT a blessed contact is in this group
                log.info(f"Unknown sender {phone} but group has blessed participant, allowing message and creating session")
                await self.sessions.inject_group_message(
                    chat_id=chat_identifier,
                    display_name=group_name,
                    sender_name=phone,
                    sender_tier="unknown",
                    text=text,
                    attachments=attachments,
                    audio_transcription=audio_transcription,
                    thread_originator_guid=thread_originator_guid,
                    source=source,
                    message_timestamp=message_timestamp,
                    message_guid=message_guid,
                )
            else:
                # Ignore group messages from non-blessed contacts if no existing session
                log.info(f"Ignoring group message from non-blessed sender {sender_name or phone} (no existing session and no blessed participants)")
        elif not contact:
            # Unknown sender for individual (non-group) message - ignore
            log.info(f"Unknown sender {phone}, ignoring (not in any Claude tier group)")
            source = msg.get("source", "imessage")
            produce_event(self._producer, "messages", "message.ignored", {
                "phone": phone, "reason": "unknown_sender",
                "is_group": is_group, "chat_identifier": chat_identifier,
            }, key=f"{source}/{phone}", source=source)
        elif sender_tier in ("admin", "partner", "family", "favorite"):
            # Blessed individual: route to their SDK session
            # For individuals, phone IS the chat_id
            if not phone:
                log.error(f"Missing phone (chat_id) for individual message")
                return
            await self.sessions.inject_message(
                sender_name or phone, phone, text, sender_tier,
                attachments, audio_transcription, thread_originator_guid,
                source=source,
                message_timestamp=message_timestamp,
                message_guid=message_guid,
            )
            # Produce read receipt for iMessage chats (navigates Messages.app
            # to this chat, which marks messages as read for the sender)
            if source == "imessage":
                produce_read_receipt(self._producer, phone, source="daemon")
        else:
            # Contact exists but has unknown/unrecognized tier
            log.warning(f"Contact {sender_name} has unexpected tier '{sender_tier}', ignoring")

    async def process_reaction(self, reaction: dict):
        """Process a single incoming reaction.

        Reactions are injected into the session with context about what was reacted to.
        Only 👎 reactions require the session to respond; others are silent acknowledgments.
        """
        phone = reaction["phone"]
        emoji = reaction["emoji"]
        rowid = reaction["rowid"]
        is_removal = reaction.get("is_removal", False)
        target_text = reaction.get("target_text")
        target_is_from_me = reaction.get("target_is_from_me", False)
        is_group = reaction.get("is_group", False)
        chat_identifier = reaction.get("chat_identifier")
        source = reaction.get("source", "imessage")

        produce_event(self._producer, "messages", "reaction.received",
            sanitize_reaction_for_bus(reaction),
            key=f"{source}/{reaction.get('chat_identifier') or reaction.get('phone')}",
            source=source)

        # Skip reaction removals - they don't need to be surfaced
        if is_removal:
            log.debug(f"Ignoring reaction removal {rowid} from {phone}: {emoji}")
            produce_event(self._producer, "messages", "reaction.ignored", {
                "phone": phone, "emoji": emoji, "reason": "removal",
            }, key=f"{source}/{phone}", source=source)
            return

        # Only care about reactions to OUR messages (is_from_me on target)
        if not target_is_from_me:
            log.debug(f"Ignoring reaction {rowid} to someone else's message from {phone}")
            produce_event(self._producer, "messages", "reaction.ignored", {
                "phone": phone, "emoji": emoji, "reason": "not_from_me",
            }, key=f"{source}/{phone}", source=source)
            return

        # Lookup contact
        contact = self.contacts.lookup_identifier(phone)
        if not contact:
            log.debug(f"Ignoring reaction {rowid} from unknown contact {phone}")
            produce_event(self._producer, "messages", "reaction.ignored", {
                "phone": phone, "emoji": emoji, "reason": "unknown_sender",
            }, key=f"{source}/{phone}", source=source)
            return

        sender_name = contact["name"]
        sender_tier = contact["tier"]

        # Only process reactions from blessed tiers
        if sender_tier not in ("admin", "partner", "family", "favorite"):
            log.debug(f"Ignoring reaction {rowid} from {sender_name} (tier: {sender_tier})")
            produce_event(self._producer, "messages", "reaction.ignored", {
                "phone": phone, "emoji": emoji, "reason": "non_blessed_tier",
            }, key=f"{source}/{phone}", source=source)
            return

        # Determine chat_id (group vs individual)
        chat_id: str = chat_identifier if is_group and chat_identifier else phone

        # Build the reaction notification
        target_preview = f': "{target_text}"' if target_text else ""

        # Format reaction for injection
        reaction_text = f"""
---REACTION from {sender_name}---
{emoji} reacted to your message{target_preview}
---END REACTION---
"""

        log.info(f"Processing reaction {rowid} from {sender_name}: {emoji} on message{target_preview[:50]}")

        # Inject into session (if it exists)
        await self.sessions.inject_reaction(
            chat_id=chat_id,
            reaction_text=reaction_text.strip(),
            emoji=emoji,
            sender_name=sender_name,
            sender_tier=sender_tier,
            source=source,
        )

    async def _shutdown(self):
        """Graceful shutdown.

        Handles application-level teardown (sessions, IPC, signal listener).
        Infrastructure resources (bus, producer, connections, file handles,
        subprocesses) are cleaned up by ResourceRegistry's AsyncExitStack
        in LIFO order when _run_with_registry() exits.
        """
        # Guard against concurrent shutdown calls (e.g., SIGTERM + SIGINT race)
        if self._shutdown_flag:
            log.info("DAEMON | SHUTDOWN | Already in progress, skipping")
            return
        self._shutdown_flag = True
        log.info("DAEMON | SHUTDOWN | START")
        lifecycle_log.info("DAEMON | SHUTDOWN | START")

        # Drain message consumer: wake it to process remaining records, then cancel
        if self._consumer_task and not self._consumer_task.done():
            self._consumer_notify.set()  # Wake to drain remaining
            try:
                await asyncio.wait_for(self._consumer_task, timeout=2.0)
            except asyncio.TimeoutError:
                self._consumer_task.cancel()
                await asyncio.gather(self._consumer_task, return_exceptions=True)
            except Exception:
                self._consumer_task.cancel()
                await asyncio.gather(self._consumer_task, return_exceptions=True)
            log.info("Message consumer drained and stopped")

        # Drain task consumer
        if self._task_consumer_task and not self._task_consumer_task.done():
            self._task_consumer_notify.set()
            try:
                await asyncio.wait_for(self._task_consumer_task, timeout=2.0)
            except asyncio.TimeoutError:
                self._task_consumer_task.cancel()
                await asyncio.gather(self._task_consumer_task, return_exceptions=True)
            except Exception:
                self._task_consumer_task.cancel()
                await asyncio.gather(self._task_consumer_task, return_exceptions=True)
            log.info("Task consumer drained and stopped")

        # Cancel task supervisor
        if hasattr(self, '_task_supervisor_task') and self._task_supervisor_task and not self._task_supervisor_task.done():
            self._task_supervisor_task.cancel()
            await asyncio.gather(self._task_supervisor_task, return_exceptions=True)
            log.info("Task supervisor stopped")

        # Kill remaining ephemeral sessions
        for task_id in list(self._ephemeral_tasks.keys()):
            try:
                await self.sessions.kill_ephemeral_session(task_id)
            except Exception as e:
                log.warning(f"Failed to kill ephemeral task {task_id} on shutdown: {e}")
        self._ephemeral_tasks.clear()

        # Application-level teardown (not resource cleanup)
        try:
            await self.ipc.stop()
        except (Exception, asyncio.CancelledError) as e:
            log.error(f"Error stopping IPC: {e}")
        try:
            await self.sessions.shutdown()
        except (Exception, asyncio.CancelledError) as e:
            log.error(f"Error during session shutdown: {e}")
        finally:
            # Clear ALL stacked cancellations from SDK client anyio internals
            task = asyncio.current_task()
            if task is not None:
                while task.cancelling() > 0:
                    task.uncancel()

        # Stop signal listener (not a registry resource — it's a thread with complex shutdown)
        if self.signal_listener:
            self.signal_listener.stop()
            self.signal_listener = None

        # Stop Discord listener
        if self.discord_listener:
            self.discord_listener.stop()
            self.discord_listener = None

        # Cancel supervisor background tasks
        for task_ref in [self._dispatch_api_supervisor_task, self._metro_supervisor_task]:
            if task_ref and not task_ref.done():
                task_ref.cancel()
                try:
                    await task_ref
                except (asyncio.CancelledError, Exception):
                    pass

        # Stop child processes via supervisors
        await self.dispatch_api_supervisor.stop()
        self._close_log_fh('_dispatch_api_log_fh')
        self.dispatch_api_daemon = None

        await self.metro_supervisor.stop()
        self._close_log_fh('_metro_log_fh')
        self.metro_daemon = None

        # Wait for signal daemon
        if self.signal_daemon:
            try:
                self.signal_daemon.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.signal_daemon.kill()
            log.info("Stopped signal daemon")

        # Emit stopped event before producer is closed by registry
        produce_event(self._producer, "system", "daemon.stopped", {
            "session_count": len(self.sessions.sessions),
            "uptime_seconds": round(time.time() - self._start_time, 1),
        }, source="daemon")

        # Flush producer before registry closes it
        if hasattr(self, '_producer'):
            try:
                self._producer.flush(timeout=3.0)
                log.info("Producer flushed")
            except Exception as e:
                log.error(f"Error flushing producer: {e}")

        # Log resource status before registry cleanup
        if self._resource_registry:
            status = self._resource_registry.get_status()
            log.info(f"DAEMON | SHUTDOWN | {status['total']} resources will be cleaned up by registry")

        log.info("DAEMON | SHUTDOWN | COMPLETE (registry cleanup follows)")
        lifecycle_log.info("DAEMON | SHUTDOWN | COMPLETE")

    def _check_nightly_tasks_configured(self):
        """Warn at startup if nightly task reminders are missing.

        Called during daemon startup to catch the case where the code was
        deployed but setup-nightly-tasks.py was never run.
        """
        try:
            from assistant.reminders import load_reminders, reminders_lock
            with reminders_lock():
                data = load_reminders()
            task_ids = set()
            for r in data.get("reminders", []):
                event = r.get("event", {})
                payload = event.get("payload", {})
                tid = payload.get("task_id")
                if tid:
                    task_ids.add(tid)
            nightly_tasks = {tid for tid in task_ids if tid.startswith("nightly-")}
            if not nightly_tasks:
                log.warning(
                    "STARTUP_CHECK | No nightly task reminders found. "
                    "Run 'claude-assistant remind add --cron --event' to configure."
                )
            else:
                log.info(f"STARTUP_CHECK | Nightly task reminders verified ✓ ({len(nightly_tasks)} tasks: {', '.join(sorted(nightly_tasks))})")
        except Exception as e:
            log.warning(f"STARTUP_CHECK | Could not verify nightly reminders: {e}")


    async def run(self):
        """Main async loop."""
        log.info("=" * 60)
        log.info("Claude Assistant Manager starting (SDK backend)...")
        log.info(f"Polling interval: {POLL_INTERVAL}s")
        log.info(f"Starting from ROWID: {self.last_rowid}")
        log.info("=" * 60)
        lifecycle_log.info(f"DAEMON | START | rowid={self.last_rowid}")
        produce_event(self._producer, "system", "daemon.started", {
            "rowid": self.last_rowid,
            "session_count": len(self.sessions.sessions),
        }, source="daemon")
        # Startup bus writability canary (startup-only; mid-run bus health
        # inferred from absence of produce_event warnings in logs)
        try:
            produce_event(self._producer, "system", "health.bus_check",
                {"status": "ok"}, source="health")
            log.info("bus: OK")
        except Exception:
            log.warning("bus: FAILED — diagnostic events will be log-only")

        async with ResourceRegistry() as resource_registry:
            self._resource_registry = resource_registry
            await self._run_with_registry(resource_registry)

    async def _run_with_registry(self, resource_registry: ResourceRegistry):
        """Main loop body wrapped in ResourceRegistry for lifecycle management."""
        # ── Register resources created in __init__ ──
        # Bus and producer have their own connections — register for tracking + clean shutdown
        resource_registry.register('bus', self._bus, self._bus.close)
        resource_registry.register('producer', self._producer, self._producer.close)
        resource_registry.register('consumer_runner', self._consumer_runner, self._consumer_runner.stop)

        # Note: dispatch_api and metro log file handles + subprocesses are now
        # managed by ChildSupervisor instances. They are registered in the
        # supervisor startup section below (after await supervisor.start()).

        # ── Create ManagedSQLiteReader for chat.db (single reader, dedicated thread) ──
        self._chat_reader = ManagedSQLiteReader(
            'chat.db', str(MESSAGES_DB), resource_registry,
            pragmas={'read_uncommitted': '1'},
        )
        # Inject managed connection into MessagesReader — replaces its internal connection
        self.messages.set_managed_connection(self._chat_reader.connection)
        log.info(f"RESOURCES | chat.db reader on dedicated thread | "
                 f"{resource_registry.get_open_count()} resources tracked")

        # Ensure global settings.json symlink is correct
        global_settings_target = HOME / "dispatch" / "config" / "global-settings.json"
        global_settings_link = HOME / ".claude" / "settings.json"
        if global_settings_target.exists():
            if global_settings_link.is_symlink():
                if global_settings_link.resolve() != global_settings_target.resolve():
                    log.info(f"Fixing global settings.json symlink -> {global_settings_target}")
                    global_settings_link.unlink()
                    global_settings_link.symlink_to(global_settings_target)
            elif global_settings_link.exists():
                log.warning(f"Global settings.json is a regular file, replacing with symlink")
                global_settings_link.unlink()
                global_settings_link.symlink_to(global_settings_target)
            else:
                log.info(f"Creating global settings.json symlink -> {global_settings_target}")
                global_settings_link.symlink_to(global_settings_target)

        # Setup signal handlers
        loop = asyncio.get_event_loop()
        loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(self._shutdown()))
        loop.add_signal_handler(signal.SIGINT, lambda: asyncio.create_task(self._shutdown()))

        # Register consumer executor for clean shutdown
        resource_registry.register('consumer_executor', self._consumer_executor,
            lambda: self._consumer_executor.shutdown(wait=False))

        # Start audit consumers (background thread)
        self._consumer_thread = self._start_consumer_thread()

        # Build the unified dependency-health runner (chrome_control, signal_cli,
        # bus_consumers, disk, fd_leak). Probes/recoveries run off the main loop;
        # the runner is ticked from the poll loop below. No resources to release —
        # it captures the manager and shells out per-cycle — so no registry entry.
        try:
            from assistant.dependency_checks import build_default_registry
            self._dep_health_runner = build_default_registry(self)
            log.info(f"DEP_HEALTH | registered checks: {', '.join(self._dep_health_runner.names())}")
        except Exception as e:
            log.error(f"DEP_HEALTH | failed to build registry: {e}")
            self._dep_health_runner = None

        # Start message-router consumer (async task — processes messages from bus)
        self._consumer_task = asyncio.create_task(
            self._run_message_consumer(), name="message-consumer"
        )
        self._consumer_task.add_done_callback(self._on_consumer_done)

        # Clean up orphaned ephemeral sessions BEFORE starting task consumer
        # to prevent race where new tasks get cleaned up as orphans
        await self._cleanup_orphaned_ephemeral_sessions()

        # Start task consumer (async task — processes task.requested from bus)
        self._task_consumer_task = asyncio.create_task(
            self._run_task_consumer(), name="task-consumer"
        )
        self._task_consumer_task.add_done_callback(self._on_task_consumer_done)

        # Register task consumer executor for clean shutdown
        resource_registry.register('task_consumer_executor', self._task_consumer_executor,
            lambda: self._task_consumer_executor.shutdown(wait=False))

        # Start ephemeral task supervisor (async task — checks for timeouts)
        self._task_supervisor_task = asyncio.create_task(
            self._supervise_ephemeral_tasks(), name="task-supervisor"
        )

        # Start periodic WAL checkpoint (separate from poll cycle to avoid blocking)
        self._wal_checkpoint_task = asyncio.create_task(
            self._periodic_wal_checkpoint(), name="wal-checkpoint"
        )

        # Start IPC server
        await self.ipc.start()

        # Lazy loading: sessions created on first message (not pre-warmed)
        log.info("Lazy loading enabled - sessions will be created on first message")

        # Recreate sessions that were active during previous shutdown
        # Sessions resume via stored session_id (native compaction handles context)
        recreated = await self.sessions.recreate_active_sessions()
        if recreated:
            log.info(f"Recreated {recreated} active sessions from previous shutdown")

        # Start Signal daemon and listener (if not disabled)
        if SIGNAL_ENABLED:
            log.info("Starting Signal integration...")
            self.signal_daemon = self._spawn_signal_daemon()
            if self.signal_daemon:
                self._start_signal_listener()
                # Register signal resources
                if self.signal_daemon:
                    resource_registry.register(
                        'signal_daemon', self.signal_daemon, self.signal_daemon.terminate,
                    )
                fh = getattr(self, '_signal_log_fh', None)
                if fh is not None:
                    resource_registry.register('signal_log_fh', fh, fh.close)
        else:
            log.info("Signal integration disabled via DISABLE_SIGNAL env var")

        # Start test message watcher
        log.info("Starting test message watcher...")
        self.test_watcher = TestMessageWatcher(self.test_queue)
        self.test_watcher.start()

        # Start Discord listener (if configured)
        self._start_discord_listener()

        # Start child process supervisors (dispatch-api + metro)
        log.info("Starting child process supervisors...")
        api_result = await self.dispatch_api_supervisor.start()
        self.dispatch_api_daemon = self.dispatch_api_supervisor.proc  # Compat ref
        log.info(f"Dispatch API startup: {api_result.value}")
        if self.dispatch_api_daemon:
            resource_registry.register('dispatch_api_daemon', self.dispatch_api_daemon, self.dispatch_api_daemon.terminate)

        metro_result = await self.metro_supervisor.start()
        self.metro_daemon = self.metro_supervisor.proc  # Compat ref
        log.info(f"Metro startup: {metro_result.value}")

        # Start supervisor background tasks (10s liveness monitoring)
        self._dispatch_api_supervisor_task = asyncio.create_task(
            self.dispatch_api_supervisor.run_forever(),
            name="supervisor-dispatch-api",
        )
        self._metro_supervisor_task = asyncio.create_task(
            self.metro_supervisor.run_forever(),
            name="supervisor-metro",
        )

        # Start auth dialog monitor (Phase 1: detection + logging, dry_run only)
        try:
            from assistant.auth_dialog import AuthDialogMonitor, load_default_config
            auth_cfg = load_default_config()

            async def _auth_dialog_escalate(msg: str, dialog_info: dict | None = None):
                """Route auth dialog escalations — log, bus event, and notify admin."""
                log.warning(f"AUTH_DIALOG_ESCALATE | {msg}")
                escalation_payload = {"message": msg}
                if dialog_info:
                    escalation_payload["dialog"] = {
                        "app": dialog_info.get("app_name", ""),
                        "action": dialog_info.get("action", ""),
                        "dialog_type": dialog_info.get("dialog_type", ""),
                        "dialog_id": dialog_info.get("dialog_id", ""),
                    }
                produce_event(
                    self._producer, "system", "auth_dialog.escalation",
                    escalation_payload, source="auth-dialog-monitor",
                )

                # Save pending dialog info for approve/deny CLI
                if dialog_info:
                    import json
                    pending_path = STATE_DIR / "auth_dialog_pending.json"
                    try:
                        pending = json.loads(pending_path.read_text())
                    except (FileNotFoundError, json.JSONDecodeError):
                        pending = {}
                    did = dialog_info.get("dialog_id", "")
                    if did and did not in pending:
                        pending[did] = dialog_info
                        pending_path.write_text(json.dumps(pending, indent=2))

                # Inject into admin session so the AI can present and handle the reply
                try:
                    escalation_chat_id = auth_cfg.resolution.escalation_chat_id
                    if escalation_chat_id:
                        did = dialog_info.get("dialog_id", "?") if dialog_info else "?"
                        app_name = dialog_info.get("app_name", "?") if dialog_info else "?"
                        action_desc = dialog_info.get("action", "?") if dialog_info else "?"
                        dialog_type = dialog_info.get("dialog_type", "?") if dialog_info else "?"
                        inject_msg = (
                            f"[SYSTEM AUTH_DIALOG_ESCALATION]\n"
                            f"A macOS auth dialog needs admin attention.\n"
                            f"  App: {app_name}\n"
                            f"  Action: {action_desc}\n"
                            f"  Type: {dialog_type}\n"
                            f"  Dialog ID: {did}\n"
                            f"  Reason: {msg}\n\n"
                            f"Present this to the user and ask what to do. They can:\n"
                            f"  approve — run: ~/dispatch/bin/auth-dialog-approve approve {did}\n"
                            f"  deny — run: ~/dispatch/bin/auth-dialog-approve deny {did}\n"
                            f"  always approve — run: ~/dispatch/bin/auth-dialog-approve always {did}\n"
                            f"Show the user the app name and action clearly. "
                            f"If they say 'always', that adds a permanent rule to auto-approve this pattern."
                        )
                        # Inject via CLI subprocess (Manager doesn't own _cmd_inject)
                        import subprocess as sp
                        cli = str(Path.home() / "dispatch" / "bin" / "claude-assistant")
                        sp.Popen(
                            [cli, "inject-prompt", "--admin",
                             escalation_chat_id, inject_msg],
                            stdout=sp.DEVNULL, stderr=sp.DEVNULL,
                        )
                except Exception as e:
                    log.error(f"AUTH_DIALOG | Failed to notify admin: {e}")

            self._auth_dialog_monitor = AuthDialogMonitor(
                config=auth_cfg,
                producer=self._producer,
                escalate_fn=_auth_dialog_escalate,
            )
            asyncio.create_task(
                self._auth_dialog_monitor.start(),
                name="auth-dialog-monitor-start",
            )
        except Exception as e:
            log.warning(f"AUTH_DIALOG | Failed to start monitor: {e}")

        # Track last health check time — first check 30s after start (not full 300s wait)
        last_health_check = time.time() - 270  # 300 - 30 = triggers in 30s
        HEALTH_CHECK_INTERVAL = 300  # 5 minutes

        # Track fast health check (Tier 1: regex-based fatal error detection)
        last_fast_health = time.time()
        FAST_HEALTH_INTERVAL = 60  # 1 minute

        # Dependency-health runner tick — the runner internally rate-limits each
        # check to its own interval_s (chrome_control 90s, signal_cli/bus/disk/fd
        # 300s) and runs probes/recoveries off the main loop. We just poke it
        # every ~15s so a due check fires within ~15s of its interval elapsing.
        # First tick ~10s after start.
        last_dep_health_tick = time.time() - (15 - 10)
        DEP_HEALTH_TICK_INTERVAL = 15

        # Track blocked-session watchdog — nudges a session that's gone idle
        # with an outstanding commitment. ~75s cadence so a stuck session is
        # caught + nudged (→ escalation) well within the 5-min SLO. First check
        # ~20s after start.
        last_stuck_check = time.time() - (75 - 20)
        STUCK_CHECK_INTERVAL = 75  # 1.25 minutes

        # Track last idle check time
        last_idle_check = time.time()
        IDLE_CHECK_INTERVAL = 300  # Check for idle sessions every 5 minutes
        IDLE_TIMEOUT_HOURS = 2.0  # Kill sessions idle for more than 2 hours

        # Track last reminder check time
        last_reminder_check = time.time()
        REMINDER_CHECK_INTERVAL = 5  # Check reminders every 5 seconds

        # Track last iMessage tickle — works around App Nap / IMDPersistenceAgent
        # delaying chat.db writes when Messages.app has been backgrounded.
        # First tickle fires ~5s after start so any backlog flushes early.
        last_imessage_tickle = time.time() - 25
        IMESSAGE_TICKLE_INTERVAL = 30

        # Escalated tickle: when the 30s light tickle isn't enough to wake
        # IMDPersistenceAgent (observed 2026-07-31 — 8min chat.db write delay
        # after Messages.app sat idle for 17h), fire launchctl kickstart +
        # brief foreground of Messages.app. Only trigger when BOTH:
        #   - last_rowid hasn't advanced for >5min (chat.db appears frozen)
        #   - Messages.app has been idle >1hr (no inbound activity)
        # Debounced so we don't loop-kick every poll iteration.
        _now_init = time.time()
        last_rowid_change_ts = _now_init
        last_rowid_snapshot = self.last_rowid
        last_inbound_activity_ts = _now_init
        last_escalated_tickle_ts = 0.0  # never fired yet
        STALE_CHATDB_THRESHOLD = 300      # 5 min without a new rowid
        MESSAGES_IDLE_THRESHOLD = 3600    # 1 hr with no inbound
        ESCALATED_TICKLE_DEBOUNCE = 900   # don't re-escalate for 15 min

        # Nightly consolidation is now handled by cron reminders firing
        # task.requested events. See scripts/setup-nightly-tasks.py.
        # Verify nightly task reminders exist at startup
        self._check_nightly_tasks_configured()

        self._shutdown_flag = False
        spurious_cancel_count = 0
        imessage_error_logged = False
        last_poll_log_time = 0  # Telemetry: log poll status every 5 minutes
        POLL_LOG_INTERVAL = 300  # 5 minutes
        last_iteration_end = time.time()  # Track time between poll iterations
        while not self._shutdown_flag:
            try:
                # Track poll gap - should be ~100ms, drift indicates CPU pressure
                poll_gap_ms = (time.time() - last_iteration_end) * 1000
                perf.timing("poll_gap_ms", poll_gap_ms, sample_rate=10, component="daemon")
                if poll_gap_ms > 500:  # Warn if gap > 500ms (5x expected)
                    log.warning(f"POLL_GAP | gap={poll_gap_ms:.0f}ms | expected ~100ms")

                # Run blocking SQLite poll in executor
                poll_start = time.time()
                try:
                    messages = await loop.run_in_executor(
                        self._chat_reader._executor, self.messages.get_new_messages, self.last_rowid
                    )
                    poll_duration_ms = (time.time() - poll_start) * 1000
                    # Perf: log poll cycle (sample every 10th to avoid log bloat)
                    perf.timing("poll_cycle_ms", poll_duration_ms, sample_rate=10, component="daemon")
                    if messages:
                        perf.incr("messages_read", count=len(messages), component="daemon", source="imessage")
                    if imessage_error_logged:
                        log.info("iMessage chat.db access restored")
                        imessage_error_logged = False
                    # Telemetry: log poll status periodically or if messages found
                    now = time.time()
                    if messages or (now - last_poll_log_time > POLL_LOG_INTERVAL):
                        log.info(f"POLL_TELEMETRY | rowid={self.last_rowid} | found={len(messages)} | duration={poll_duration_ms:.1f}ms")
                        last_poll_log_time = now
                except Exception as db_err:
                    if "unable to open database" in str(db_err) or "authorization denied" in str(db_err):
                        if not imessage_error_logged:
                            log.error(f"iMessage chat.db unavailable (FDA required): {db_err}")
                            imessage_error_logged = True
                        messages = []
                        # Backoff to avoid hammering unavailable database every 100ms
                        await asyncio.sleep(5)
                        continue
                    else:
                        raise

                # Log batch size - bursts indicate sync backlog
                if messages:
                    batch_size = len(messages)
                    perf.gauge("messages_batch_size", batch_size, component="daemon", source="imessage")
                    if batch_size > 5:
                        log.warning(f"BATCH_BURST | count={batch_size} | possible sync backlog")

                for msg in messages:
                    msg["source"] = "imessage"  # Tag source
                    # Log message staleness (time from message creation to discovery)
                    if msg.get("timestamp"):
                        staleness_ms = (time.time() - msg["timestamp"].timestamp()) * 1000
                        perf.timing("message_staleness_ms", staleness_ms, component="daemon", source="imessage")
                        if staleness_ms > 30000:  # Warn if >30s stale
                            log.warning(f"STALENESS | rowid={msg['rowid']} | staleness={staleness_ms:.0f}ms")
                    # Produce to bus (PRIMARY delivery path) and advance state
                    raw_key = msg.get("chat_identifier") or msg.get("phone")
                    produce_event(self._producer, "messages", "message.received",
                        sanitize_msg_for_bus(msg),
                        key=f"imessage/{raw_key}",
                        source="imessage")
                    self._save_state(msg["rowid"])

                # Wake message consumer if any messages were produced
                if messages:
                    self._consumer_notify.set()

                # Poll for reactions (same rowid sequence as messages)
                reactions = await loop.run_in_executor(
                    self._chat_reader._executor, self.messages.get_new_reactions, self.last_rowid
                )

                for reaction in reactions:
                    reaction["source"] = "imessage"
                    try:
                        await self.process_reaction(reaction)
                        self._save_state(reaction["rowid"])
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        log.error(f"Failed to process reaction {reaction['rowid']}: {e}")
                        self._save_state(reaction["rowid"])

                # Process Signal messages from queue → produce to bus
                signal_count = 0
                while not self.signal_queue.empty():
                    try:
                        signal_msg = self.signal_queue.get_nowait()
                        sig_source = signal_msg.get("source", "signal")
                        sig_raw_key = signal_msg.get("chat_identifier") or signal_msg.get("phone")
                        produce_event(self._producer, "messages", "message.received",
                            sanitize_msg_for_bus(signal_msg),
                            key=f"{sig_source}/{sig_raw_key}",
                            source=sig_source)
                        signal_count += 1
                    except queue.Empty:
                        break
                if signal_count > 0:
                    perf.incr("messages_read", count=signal_count, component="daemon", source="signal")
                    self._consumer_notify.set()

                # Process test messages from queue → produce to bus
                test_count = 0
                while not self.test_queue.empty():
                    try:
                        test_msg = self.test_queue.get_nowait()
                        test_source = test_msg.get("source", "test")
                        test_raw_key = test_msg.get("chat_identifier") or test_msg.get("phone")
                        produce_event(self._producer, "messages", "message.received",
                            sanitize_msg_for_bus(test_msg),
                            key=f"{test_source}/{test_raw_key}",
                            source=test_source)
                        test_count += 1
                    except queue.Empty:
                        break
                if test_count > 0:
                    self._consumer_notify.set()

                # Process Discord messages from queue → produce to bus
                discord_count = 0
                while not self.discord_queue.empty():
                    try:
                        discord_msg = self.discord_queue.get_nowait()
                        disc_source = discord_msg.get("source", "discord")
                        disc_raw_key = discord_msg.get("chat_identifier") or discord_msg.get("phone")
                        produce_event(self._producer, "messages", "message.received",
                            sanitize_msg_for_bus(discord_msg),
                            key=f"{disc_source}/{disc_raw_key}", source=disc_source)
                        discord_count += 1
                    except queue.Empty:
                        break
                if discord_count:
                    self._consumer_notify.set()
                    perf.incr("messages_read", count=discord_count, component="daemon", source="discord")

                # Flush rowid state to disk once per poll cycle (batched)
                self._flush_state()

                # Check for due reminders
                if time.time() - last_reminder_check > REMINDER_CHECK_INTERVAL:
                    await self.reminders.process_due_reminders()
                    last_reminder_check = time.time()

                # Track inbound activity + rowid movement for the escalated
                # tickle heuristic. If either messages OR reactions arrived, or
                # last_rowid advanced (e.g. from _save_state above), refresh
                # both trackers.
                _now_tick = time.time()
                if messages or reactions:
                    last_inbound_activity_ts = _now_tick
                if self.last_rowid != last_rowid_snapshot:
                    last_rowid_snapshot = self.last_rowid
                    last_rowid_change_ts = _now_tick

                # Periodic Messages.app tickle (fire-and-forget) — keeps
                # IMDPersistenceAgent flushing chat.db despite App Nap.
                if _now_tick - last_imessage_tickle > IMESSAGE_TICKLE_INTERVAL:
                    asyncio.create_task(self._imessage_tickle(), name="imessage-tickle")
                    last_imessage_tickle = _now_tick

                # Escalated tickle: only when chat.db appears frozen AND
                # Messages.app has been idle long enough that App Nap likely
                # kicked in. Debounced so a single stale window fires once.
                chatdb_stale_s = _now_tick - last_rowid_change_ts
                messages_idle_s = _now_tick - last_inbound_activity_ts
                since_last_escalation = _now_tick - last_escalated_tickle_ts
                if (chatdb_stale_s > STALE_CHATDB_THRESHOLD
                        and messages_idle_s > MESSAGES_IDLE_THRESHOLD
                        and since_last_escalation > ESCALATED_TICKLE_DEBOUNCE):
                    log.warning(
                        f"IMESSAGE_TICKLE_ESCALATED | firing | "
                        f"chatdb_stale={chatdb_stale_s:.0f}s "
                        f"messages_idle={messages_idle_s:.0f}s "
                        f"last_rowid={self.last_rowid}"
                    )
                    try:
                        produce_event(
                            self._producer, "system", "imessage.tickle_escalated",
                            {
                                "chatdb_stale_seconds": round(chatdb_stale_s, 1),
                                "messages_idle_seconds": round(messages_idle_s, 1),
                                "last_rowid": self.last_rowid,
                            },
                            source="daemon",
                        )
                    except Exception as e:
                        log.debug(f"IMESSAGE_TICKLE_ESCALATED | bus produce failed: {e}")
                    asyncio.create_task(
                        self._imessage_tickle_escalated(),
                        name="imessage-tickle-escalated",
                    )
                    last_escalated_tickle_ts = _now_tick

                # Fast health check (Tier 1: regex-based fatal error detection)
                if time.time() - last_fast_health > FAST_HEALTH_INTERVAL:
                    try:
                        await self.sessions.fast_health_check()
                    except Exception as e:
                        log.error(f"Fast health check failed: {e}")
                    last_fast_health = time.time()

                # Dependency-health runner tick (non-blocking — fires due checks
                # as fire-and-forget tasks; each check runs its probe/recover off
                # the main loop with a per-check timeout + overlap guard).
                if time.time() - last_dep_health_tick > DEP_HEALTH_TICK_INTERVAL:
                    if self._dep_health_runner is not None:
                        try:
                            self._dep_health_runner.tick()
                        except Exception as e:
                            log.error(f"DEP_HEALTH | tick failed: {e}")
                    last_dep_health_tick = time.time()

                # blocked-session watchdog (background, non-blocking, ~75s)
                if time.time() - last_stuck_check > STUCK_CHECK_INTERVAL:
                    if not self._stuck_check_running:
                        self._stuck_check_running = True
                        async def _stuck_check_guarded():
                            try:
                                await asyncio.wait_for(
                                    self._run_stuck_session_check(),
                                    timeout=30,
                                )
                            except asyncio.TimeoutError:
                                log.error("STUCK_CHECK | timed out after 30s — clearing guard")
                            except asyncio.CancelledError:
                                # SDK anyio cancel-scope leak — recover unless shutting down.
                                task = asyncio.current_task()
                                if task is not None:
                                    while task.cancelling() > 0:
                                        task.uncancel()
                                if self._shutdown_flag:
                                    raise
                                log.warning("STUCK_CHECK | CancelledError (SDK cancel scope leak), recovered")
                            except Exception as e:
                                log.error(f"STUCK_CHECK | error: {e}")
                            finally:
                                self._stuck_check_running = False
                        asyncio.create_task(
                            _stuck_check_guarded(), name="stuck-session-check"
                        )
                    last_stuck_check = time.time()

                # Periodic health check (runs in background, non-blocking)
                if time.time() - last_health_check > HEALTH_CHECK_INTERVAL:
                    if not self._health_check_running:
                        self._health_check_running = True
                        async def _health_with_timeout():
                            try:
                                await asyncio.wait_for(
                                    self._run_health_checks(),
                                    timeout=120,  # 2 min max
                                )
                            except asyncio.TimeoutError:
                                log.error("Health check timed out after 120s — force-clearing lock")
                            finally:
                                self._health_check_running = False
                        asyncio.create_task(
                            _health_with_timeout(),
                            name="health-check-background"
                        )
                    else:
                        log.debug("Health check still running, skipping this cycle")
                    last_health_check = time.time()

                # Periodic idle session check
                if time.time() - last_idle_check > IDLE_CHECK_INTERVAL:
                    try:
                        await self.sessions.check_idle_sessions(IDLE_TIMEOUT_HOURS)
                    except asyncio.CancelledError:
                        # SDK client disconnect can leak CancelledError via anyio
                        # cancel scopes. Clear ALL stacked cancellations.
                        task = asyncio.current_task()
                        if task is not None:
                            while task.cancelling() > 0:
                                task.uncancel()
                        if self._shutdown_flag:
                            raise
                        log.warning("CancelledError during idle check (SDK cancel scope leak), recovered")
                    except Exception as e:
                        log.error(f"Error during idle session check: {e}")
                    last_idle_check = time.time()

                await asyncio.sleep(POLL_INTERVAL)
                last_iteration_end = time.time()  # Update for next poll_gap measurement
                spurious_cancel_count = 0  # Reset on successful iteration

            except asyncio.CancelledError:
                if self._shutdown_flag:
                    log.info("CancelledError in main loop, shutting down...")
                    break
                # Clear ALL stacked cancellations from SDK anyio cancel scopes
                task = asyncio.current_task()
                cancel_depth = 0
                if task is not None:
                    while task.cancelling() > 0:
                        task.uncancel()
                        cancel_depth += 1
                spurious_cancel_count += 1
                log.warning(f"Spurious CancelledError in main loop (#{spurious_cancel_count}), "
                            f"cleared {cancel_depth} cancellation(s)")
                if spurious_cancel_count >= 500:
                    log.error("Too many spurious CancelledErrors, shutting down to avoid infinite loop")
                    await self._shutdown()
                    break
                # Small yield to break tight loops if cancel keeps firing
                await asyncio.sleep(0.01)
                continue
            except Exception as e:
                log.error(f"Error in main loop: {e}")
                await asyncio.sleep(5)  # Back off on error


def main():
    # Validate config before anything else
    from assistant import config
    config.load()
    config.require("owner.name")
    config.require("owner.phone")

    # Ensure directories exist
    (ASSISTANT_DIR / "state").mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    # Honor a previously-persisted auth mode flip (state/auth_mode.json).
    # If the last run ended in api_key mode (OAuth quota fallback), promote
    # ANTHROPIC_API_KEY_FALLBACK into ANTHROPIC_API_KEY before spawning any
    # SDK subprocesses so they inherit the right credentials.
    from assistant import auth_mode
    active_mode = auth_mode.apply_at_startup()
    print(f"[startup] SDK auth mode: {active_mode}")

    manager = Manager()
    asyncio.run(manager.run())


if __name__ == "__main__":
    main()
