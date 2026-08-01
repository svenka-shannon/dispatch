"""
SDKBackend: Manages all Claude Agent SDK sessions.

Replaces the tmux-based SessionManager with ClaudeSDKClient-based sessions.
All session operations go through this backend.
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from assistant.common import (
    HOME,
    SKILLS_DIR,
    TRANSCRIPTS_DIR,
    UV,
    MASTER_SESSION,
    MASTER_TRANSCRIPT_DIR,
    ensure_transcript_dir,
    get_session_name,
    get_group_session_name_from_participants,
    normalize_chat_id,
    wrap_sms,
    wrap_admin,
    wrap_group_message,
    format_message_body,
    get_reply_chain,
)
from assistant.health import get_transcript_entries_since, check_fatal_regex, check_deep_haiku, HaikuCallFailed
from assistant.sdk_session import SDKSession
from assistant.quota_manager import QuotaManager, HaikuCircuitBreaker
from assistant import perf
from assistant.bus_helpers import (
    produce_event, produce_session_event,
    session_injected_payload, health_check_payload,
    vision_payload,
    compaction_user_waiting_payload,
    CheckContext, haiku_verdict_payload, circuit_breaker_payload,
)

log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# Fire-and-forget task tracking
# ──────────────────────────────────────────────────────────────

_background_tasks: set[asyncio.Task] = set()


def _fire_and_forget(coro, *, name: str | None = None) -> asyncio.Task:
    """Create a task with automatic cleanup and exception logging.

    Prevents exceptions from being silently lost in fire-and-forget patterns.
    """
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)

    def _task_done(t: asyncio.Task):
        _background_tasks.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            log.error(f"Background task {t.get_name()!r} failed: {exc}")

    task.add_done_callback(_task_done)
    return task


# ──────────────────────────────────────────────────────────────
# Gemini Vision Analysis (async image understanding)
# ──────────────────────────────────────────────────────────────

# Image extensions that Gemini can analyze
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"}

GEMINI_CLI = Path.home() / ".claude" / "skills" / "gemini" / "scripts" / "gemini"


async def analyze_image_with_gemini(image_path: str, message_context: str | None = None) -> Optional[str]:
    """Analyze an image using Gemini Vision.

    Runs in background, returns description or None on failure.
    Uses gemini-3-pro-image-preview for best visual understanding.

    Args:
        image_path: Path to the image file
        message_context: Optional text that was sent with the image (provides context)
    """
    path = Path(image_path)
    if not path.exists():
        log.warning(f"Gemini vision: image not found: {image_path}")
        return None

    # Check if it's an image
    suffix = path.suffix.lower()
    if suffix not in IMAGE_EXTENSIONS:
        log.debug(f"Gemini vision: not an image: {image_path}")
        return None

    # Convert HEIC to JPEG if needed (Gemini doesn't support HEIC directly)
    actual_path = image_path
    heic_temp_path: Path | None = None
    if suffix in {".heic", ".heif"}:
        try:
            import tempfile
            jpeg_path = Path(tempfile.gettempdir()) / f"{path.stem}_converted.jpg"
            proc = await asyncio.create_subprocess_exec(
                "sips", "-s", "format", "jpeg", str(path), "--out", str(jpeg_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            if jpeg_path.exists():
                actual_path = str(jpeg_path)
                heic_temp_path = jpeg_path
                log.debug(f"Gemini vision: converted HEIC to JPEG: {jpeg_path}")
            else:
                log.warning(f"Gemini vision: HEIC conversion failed for {image_path}")
                return None
        except Exception as e:
            log.warning(f"Gemini vision: HEIC conversion error: {e}")
            return None

    # Build context-aware prompt
    if message_context and message_context.strip() and message_context != "(no text)":
        # Check if it looks like multi-line conversation context vs single message
        if "\n" in message_context:
            prompt = f"""Recent conversation context:
{message_context}

Now an image was shared. Briefly describe what you see in this image, considering the conversation context above. Be concise but capture key details. 2-3 sentences max."""
        else:
            prompt = f"""The sender shared this image with the message: "{message_context}"

Briefly describe what you see in this image, keeping the sender's context in mind. Be concise but capture key details. 2-3 sentences max."""
    else:
        prompt = "Briefly describe what you see in this image. Be concise but capture key details - who/what is shown, the setting, and any notable elements. 2-3 sentences max."

    # Call Gemini CLI (with 1 retry for transient failures)
    import time
    max_attempts = 2
    try:
        for attempt in range(max_attempts):
            try:
                gemini_start = time.perf_counter()
                proc = await asyncio.create_subprocess_exec(
                    str(GEMINI_CLI),
                    "-m", "gemini-3-pro-image-preview",
                    "-i", actual_path,
                    prompt,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60.0)
                gemini_ms = (time.perf_counter() - gemini_start) * 1000
                perf.timing("gemini_vision_ms", gemini_ms, component="daemon")

                if proc.returncode == 0 and stdout:
                    description = stdout.decode().strip()
                    if attempt > 0:
                        log.info(f"Gemini vision: succeeded on retry for {path.name}")
                    log.info(f"Gemini vision: analyzed {path.name} ({len(description)} chars)")
                    return description
                else:
                    error_msg = stderr.decode() if stderr else "no output"
                    if attempt < max_attempts - 1:
                        log.warning(f"Gemini vision failed (attempt {attempt + 1}), retrying: {error_msg}")
                        await asyncio.sleep(2.0)  # backoff before retry
                        continue
                    log.warning(f"Gemini vision failed: {error_msg}")
                    perf.error("gemini_vision_failed", component="daemon")
                    return None
            except asyncio.TimeoutError:
                if attempt < max_attempts - 1:
                    log.warning(f"Gemini vision: timeout (attempt {attempt + 1}), retrying: {image_path}")
                    await asyncio.sleep(2.0)  # backoff before retry
                    continue
                log.warning(f"Gemini vision: timeout for {image_path}")
                return None
            except Exception as e:
                if attempt < max_attempts - 1:
                    log.warning(f"Gemini vision error (attempt {attempt + 1}), retrying: {e}")
                    await asyncio.sleep(2.0)  # backoff before retry
                    continue
                log.warning(f"Gemini vision error: {e}")
                return None
        return None  # unreachable, but satisfies type checker
    finally:
        # Clean up HEIC temp file to prevent /tmp accumulation
        if heic_temp_path and heic_temp_path.exists():
            try:
                heic_temp_path.unlink()
            except Exception:
                pass

# Lifecycle logger (matches current format)
lifecycle_log = logging.getLogger("lifecycle")


class SessionRegistry:
    """Persistent registry mapping chat_id to session metadata.

    Unified registry - replaces both the old SessionRegistry in manager.py
    and the _load_registry/_save_registry in cli.py.
    """

    def __init__(self, registry_file: Path):
        self._file = registry_file
        self._registry: Dict[str, Dict[str, Any]] = {}
        self._dirty = False
        self._last_save_time = 0.0
        self._save_interval = 1.0  # Debounce: at most one save per second for frequent updates
        self._load()

    def _load(self):
        if self._file.exists():
            try:
                self._registry = json.loads(self._file.read_text())
                log.info(f"Loaded {len(self._registry)} sessions from registry")
            except Exception as e:
                log.error(f"Failed to load session registry: {e}")
                self._registry = {}

    def _save(self):
        import fcntl
        self._file.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write with file locking (bug #12 fix: CLI and daemon race)
        tmp_path = self._file.with_suffix('.tmp')
        with open(tmp_path, 'w') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.write(json.dumps(self._registry, indent=2, default=str))
        tmp_path.rename(self._file)  # Atomic rename

    def register(self, chat_id: str, session_name: str, **metadata) -> Dict[str, Any]:
        if not chat_id:
            raise ValueError("chat_id cannot be empty")
        existing = self._registry.get(chat_id, {})
        session_data = {
            "chat_id": chat_id,
            "session_name": session_name,
            "created_at": existing.get("created_at", datetime.now().isoformat()),
            "updated_at": datetime.now().isoformat(),
            **metadata,
        }
        self._registry[chat_id] = session_data
        self._save_debounced()
        return session_data

    def get(self, chat_id: str) -> Optional[Dict[str, Any]]:
        return self._registry.get(chat_id)

    def get_by_session_name(self, session_name: str) -> Optional[Dict[str, Any]]:
        for data in self._registry.values():
            if data.get("session_name") == session_name:
                return data
        return None

    def all(self) -> Dict[str, Dict[str, Any]]:
        return self._registry.copy()

    def remove(self, chat_id: str):
        if chat_id in self._registry:
            del self._registry[chat_id]
            self._save()

    def update_session_id(self, chat_id: str, session_id: str | None):
        """Update the SDK session_id for resume support."""
        if chat_id in self._registry:
            self._registry[chat_id]["session_id"] = session_id
            self._registry[chat_id]["updated_at"] = datetime.now().isoformat()
            self._save()

    def _save_debounced(self):
        """Save only if enough time has elapsed since last save. Otherwise mark dirty."""
        import time as _time
        now = _time.monotonic()
        if now - self._last_save_time >= self._save_interval:
            self._save()
            self._last_save_time = now
            self._dirty = False
        else:
            self._dirty = True

    def flush(self):
        """Force save if there are pending dirty writes."""
        if self._dirty:
            self._save()
            self._dirty = False

    def update_last_message_time(self, chat_id: str):
        """Update last_message_time for idle tracking. Uses debounced save."""
        if chat_id in self._registry:
            self._registry[chat_id]["last_message_time"] = datetime.now().isoformat()
            self._registry[chat_id]["updated_at"] = datetime.now().isoformat()
            self._save_debounced()

    def mark_was_active(self, chat_id: str):
        """Mark a session as active before shutdown, so it gets recreated on startup."""
        if chat_id in self._registry:
            self._registry[chat_id]["was_active"] = True
            self._registry[chat_id]["updated_at"] = datetime.now().isoformat()
            self._save()

    def clear_was_active(self, chat_id: str):
        """Clear the was_active flag after successful recreation."""
        if chat_id in self._registry:
            self._registry[chat_id].pop("was_active", None)
            self._save()


class SDKBackend:
    """Agent SDK-based session management using ClaudeSDKClient.

    Replaces the tmux-based SessionManager entirely.
    """

    def __init__(
        self,
        registry: SessionRegistry,
        contacts_manager=None,
        producer=None,
    ):
        self.registry = registry
        self.contacts = contacts_manager
        self._producer = producer
        self.sessions: Dict[str, SDKSession] = {}  # chat_id -> SDKSession
        self._lock = asyncio.Lock()

        # Quota-aware model degradation
        state_dir = Path(__file__).parent.parent / "state"
        self.quota_manager = QuotaManager(state_dir)
        self.haiku_circuit_breaker = HaikuCircuitBreaker()

        # Two-tier healing state
        self._last_fast_check: Dict[str, datetime] = {}  # chat_id -> last scan timestamp
        self._recently_healed: Dict[str, datetime] = {}  # chat_id -> heal timestamp
        self._last_auth_error_notification: Optional[datetime] = None  # debounce auth error SMS

        # Circuit breaker: track restart timestamps per session to prevent crash loops
        self._restart_timestamps: Dict[str, list] = {}  # chat_id -> list of restart datetimes
        self.CIRCUIT_BREAKER_MAX_RESTARTS = 3  # max restarts allowed in window
        self.CIRCUIT_BREAKER_WINDOW_SECONDS = 1200  # 20-minute window (must exceed stuck detection's 10min)

    def _clear_all_busy_flags(self) -> None:
        """Clear all is_busy flags in session_states at startup.

        On daemon startup, no sessions are active yet — any is_busy=1 is stale
        from the previous process. Without clearing, the app shows a stuck
        thinking bubble until the 10-minute staleness guard kicks in.
        """
        if not self._producer:
            return
        try:
            import sqlite3
            bus_db = Path.home() / "dispatch" / "state" / "bus.db"
            if not bus_db.exists():
                return
            conn = sqlite3.connect(str(bus_db))
            updated = conn.execute(
                "UPDATE session_states SET is_busy = 0, updated_at = ? WHERE is_busy = 1",
                (int(time.time() * 1000),),
            ).rowcount
            conn.commit()
            conn.close()
            if updated:
                log.info(f"STARTUP | Cleared {updated} stale is_busy flags from session_states")
        except Exception as e:
            log.warning(f"STARTUP | Failed to clear stale is_busy flags: {e}")

    def _read_restart_initiator(self) -> str | None:
        """Read the restart initiator chat_id from the graceful restart marker.

        Returns the chat_id of the session that triggered the restart, or None
        if this was a crash/external restart or the marker doesn't exist.
        """
        import json
        marker = Path("/tmp/dispatch-graceful-restart")
        if not marker.exists():
            return None
        try:
            data = json.loads(marker.read_text())
            return data.get("initiator_chat_id")
        except (json.JSONDecodeError, ValueError):
            return None

    async def recreate_active_sessions(self) -> int:
        """Recreate sessions that were active during previous daemon shutdown.

        Called at startup to preserve context across daemon restarts.
        Sessions are recreated if they were marked as was_active in the registry.
        When a stored session_id exists, it is passed as resume_id to enable
        native session resume (the SDK will compact and continue).

        Returns the number of sessions recreated.
        """
        recreated = 0

        # Clear all stale is_busy flags from session_states.
        # On startup, no sessions are running yet — any is_busy=1 is leftover
        # from the previous daemon process (crashed or restarted).
        # Without this, the app shows a stuck thinking bubble for up to 10 min.
        self._clear_all_busy_flags()

        # Determine which session (if any) initiated this restart
        restart_initiator = self._read_restart_initiator()

        # Iterate through registry to find sessions to recreate
        for chat_id, entry in self.registry.all().items():
            session_name = entry.get("session_name")
            if not session_name:
                continue

            was_active = entry.get("was_active", False)
            if not was_active:
                continue

            # Get session metadata from registry
            contact_name = entry.get("contact_name", "Unknown")
            tier = entry.get("tier", "favorite")
            session_type = entry.get("type", "individual")

            # Parse source from session_name (format: imessage/chat_id or signal/chat_id)
            source = "imessage"
            if "/" in session_name:
                source = session_name.split("/")[0]

            # Determine restart role: "initiator" if this session triggered the restart,
            # "passive" if restarted by something else
            if restart_initiator is not None and chat_id == restart_initiator:
                restart_role = "initiator"
            else:
                restart_role = "passive"

            # Get stored session_id for resume support
            stored_session_id = entry.get("session_id")

            log.info(f"STARTUP | Recreating session (was_active): {session_name}"
                     f" [restart_role={restart_role}, resume_id={stored_session_id}]")
            lifecycle_log.info(f"STARTUP | RECREATE_WAS_ACTIVE | {session_name}")

            try:
                if session_type == "group":
                    display_name = entry.get("display_name", chat_id)
                    await self.create_group_session(
                        chat_id=chat_id,
                        display_name=display_name,
                        source=source,
                        restart_role=restart_role,
                        resume_id=stored_session_id,
                    )
                else:
                    await self.create_session(
                        contact_name=contact_name,
                        chat_id=chat_id,
                        tier=tier,
                        source=source,
                        session_type=session_type,
                        restart_role=restart_role,
                        resume_id=stored_session_id,
                    )
                recreated += 1
                # Clear the was_active flag after successful recreation
                self.registry.clear_was_active(chat_id)

                # Replay undelivered messages from bus WAL
                await self._replay_undelivered(chat_id, stored_session_id)

                # Nudge session to continue any in-progress work
                session = self.sessions.get(chat_id)
                if session:
                    await session.inject(
                        "[SYSTEM] Session resumed after daemon restart. "
                        "If you were in the middle of a task, continue where you left off. "
                        "If you were idle/waiting for user input, ignore this message and wait silently."
                    )

            except Exception as e:
                log.error(f"STARTUP | Failed to recreate {session_name}: {e}")
                # Clear was_active + stale session_id so we don't retry forever.
                # A fresh session will be created when the next message arrives.
                self.registry.clear_was_active(chat_id)
                self.registry.update_session_id(chat_id, session_id=None)
                self.registry.flush()

        if recreated:
            log.info(f"STARTUP | Recreated {recreated} sessions")
            lifecycle_log.info(f"STARTUP | RECREATE_COMPLETE | count={recreated}")
            # Flush any debounced registry writes from batch session creation
            self.registry.flush()

        return recreated


    async def _replay_undelivered(self, chat_id: str, stored_session_id: str | None,
                                   max_replay: int = 50) -> int:
        """Replay undelivered messages from bus WAL for a session.

        Queries bus for message.queued events without matching message.delivered,
        deduplicates by text within the batch, and re-injects into the session.

        Uses at-most-once semantics: marks original as delivered BEFORE re-inject
        so a crash during replay doesn't cause infinite replay loops. The re-inject
        creates a NEW message.queued event with its own delivery tracking.

        Args:
            max_replay: Cap on messages to replay per session. Prevents thundering
                herd after long downtime. Remaining are left for next restart.

        Returns count of messages replayed.
        """
        try:
            from .bus_helpers import query_undelivered_messages
            bus_db_path = str(Path.home() / "dispatch" / "state" / "bus.db")
            undelivered = query_undelivered_messages(bus_db_path, chat_id)
        except FileNotFoundError:
            log.warning(f"REPLAY_DB_MISSING | {chat_id} | bus.db not found")
            return 0
        except Exception as e:
            log.error(f"REPLAY_QUERY_FAILED | {chat_id} | {e}")
            return 0

        if not undelivered:
            return 0

        if len(undelivered) > max_replay:
            log.warning(f"REPLAY_TRUNCATED | {chat_id} | {len(undelivered)} undelivered, capping at {max_replay}")
            undelivered = undelivered[:max_replay]

        session = self.sessions.get(chat_id)
        if not session:
            return 0

        log.info(f"REPLAY | {chat_id} | {len(undelivered)} undelivered messages")

        # Inject system prompt for fresh sessions (not resumed from checkpoint)
        if not stored_session_id:
            await self._inject_system_prompt_if_needed(session)

        replayed = 0
        seen_texts: set[str] = set()
        for umsg in undelivered:
            if umsg["text"] in seen_texts:
                log.warning(f"REPLAY_DEDUP | {chat_id} | msg_id={umsg['message_id'][:8]} | skipped duplicate")
                if self._producer:
                    produce_event(self._producer, "messages", "message.delivered", {
                        "message_id": umsg["message_id"],
                        "replayed": True,
                        "deduped": True,
                    }, key=chat_id, source="sdk_backend.replay")
                continue

            try:
                # At-most-once: mark delivered BEFORE inject to prevent
                # infinite replay if daemon crashes during inject.
                # The inject() call creates a NEW message.queued with a new ID.
                if self._producer:
                    produce_event(self._producer, "messages", "message.delivered", {
                        "message_id": umsg["message_id"],
                        "replayed": True,
                    }, key=chat_id, source="sdk_backend.replay")
                await session.inject(umsg["text"], replay_source_message_id=umsg["message_id"])
                seen_texts.add(umsg["text"])
                replayed += 1
            except Exception as e:
                log.error(f"REPLAY_INJECT_FAILED | {chat_id} | msg_id={umsg['message_id'][:8]} | {e}")
                if self._producer:
                    try:
                        produce_event(self._producer, "messages", "message.replay_failed", {
                            "message_id": umsg["message_id"],
                            "error": str(e),
                        }, key=chat_id, source="sdk_backend.replay")
                    except Exception:
                        pass

        return replayed

    # ──────────────────────────────────────────────────────────────
    # Individual sessions
    # ──────────────────────────────────────────────────────────────

    async def create_session(
        self,
        contact_name: str,
        chat_id: str,
        tier: str,
        source: str = "imessage",
        session_type: str = "individual",
        restart_role: str | None = None,
        resume_id: str | None = None,
    ) -> SDKSession:
        """Create a new SDK session for an individual contact."""
        async with self._lock:
            if chat_id in self.sessions and self.sessions[chat_id].is_alive():
                return self.sessions[chat_id]

            # Kill zombie session: if session exists but is not alive, its subprocess
            # may still be running (buffer overflow crash leaves orphan PIDs).
            # Must kill it before creating a new session to prevent duplicates.
            if chat_id in self.sessions:
                old_session = self.sessions.pop(chat_id)
                lifecycle_log.info(
                    f"ZOMBIE_CLEANUP | {chat_id} | Killing orphan subprocess before recreate"
                )
                await old_session._kill_subprocess()

            session_name = get_session_name(chat_id, source=source)
            transcript_dir = ensure_transcript_dir(session_name)

            # For non-default backends, create session-specific CLAUDE.md override
            self._create_backend_claude_md(transcript_dir, source)

            lifecycle_log.info(
                f"CREATE | {session_name} | START | contact={contact_name} "
                f"tier={tier} chat_id={chat_id} source={source} resume_id={resume_id}"
            )

            # Resolve model: global override > per-session registry > default
            existing_entry = self.registry.get(chat_id)
            default_model = "sonnet" if source == "discord" else "opus"
            registry_model = existing_entry.get("model", default_model) if existing_entry else default_model
            model, model_source = self.quota_manager.get_effective_model(chat_id, registry_model, default_model)
            log.info(f"SESSION_MODEL | chat_id={chat_id} | model={model} | source={model_source}")

            session = SDKSession(
                chat_id=chat_id,
                contact_name=contact_name,
                tier=tier,
                cwd=str(transcript_dir),
                session_type=session_type,
                source=source,
                model=model,
                producer=self._producer,
                resume_id=resume_id,
            )
            await session.start(resume_session_id=None)
            self.sessions[chat_id] = session

            # Defer system prompt to outside the lock
            session._needs_system_prompt = True
            session._system_prompt_args = (session_name, contact_name, tier, chat_id, source)
            session._restart_role = restart_role

            # Register in persistent registry (preserve registry model, not overridden model)
            self.registry.register(
                chat_id=chat_id,
                session_name=session_name,
                transcript_dir=str(transcript_dir),
                type=session_type,
                contact_name=contact_name,
                tier=tier,
                source=source,
                model=registry_model,
            )

            lifecycle_log.info(
                f"CREATE | {session_name} | SUCCESS | contact={contact_name} "
                f"tier={tier}"
            )
            produce_session_event(self._producer, chat_id, "session.created", {
                "contact_name": contact_name, "tier": tier,
                "session_type": session_type, "source": source,
            }, source="daemon")

        # Inject system prompt outside lock (includes slow subprocess for memory)
        await self._inject_system_prompt_if_needed(session)
        return session

    async def _create_session_unlocked(
        self,
        contact_name: str,
        chat_id: str,
        tier: str,
        source: str = "imessage",
        session_type: str = "individual",
    ) -> SDKSession:
        """Create a session without acquiring the lock (caller must hold it).

        This avoids deadlock when inject_message needs to create + inject atomically.
        """
        if chat_id in self.sessions and self.sessions[chat_id].is_alive():
            return self.sessions[chat_id]

        # Kill zombie session: if session exists but is not alive, its subprocess
        # may still be running (buffer overflow crash leaves orphan PIDs).
        # Must kill it before creating a new session to prevent duplicates.
        if chat_id in self.sessions:
            old_session = self.sessions.pop(chat_id)
            lifecycle_log.info(
                f"ZOMBIE_CLEANUP | {chat_id} | Killing orphan subprocess before recreate"
            )
            await old_session._kill_subprocess()

        session_name = get_session_name(chat_id, source=source)
        transcript_dir = ensure_transcript_dir(session_name)

        self._create_backend_claude_md(transcript_dir, source)

        # Resolve model: global override > per-session registry > default
        existing_entry = self.registry.get(chat_id)
        stored_resume_id_for_log = existing_entry.get("session_id") if existing_entry else None

        lifecycle_log.info(
            f"CREATE | {session_name} | START | contact={contact_name} "
            f"tier={tier} chat_id={chat_id} source={source}"
            f"{f' resume_id={stored_resume_id_for_log}' if stored_resume_id_for_log else ''}"
        )

        # (existing_entry reused below)
        default_model = "sonnet" if source == "discord" else "opus"
        registry_model = existing_entry.get("model", default_model) if existing_entry else default_model
        model, model_source = self.quota_manager.get_effective_model(chat_id, registry_model, default_model)
        log.info(f"SESSION_MODEL | chat_id={chat_id} | model={model} | source={model_source}")

        # Resume from stored session_id if available (e.g., after idle timeout kill).
        # kill_session() saves the session_id to the registry before stopping,
        # so we can resume the previous conversation instead of starting blank.
        stored_resume_id = existing_entry.get("session_id") if existing_entry else None
        if stored_resume_id:
            lifecycle_log.info(
                f"CREATE | {session_name} | RESUME_FROM_REGISTRY | "
                f"resume_id={stored_resume_id}"
            )

        session = SDKSession(
            chat_id=chat_id,
            contact_name=contact_name,
            tier=tier,
            cwd=str(transcript_dir),
            session_type=session_type,
            source=source,
            model=model,
            producer=self._producer,
            resume_id=stored_resume_id,
        )
        import time
        spawn_start = time.perf_counter()
        await session.start(resume_session_id=None)
        spawn_ms = (time.perf_counter() - spawn_start) * 1000
        perf.timing("session_spawn_ms", spawn_ms, component="daemon", tier=tier, source=source)
        self.sessions[chat_id] = session

        # System prompt injection deferred to _inject_system_prompt_if_needed()
        # so it can run outside the lock (includes slow subprocess calls)
        session._needs_system_prompt = True
        session._system_prompt_args = (session_name, contact_name, tier, chat_id, source)

        self.registry.register(
            chat_id=chat_id,
            session_name=session_name,
            transcript_dir=str(transcript_dir),
            type=session_type,
            contact_name=contact_name,
            tier=tier,
            source=source,
            model=model,
        )

        lifecycle_log.info(
            f"CREATE | {session_name} | SUCCESS | contact={contact_name} tier={tier}"
        )
        produce_session_event(self._producer, chat_id, "session.created", {
            "contact_name": contact_name, "tier": tier,
            "session_type": session_type, "source": source,
            "spawn_ms": round(spawn_ms, 1),
        }, source="daemon")

        # Replay any undelivered WAL messages for this session.
        # This is critical for the lazy-recreation path: if a previous session
        # died (e.g., bad resume ID, race with idle-kill) and messages were
        # queued to it, they'd be stuck in the WAL forever. Replaying here
        # ensures they're delivered immediately when the session is recreated.
        try:
            replayed = await self._replay_undelivered(chat_id, stored_resume_id)
            if replayed:
                lifecycle_log.info(
                    f"CREATE | {session_name} | LAZY_REPLAY | "
                    f"replayed={replayed} messages"
                )
        except Exception as e:
            log.warning(f"LAZY_REPLAY_FAILED | {chat_id} | {e}")

        # Yield control to event loop so other tasks can run during concurrent creations
        await asyncio.sleep(0)
        return session

    async def _inject_system_prompt_if_needed(self, session: SDKSession):
        """Inject the system prompt if the session was just created.

        Called OUTSIDE the lock so the slow subprocess call (_get_memory_summary)
        doesn't block other session operations.
        """
        if not getattr(session, '_needs_system_prompt', False):
            return
        session._needs_system_prompt = False
        args = getattr(session, '_system_prompt_args', None)
        if not args:
            return
        restart_role = getattr(session, '_restart_role', None)
        prompt_type = getattr(session, '_system_prompt_type', 'individual')
        if prompt_type == 'group':
            system_prompt = await self._build_group_system_prompt(*args, restart_role=restart_role)
        else:
            system_prompt = await self._build_individual_system_prompt(*args, restart_role=restart_role)
        await session.inject(system_prompt)

    async def inject_message(
        self,
        contact_name: str,
        chat_id: str,
        text: str,
        tier: str,
        attachments: list | None = None,
        audio_transcription: str | None = None,
        thread_originator_guid: str | None = None,
        source: str = "imessage",
        message_timestamp: datetime | None = None,
        message_guid: str | None = None,
    ) -> bool:
        """Inject a message into an existing session.
        Creates session on-demand if missing (lazy creation).
        """
        import time
        inject_start = time.perf_counter()
        if not chat_id:
            raise ValueError(f"chat_id cannot be empty for contact {contact_name}")

        # Format message body
        msg_body = format_message_body(text, attachments, audio_transcription)

        # Prefix chat_id for registry storage (e.g. signal:+1234567890)
        # But don't add prefix if chat_id already has it
        from assistant.backends import get_backend
        backend = get_backend(source)
        if backend.registry_prefix and not chat_id.startswith(backend.registry_prefix):
            registry_chat_id = f"{backend.registry_prefix}{chat_id}"
        else:
            registry_chat_id = chat_id
        normalized = normalize_chat_id(registry_chat_id)

        # Only hold the lock for session creation check + creation.
        # Once we have the session, inject outside the lock (session has its own queue).
        needs_restart = False
        async with self._lock:
            existing = self.sessions.get(normalized)
            if not existing or not existing.is_alive():
                await self._create_session_unlocked(contact_name, normalized, tier, source)
            elif existing.tier != tier:
                # Tier mismatch! Session was created with different tier.
                # Must restart to apply correct permissions (e.g., favorite -> admin).
                log.info(f"Tier mismatch for {chat_id}: session has {existing.tier}, inject wants {tier}. Restarting...")
                needs_restart = True

        # Do restart outside the lock to avoid manual release/re-acquire
        if needs_restart:
            await self.restart_session(normalized, tier_override=tier)

        async with self._lock:
            session = self.sessions.get(normalized)

        if not session:
            log.error(f"Failed to create session for {chat_id}")
            return False

        # Inject system prompt outside lock (includes slow subprocess for memory)
        await self._inject_system_prompt_if_needed(session)

        # Block limit suppression: if session already notified user about a block limit,
        # silently drop further messages until the limit resets. This prevents spamming
        # the user with repeated "You've hit your limit" messages for every incoming text.
        if session.is_block_limited:
            log.info(f"BLOCK_LIMIT_SUPPRESSED | {chat_id} | message dropped (limit active until {session._block_limit_until})")
            return True  # Return True to prevent upstream error handling

        wrapped = wrap_sms(
            msg_body, contact_name, tier, chat_id,
            reply_to_guid=thread_originator_guid, source=source,
            message_guid=message_guid,
        )
        await session.inject(wrapped)
        self.registry.update_last_message_time(normalized)
        inject_ms = (time.perf_counter() - inject_start) * 1000
        perf.timing("inject_ms", inject_ms, component="daemon", source=source, tier=tier)
        log.info(f"Injected message for {chat_id} via {source}")
        produce_session_event(self._producer, normalized, "session.injected",
            session_injected_payload(normalized, "message", contact_name, tier,
                                     has_attachments=bool(attachments)),
            source="inject")

        # Conditional compaction notification: if session is compacting and user
        # hasn't been notified yet, send a one-time "compacting, one sec…" SMS.
        # The message was still injected above (SDK buffers during compaction).
        if session.check_compacting() and not session.compaction_notified:
            self._send_compaction_notice(session, normalized, source)

        # Spawn async Gemini vision analysis for image attachments
        if attachments:
            for att in attachments:
                image_path = att.get("path")
                if image_path and Path(image_path).suffix.lower() in IMAGE_EXTENSIONS:
                    _fire_and_forget(
                        self._inject_gemini_vision(
                            session, normalized, image_path,
                            source=source,
                            chat_id=chat_id,
                            message_timestamp=message_timestamp,
                        ),
                        name=f"gemini-vision-{normalized}",
                    )

        return True

    def _send_compaction_notice(self, session, normalized: str, source: str):
        """Send a one-time 'compacting, one sec…' notice when user messages during compaction.

        Sends SMS directly (fire-and-forget with 30s subprocess timeout).
        Produces compaction.user_waiting bus event for observability.
        """
        import threading
        from assistant.backends import get_backend
        from assistant import config
        from assistant.common import get_session_name

        session_name = get_session_name(session.chat_id, source)
        assistant_name = config.get("assistant.name", "Assistant")

        # Mark as notified BEFORE sending (prevents duplicate sends on rapid messages)
        session.compaction_notified = True
        session.compaction_notified_at = time.monotonic()

        # Produce bus event for observability
        produce_event(self._producer, "system", "compaction.user_waiting",
            compaction_user_waiting_payload(session_name, session.chat_id,
                session.contact_name, source, session.session_type,
                compaction_epoch=session.compaction_epoch),
            source="compaction")

        # Send SMS directly (fire-and-forget with timeout)
        try:
            backend = get_backend(source)
            if session.session_type == "group":
                send_tpl = backend.send_group_cmd
            else:
                send_tpl = backend.send_cmd
            script_path = str(Path(send_tpl.split()[0]).expanduser())
            bare_chat_id = session.chat_id.removeprefix(backend.registry_prefix) if backend.registry_prefix else session.chat_id
            msg = f"[{assistant_name.upper()}] compacting, one sec\u2026"
            proc = subprocess.Popen(
                [script_path, bare_chat_id, msg],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            def _reap_with_timeout(p, sname):
                try:
                    p.wait(timeout=30)
                    if p.returncode != 0:
                        log.warning(f"COMPACTION_NOTICE | send failed | session={sname} rc={p.returncode}")
                except subprocess.TimeoutExpired:
                    p.kill()
                    log.warning(f"COMPACTION_NOTICE | send timed out (30s) | session={sname}")

            threading.Thread(target=_reap_with_timeout, args=(proc, session_name),
                           daemon=True, name="reap-compact-notice").start()
            log.info(f"COMPACTION_NOTICE | sent | session={session_name}")
        except Exception as e:
            log.error(f"COMPACTION_NOTICE | send error | session={session_name} | {e}")

    def handle_compaction_completed(self, session_name: str, bus_duration_s: float = 0,
                                     compaction_epoch: int | None = None):
        """Handle a compaction.completed bus event from post-compact-hook.

        If the user was notified during compaction and enough time has passed (>3s),
        sends a 'done compacting' SMS. Otherwise stays silent.
        Called by the manager's bus consumer.

        Args:
            compaction_epoch: If provided, guards against stale events from a previous
                compaction being applied to a newer one.
        """
        import threading
        from assistant import config

        # Find the session by session_name
        session = None
        for s in list(self.sessions.values()):
            from assistant.common import get_session_name
            if get_session_name(s.chat_id, s.source) == session_name:
                session = s
                break

        if session is None:
            log.debug(f"COMPACTION_DONE | no session found for {session_name}")
            return

        # Epoch guard: skip stale completed events from a previous compaction
        if compaction_epoch is not None and compaction_epoch != session.compaction_epoch:
            log.info(f"COMPACTION_DONE | stale epoch | session={session_name} event_epoch={compaction_epoch} session_epoch={session.compaction_epoch}")
            return

        # Capture duration BEFORE clearing flags (compacting_since will be None after clear)
        duration_s = bus_duration_s
        if session.compacting_since:
            duration_s = time.monotonic() - session.compacting_since

        was_notified = session.compaction_notified
        notified_at = session.compaction_notified_at

        # Always clear compaction flags
        session._clear_compaction_flags()

        if not was_notified:
            log.info(f"COMPACTION_DONE | silent | session={session_name} duration={duration_s:.0f}s (no user messages during compaction)")
            return

        # 3s debounce: if user was notified <3s ago, skip "done" message.
        # Two rapid-fire messages feel spammy; one slightly delayed reply is better.
        if notified_at and (time.monotonic() - notified_at) <= 3.0:
            log.info(f"COMPACTION_DONE | skipped done msg | session={session_name} (notified <3s ago)")
            return

        # Send "done compacting" SMS — self-contained message that makes sense
        # even if user didn't see the "compacting" notice (e.g., SMS failed)
        assistant_name = config.get("assistant.name", "Assistant")
        from assistant.backends import get_backend
        try:
            backend = get_backend(session.source)
            if session.session_type == "group":
                send_tpl = backend.send_group_cmd
            else:
                send_tpl = backend.send_cmd
            script_path = str(Path(send_tpl.split()[0]).expanduser())
            bare_chat_id = session.chat_id.removeprefix(backend.registry_prefix) if backend.registry_prefix else session.chat_id
            msg = f"[{assistant_name.upper()}] back \u2014 had to reorganize my memory (took {duration_s:.0f}s)"
            proc = subprocess.Popen(
                [script_path, bare_chat_id, msg],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            def _reap_with_timeout(p, sname):
                try:
                    p.wait(timeout=30)
                    if p.returncode != 0:
                        log.warning(f"COMPACTION_DONE | send failed | session={sname} rc={p.returncode}")
                except subprocess.TimeoutExpired:
                    p.kill()
                    log.warning(f"COMPACTION_DONE | send timed out (30s) | session={sname}")

            threading.Thread(target=_reap_with_timeout, args=(proc, session_name),
                           daemon=True, name="reap-compact-done").start()
            log.info(f"COMPACTION_DONE | sent | session={session_name} duration={duration_s:.0f}s")
        except Exception as e:
            log.error(f"COMPACTION_DONE | send error | session={session_name} | {e}")

    async def inject_reaction(
        self,
        chat_id: str,
        reaction_text: str,
        emoji: str,
        sender_name: str,
        sender_tier: str,
        source: str = "imessage",
    ) -> bool:
        """Inject a reaction notification into an existing session.

        Unlike inject_message, this does NOT create a session on-demand.
        Reactions only matter if there's an active conversation.
        """
        from assistant.backends import get_backend
        backend = get_backend(source)
        if backend.registry_prefix and not chat_id.startswith(backend.registry_prefix):
            registry_chat_id = f"{backend.registry_prefix}{chat_id}"
        else:
            registry_chat_id = chat_id
        normalized = normalize_chat_id(registry_chat_id)

        # Only inject if session exists and is alive
        async with self._lock:
            session = self.sessions.get(normalized)
            if not session or not session.is_alive():
                log.debug(f"No active session for {chat_id}, skipping reaction")
                return False

        # Inject the reaction notification (no wrapping needed, it's already formatted)
        await session.inject(reaction_text)
        log.info(f"Injected reaction {emoji} from {sender_name} for {chat_id}")
        produce_session_event(self._producer, normalized, "session.injected",
            session_injected_payload(normalized, "reaction", sender_name, sender_tier,
                                     emoji=emoji),
            source="inject")
        return True

    async def _inject_gemini_vision(
        self,
        session: SDKSession,
        normalized_chat_id: str,
        image_path: str,
        source: str = "imessage",
        chat_id: str | None = None,
        message_timestamp: datetime | None = None,
    ) -> None:
        """Background task: analyze image with Gemini and inject result into session.

        Silently skips if Gemini fails (no error injection).

        Args:
            session: The SDK session to inject into
            normalized_chat_id: Internal chat ID (normalized, for logging)
            image_path: Path to the image file
            source: Backend source ("imessage", "signal", "dispatch-app")
            chat_id: Original chat identifier for context lookup
            message_timestamp: Timestamp of the image message for context anchoring
        """
        try:
            # Get conversation context using the appropriate reader
            conversation_context = ""

            from assistant.backends import get_backend
            backend_config = get_backend(source)

            if backend_config.supports_image_context and chat_id and message_timestamp:
                from assistant.readers import get_reader, format_context_for_gemini
                reader = get_reader(source)

                if reader:
                    # Run blocking DB query in executor to avoid blocking event loop
                    loop = asyncio.get_event_loop()
                    messages = await loop.run_in_executor(
                        None,
                        reader.get_context_around,
                        chat_id,
                        message_timestamp,
                        10,  # before
                        1,   # after
                    )
                    conversation_context = format_context_for_gemini(messages)

            description = await analyze_image_with_gemini(image_path, conversation_context)
            if description:
                # Check session is still alive before injecting
                if session.is_alive():
                    vision_msg = f"""
---VISION ANALYSIS---
Gemini analyzed the attached image:
{description}
---END VISION---
"""
                    await session.inject(vision_msg)
                    log.info(f"Injected Gemini vision for {normalized_chat_id}")
                    produce_event(self._producer, "system", "vision.analyzed",
                        vision_payload(normalized_chat_id, image_path, True,
                                       description_length=len(description)),
                        source="vision")
                else:
                    log.debug(f"Session {normalized_chat_id} died before vision inject")
            else:
                produce_event(self._producer, "system", "vision.failed",
                    vision_payload(normalized_chat_id, image_path, False),
                    source="vision")
        except Exception as e:
            log.warning(f"Gemini vision task failed for {normalized_chat_id}: {e}")
            produce_event(self._producer, "system", "vision.failed",
                vision_payload(normalized_chat_id, image_path, False, error=str(e)),
                source="vision")

    # ──────────────────────────────────────────────────────────────
    # Group sessions
    # ──────────────────────────────────────────────────────────────

    def get_group_session_name(self, chat_id: str, display_name: str | None = None,
                                source: str = "imessage") -> str:
        existing = self.registry.get(chat_id)
        if existing:
            return existing["session_name"]
        return get_session_name(chat_id, source)

    async def create_group_session(
        self,
        chat_id: str,
        display_name: str | None = None,
        participants: list | None = None,
        source: str = "imessage",
        restart_role: str | None = None,
        resume_id: str | None = None,
    ) -> SDKSession:
        """Create a group session."""
        async with self._lock:
            if chat_id in self.sessions and self.sessions[chat_id].is_alive():
                return self.sessions[chat_id]

            # Kill zombie session: if session exists but is not alive, its subprocess
            # may still be running (buffer overflow crash leaves orphan PIDs).
            if chat_id in self.sessions:
                old_session = self.sessions.pop(chat_id)
                lifecycle_log.info(
                    f"ZOMBIE_CLEANUP | {chat_id} | Killing orphan subprocess before recreate"
                )
                await old_session._kill_subprocess()

            # Resolve participants from chat.db if not provided (only works for iMessage)
            if not participants:
                from assistant.backends import get_backend
                backend = get_backend(source)
                if not backend.registry_prefix:  # iMessage has no prefix
                    participants = await self._resolve_group_participants(chat_id)

            session_name = self.get_group_session_name(chat_id, display_name, source)
            transcript_dir = ensure_transcript_dir(session_name)

            # Resolve model: global override > source-aware default for groups
            default_model = "sonnet" if source == "discord" else "opus"
            model, model_source = self.quota_manager.get_effective_model(chat_id, "", default_model)
            log.info(f"SESSION_MODEL | chat_id={chat_id} | model={model} | source={model_source} | type=group")

            session = SDKSession(
                chat_id=chat_id,
                contact_name=display_name or chat_id,
                tier="admin",  # Groups get full access
                cwd=str(transcript_dir),
                session_type="group",
                source=source,
                model=model,
                producer=self._producer,
                resume_id=resume_id,
            )
            await session.start(resume_session_id=None)
            self.sessions[chat_id] = session

            # Always inject system prompt - session reads old messages for context
            startup = await self._build_group_system_prompt(
                session_name, chat_id, display_name, participants, source,
                restart_role=restart_role,
            )
            await session.inject(startup)

            self.registry.register(
                chat_id=chat_id,
                session_name=session_name,
                transcript_dir=str(transcript_dir),
                type="group",
                display_name=display_name or chat_id,
                participants=participants or [],
                source=source,
            )

            lifecycle_log.info(f"CREATE | {session_name} | SUCCESS | type=group chat_id={chat_id}")
            produce_session_event(self._producer, chat_id, "session.created", {
                "contact_name": display_name or chat_id, "tier": "admin",
                "session_type": "group", "source": source,
            }, source="daemon")
            return session

    async def _create_group_session_unlocked(
        self,
        chat_id: str,
        display_name: str | None = None,
        participants: list | None = None,
        source: str = "imessage",
    ) -> SDKSession:
        """Create a group session without acquiring the lock (caller must hold it)."""
        if chat_id in self.sessions and self.sessions[chat_id].is_alive():
            return self.sessions[chat_id]

        # Kill zombie session: if session exists but is not alive, its subprocess
        # may still be running (buffer overflow crash leaves orphan PIDs).
        if chat_id in self.sessions:
            old_session = self.sessions.pop(chat_id)
            lifecycle_log.info(
                f"ZOMBIE_CLEANUP | {chat_id} | Killing orphan subprocess before recreate"
            )
            await old_session._kill_subprocess()

        # Resolve participants from chat.db if not provided (only works for iMessage)
        if not participants:
            from assistant.backends import get_backend
            backend = get_backend(source)
            if not backend.registry_prefix:  # iMessage has no prefix
                participants = await self._resolve_group_participants(chat_id)

        session_name = self.get_group_session_name(chat_id, display_name, source)
        transcript_dir = ensure_transcript_dir(session_name)

        # Resolve model: global override > source-aware default for groups
        default_model = "sonnet" if source == "discord" else "opus"
        model, model_source = self.quota_manager.get_effective_model(chat_id, "", default_model)
        log.info(f"SESSION_MODEL | chat_id={chat_id} | model={model} | source={model_source} | type=group")

        # Resume from stored session_id if available (e.g., after idle timeout kill).
        existing_entry = self.registry.get(chat_id)
        stored_resume_id = existing_entry.get("session_id") if existing_entry else None
        if stored_resume_id:
            lifecycle_log.info(
                f"CREATE | {session_name} | RESUME_FROM_REGISTRY | "
                f"resume_id={stored_resume_id}"
            )

        session = SDKSession(
            chat_id=chat_id,
            contact_name=display_name or chat_id,
            tier="admin",
            cwd=str(transcript_dir),
            session_type="group",
            source=source,
            model=model,
            producer=self._producer,
            resume_id=stored_resume_id,
        )
        await session.start(resume_session_id=None)
        self.sessions[chat_id] = session

        # Defer system prompt to outside the lock
        session._needs_system_prompt = True
        session._system_prompt_args = (session_name, chat_id, display_name, participants, source)
        session._system_prompt_type = "group"

        self.registry.register(
            chat_id=chat_id,
            session_name=session_name,
            transcript_dir=str(transcript_dir),
            type="group",
            display_name=display_name or chat_id,
            participants=participants or [],
            source=source,
        )

        lifecycle_log.info(f"CREATE | {session_name} | SUCCESS | type=group chat_id={chat_id}")
        produce_session_event(self._producer, chat_id, "session.created", {
            "contact_name": display_name or chat_id, "tier": "admin",
            "session_type": "group", "source": source,
        }, source="daemon")
        return session

    async def inject_group_message(
        self,
        chat_id: str,
        display_name: str | None,
        sender_name: str,
        sender_tier: str,
        text: str,
        attachments: list | None = None,
        audio_transcription: str | None = None,
        thread_originator_guid: str | None = None,
        source: str = "imessage",
        message_timestamp: datetime | None = None,
        message_guid: str | None = None,
    ) -> bool:
        """Inject a message into a group session."""
        if not chat_id:
            raise ValueError("chat_id cannot be empty for group message")

        # Prefix chat_id for registry storage (e.g. discord:abc123, signal:xyz)
        # Same logic as inject_message — without this, Discord/Signal group
        # sessions use wrong keys and can't be found in the registry.
        from assistant.backends import get_backend
        backend = get_backend(source)
        if backend.registry_prefix and not chat_id.startswith(backend.registry_prefix):
            chat_id = f"{backend.registry_prefix}{chat_id}"
        chat_id = normalize_chat_id(chat_id)

        msg_body = format_message_body(text, attachments, audio_transcription)

        # Lock only for session creation check; inject happens outside lock
        async with self._lock:
            if chat_id not in self.sessions or not self.sessions[chat_id].is_alive():
                await self._create_group_session_unlocked(chat_id, display_name, source=source)
            session = self.sessions.get(chat_id)

        if not session:
            log.error(f"Failed to create group session for {chat_id}")
            return False

        # Inject system prompt outside lock if session was just created
        await self._inject_system_prompt_if_needed(session)

        # Block limit suppression (same as inject_message)
        if session.is_block_limited:
            log.info(f"BLOCK_LIMIT_SUPPRESSED | {chat_id} | group message dropped (limit active until {session._block_limit_until})")
            return True

        wrapped = wrap_group_message(
            chat_id, display_name, sender_name, sender_tier,
            msg_body, reply_to_guid=thread_originator_guid, source=source,
            message_guid=message_guid,
        )
        await session.inject(wrapped)
        self.registry.update_last_message_time(chat_id)
        produce_session_event(self._producer, chat_id, "session.injected",
            session_injected_payload(chat_id, "group", sender_name, sender_tier,
                                     group_name=display_name,
                                     has_attachments=bool(attachments)),
            source="inject")

        # Spawn async Gemini vision analysis for image attachments
        if attachments:
            for att in attachments:
                image_path = att.get("path")
                if image_path and Path(image_path).suffix.lower() in IMAGE_EXTENSIONS:
                    _fire_and_forget(
                        self._inject_gemini_vision(
                            session, chat_id, image_path,
                            source=source,
                            chat_id=chat_id,
                            message_timestamp=message_timestamp,
                        ),
                        name=f"gemini-vision-{chat_id}",
                    )

        return True

    # ──────────────────────────────────────────────────────────────
    # Ephemeral sessions (for tasks)
    # ──────────────────────────────────────────────────────────────

    async def create_ephemeral_session(
        self,
        task_id: str,
        title: str,
        instructions: str,
        requested_by: str,
        timeout_minutes: int = 30,
        notify: bool = True,
        *,
        system_prompt_override: str | None = None,
        disallowed_tools: tuple[str, ...] = (),
        bash_deny_regex: tuple[str, ...] = (),
    ) -> SDKSession:
        """Create an ephemeral SDK session for a one-off task.

        Ephemeral sessions are short-lived agents that execute a task and
        auto-terminate. They reuse the same SDK infrastructure as normal
        chat sessions but have their own cwd and no transcript persistence.

        Args:
            task_id: Unique task identifier (used as session key)
            title: Human-readable task title
            instructions: Full prompt/instructions for the agent
            requested_by: chat_id of requester (for result routing)
            timeout_minutes: Auto-kill after this many minutes
            notify: Whether to text requester on start/finish
            system_prompt_override: If set, overrides the SDK system prompt and
                skips the inline first-message task_prompt inject (the SDK
                delivers system_prompt directly).
            disallowed_tools: Extra tool names to append to the SDK's
                disallowed_tools list (harness-level enforcement).
            bash_deny_regex: Regex patterns; any Bash command matching any of
                them is denied via can_use_tool (survives compaction).

        Returns:
            The created SDKSession
        """
        session_key = f"ephemeral-{task_id}"

        async with self._lock:
            if session_key in self.sessions and self.sessions[session_key].is_alive():
                return self.sessions[session_key]

            # Kill zombie
            if session_key in self.sessions:
                old_session = self.sessions.pop(session_key)
                lifecycle_log.info(
                    f"ZOMBIE_CLEANUP | {session_key} | Killing orphan subprocess before recreate"
                )
                await old_session._kill_subprocess()

            # Create ephemeral cwd with .claude symlink for skill access
            ephemeral_dir = HOME / "dispatch" / "state" / "ephemeral" / task_id
            ephemeral_dir.mkdir(parents=True, exist_ok=True)
            claude_symlink = ephemeral_dir / ".claude"
            if not claude_symlink.exists():
                claude_symlink.symlink_to(HOME / ".claude")

            session = SDKSession(
                chat_id=session_key,
                contact_name=f"Task: {title}",
                tier="admin",
                cwd=str(ephemeral_dir),
                session_type="ephemeral",
                producer=self._producer,
                system_prompt_override=system_prompt_override,
                extra_disallowed_tools=disallowed_tools,
                bash_deny_regex=bash_deny_regex,
            )
            await session.start()
            self.sessions[session_key] = session

            lifecycle_log.info(
                f"CREATE | {session_key} | SUCCESS | ephemeral task={title} "
                f"requested_by={requested_by} timeout={timeout_minutes}m"
            )
            produce_session_event(self._producer, session_key, "session.created", {
                "contact_name": f"Task: {title}",
                "tier": "admin",
                "session_type": "ephemeral",
                "task_id": task_id,
                "requested_by": requested_by,
            }, source="task-runner")

        # Inject task instructions outside lock.
        # When system_prompt_override is set, the SDK delivers the system prompt
        # directly — seeding via inject would double-send the framing.
        if system_prompt_override is None:
            task_prompt = f"""EPHEMERAL TASK SESSION — {title}

You are an autonomous agent executing a specific task. When done, state your
results clearly. You have access to all skills and tools.

Task ID: {task_id}
Requested by: {requested_by}

--- INSTRUCTIONS ---
{instructions}
--- END INSTRUCTIONS ---

Execute the task now. Be thorough but efficient."""
            await session.inject(task_prompt)
        else:
            await session.inject(instructions)
        return session

    async def kill_ephemeral_session(self, task_id: str) -> bool:
        """Kill an ephemeral session and clean up its cwd."""
        session_key = f"ephemeral-{task_id}"

        async with self._lock:
            session = self.sessions.pop(session_key, None)

        if session:
            await session.stop()
            lifecycle_log.info(f"KILL | {session_key} | ephemeral task killed")

        # Clean up ephemeral cwd
        import shutil
        ephemeral_dir = HOME / "dispatch" / "state" / "ephemeral" / task_id
        if ephemeral_dir.exists():
            try:
                shutil.rmtree(ephemeral_dir)
            except Exception as e:
                log.warning(f"Failed to clean up ephemeral dir {ephemeral_dir}: {e}")

        return session is not None

    # ──────────────────────────────────────────────────────────────
    # Master session
    # ──────────────────────────────────────────────────────────────

    async def create_master_session(self) -> SDKSession:
        """Create the always-alive master admin session."""
        async with self._lock:
            if MASTER_SESSION in self.sessions and self.sessions[MASTER_SESSION].is_alive():
                return self.sessions[MASTER_SESSION]

            # Kill zombie session: if session exists but is not alive, its subprocess
            # may still be running (buffer overflow crash leaves orphan PIDs).
            if MASTER_SESSION in self.sessions:
                old_session = self.sessions.pop(MASTER_SESSION)
                lifecycle_log.info(
                    f"ZOMBIE_CLEANUP | {MASTER_SESSION} | Killing orphan subprocess before recreate"
                )
                await old_session._kill_subprocess()

            MASTER_TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
            claude_symlink = MASTER_TRANSCRIPT_DIR / ".claude"
            if not claude_symlink.exists():
                claude_symlink.symlink_to(HOME / ".claude")

            session = SDKSession(
                chat_id=MASTER_SESSION,
                contact_name="Master",
                tier="admin",
                cwd=str(MASTER_TRANSCRIPT_DIR),
                session_type="master",
                producer=self._producer,
            )
            await session.start()
            self.sessions[MASTER_SESSION] = session

            lifecycle_log.info("MASTER | CREATED")
            produce_session_event(self._producer, MASTER_SESSION, "session.created", {
                "contact_name": "Master", "tier": "admin",
                "session_type": "master", "source": "imessage",
            }, source="daemon")
            return session

    async def inject_master_prompt(self, admin_phone: str, prompt: str) -> bool:
        """Inject a prompt into the master session."""
        if MASTER_SESSION not in self.sessions or not self.sessions[MASTER_SESSION].is_alive():
            await self.create_master_session()

        session = self.sessions.get(MASTER_SESSION)
        if not session:
            return False

        wrapped = f"""---MASTER COMMAND---
From: Admin ({admin_phone})
{prompt}
---END MASTER COMMAND---

Respond via: ~/.claude/skills/sms-assistant/scripts/send-sms "{admin_phone}" "[MASTER] your response"
"""
        await session.inject(wrapped)
        lifecycle_log.info(f"MASTER | INJECTED | prompt_len={len(prompt)}")
        produce_session_event(self._producer, MASTER_SESSION, "session.injected", {
            "chat_id": MASTER_SESSION, "injection_type": "master",
            "contact_name": "Master", "tier": "admin",
        }, source="inject")
        return True

    # ──────────────────────────────────────────────────────────────
    # Session lifecycle
    # ──────────────────────────────────────────────────────────────

    async def kill_session(self, chat_id: str) -> bool:
        """Kill a session."""
        async with self._lock:
            session = self.sessions.pop(chat_id, None)
            # Save session_id and clear was_active INSIDE the lock so that
            # inject_message (which also holds the lock) always sees a consistent
            # registry state. Without this, a concurrent inject could create a new
            # session from stale registry data (missing the session_id from the
            # session we're about to kill).
            if session:
                if session.session_id:
                    self.registry.update_session_id(chat_id, session.session_id)
                self.registry.clear_was_active(chat_id)

        if session:
            uptime = (datetime.now() - session.created_at).total_seconds()
            produce_session_event(self._producer, chat_id, "session.killed", {
                "contact_name": session.contact_name, "tier": session.tier,
                "turn_count": session.turn_count, "uptime_seconds": round(uptime, 1),
                "error_count": session._error_count,
            }, source="daemon")
            # Clear is_busy BEFORE stopping — session.stop() may not complete
            # cleanly (e.g., subprocess hangs), leaving a stale thinking bubble.
            if self._producer:
                self._producer.set_session_busy(session._session_name, False)
            await session.stop()

        lifecycle_log.info(f"KILL | {chat_id} | killed={'yes' if session else 'no'}")
        return session is not None

    async def kill_all_sessions(self) -> int:
        """Kill all sessions."""
        async with self._lock:
            sessions = list(self.sessions.values())
            self.sessions.clear()

        # Save session_ids before stopping
        for s in sessions:
            if s.session_id and s.chat_id:
                self.registry.update_session_id(s.chat_id, s.session_id)

        for s in sessions:
            try:
                await s.stop()
            except (Exception, asyncio.CancelledError) as e:
                log.error(f"Error stopping session {s.contact_name}: {e}")
            finally:
                task = asyncio.current_task()
                if task is not None:
                    while task.cancelling() > 0:
                        task.uncancel()

        lifecycle_log.info(f"KILL_ALL | count={len(sessions)}")
        return len(sessions)

    def _clear_sdk_session_index(self, session_name: str) -> None:
        """Clear the SDK's sessions-index.json to prevent auto-resume of poisoned sessions.

        The SDK auto-resumes from ~/.claude/projects/<sanitized-cwd>/sessions-index.json.
        When restarting a crashed/poisoned session, we need to clear this index so the SDK
        creates a fresh session instead of resuming the old one.
        """
        transcript_dir = ensure_transcript_dir(session_name)
        # SDK sanitizes cwd path: /Users/sven/transcripts/foo -> -Users-sven-transcripts-foo
        sanitized = str(transcript_dir).replace("/", "-")
        if sanitized.startswith("-"):
            sanitized = sanitized  # Already starts with dash from leading /

        sdk_project_dir = HOME / ".claude" / "projects" / sanitized
        index_file = sdk_project_dir / "sessions-index.json"

        if index_file.exists():
            try:
                index_file.unlink()
                lifecycle_log.info(f"CLEAR_INDEX | {session_name} | Deleted {index_file}")
            except Exception as e:
                log.warning(f"Failed to delete session index {index_file}: {e}")

    async def restart_session(self, chat_id: str, tier_override: str | None = None, clean: bool = False) -> Optional[SDKSession]:
        """Kill and recreate a session.

        Args:
            chat_id: The chat ID to restart
            tier_override: Optional tier to use instead of registry value
            clean: If True, clear the SDK session index and stored session_id
                   to force a completely fresh session (no resume)
        """
        # Circuit breaker: prevent crash loops (max N restarts in M seconds)
        now = datetime.now()
        timestamps = self._restart_timestamps.get(chat_id, [])
        cutoff = now - timedelta(seconds=self.CIRCUIT_BREAKER_WINDOW_SECONDS)
        timestamps = [t for t in timestamps if t > cutoff]
        if len(timestamps) >= self.CIRCUIT_BREAKER_MAX_RESTARTS:
            session_name = (self.registry.get(chat_id) or {}).get("session_name", chat_id)
            lifecycle_log.warning(
                f"CIRCUIT_BREAKER | {session_name} | "
                f"{len(timestamps)} restarts in {self.CIRCUIT_BREAKER_WINDOW_SECONDS}s, "
                f"refusing restart"
            )
            return None
        timestamps.append(now)
        self._restart_timestamps[chat_id] = timestamps

        # Save session info before killing
        reg = self.registry.get(chat_id)

        await self.kill_session(chat_id)

        if clean and reg and reg.get("session_name"):
            # Clean mode: clear session index and stored session_id for fresh start
            self._clear_sdk_session_index(reg["session_name"])
            reg.pop("session_id", None)
            self.registry.register(**reg)
            lifecycle_log.info(f"RESTART_CLEAN | {reg.get('session_name', chat_id)} | Cleared session index and resume ID")

        if reg:
            lifecycle_log.info(f"RESTART | {reg.get('session_name', chat_id)} | START | clean={clean}")
            # Group chats use display_name; individuals use contact_name
            contact_name = reg.get("contact_name") or reg.get("display_name", "Unknown")
            # Use tier_override if provided, else fall back to registry or default
            tier = tier_override or reg.get("tier", "admin")
            source = reg.get("source", "imessage")
            # For normal restarts, pass stored session_id to enable resume
            resume_id = None if clean else reg.get("session_id")
            produce_session_event(self._producer, chat_id, "session.restarted", {
                "contact_name": contact_name, "tier": tier,
                "reason": "restart_requested", "clean": clean,
            }, source="daemon")
            session_type = reg.get("type", "individual")
            if session_type == "group":
                display_name = reg.get("display_name") or contact_name
                session = await self.create_group_session(
                    chat_id=chat_id,
                    display_name=display_name,
                    source=source,
                    resume_id=resume_id,
                )
            else:
                session = await self.create_session(
                    contact_name,
                    chat_id,
                    tier,
                    source,
                    session_type,
                    resume_id=resume_id,
                )
            lifecycle_log.info(f"RESTART | {reg.get('session_name', chat_id)} | COMPLETE")
            # Replay any undelivered messages from WAL after crash recovery
            try:
                await self._replay_undelivered(chat_id, resume_id)
            except Exception as e:
                log.warning(f"RESTART_REPLAY_FAILED | {chat_id} | {e}")
            return session
        return None

    async def check_session_health(self, chat_id: str, ctx: CheckContext | None = None) -> bool:
        """Check if a session is healthy. Auto-restarts if not.

        For "stuck" sessions (inject pending >10 min), launches a Haiku investigation
        to determine if the session is genuinely stuck or just running a long operation
        (e.g. subagent tasks). Only restarts if Haiku confirms the session is stuck.
        """
        session = self.sessions.get(chat_id)
        if not session:
            return False
        # Skip if recently healed by fast_health_check or deep_health_check
        if chat_id in self._recently_healed:
            return True
        healthy, reason = session.is_healthy()
        if not healthy:
            lifecycle_log.info(
                f"HEALTH_CHECK | {session.contact_name} | UNHEALTHY | "
                f"reason={reason} alive={session.is_alive()} errors={session._error_count}"
            )

            # For stuck sessions, investigate with Haiku before restarting
            if reason.startswith("stuck("):
                # Skip Haiku investigation if circuit breaker is open
                if self.haiku_circuit_breaker.is_open():
                    log.info(f"STUCK_CHECK | {session.chat_id} | Skipped (circuit breaker open)")
                    return True  # treat as healthy to avoid restart without Haiku confirmation

                from assistant.common import get_session_name
                session_name = get_session_name(session.chat_id, session.source)
                # last_inject_at is guaranteed non-None here (stuck detection requires it)
                stuck_minutes = (datetime.now() - session.last_inject_at).total_seconds() / 60 if session.last_inject_at else 0

                async def _investigate_and_maybe_restart(cid: str):
                    # ctx captured from enclosing check_session_health scope —
                    # safe because ctx is assigned once and never reassigned
                    try:
                        from assistant.health import check_stuck_haiku
                        is_stuck, reasoning = await check_stuck_haiku(
                            session.cwd, session.session_id,
                            session_name, stuck_minutes,
                        )
                        # Haiku call succeeded — record success for circuit breaker
                        cb_state_before = self.haiku_circuit_breaker.state
                        self.haiku_circuit_breaker.record_success()
                        if self.haiku_circuit_breaker.state != cb_state_before:
                            produce_event(self._producer, "system", "health.circuit_breaker",
                                circuit_breaker_payload(session_name, cid, "closed",
                                    self.haiku_circuit_breaker.consecutive_failures, ctx),
                                source="health")
                        if is_stuck:
                            lifecycle_log.info(
                                f"STUCK_CONFIRMED | {session_name} | Haiku says stuck, restarting"
                            )
                            produce_session_event(self._producer, cid, "session.crashed", {
                                "contact_name": session.contact_name,
                                "error_count": session._error_count,
                                "turn_count": session.turn_count,
                                "reason": reason,
                                "haiku_verdict": "stuck",
                            }, source="daemon")
                            produce_event(self._producer, "system", "health.haiku_verdict",
                                haiku_verdict_payload(ctx, "stuck", session_name, cid, "STUCK", "restart",
                                                     reasoning=reasoning),
                                source="health")
                            await self.restart_session(cid)
                        else:
                            lifecycle_log.info(
                                f"STUCK_FALSE_POSITIVE | {session_name} | Haiku says working, leaving alone"
                            )
                            produce_session_event(self._producer, cid, "session.stuck_cleared", {
                                "contact_name": session.contact_name,
                                "turn_count": session.turn_count,
                                "stuck_minutes": stuck_minutes,
                                "haiku_verdict": "working",
                            }, source="daemon")
                            produce_event(self._producer, "system", "health.haiku_verdict",
                                haiku_verdict_payload(ctx, "stuck", session_name, cid, "WORKING", "none",
                                                     reasoning=reasoning),
                                source="health")
                            # Clear recently_healed so future health checks can re-examine
                            self._recently_healed.pop(cid, None)
                    except HaikuCallFailed:
                        # Haiku API failed — feed circuit breaker
                        cb_state_before = self.haiku_circuit_breaker.state
                        cb_actions = self.haiku_circuit_breaker.record_failure()
                        if "sms_circuit_open" in cb_actions:
                            self._circuit_breaker_actions = cb_actions
                        if self.haiku_circuit_breaker.state != cb_state_before:
                            produce_event(self._producer, "system", "health.circuit_breaker",
                                circuit_breaker_payload(session_name, cid, "opened",
                                    self.haiku_circuit_breaker.consecutive_failures, ctx),
                                source="health")
                        log.warning(f"Stuck investigation Haiku call failed for {cid}")
                        # Clear recently_healed on failure so session isn't shielded
                        self._recently_healed.pop(cid, None)
                    except Exception as e:
                        log.error(f"Stuck investigation failed for {cid}: {e}")
                        # Clear recently_healed on failure so session isn't shielded
                        self._recently_healed.pop(cid, None)

                self._recently_healed[chat_id] = datetime.now()
                _fire_and_forget(
                    _investigate_and_maybe_restart(chat_id),
                    name=f"stuck-investigate-{chat_id}",
                )
                return False

            # Non-stuck failures: restart immediately
            produce_session_event(self._producer, chat_id, "session.crashed", {
                "contact_name": session.contact_name,
                "error_count": session._error_count,
                "turn_count": session.turn_count,
                "reason": reason,
            }, source="daemon")
            # Mark as recently healed to prevent double-restart from other health checks
            self._recently_healed[chat_id] = datetime.now()
            # Fire-and-forget: do NOT await restart_session at all.
            async def _isolated_restart(cid: str):
                try:
                    await self.restart_session(cid)
                except Exception as e:
                    log.error(f"Health check restart failed for {cid}: {e}")
                    # Clear recently_healed on failure so session isn't shielded
                    self._recently_healed.pop(cid, None)

            _fire_and_forget(_isolated_restart(chat_id), name=f"health-restart-{chat_id}")
            return False
        return True

    def _send_auth_error_notification(self):
        """Send SMS to admin when OAuth token has expired.

        Uses subprocess directly (not a Claude session) since sessions
        can't function without valid auth. Runs in fire-and-forget mode.
        """
        from assistant import config

        try:
            admin_phone = config.require("owner.phone")
            send_sms = HOME / ".claude/skills/sms-assistant/scripts/send-sms"
            message = "[AUTH] OAuth token expired - run /login to refresh"

            result = subprocess.run(
                [str(send_sms), admin_phone, message],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                lifecycle_log.info(f"AUTH_ERROR_NOTIFY | Sent SMS to admin: {message}")
                produce_event(self._producer, "messages", "message.sent", {
                    "chat_id": admin_phone, "text": message,
                    "is_group": False, "success": True, "context": "auth_error",
                }, key=f"imessage/{admin_phone}", source="daemon")
            else:
                log.warning(f"AUTH_ERROR_NOTIFY | SMS failed: {result.stderr}")
        except Exception as e:
            log.error(f"AUTH_ERROR_NOTIFY | Failed to send SMS: {e}")

    async def health_check_all(self, ctx: CheckContext | None = None) -> Dict[str, bool]:
        """Check all sessions. Auto-restarts unhealthy ones."""
        ctx = ctx or CheckContext()
        results = {}
        for chat_id in list(self.sessions.keys()):
            results[chat_id] = await self.check_session_health(chat_id, ctx=ctx)

        # Perf: track active session count
        alive_count = sum(1 for s in self.sessions.values() if s.is_alive())
        perf.gauge("active_sessions", alive_count, component="daemon")

        lifecycle_log.info(f"HEALTH_CHECK_ALL | COMPLETE | {sum(results.values())}/{len(results)} healthy")
        produce_event(self._producer, "system", "health.check_completed",
            health_check_payload(active_sessions=alive_count,
                                 healthy=sum(results.values()),
                                 total=len(results),
                                 check_run_id=ctx.check_run_id),
            source="health")
        return results

    async def fast_health_check(self, ctx: CheckContext | None = None) -> List[str]:
        """Tier 1: Regex-based fatal error detection from transcripts.

        Reads recent transcript JSONL entries for each session and checks
        for known unrecoverable error patterns (400 API errors, image limits,
        context overflow, etc.). Returns list of chat_ids that were restarted.

        Also restarts dead sessions (e.g., buffer overflow crashes that set
        running=False but weren't auto-restarted).

        Runs every 60 seconds.
        """
        ctx = ctx or CheckContext()
        now = datetime.now()
        restarted = []

        # Clean stale entries from recently_healed (older than 5 min)
        cutoff = now.timestamp() - 300
        self._recently_healed = {
            cid: ts for cid, ts in self._recently_healed.items()
            if ts.timestamp() > cutoff
        }

        for chat_id, session in list(self.sessions.items()):
            if chat_id == MASTER_SESSION:
                continue
            if chat_id.startswith("ephemeral-"):
                continue
            if chat_id in self._recently_healed:
                continue

            # Restart dead sessions (buffer overflow, receiver crash, etc.)
            if not session.is_alive():
                session_name = get_session_name(session.chat_id, session.source)

                # Check transcript for context-size errors before deciding clean flag
                since = self._last_fast_check.get(chat_id, session.created_at)
                dead_entries = get_transcript_entries_since(session.cwd, session.session_id, since)
                CONTEXT_SIZE_ERRORS = {"prompt_too_long", "context_too_long", "content_too_large", "buffer_overflow"}
                dead_fatal = check_fatal_regex(dead_entries) if dead_entries else None
                needs_clean = dead_fatal in CONTEXT_SIZE_ERRORS if dead_fatal else False

                lifecycle_log.info(
                    f"FAST_HEAL | {session_name} | DEAD_SESSION"
                    f"{f' ({dead_fatal})' if dead_fatal else ''}"
                    f" | Restarting (clean={needs_clean})"
                )
                self._recently_healed[chat_id] = now

                async def _isolated_restart(cid: str, clean: bool = False):
                    try:
                        await self.restart_session(cid, clean=clean)
                    except Exception as e:
                        log.error(f"Fast heal restart failed for {cid}: {e}")
                        # Clear recently_healed on failure so session isn't shielded
                        self._recently_healed.pop(cid, None)

                _fire_and_forget(
                    _isolated_restart(chat_id, clean=needs_clean),
                    name=f"fast-heal-dead-{chat_id}",
                )
                restarted.append(chat_id)
                continue

            # Only scan entries since last check for this session
            since = self._last_fast_check.get(chat_id, session.created_at)
            entries = get_transcript_entries_since(session.cwd, session.session_id, since)
            self._last_fast_check[chat_id] = now

            if not entries:
                continue

            fatal_label = check_fatal_regex(entries)
            if fatal_label:
                session_name = get_session_name(session.chat_id, session.source)
                lifecycle_log.info(
                    f"FAST_HEAL | {session_name} | {fatal_label} | Restarting"
                )

                # Send SMS notification for auth errors (OAuth token expired)
                # Debounce to max once per 5 minutes to avoid spam
                if fatal_label == "auth_error":
                    should_notify = True
                    if self._last_auth_error_notification:
                        elapsed = (now - self._last_auth_error_notification).total_seconds()
                        if elapsed < 300:  # 5 minutes
                            should_notify = False

                    if should_notify:
                        self._last_auth_error_notification = now
                        self._send_auth_error_notification()

                # Use clean restart for context/prompt size errors — resuming
                # the same session will never fix these, causing a restart loop.
                CONTEXT_SIZE_ERRORS = {"prompt_too_long", "context_too_long", "content_too_large", "buffer_overflow"}
                needs_clean = fatal_label in CONTEXT_SIZE_ERRORS
                if needs_clean:
                    lifecycle_log.info(
                        f"FAST_HEAL | {session_name} | {fatal_label} | Using clean=True (context size error)"
                    )

                self._recently_healed[chat_id] = now

                async def _isolated_restart(cid: str, clean: bool = False):
                    try:
                        await self.restart_session(cid, clean=clean)
                    except Exception as e:
                        log.error(f"Fast heal restart failed for {cid}: {e}")
                        # Clear recently_healed on failure so session isn't shielded
                        self._recently_healed.pop(cid, None)

                _fire_and_forget(
                    _isolated_restart(chat_id, clean=needs_clean),
                    name=f"fast-heal-{chat_id}",
                )
                restarted.append(chat_id)

        # Periodic persistence: flush session_id and was_active to registry.
        # This ensures crash recovery can resume sessions even if shutdown() never ran.
        # Runs as part of the 60s health check loop — writes are immediate but guarded
        # by equality checks, so typically 0 writes per cycle after first persistence.
        persisted = 0
        for chat_id, session in list(self.sessions.items()):
            if chat_id == MASTER_SESSION or chat_id.startswith("ephemeral-"):
                continue
            if session.session_id:
                entry = self.registry.get(chat_id)
                if entry and entry.get("session_id") != session.session_id:
                    self.registry.update_session_id(chat_id, session.session_id)
                    persisted += 1
                # Mark was_active eagerly so crash recovery knows to recreate
                if entry and not entry.get("was_active"):
                    self.registry.mark_was_active(chat_id)

        lifecycle_log.info(
            f"FAST_HEAL | SCAN | {len(self.sessions)} sessions checked | "
            f"{len(restarted)} fatal"
            f"{f' | {persisted} session_ids persisted' if persisted else ''}"
        )
        produce_event(self._producer, "system", "health.fast_check_completed", {
            "sessions_checked": len(self.sessions),
            "restarted": len(restarted),
            "check_run_id": ctx.check_run_id,
        }, source="health")
        return restarted

    async def deep_health_check(self, skip_chat_ids: set | None = None,
                                ctx: CheckContext | None = None) -> List[str]:
        """Tier 2: Haiku-based deep analysis of session health.

        Sends recent assistant messages to Haiku for classification of
        subtle/complex failure modes. Skips sessions already handled by
        fast_health_check or recently healed.

        Runs every 5 minutes alongside the existing health_check_all().
        """
        ctx = ctx or CheckContext()
        skip = skip_chat_ids or set()
        now = datetime.now()
        restarted = []

        # Look back 5 minutes for deep analysis
        from datetime import timedelta
        since = now - timedelta(minutes=5)

        checked = 0
        for chat_id, session in list(self.sessions.items()):
            if chat_id == MASTER_SESSION:
                continue
            if chat_id.startswith("ephemeral-"):
                continue
            if not session.is_alive():
                continue
            if chat_id in skip or chat_id in self._recently_healed:
                continue

            checked += 1

            # Circuit breaker: skip Haiku calls when API is down
            if self.haiku_circuit_breaker.is_open():
                continue

            entries = get_transcript_entries_since(session.cwd, session.session_id, since)
            if not entries:
                continue

            session_name = get_session_name(session.chat_id, session.source)
            try:
                diagnosis, reasoning = await check_deep_haiku(entries, session_name)
                # Haiku call succeeded — record success for circuit breaker
                cb_state_before = self.haiku_circuit_breaker.state
                self.haiku_circuit_breaker.record_success()
                if self.haiku_circuit_breaker.state != cb_state_before:
                    produce_event(self._producer, "system", "health.circuit_breaker",
                        circuit_breaker_payload(session_name, chat_id, "closed",
                            self.haiku_circuit_breaker.consecutive_failures, ctx),
                        source="health")
            except HaikuCallFailed:
                # Haiku API call failed — record failure for circuit breaker
                cb_state_before = self.haiku_circuit_breaker.state
                cb_actions = self.haiku_circuit_breaker.record_failure()
                if "sms_circuit_open" in cb_actions:
                    self._circuit_breaker_actions = cb_actions
                if self.haiku_circuit_breaker.state != cb_state_before:
                    produce_event(self._producer, "system", "health.circuit_breaker",
                        circuit_breaker_payload(session_name, chat_id, "opened",
                            self.haiku_circuit_breaker.consecutive_failures, ctx),
                        source="health")
                continue  # skip this session, move to next

            if diagnosis:
                lifecycle_log.info(
                    f"DEEP_HEAL | {session_name} | {diagnosis} | Restarting"
                )
                produce_event(self._producer, "system", "health.haiku_verdict",
                    haiku_verdict_payload(ctx, "deep", session_name, chat_id, "FATAL", "restart",
                                         reasoning=reasoning),
                    source="health")

                self._recently_healed[chat_id] = now

                async def _isolated_restart(cid: str):
                    try:
                        await self.restart_session(cid, clean=True)
                    except Exception as e:
                        log.error(f"Deep heal restart failed for {cid}: {e}")

                _fire_and_forget(
                    _isolated_restart(chat_id),
                    name=f"deep-heal-{chat_id}",
                )
                restarted.append(chat_id)
            else:
                # Haiku says healthy — emit verdict for tracking
                produce_event(self._producer, "system", "health.haiku_verdict",
                    haiku_verdict_payload(ctx, "deep", session_name, chat_id, "HEALTHY", "none",
                                         reasoning=reasoning),
                    source="health")

        lifecycle_log.info(
            f"DEEP_HEAL | SCAN | {checked} sessions checked | "
            f"{len(restarted)} fatal"
        )
        produce_event(self._producer, "system", "health.deep_check_completed", {
            "sessions_checked": checked,
            "restarted": len(restarted),
            "check_run_id": ctx.check_run_id,
        }, source="health")
        return restarted

    async def check_idle_sessions(self, timeout_hours: float) -> List[str]:
        """Kill idle sessions exceeding timeout. Returns chat_ids killed."""
        now = datetime.now()
        killed = []
        # Snapshot under lock to avoid concurrent modification (bug #16 fix)
        async with self._lock:
            sessions_snapshot = list(self.sessions.items())
        for chat_id, session in sessions_snapshot:
            if chat_id == MASTER_SESSION:
                continue  # Don't idle-kill master session
            # Admin-tier sessions stay warm indefinitely — cold-starts on
            # admin's next inbound compound any macOS-side chat.db delay
            # (see IMDPersistenceAgent App-Nap escalated tickle in manager).
            if getattr(session, "tier", None) == "admin":
                continue
            idle_seconds = (now - session.last_activity).total_seconds()
            if idle_seconds > timeout_hours * 3600:
                idle_hours = idle_seconds / 3600
                log.info(f"Session {session.contact_name} idle for {idle_hours:.1f}h, killing...")
                lifecycle_log.info(
                    f"IDLE_TIMEOUT | {session.contact_name} | KILLING | "
                    f"idle_hours={idle_hours:.1f} threshold={timeout_hours}"
                )
                produce_session_event(self._producer, chat_id, "session.idle_killed", {
                    "contact_name": session.contact_name, "idle_hours": round(idle_hours, 1),
                }, source="health")
                # Fire-and-forget: do NOT await kill_session at all.
                # Awaiting (even via wait_for on a separate task) allows anyio
                # cancel scopes to leak CancelledError to this task.
                async def _isolated_kill(cid: str):
                    try:
                        await self.kill_session(cid)
                    except Exception as e:
                        log.error(f"Idle kill failed for {cid}: {e}")

                _fire_and_forget(_isolated_kill(chat_id), name=f"idle-kill-{chat_id}")
                killed.append(chat_id)
        return killed

    async def get_session_info(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Get info about a session."""
        session = self.sessions.get(chat_id)
        if not session:
            return None
        healthy, health_reason = session.is_healthy()
        return {
            "chat_id": chat_id,
            "contact_name": session.contact_name,
            "tier": session.tier,
            "session_type": session.session_type,
            "source": session.source,
            "is_alive": session.is_alive(),
            "is_healthy": healthy,
            "health_reason": health_reason,
            "is_busy": session.is_busy,
            "turn_count": session.turn_count,
            "session_id": session.session_id,
            "created_at": session.created_at.isoformat(),
            "last_activity": session.last_activity.isoformat(),
            "queue_size": session._message_queue.qsize(),
        }

    async def get_all_sessions(self) -> List[Dict[str, Any]]:
        """Get info about all active sessions."""
        infos = []
        for chat_id in self.sessions:
            info = await self.get_session_info(chat_id)
            if info:
                infos.append(info)
        return infos

    async def get_recent_output(self, chat_id: str, lines: int = 30) -> str:
        """Get recent output from per-session log file."""
        session = self.sessions.get(chat_id)
        if not session:
            return ""
        session_name = get_session_name(session.chat_id, session.source)
        log_name = session_name.replace("/", "-")
        from assistant.sdk_session import SESSION_LOG_DIR
        log_path = SESSION_LOG_DIR / f"{log_name}.log"
        if log_path.exists():
            # Read only the tail of the file efficiently
            import os
            with open(log_path, 'rb') as f:
                try:
                    f.seek(0, os.SEEK_END)
                    size = f.tell()
                    # Read last 64KB max (enough for ~30 lines)
                    chunk_size = min(size, 65536)
                    f.seek(size - chunk_size)
                    tail = f.read().decode('utf-8', errors='replace')
                    tail_lines = tail.splitlines()
                    return "\n".join(tail_lines[-lines:])
                except Exception:
                    return ""
        return ""

    async def shutdown(self):
        """Clean shutdown: save session_ids, disconnect all clients.

        Native Claude Code compaction handles context preservation.
        Sessions resume via stored session_id on restart.
        """
        log.info("SHUTDOWN | Saving session_ids and disconnecting all clients...")
        async with self._lock:
            sessions = list(self.sessions.values())
            self.sessions.clear()

        # Mark all active sessions in registry so they get recreated on startup
        for s in sessions:
            if s.chat_id:
                self.registry.mark_was_active(s.chat_id)

        # Save session_ids for future resume
        for s in sessions:
            if s.session_id and s.chat_id:
                self.registry.update_session_id(s.chat_id, s.session_id)

        # Clear all is_busy flags before disconnecting — prevents stuck
        # thinking bubbles if stop() doesn't complete cleanly.
        if self._producer:
            for s in sessions:
                try:
                    self._producer.set_session_busy(s._session_name, False)
                except Exception:
                    pass

        # Disconnect all
        for s in sessions:
            try:
                await s.stop()
            except (Exception, asyncio.CancelledError) as e:
                log.error(f"Error stopping session {s.contact_name}: {e}")
            finally:
                # Clear ALL stacked cancellations from SDK anyio internals
                task = asyncio.current_task()
                if task is not None:
                    while task.cancelling() > 0:
                        task.uncancel()

        lifecycle_log.info(f"SHUTDOWN | COMPLETE | {len(sessions)} sessions stopped")

    # ──────────────────────────────────────────────────────────────
    # System prompt builders (identical to current tmux prompts)
    # ──────────────────────────────────────────────────────────────


    async def _build_individual_system_prompt(
        self,
        session_name: str,
        contact_name: str,
        tier: str,
        chat_id: str,
        source: str = "imessage",
        restart_role: str | None = None,
    ) -> str:
        """Build the startup prompt for an individual session.

        Auto-injects SOUL.md, memory summary, and chat context for faster startup.
        """
        # Fetch all context in parallel (async, non-blocking)
        soul_content, memory_summary, chat_context = await asyncio.gather(
            self._get_soul_content(),
            self._get_memory_summary(contact_name),
            self._get_chat_context(session_name),
        )

        # Build sections with clear labels
        soul_section = f"\n## My Identity (from SOUL.md)\n\n{soul_content}\n" if soul_content else ""
        memory_section = f"\n## About {contact_name} (from memories)\n\n{memory_summary}\n" if memory_summary else ""
        context_section = f"\n## Current Conversation Context\n\n{chat_context}\n" if chat_context else ""

        # Determine send command and history based on source
        from assistant.backends import get_backend
        backend = get_backend(source)
        bare_chat_id = chat_id.removeprefix(backend.registry_prefix) if backend.registry_prefix else chat_id
        send_cmd = backend.send_cmd.replace("{chat_id}", bare_chat_id)
        # Widget hint only for backends that support it (dispatch-app)
        if backend.name == "dispatch-app":
            widget_hint = (
                f'- Send widget (ask_question — use for structured choices, NOT open-ended):\n'
                f'  cat <<\'EOF\' | ~/.claude/skills/dispatch-app/scripts/reply-widget "{bare_chat_id}" ask_question\n'
                f'  {{"questions":[{{"question":"...","options":[{{"label":"A"}},{{"label":"B"}}]}}]}}\n'
                f'  EOF\n'
                f'  1-4 questions, 2-4 options each. Each has "Other" text input by default (include_other:false to hide).\n'
                f'  All questions shown at once with Save button. Response: [Widget Response <id>] Q: "..." → answer per question.\n'
                f'- Send progress tracker (display-only, for multi-step tasks):\n'
                f'  cat <<\'EOF\' | ~/.claude/skills/dispatch-app/scripts/reply-widget "{bare_chat_id}" progress_tracker\n'
                f'  {{"title":"...","steps":[{{"label":"Step 1","status":"complete"}},{{"label":"Step 2","status":"in_progress"}},{{"label":"Step 3"}}]}}\n'
                f'  EOF\n'
                f'  Statuses: pending (default), in_progress, complete, error. 1-10 steps, optional title/detail.\n'
                f'- Send map pin (display-only, opens Apple Maps on tap):\n'
                f'  cat <<\'EOF\' | ~/.claude/skills/dispatch-app/scripts/reply-widget "{bare_chat_id}" map_pin\n'
                f'  {{"title":"...","pins":[{{"latitude":42.36,"longitude":-71.06,"label":"Boston"}}]}}\n'
                f'  EOF\n'
            )
        else:
            widget_hint = ""
        if backend.history_cmd:
            history_note = f'**FIRST**: Run this command to see recent conversation history:\n{backend.history_cmd.replace("{chat_id}", bare_chat_id).replace("{limit}", "20")}'
        else:
            history_note = f"({backend.label} session - no message history command available yet)"

        # Build the action instructions based on whether this is a resumed or fresh session.
        # When restart_role is set, this session was recreated after a daemon restart.
        # The Previous Session Context and Current Conversation Context above contain
        # everything the session needs — it should NOT re-read SMS history and re-execute
        # old instructions. New messages will be injected by the daemon's polling loop.
        if restart_role == "initiator":
            # This session triggered the restart — let the user know we're back
            action_block = (
                f"**You are being RESUMED after a daemon restart that you initiated.**\n\n"
                f"Your previous incarnation already handled all messages up to this point. "
                f"The 'Previous Session Context' and 'Current Conversation Context' above describe what you were doing.\n\n"
                f"IMPORTANT: Send a brief back-online message to let the user know you're back. "
                f"Keep it short and casual (e.g. 'back online 👍'). "
                f"Do this BEFORE resuming any work.\n\n"
                f"Then WAIT for new messages — they will be injected by the daemon automatically. "
                f"Do NOT read SMS history. Do NOT re-execute tasks from your previous session. "
                f"Everything before this point is HISTORICAL and already handled.\n\n"
                f"If the Previous Session Context mentions incomplete work, you may continue it "
                f"ONLY if no user action is needed first."
            )
        elif restart_role == "passive":
            # Restarted by something else — stay silent, user shouldn't notice
            action_block = (
                f"**You are being RESUMED after a daemon restart.**\n\n"
                f"Your previous incarnation already handled all messages up to this point. "
                f"The 'Previous Session Context' and 'Current Conversation Context' above describe what you were doing.\n\n"
                f"CRITICAL: Do NOT send any restart notification, 'back online' message, or any greeting. "
                f"Users should NOT notice session restarts.\n\n"
                f"WAIT for new messages — they will be injected by the daemon automatically. "
                f"Do NOT read SMS history. Do NOT re-execute tasks from your previous session. "
                f"Everything before this point is HISTORICAL and already handled.\n\n"
                f"If the Previous Session Context mentions incomplete work that doesn't require user input, "
                f"you may silently continue it."
            )
        else:
            # Fresh session creation (not a restart) — read history to bootstrap context
            action_block = (
                f"{history_note}\n\n"
                f"After reading, act based on what you see:\n\n"
                f"1. **Task was in progress** (last OUT messages show active work):\n"
                f"   - Pick up EXACTLY where you left off\n"
                f"   - Do NOT announce restart or say \"catching up\"\n"
                f"   - If user asked \"how's it going?\" = you went offline during work, so continue silently\n\n"
                f"2. **Unanswered message** (last IN message has no response):\n"
                f"   - Respond to it naturally\n\n"
                f"3. **Conversation was idle** (no pending work or questions):\n"
                f"   - Wait silently for new messages\n\n"
                f"CRITICAL: Do NOT send any restart notification, 'back online' message, or any greeting. "
                f"Users should NOT notice session restarts. Just silently resume — read history and act on it "
                f"without announcing yourself.\n\n"
                f"EXCEPTION: If the last IN message from the user explicitly asked for a restart/deploy "
                f"(e.g. 'restart', 'deploy', 'yes restart'), send a brief back-online confirmation "
                f"(e.g. 'back online 👍'). The user asked for this and is waiting to know it worked."
            )

        return f"""SESSION START - INDIVIDUAL {backend.label} CHAT: {contact_name} ({tier} tier)
Chat ID: {chat_id}
{soul_section}{memory_section}{context_section}
{action_block}

**IMMEDIATELY read** ~/.claude/skills/sms-assistant/{source}.md for backend-specific rules (tapback, reply commands, etc.) before doing anything else.

**If you need more context** about what you were doing before restart:
uv run ~/.claude/skills/sms-assistant/scripts/read_transcript.py --session {session_name}

Quick reference:
- Send message using heredoc (no temp files - avoids race conditions between sessions):
  {send_cmd} "$(cat <<'ENDMSG'
  your message here
  ENDMSG
  )"
{widget_hint}- NEVER escape exclamation marks. Write "Hello!" NOT "Hello\\!". The CLI handles escaping. \\! sends a literal backslash.
- Full guidelines: ~/.claude/skills/sms-assistant/SKILL.md
"""

    async def _build_group_system_prompt(
        self,
        session_name: str,
        chat_id: str,
        display_name: str | None = None,
        participants: list | None = None,
        source: str = "imessage",
        restart_role: str | None = None,
    ) -> str:
        """Build the startup prompt for a group session.

        Auto-injects SOUL.md, memory summaries for all participants, and chat context.
        """
        participants_list = participants or []

        # Build participants list with tiers
        participant_lines = []
        for participant in participants_list:
            if self.contacts:
                contact_info = self.contacts.lookup_phone_by_name(participant)
                tier = contact_info.get("tier", "unknown") if contact_info else "unknown"
            else:
                tier = "unknown"
            participant_lines.append(f"- {participant} ({tier})")

        # Fetch ALL context in parallel: SOUL + chat context + memory for each
        async_tasks = [self._get_soul_content(), self._get_chat_context(session_name)]
        async_tasks.extend(self._get_memory_summary(p) for p in participants_list)

        results = await asyncio.gather(*async_tasks) if async_tasks else []

        # Unpack results: soul, chat_context, then N memories
        soul_content = results[0] if results else ""
        chat_context = results[1] if len(results) > 1 else ""
        n_participants = len(participants_list)
        memory_results = results[2:2+n_participants] if n_participants else []

        # Build sections with clear labels
        soul_section = f"\n## My Identity (from SOUL.md)\n\n{soul_content}\n" if soul_content else ""

        # Combine memories per participant
        participant_context_parts = []
        for i, participant in enumerate(participants_list):
            mem = memory_results[i] if i < len(memory_results) else ""
            if mem:
                part = f"## About {participant}\n"
                part += f"\n**From memories:**\n{mem}\n"
                participant_context_parts.append(part)

        participants_section = "\n".join(participant_lines) if participant_lines else "- (unknown participants)"
        participant_context_section = "\n".join(participant_context_parts) if participant_context_parts else ""
        chat_context_section = f"\n## Current Conversation Context\n\n{chat_context}\n" if chat_context else ""

        shown_name = display_name or chat_id

        from assistant.backends import get_backend
        backend = get_backend(source)
        send_cmd = backend.send_group_cmd.replace("{chat_id}", chat_id)
        if backend.history_cmd:
            history_cmd = backend.history_cmd.replace("{chat_id}", chat_id).replace("{limit}", "20")
        else:
            history_cmd = f"uv run ~/.claude/skills/sms-assistant/scripts/read_transcript.py --session {session_name}"

        # Build action instructions based on restart state (same logic as individual)
        if restart_role in ("initiator", "passive"):
            if restart_role == "initiator":
                notify = (
                    "Send a brief back-online message to let the group know you're back. "
                    "Keep it short and casual (e.g. 'back online 👍'). Do this BEFORE resuming any work."
                )
            else:
                notify = (
                    "CRITICAL: Do NOT send any restart notification, 'back online' message, or any greeting. "
                    "Users should NOT notice session restarts."
                )
            action_block = (
                f"**You are being RESUMED after a daemon restart.**\n\n"
                f"Your previous incarnation already handled all messages up to this point. "
                f"The context sections above describe what you were doing.\n\n"
                f"{notify}\n\n"
                f"WAIT for new messages — they will be injected by the daemon automatically. "
                f"Do NOT read message history. Do NOT re-execute tasks from your previous session. "
                f"Everything before this point is HISTORICAL and already handled."
            )
        else:
            action_block = (
                f"**FIRST**: Check conversation history: {history_cmd}\n\n"
                f"After reading, act based on what you see - respond to unanswered messages, "
                f"continue work in progress, or wait silently.\n\n"
                f"CRITICAL: Do NOT send any restart notification, 'back online' message, or any greeting. "
                f"Users should NOT notice session restarts. Just silently resume — read history and act on it "
                f"without announcing yourself."
            )

        # Discord-specific: instruct session to read channel history for context
        discord_context_block = ""
        if source == "discord":
            discord_context_block = f"""
**IMPORTANT — Discord Context Rule:**
You only receive messages where you are @mentioned, role-pinged, or named. You do NOT see the full channel.
**Before responding to ANY message**, FIRST run:
```
~/.claude/skills/discord/scripts/read-discord "{bare_chat_id}" --limit 25
```
This gives you the recent channel context so your reply makes sense. Without this, you'll miss the conversation flow.
"""

        return f"""SESSION START - GROUP CHAT: {shown_name}
Chat ID: {chat_id}

Participants:
{participants_section}
{soul_section}
{participant_context_section}{chat_context_section}
{action_block}
{discord_context_block}
**To send a message to this group using heredoc (no temp files - avoids race conditions between sessions):**
{send_cmd} "$(cat <<'ENDMSG'
your message here
ENDMSG
)"

You MUST call the send command above via Bash to actually send messages. Text output alone does NOT reach users.

NEVER escape exclamation marks. Write "Hello!" NOT "Hello\\!". The CLI handles escaping. \\! sends a literal backslash.

**IMMEDIATELY read** ~/.claude/skills/sms-assistant/{source}.md for backend-specific rules (tapback, reply commands, etc.) before doing anything else.

Full guidelines: ~/.claude/skills/sms-assistant/SKILL.md
"""

    def _create_backend_claude_md(self, transcript_dir: Path, source: str):
        """Create backend-specific CLAUDE.md override for non-default backends.

        Only created for non-imessage backends to emphasize the correct send commands.
        """
        from assistant.backends import get_backend, BACKENDS
        backend = get_backend(source)

        # Default backend (imessage) doesn't need an override
        if source == "imessage":
            return

        claude_md_path = transcript_dir / "CLAUDE.md"

        # Only create if it doesn't exist
        if claude_md_path.exists():
            return

        # Build the default backend name for the "NEVER use" warning
        default_backend = BACKENDS["imessage"]
        default_send = default_backend.send_cmd.split("/")[-1].split('"')[0].strip()

        content = f"""# {backend.label} Session Override

**CRITICAL: This is a {backend.label} session. You MUST use {backend.label}-specific commands.**

## Sending Messages

**For individual {backend.label} messages:**
```bash
{backend.send_cmd}
```

**For {backend.label} group messages:**
```bash
{backend.send_group_cmd}
```

**NEVER use {default_send} in {backend.label} sessions** - it will send via {default_backend.label} instead of {backend.label}, causing duplicate responses and confusion.

All other system documentation applies normally (see ~/.claude/CLAUDE.md via symlink).
"""
        # Add widget commands for dispatch-app backend
        if backend.name == "dispatch-app":
            content += f"""
## Widgets

Send interactive widgets (structured choices) instead of plain text when you need a specific selection:
```bash
cat <<'EOF' | ~/.claude/skills/dispatch-app/scripts/reply-widget "{{chat_id}}" ask_question
{{"questions":[{{"question":"Which option?","options":[{{"label":"A","description":"Details"}},{{"label":"B"}}]}}]}}
EOF
```
- 1-4 questions, 2-4 options each. Each question shows "Other" with text input by default (`"include_other": false` to hide).
- All questions shown at once with a Save button. No auto-submit.
- Response arrives as multi-line: `[Widget Response <id>]` then `Q: "..." → answer` per question.

### Display-Only Widgets

**Progress tracker** — show multi-step task progress (no user response):
```bash
cat <<'EOF' | ~/.claude/skills/dispatch-app/scripts/reply-widget "{{chat_id}}" progress_tracker
{{"title":"Deploying update","steps":[{{"label":"Building","status":"complete"}},{{"label":"Testing","status":"in_progress"}},{{"label":"Deploying"}}]}}
EOF
```
Statuses: `pending` (default), `in_progress`, `complete`, `error`. 1-10 steps. Optional `title` and `detail` per step.

**Map pin** — show locations, tap opens Apple Maps:
```bash
cat <<'EOF' | ~/.claude/skills/dispatch-app/scripts/reply-widget "{{chat_id}}" map_pin
{{"title":"Meeting spot","pins":[{{"latitude":42.36,"longitude":-71.06,"label":"Boston Common"}}]}}
EOF
```
1-10 pins. Optional `title`, optional `label` per pin, `zoom` 1-20 (default 14).
"""

        # Add markdown rendering note for backends that support it
        if backend.supports_markdown:
            content += """
## Markdown Rendering

**Your messages are rendered as markdown in the app.** Use markdown formatting in your responses:
- **Bold**, *italic*, `inline code`, code blocks, headers, lists, links, blockquotes
- The renderer handles: bold, italic, bold+italic, inline code, fenced code blocks, headers (H1-H6), blockquotes, bullet/ordered lists, horizontal rules, and links
- Use markdown intentionally to make responses clear and scannable
"""

        # Substitute {chat_id} placeholder with actual chat ID from transcript dir name
        # e.g., for dispatch-app backend, transcript_dir = ~/transcripts/dispatch-app/{chat_id}
        actual_chat_id = str(transcript_dir.name)
        content = content.replace('"{chat_id}"', f'"{actual_chat_id}"')

        claude_md_path.write_text(content)
        log.info(f"Created {backend.label}-specific CLAUDE.md at {claude_md_path}")

    async def _resolve_group_participants(self, chat_id: str) -> list:
        """Resolve group participants from chat.db and contacts (async, non-blocking)."""
        def _sync_resolve():
            import sqlite3
            from assistant.common import MESSAGES_DB
            try:
                conn = sqlite3.connect(str(MESSAGES_DB), timeout=5)
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT h.id
                    FROM handle h
                    JOIN chat_handle_join chj ON h.ROWID = chj.handle_id
                    JOIN chat c ON chj.chat_id = c.ROWID
                    WHERE c.chat_identifier = ?
                """, (chat_id,))
                phones = [row[0] for row in cursor.fetchall()]
                conn.close()
                return phones
            except Exception as e:
                log.warning(f"Failed to resolve group participants for {chat_id}: {e}")
                return []

        phones = await asyncio.get_event_loop().run_in_executor(None, _sync_resolve)

        names = []
        for phone in phones:
            if self.contacts:
                contact = self.contacts.lookup_identifier(phone)
                if contact:
                    names.append(contact["name"])
                else:
                    names.append(phone)
            else:
                names.append(phone)
        return names

    async def _get_memory_summary(self, contact_name: str) -> str:
        """Get memory summary for a contact from DuckDB (async, non-blocking)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                UV, "run", str(SKILLS_DIR / "memory/scripts/memory.py"), "summary", contact_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(SKILLS_DIR / "memory"),
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            output = stdout.decode().strip()
            if output and not output.startswith("SUMMARY|") and "No memories" not in output:
                return output
        except Exception as e:
            log.warning(f"Could not load memory summary for {contact_name}: {e}")
        return ""

    async def _get_soul_content(self) -> str:
        """Load SOUL.md content for session identity (async, non-blocking).

        Returns the full content of ~/.claude/SOUL.md which defines the assistant's
        identity, personality, and core values. Same for all sessions.
        """
        try:
            soul_path = HOME / ".claude" / "SOUL.md"
            if soul_path.exists():
                return await asyncio.get_event_loop().run_in_executor(
                    None, soul_path.read_text
                )
        except Exception as e:
            log.warning(f"Could not load SOUL.md: {e}")
        return ""

    async def _get_chat_context(self, session_name: str) -> str:
        """Load CONTEXT.md for a chat session (async, non-blocking).

        Returns conversation context (ongoing projects, pending tasks, recent topics)
        that was extracted by the nightly consolidation job.
        """
        try:
            # session_name format: imessage/_15555550100 or imessage/ab3876ca...
            parts = session_name.split("/", 1)
            if len(parts) == 2:
                backend, chat_id = parts
                context_file = HOME / "transcripts" / backend / chat_id / "CONTEXT.md"
                if context_file.exists():
                    content = await asyncio.get_event_loop().run_in_executor(
                        None, context_file.read_text
                    )
                    # Skip empty or just-header files
                    if content and ("## Ongoing" in content or "## Pending" in content or "## Recent Topics" in content):
                        return content
        except Exception as e:
            log.warning(f"Could not load CONTEXT.md for {session_name}: {e}")
        return ""
