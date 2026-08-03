"""
SDKSession: Wraps ClaudeSDKClient with async message queue for injection.

Each SDKSession manages a single Claude agent session for one contact.
Messages are sent via query() immediately and received by a background
receiver task using receive_messages(). This allows mid-turn steering:
new messages are injected while Claude is processing, and Claude sees
them as UserMessages between tool calls.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import time
import uuid
from datetime import datetime, timedelta
from uuid import uuid4
from pathlib import Path
from typing import Any, NamedTuple, Optional

from typing import TYPE_CHECKING


class QueueItem(NamedTuple):
    """Item in the session message queue. message_id is None for sentinels."""
    message_id: str | None
    text: str

from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    UserMessage,
    PermissionResultAllow,
    PermissionResultDeny,
    HookMatcher,
)

# Patch SDK message parser to handle unknown message types gracefully.
# The bundled parser raises MessageParseError for new types like rate_limit_event,
# which kills the receive iterator and crashes the session.
try:
    import claude_agent_sdk._internal.message_parser as _mp
    import claude_agent_sdk._internal.client as _client

    _original_parse = _mp.parse_message

    def _tolerant_parse(data):
        try:
            return _original_parse(data)
        except Exception:
            return SystemMessage(subtype=data.get("type", "unknown"), data=data)

    _client.parse_message = _tolerant_parse  # type: ignore[assignment]
except (ImportError, AttributeError):
    pass  # SDK mocked in tests

if TYPE_CHECKING:
    from claude_agent_sdk.types import (
        SyncHookJSONOutput,
        HookContext,
        PreToolUseHookInput,
        PostToolUseHookInput,
        PostToolUseFailureHookInput,
        UserPromptSubmitHookInput,
        StopHookInput,
        SubagentStopHookInput,
        PreCompactHookInput,
        NotificationHookInput,
    )
    # Union of all hook input types
    HookInputType = (
        PreToolUseHookInput | PostToolUseHookInput | PostToolUseFailureHookInput |
        UserPromptSubmitHookInput | StopHookInput | SubagentStopHookInput |
        PreCompactHookInput | NotificationHookInput
    )

from assistant.common import AUTH_FAILURE_FLAG, SESSION_EFFORT, SKILLS_DIR, UV
from assistant import perf, auth_mode
from assistant.bus_helpers import produce_event, produce_session_event, compaction_triggered_payload

# Script path fragments that indicate a message send
_SEND_SCRIPT_PATTERNS = (
    "/scripts/send-sms",
    "/scripts/send-signal",
    "/scripts/send-signal-group",
    "/scripts/reply",
    "/scripts/reply-sven",
)

def _is_send_command(cmd: str) -> bool:
    """Check if a Bash command is a message send.

    Matches the executable (first token) against known send script paths.
    Strips outer quotes to extract the command path.
    """
    # Extract first token (the executable), stripping quotes
    stripped = cmd.strip().strip('"').strip("'")
    first_token = stripped.split()[0] if stripped else ""
    return any(first_token.endswith(pattern) for pattern in _SEND_SCRIPT_PATTERNS)

log = logging.getLogger(__name__)

# Pin model aliases to current full model IDs. The SDK forwards both `model`
# and `betas` to the `claude` CLI as `--model` / `--betas`, which works
# identically for OAuth (Max plan) and ANTHROPIC_API_KEY auth modes.
# CLI auth failures surface as assistant text, not a typed error — the turn
# "completes" in ~20ms with is_error=true and the session otherwise looks
# healthy (Aug 2026 outage: OAuth refresh token died, sessions silently ate
# every message for a day). Matched text raises the auth-failure flag that the
# oauth_token dependency check escalates on.
_AUTH_FAILURE_RE = re.compile(
    r"Failed to authenticate"
    r"|OAuth (?:session|token) (?:has )?expired"
    r"|could not be refreshed"
    r"|Invalid API key"
    r"|authentication_error",
    re.IGNORECASE,
)

_MODEL_ALIASES = {
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5",
}


def _resolve_model_and_betas(model: str) -> tuple[str, list[str]]:
    """Resolve a model alias or full ID to (model_id, betas).

    Opus 4.7 and Sonnet 4.x get the 1M-context beta header. Opus 5 has 1M
    context by default (no beta needed); Haiku and older versions pass
    through with no betas.
    """
    model_id = _MODEL_ALIASES.get(model, model)
    betas: list[str] = []
    if model_id.startswith("claude-opus-4-7") or model_id.startswith("claude-sonnet-4"):
        betas.append("context-1m-2025-08-07")
    return model_id, betas


# Per-session log directory
SESSION_LOG_DIR = Path.home() / "dispatch/logs/sessions"
SESSION_LOG_DIR.mkdir(parents=True, exist_ok=True)


def _get_session_logger(session_name: str) -> logging.Logger:
    """Create a per-session logger with rotating file handler."""
    from logging.handlers import RotatingFileHandler

    logger = logging.getLogger(f"session.{session_name}")
    # Close existing handlers to prevent FD leaks on session restarts
    for h in logger.handlers[:]:
        try:
            h.close()
        except Exception:
            pass
        logger.removeHandler(h)
    if True:
        handler = RotatingFileHandler(
            SESSION_LOG_DIR / f"{session_name}.log",
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=5,
        )
        handler.setFormatter(logging.Formatter(
            '%(asctime)s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S',
        ))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


class SDKSession:
    """Manages a single Claude Agent SDK session using ClaudeSDKClient.

    Uses concurrent send/receive architecture: a background receiver task
    runs receive_messages() continuously while the sender dispatches
    query() calls immediately from the queue. This enables mid-turn
    steering — new messages reach Claude between tool calls without
    waiting for the current turn to finish.
    """

    def __init__(
        self,
        chat_id: str,
        contact_name: str,
        tier: str,
        cwd: str,
        session_type: str = "individual",
        source: str = "imessage",
        model: str = "opus",
        producer=None,
        resume_id: Optional[str] = None,
        *,
        system_prompt_override: str | None = None,
        extra_disallowed_tools: tuple[str, ...] = (),
        bash_deny_regex: tuple[str, ...] = (),
    ):
        self.chat_id = chat_id
        self.contact_name = contact_name
        self.tier = tier
        self.cwd = cwd
        self.session_type = session_type
        self.source = source
        self.model = model
        self._producer = producer
        self.resume_id = resume_id

        self._sp_override: str | None = system_prompt_override
        self._extra_disallowed: tuple[str, ...] = tuple(extra_disallowed_tools)
        # Pre-compile bash deny regexes; failures are skipped with a warning at first use.
        self._bash_deny_regex: tuple[re.Pattern[str], ...] = tuple(
            re.compile(pat) for pat in bash_deny_regex
        )

        # Cached session_name using get_session_name (properly strips registry prefix)
        from assistant.common import get_session_name
        self._session_name = get_session_name(self.chat_id, self.source)

        self._client: Optional[ClaudeSDKClient] = None
        self._message_queue: asyncio.Queue[QueueItem] = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self._pending_queries = 0  # Tracks in-flight queries; reset to 0 on ResultMessage
        self.running = False

        # Metrics
        self.session_id: Optional[str] = None
        self.turn_count = 0
        self.created_at = datetime.now()
        self.last_activity = datetime.now()
        self.last_inject_at: Optional[datetime] = None  # When last message was injected
        self.last_response_at: datetime = datetime.now()  # When last ResultMessage received
        self.last_tool_activity_at: Optional[datetime] = None  # When last ToolResultBlock received
        self._error_count = 0
        self._consecutive_error_turns = 0
        self.last_assistant_text: str | None = None

        # Heartbeat tracking
        self._last_heartbeat_at: float = time.time()

        # Compaction state — conditional notification (only notify if user messages during compaction)
        self.compacting_since: Optional[float] = None       # time.monotonic() when compaction started
        self.compaction_notified: bool = False               # whether we sent "compacting…" notice
        self.compaction_notified_at: Optional[float] = None  # time.monotonic() when notice was sent (for 3s debounce)
        self.compaction_count: int = 0                       # per-process-lifetime counter for diagnostics
        self.compaction_epoch: int = 0                       # increments each compaction, guards stale bus events

        # Deferred system prompt injection (set by sdk_backend, consumed by _inject_system_prompt_if_needed)
        self._needs_system_prompt: bool = False
        self._system_prompt_args: tuple | None = None
        self._system_prompt_type: str | None = None  # "individual" or "group"
        self._restart_role: str | None = None  # "initiator", "passive", or None (fresh)

        # Block limit suppression: when the API returns a "You've hit your limit" message,
        # we send ONE notification and suppress further responses until the limit resets.
        self._block_limit_notified: bool = False
        self._block_limit_until: Optional[datetime] = None  # When the limit resets

        # Tool execution timing: maps tool_use_id -> (start_time, tool_input, tool_name)
        self._pending_tools: dict[str, tuple[float, dict, str]] = {}

        # Per-session logger
        from assistant.common import get_session_name
        session_name = get_session_name(chat_id, source)
        log_name = session_name.replace("/", "-")
        self._log = _get_session_logger(log_name)

    def get_transcript_file(self) -> Optional[Path]:
        """Return path to this session's active transcript JSONL."""
        from assistant.health import _find_transcript
        return _find_transcript(self.cwd, self.session_id)

    def check_compacting(self) -> bool:
        """Check if session is compacting, with 2-min safety auto-clear.

        NOTE: This is a method, not a property, because it has side effects
        (logging, bus event) on auto-clear. Called from inject_message path
        to determine if user should be notified about ongoing compaction.

        Timeout rationale: real compactions complete in seconds. 2 minutes is
        generous enough to avoid false clears while keeping sessions from
        staying degraded if the PostCompact bus event is lost (e.g., post-compact-hook
        bash script fails silently, uv unavailable, bus write error).
        """
        if self.compacting_since is not None:
            elapsed = time.monotonic() - self.compacting_since
            if elapsed > 120:
                from assistant.common import get_session_name
                session_name = get_session_name(self.chat_id, self.source)
                self._log.warning(f"COMPACTION_AUTO_CLEAR | session={session_name} | stale flag ({elapsed:.0f}s >2min)")
                log.warning(f"[{self.contact_name}] Compaction auto-cleared after {elapsed:.0f}s")
                produce_event(self._producer, "system", "compaction.auto_cleared",
                    {"session_name": session_name, "chat_id": self.chat_id,
                     "contact_name": self.contact_name, "elapsed_s": round(elapsed, 1)},
                    source="compaction")
                self._clear_compaction_flags()
                return False
            return True
        return False

    def _clear_compaction_flags(self):
        """Reset compaction timing/notification state.

        Preserves compaction_epoch intentionally — epoch persists across
        compactions for stale bus event rejection.
        """
        self.compacting_since = None
        self.compaction_notified = False
        self.compaction_notified_at = None

    async def start(self, resume_session_id: Optional[str] = None):
        """Connect ClaudeSDKClient and start the message processing loop."""
        # Clear compaction flags on start — monotonic timestamps don't survive restarts
        self._clear_compaction_flags()
        options = self._build_options(resume_session_id)
        self._client = ClaudeSDKClient(options=options)
        await self._client.connect()
        self.running = True
        self._task = asyncio.create_task(self._run_loop())
        self._log.info(f"SESSION_START | resume={resume_session_id} | tier={self.tier}")
        log.info(f"[{self.contact_name}] SDK session started (resume={resume_session_id})")

    async def _kill_subprocess(self):
        """Kill the Claude CLI subprocess to prevent zombies.

        Called from both stop() and _run_loop's finally block to ensure
        the subprocess is terminated when the session ends or crashes.
        """
        if not self._client:
            return

        try:
            transport = getattr(self._client, '_transport', None)
            if transport:
                process = getattr(transport, '_process', None)
                if process and process.returncode is None:
                    self._log.info("SUBPROCESS_KILL | Terminating Claude CLI subprocess")
                    process.terminate()
                    # Give it a moment to terminate gracefully
                    try:
                        await asyncio.wait_for(process.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        process.kill()  # Force kill if terminate didn't work
                        self._log.warning("SUBPROCESS_KILL | Had to force kill")
        except Exception as e:
            self._log.warning(f"SUBPROCESS_KILL_ERROR | {e}")
        finally:
            self._client = None

    async def stop(self):
        """Stop the session by cancelling its task and killing the subprocess.

        We explicitly kill the subprocess to prevent zombie sessions from
        continuing to run after restart. The SDK's disconnect() method can
        cause anyio cancel scope issues, so we terminate the subprocess directly.
        """
        self.running = False

        # First cancel the task to stop the receive loop
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        await self._kill_subprocess()

        self._log.info(f"SESSION_STOP | turns={self.turn_count}")
        log.info(f"[{self.contact_name}] SDK session stopped (turns={self.turn_count})")

    async def inject(self, text: str, *, replay_source_message_id: str | None = None):
        """Queue a message for delivery to the Claude session.

        Args:
            replay_source_message_id: If this inject is a replay of a previously
                lost message, the original message_id for lineage tracing.
        """
        # Sentinel passes through without WAL
        if text == "__SHUTDOWN__":
            await self._message_queue.put(QueueItem(None, "__SHUTDOWN__"))
            return

        # Write-ahead: persist to bus BEFORE memory queue
        # Bus failure is non-fatal — degrade to memory-only (pre-fix behavior)
        message_id = str(uuid4())
        if self._producer:
            try:
                payload = {
                    "message_id": message_id,
                    "chat_id": self.chat_id,
                    "text": text,
                    "source": self.source,
                }
                if replay_source_message_id:
                    payload["replay_source_message_id"] = replay_source_message_id
                produce_event(self._producer, "messages", "message.queued",
                    payload, key=self.chat_id, source="sdk_session")
            except Exception as e:
                self._log.warning(f"WAL_WRITE_FAILED | msg_id={message_id[:8]} | {e}")

        queue_depth = self._message_queue.qsize()
        await self._message_queue.put(QueueItem(message_id, text))

        self.last_inject_at = datetime.now()
        perf.gauge("sdk_queue_depth", queue_depth + 1, component="session", contact=self.contact_name)
        self._log.info(f"QUEUED | msg_id={message_id[:8]} | len={len(text)} | queue_depth={queue_depth + 1}")

    @property
    def is_busy(self) -> bool:
        return self._pending_queries > 0

    @property
    def is_block_limited(self) -> bool:
        """Check if session is in block-limit suppression mode.

        Returns True if we've already notified the user about a block limit
        and the limit hasn't expired yet.
        """
        if not self._block_limit_notified:
            return False
        if self._block_limit_until and datetime.now() >= self._block_limit_until:
            # Limit has expired, clear the flag
            self._block_limit_notified = False
            self._block_limit_until = None
            self._log.info("BLOCK_LIMIT | expired, resuming normal operation")
            return False
        return True

    # How long to suppress messages after detecting a block limit.
    # 2 hours is generous — API block limits typically reset every 5 hours.
    # Using a fixed duration avoids fragile parsing of the API's reset time text,
    # which is not a stable contract and varies by timezone.
    BLOCK_LIMIT_SUPPRESS_SECONDS = 7200  # 2 hours

    def _detect_block_limit(self, text: str):
        """Detect block limit messages from the API and set suppression flag.

        Uses simple keyword matching — no time parsing. The API message format
        (e.g., "You've hit your limit · resets 10am") is not a stable contract,
        so we suppress for a fixed duration instead of trying to parse reset times.
        """
        if not text:
            return
        # Match patterns like "hit your limit", "hit the limit", "reached your limit"
        if not re.search(r"(hit|reached)\s+(your|the)\s+limit", text, re.IGNORECASE):
            return

        self._log.info(f"BLOCK_LIMIT | detected: {text}")
        self._block_limit_until = datetime.now() + timedelta(seconds=self.BLOCK_LIMIT_SUPPRESS_SECONDS)
        self._block_limit_notified = True
        self._log.info(f"BLOCK_LIMIT | suppressing for {self.BLOCK_LIMIT_SUPPRESS_SECONDS}s until {self._block_limit_until.isoformat()}")

    def _note_auth_failure(self, text: str) -> None:
        """Record an auth-failure turn so the oauth_token dependency check can escalate.

        Writes state/auth_failure.json (read by the dependency probe) and emits a
        session.auth_failure bus event. The flag is cleared by the first successful
        turn (any session) or by the probe once the keychain shows a fresh token.
        """
        self._log.error(f"AUTH_FAILURE | {text}")
        try:
            AUTH_FAILURE_FLAG.write_text(json.dumps({
                "ts": datetime.now().astimezone().isoformat(),
                "session_name": self._session_name,
                "detail": text[:300],
            }))
        except OSError as e:
            self._log.warning(f"AUTH_FAILURE | could not write flag: {e}")
        produce_event(self._producer, "sessions", "session.auth_failure", {
            "session_name": self._session_name,
            "chat_id": self.chat_id,
            "contact_name": self.contact_name,
            "detail": text[:300],
        }, key=self._session_name, source="sdk_session")

    @staticmethod
    def _clear_auth_failure_flag() -> None:
        try:
            AUTH_FAILURE_FLAG.unlink(missing_ok=True)
        except OSError:
            pass

    def _maybe_handle_oauth_quota_error(self, error_text: str) -> bool:
        """If error_text indicates OAuth quota exhaustion, switch to API key.

        Promotes ANTHROPIC_API_KEY_FALLBACK into ANTHROPIC_API_KEY (so the
        next subprocess spawned by the SDK uses API credits), persists the
        switch to state/auth_mode.json, and fires a health.quota_alert event.

        Returns True if the error was an OAuth quota error (caller should
        mark session for restart so the new subprocess inherits the key).
        Returns False otherwise — caller handles the error normally.
        """
        if not auth_mode.is_oauth_quota_error(error_text):
            return False
        if auth_mode.is_api_key_mode():
            # Already switched — this error came from API credits, not OAuth.
            return False

        self._log.warning(f"OAUTH_QUOTA_EXHAUSTED | falling back to API key | {error_text[:200]}")
        log.warning(f"[{self.contact_name}] OAuth quota exhausted, falling back to API key")

        promoted = auth_mode.promote_fallback_key()
        auth_mode.write_mode(
            "api_key" if promoted else "oauth",
            reason="oauth_quota_exhausted" if promoted else "oauth_quota_exhausted_no_fallback",
            error_text=error_text,
            session_name=self._session_name,
        )

        if self._producer:
            try:
                produce_event(self._producer, "system", "health.quota_alert", {
                    "schema_v": 1,
                    "source": "oauth",
                    "quota_type": "oauth_exhausted",
                    "session_name": self._session_name,
                    "chat_id": self.chat_id,
                    "contact_name": self.contact_name,
                    "error_text": error_text[:500],
                    "auth_mode_after": "api_key" if promoted else "oauth",
                    "fallback_promoted": promoted,
                }, key=self._session_name, source="sdk_session")
            except Exception as bus_err:
                self._log.warning(f"OAUTH_QUOTA_BUS_WRITE_FAILED | {bus_err}")

        return True

    def is_alive(self) -> bool:
        return self.running and self._task is not None and not self._task.done()

    def is_healthy(self) -> tuple[bool, str]:
        """Check session health. Returns (healthy, reason) tuple.

        The reason string describes why the session is unhealthy, or "ok" if healthy.
        """
        if not self.is_alive():
            return False, "dead"
        if self._error_count >= 3:
            return False, f"error_count={self._error_count}"
        # API errors (e.g. context too large, image size limits)
        if self._consecutive_error_turns >= 3:
            return False, f"consecutive_error_turns={self._consecutive_error_turns}"
        # Stale: messages pending but no activity for 10+ min
        if self._message_queue.qsize() > 0:
            idle = (datetime.now() - self.last_activity).total_seconds()
            if idle > 600:
                return False, f"stale_queue(qsize={self._message_queue.qsize()}, idle={idle:.0f}s)"
        # Stuck: message was injected but no ResultMessage received for 10+ min.
        # Catches silent SDK connection hangs where process is alive but not responding.
        # When this triggers, check_session_health() launches a Haiku investigation
        # to determine if the session is genuinely stuck or just running a long operation.
        #
        # Sessions that have never completed a turn (turn_count == 0) are still
        # initializing (processing system prompt, compacting large resume context).
        # Use a longer timeout (30 min) to avoid false-positive kills during init.
        if self.last_inject_at and self.last_inject_at > self.last_response_at:
            stuck_seconds = (datetime.now() - self.last_inject_at).total_seconds()
            stuck_threshold = 1800 if self.turn_count == 0 else 600  # 30 min for init, 10 min otherwise
            if stuck_seconds > stuck_threshold:
                # If tools are still completing, the session is actively working
                # (e.g. Agent subagents, long Bash commands). Don't flag as stuck
                # unless tool activity also stalled for 10+ min.
                # Hard cap at 60 min to catch truly infinite loops.
                if self.last_tool_activity_at and self.last_tool_activity_at > self.last_response_at:
                    tool_idle = (datetime.now() - self.last_tool_activity_at).total_seconds()
                    if tool_idle < 600 and stuck_seconds < 3600:
                        return True, "ok"  # Tools still active, not stuck
                return False, f"stuck(inject={stuck_seconds:.0f}s_ago)"
        return True, "ok"

    async def interrupt(self):
        """Interrupt a stuck session."""
        if self._client:
            await self._client.interrupt()
            self._log.info("INTERRUPTED")
            log.info(f"[{self.contact_name}] Session interrupted")

    async def _run_loop(self):
        """Main loop: start background receiver, then send queries from queue.

        The receiver runs receive_messages() continuously in the background.
        The sender dispatches query() calls immediately — Claude sees new
        messages as UserMessages between tool calls (mid-turn steering).
        Multiple queries during a single turn merge into one ResultMessage.
        """
        receiver = asyncio.create_task(self._receive_loop())
        try:
            while self.running:
                # Check if receiver crashed
                if receiver.done():
                    self._log.warning("RECEIVER_CRASHED | Exiting main loop")
                    break

                try:
                    message_id, msg = await asyncio.wait_for(
                        self._message_queue.get(), timeout=30
                    )
                except asyncio.TimeoutError:
                    # Emit heartbeat every 2 min to prove session loop is responsive
                    now = time.time()
                    if now - self._last_heartbeat_at >= 120:
                        produce_event(self._producer, "system", "session.heartbeat", {
                            "session_name": self._session_name,
                            "chat_id": self.chat_id,
                            "contact_name": self.contact_name,
                            "queue_depth": self._message_queue.qsize(),
                            "pending_queries": self._pending_queries,
                            "turn_count": self.turn_count,
                            "is_busy": self.is_busy,
                            "idle_seconds": round((datetime.now() - self.last_activity).total_seconds()),
                        }, key=self._session_name, source="sdk")
                        self._last_heartbeat_at = now
                    continue  # Check receiver health every 30s

                # Sentinel from _receive_loop signals shutdown
                if msg == "__SHUTDOWN__":
                    self._log.info("SHUTDOWN_SENTINEL | Receiver requested shutdown")
                    break

                wake_start = time.time()
                self.last_activity = datetime.now()
                self._log.info(f"IN | {msg}")
                self._pending_queries += 1
                if self._producer and self._pending_queries == 1:  # 0→1 transition
                    self._producer.set_session_busy(
                        self._session_name, True
                    )

                try:
                    assert self._client is not None
                    await self._client.query(msg)
                    # Log wake latency - time from queue get to query completion
                    wake_ms = (time.time() - wake_start) * 1000
                    perf.timing("session_wake_latency_ms", wake_ms, component="session", contact=self.contact_name)
                    # Mark as delivered — message_id came WITH the message from queue
                    if message_id and self._producer:
                        try:
                            produce_event(self._producer, "messages", "message.delivered", {
                                "message_id": message_id,
                                "chat_id": self.chat_id,
                            }, key=self.chat_id, source="sdk_session")
                        except Exception:
                            pass  # Non-fatal: message was delivered to Claude regardless
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self._pending_queries = max(0, self._pending_queries - 1)
                    if self._producer and self._pending_queries == 0:
                        self._producer.set_session_busy(
                            self._session_name, False
                        )
                    self._error_count += 1
                    self._log.error(f"ERROR #{self._error_count} | {e}")
                    log.error(f"[{self.contact_name}] Query error #{self._error_count}: {e}")
                    # If this is OAuth quota exhaustion, switch to API-key mode
                    # and exit the loop immediately so the manager respawns the
                    # subprocess with ANTHROPIC_API_KEY set.
                    if self._maybe_handle_oauth_quota_error(str(e)):
                        self.running = False
                        break
                    if self._error_count >= 3:
                        self._log.error("MAX_ERRORS | Session dead")
                        self.running = False
                        break
                    await asyncio.sleep(2 * self._error_count)

        except asyncio.CancelledError:
            self._log.info("LOOP_CANCELLED")
            raise
        finally:
            # Clear busy state on session shutdown
            if self._producer:
                self._producer.set_session_busy(
                    self._session_name, False
                )
            receiver.cancel()
            try:
                await receiver
            except asyncio.CancelledError:
                pass
            # Kill subprocess to prevent zombies when receiver crashes
            await self._kill_subprocess()

    async def _receive_loop(self):
        """Background receiver: continuously handle all messages from the SDK.

        Uses receive_messages() (infinite async iterator) instead of
        receive_response() (stops at ResultMessage). This allows the
        receiver to span multiple merged turns.
        """
        try:
            assert self._client is not None
            async for message in self._client.receive_messages():
                await self._handle_message(message)
                # Refresh busy flag periodically so updated_at stays fresh
                # during long operations (prevents staleness guard false negatives)
                if self._producer and self.is_busy:
                    self._producer.set_session_busy(self._session_name, True)
                if isinstance(message, ResultMessage):
                    self._pending_queries = 0  # Reset: merged queries produce 1 ResultMessage
                    if self._producer:
                        self._producer.set_session_busy(
                            self._session_name, False
                        )
                    self._error_count = 0
                    # Note: _consecutive_error_turns is tracked in _handle_message
                    # Cleanup stale pending tools (edge case: tool call never got result)
                    self._cleanup_stale_pending_tools()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self._error_count += 1
            self._log.error(f"RECEIVER_ERROR #{self._error_count} | {e}")
            log.error(f"[{self.contact_name}] Receiver error: {e}")
            # Populate sdk_events for error tracing
            if self._producer and hasattr(self._producer, 'send_sdk_event'):
                self._producer.send_sdk_event(
                    session_name=self._session_name,
                    chat_id=self.chat_id,
                    event_type="error",
                    is_error=True,
                    payload=str(e),
                )
            # Buffer overflow is fatal - the SDK connection is broken
            error_str = str(e).lower()
            is_fatal = "buffer" in error_str or "1048576" in error_str
            # OAuth quota exhaustion: also fatal, but we want a clean restart
            # so the new subprocess inherits ANTHROPIC_API_KEY.
            quota_handled = self._maybe_handle_oauth_quota_error(str(e))
            produce_session_event(self._producer, self.chat_id, "session.receive_error", {
                "error": str(e), "error_count": self._error_count,
                "is_fatal": is_fatal or quota_handled,
                "oauth_quota_fallback": quota_handled,
                "contact_name": self.contact_name,
            }, source="sdk")
            if quota_handled:
                self._log.error("RECEIVER_FATAL | OAuth quota exhausted - flipped to api_key, marking session dead for restart")
                self.running = False
                try:
                    self._message_queue.put_nowait(QueueItem(None, "__SHUTDOWN__"))
                except Exception:
                    pass
            elif is_fatal:
                self._log.error("RECEIVER_FATAL | Buffer overflow - marking session dead")
                self.running = False
                # Wake _run_loop immediately instead of waiting for 30s timeout
                try:
                    self._message_queue.put_nowait(QueueItem(None, "__SHUTDOWN__"))
                except Exception:
                    pass
            elif self._error_count >= 3:
                self._log.error("RECEIVER_DEAD | Stopping session")
                self.running = False
                try:
                    self._message_queue.put_nowait(QueueItem(None, "__SHUTDOWN__"))
                except Exception:
                    pass

    def _cleanup_stale_pending_tools(self) -> None:
        """Remove pending tools older than 30 minutes.

        Edge case handling: if a tool call never receives a result (e.g. session
        killed mid-call), the entry would stay forever. This cleans them up.
        """
        now = time.perf_counter()
        stale_ids = [
            tid for tid, (start_time, _, _) in self._pending_tools.items()
            if now - start_time > 1800  # 30 minutes
        ]
        for tid in stale_ids:
            self._log.warning(f"PENDING_TOOL_STALE | tool_use_id={tid}")
            self._pending_tools.pop(tid, None)

    def _build_options(self, resume_id: Optional[str] = None) -> ClaudeAgentOptions:
        """Build ClaudeAgentOptions based on contact tier."""
        if self.tier in ("admin", "partner"):
            tools = [
                "Read", "Write", "Edit", "Bash", "Glob", "Grep",
                "WebSearch", "WebFetch", "Task", "NotebookEdit",
                "Skill", "AskUserQuestion",
            ]
            perm_mode = "bypassPermissions"
        elif self.tier == "family":
            tools = [
                "Read", "Write", "Edit", "Bash", "Glob", "Grep",
                "WebSearch", "WebFetch", "Task",
            ]
            perm_mode = "default"
        elif self.tier == "favorite":
            tools = ["Read", "WebSearch", "WebFetch", "Grep", "Glob", "Bash"]
            perm_mode = "default"
        else:
            # Group sessions, master, etc - full access
            tools = [
                "Read", "Write", "Edit", "Bash", "Glob", "Grep",
                "WebSearch", "WebFetch", "Task", "NotebookEdit",
                "Skill", "AskUserQuestion",
            ]
            perm_mode = "bypassPermissions"

        # No per-turn limit — quota monitoring system handles cost protection.
        # Previously had tiered limits (bug #15) but they caused sessions to
        # silently stop responding when turns ran out mid-task.
        turn_limit = None

        resolved_model, betas = _resolve_model_and_betas(self.model)
        fallback_model_id, _ = _resolve_model_and_betas("sonnet")

        opts = ClaudeAgentOptions(
            cli_path=Path.home() / ".local" / "bin" / "claude",  # Use system CLI (not bundled) for OAuth compat
            cwd=self.cwd,
            allowed_tools=tools,
            permission_mode=perm_mode,
            setting_sources=["project"],  # Load CLAUDE.md + skills from cwd
            model=resolved_model,
            fallback_model=None if resolved_model == fallback_model_id else fallback_model_id,  # Only triggers on 529
            betas=betas,  # type: ignore[arg-type]  # 1M context for opus 4.7 / sonnet 4.x
            effort=SESSION_EFFORT,
            max_turns=turn_limit,
            max_buffer_size=10 * 1024 * 1024,  # 10MB - prevents crash on large Task outputs
            hooks={
                "PreToolUse": [HookMatcher(matcher="Read", hooks=[self._resize_image_hook])],  # type: ignore[list-item]
                "Stop": [HookMatcher(hooks=[self._stop_hook])],  # type: ignore[list-item]
                "PreCompact": [HookMatcher(hooks=[self._pre_compact_hook])],  # type: ignore[list-item]
                # PostCompact: handled by settings.json command hook (bin/post-compact-hook)
                # which produces compaction.completed bus event. Manager consumes that event
                # and handles conditional "done" notification. SDK doesn't support PostCompact
                # as a Python hook type.
            }
        )

        if self._sp_override:
            opts.system_prompt = self._sp_override

        if self._extra_disallowed:
            existing = list(getattr(opts, "disallowed_tools", []) or [])
            for t in self._extra_disallowed:
                if t not in existing:
                    existing.append(t)
            opts.disallowed_tools = existing

        # Permission callback for tier-based security enforcement.
        # NOTE: family tier is intentionally excluded from SDK-level blocking.
        # Family restrictions are enforced via system prompt only (family-rules.md),
        # which allows the admin to override via inject-prompt when needed.
        # Do NOT add SDK-level can_use_tool blocks for family — admin explicitly
        # rejected this ("revert", "do not do sdk level blocks"). Only favorites
        # use hard SDK-level blocks here.
        if self.tier == "favorite" or self._bash_deny_regex:
            opts.can_use_tool = self._permission_check

        # Session resume: use --resume <session_id> to continue a previous session.
        # CLI 2.1+ error: "--session-id can only be used with --continue or --resume
        # if --fork-session is also specified" — means --session-id + --resume needs
        # --fork-session. Fix: use --resume <id> alone (no --session-id).
        # For fresh sessions: --session-id <uuid> alone works fine (sets ID for new session).
        if resume_id or self.resume_id:
            session_id = resume_id or self.resume_id
            opts.extra_args = {"resume": session_id}
        else:
            fresh_session_id = str(uuid.uuid4())
            opts.extra_args = {"session-id": fresh_session_id}

        return opts

    async def _permission_check(self, tool_name: str, tool_input: dict[str, Any], context: Any) -> PermissionResultAllow | PermissionResultDeny:
        """Security callback: enforce tier-based tool restrictions.

        Runs before every tool call. Better than system prompt alone -
        survives compaction.
        """
        self._log.info(f"PERM_CHECK | tool={tool_name} tier={self.tier}")

        if self._bash_deny_regex and tool_name == "Bash":
            cmd = tool_input.get("command", "") or ""
            for pat in self._bash_deny_regex:
                if pat.search(cmd):
                    produce_session_event(self._producer, self.chat_id, "permission.denied", {
                        "tool_name": tool_name, "tier": self.tier,
                        "reason": f"Bash command blocked by deny regex: {pat.pattern}",
                        "contact_name": self.contact_name,
                    }, source="sdk")
                    return PermissionResultDeny(message=f"Bash command blocked by deny regex: {pat.pattern}")

        if self.tier == "favorite":
            # Block file modifications
            if tool_name in ("Write", "Edit", "NotebookEdit"):
                produce_session_event(self._producer, self.chat_id, "permission.denied", {
                    "tool_name": tool_name, "tier": self.tier,
                    "reason": f"{tool_name} blocked for favorites tier",
                    "contact_name": self.contact_name,
                }, source="sdk")
                return PermissionResultDeny(message=f"{tool_name} blocked for favorites tier")
            # Block dangerous bash
            if tool_name == "Bash":
                cmd = tool_input.get("command", "")
                if not cmd.startswith("osascript"):
                    produce_session_event(self._producer, self.chat_id, "permission.denied", {
                        "tool_name": tool_name, "tier": self.tier,
                        "reason": "Only osascript allowed for favorites tier",
                        "contact_name": self.contact_name,
                    }, source="sdk")
                    return PermissionResultDeny(message="Only osascript allowed for favorites tier")
                # Block osascript commands that attempt shell escape via "do shell script"
                if re.search(r'do\s+shell\s+script', cmd, re.IGNORECASE):
                    produce_session_event(self._producer, self.chat_id, "permission.denied", {
                        "tool_name": tool_name, "tier": self.tier,
                        "reason": "osascript 'do shell script' blocked for favorites tier",
                        "contact_name": self.contact_name,
                    }, source="sdk")
                    return PermissionResultDeny(message="osascript 'do shell script' blocked for favorites tier")
                # Block JXA (JavaScript for Automation) which can bypass AppleScript restrictions
                if re.search(r'-l\s+(JavaScript|js)\b', cmd, re.IGNORECASE):
                    produce_session_event(self._producer, self.chat_id, "permission.denied", {
                        "tool_name": tool_name, "tier": self.tier,
                        "reason": "osascript JXA (-l JavaScript) blocked for favorites tier",
                        "contact_name": self.contact_name,
                    }, source="sdk")
                    return PermissionResultDeny(message="osascript JXA (-l JavaScript) blocked for favorites tier")
            # Block sensitive file reads
            if tool_name == "Read":
                path = tool_input.get("file_path", "")
                if any(s in path for s in [".ssh", ".env", "credentials", "secrets"]):
                    produce_session_event(self._producer, self.chat_id, "permission.denied", {
                        "tool_name": tool_name, "tier": self.tier,
                        "reason": "Sensitive file blocked for favorites tier",
                        "contact_name": self.contact_name,
                    }, source="sdk")
                    return PermissionResultDeny(message="Sensitive file blocked for favorites tier")

        return PermissionResultAllow()

    async def _resize_image_hook(self, input_data: "HookInputType", tool_use_id: str | None, context: "HookContext") -> "SyncHookJSONOutput":
        """PreToolUse hook for Read: block oversized images to prevent API errors.

        The API rejects images >2000px in multi-image conversations.
        If an image is too large, deny the read and tell Claude to resize first.
        """
        IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff"}
        MAX_DIM = 2000  # API limit for multi-image (>20 images) requests

        tool_input = input_data.get("tool_input", {})
        file_path = tool_input.get("file_path", "")
        if not file_path:
            return {}

        ext = Path(file_path).suffix.lower()
        if ext not in IMAGE_EXTS:
            return {}

        if not Path(file_path).exists():
            return {}

        try:
            result = subprocess.run(
                ["sips", "-g", "pixelWidth", "-g", "pixelHeight", file_path],
                capture_output=True, text=True, timeout=5,
            )
            width = height = 0
            for line in result.stdout.splitlines():
                if "pixelWidth" in line:
                    width = int(line.split(":")[-1].strip())
                elif "pixelHeight" in line:
                    height = int(line.split(":")[-1].strip())

            if width <= MAX_DIM and height <= MAX_DIM:
                return {}

            self._log.info(f"IMAGE_TOO_LARGE | {file_path} | {width}x{height}")
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        f"Image is {width}x{height}px. The API rejects images >2000px when there are many images in the conversation. "
                        f"Resize first: sips --resampleHeightWidthMax {MAX_DIM} \"{file_path}\" then read again."
                    ),
                },
            }
        except Exception as e:
            self._log.warning(f"IMAGE_CHECK_ERROR | {file_path} | {e}")
            return {}

    async def _pre_compact_hook(self, input_data: "HookInputType", tool_use_id: str | None, context: "HookContext") -> "SyncHookJSONOutput":
        """PreCompact hook: set compaction flag and let Claude Code compact natively.

        Fires when the CLI's context window is about to be compacted.
        Sets session compaction state so the manager can conditionally notify
        users who message during the compaction window. No SMS is sent here —
        notifications are handled by the manager on message arrival.
        """
        from assistant.common import get_session_name
        session_name = get_session_name(self.chat_id, self.source)

        # Guard: if already compacting (shouldn't happen), preserve notification state
        # so we don't lose a pending "done compacting" message
        if self.compacting_since is not None:
            self._log.warning(f"PRECOMPACT | double compaction detected | session={session_name} | preserving notified={self.compaction_notified}")
            # Keep compaction_notified and compaction_notified_at as-is
            self.compacting_since = time.monotonic()
            self.compaction_count += 1
            self.compaction_epoch += 1
        else:
            # Fresh compaction — reset all state
            self.compacting_since = time.monotonic()
            self.compaction_notified = False
            self.compaction_notified_at = None
            self.compaction_count += 1
            self.compaction_epoch += 1

        self._log.info(f"PRECOMPACT | triggered | session={session_name} turns={self.turn_count} count={self.compaction_count}")
        log.info(f"[{self.contact_name}] PreCompact hook fired (turns={self.turn_count})")
        produce_event(self._producer, "system", "compaction.triggered",
            compaction_triggered_payload(session_name, self.chat_id, self.contact_name, self.turn_count,
                                        compaction_count=self.compaction_count, compaction_epoch=self.compaction_epoch),
            source="compaction")

        # Write epoch to transcript dir so bash post-compact-hook can include it in bus event
        if self.cwd:
            epoch_file = Path(self.cwd) / ".compaction_epoch"
            try:
                epoch_file.write_text(str(self.compaction_epoch))
            except OSError:
                pass  # Non-critical

        # Log to compactions.log for visibility (always, regardless of notification)
        compaction_log = Path.home() / "dispatch/logs/compactions.log"
        compaction_log.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(compaction_log, "a") as f:
            f.write(f"{timestamp} | HOOK | PreCompact fired for {session_name}\n")

        # Let native compaction proceed — no SMS here, manager handles notifications
        return {}

    # PostCompact is handled by settings.json command hook (bin/post-compact-hook).
    # The old _notification_hook workaround has been removed since the CLI now
    # supports PostCompact as a native hook event in settings.json.

    async def _stop_hook(self, input_data: "HookInputType", tool_use_id: str | None, context: "HookContext") -> "SyncHookJSONOutput":
        """Stop hook: remind Claude to send updates via send-sms if needed.

        Only fires if the session is processing an incoming message (not idle turns).
        Checks if send-sms was already called to avoid duplicate sends.
        """
        # Check if we recently sent a message by looking at recent tool use
        from assistant.backends import get_backend
        backend = get_backend(self.source)
        # Extract CLI name from send_cmd template (e.g. "send-sms" from '~/.claude/skills/sms-assistant/scripts/send-sms "{chat_id}"')
        send_marker = backend.send_cmd.split("/")[-1].split('"')[0].strip()
        response = getattr(context, 'response', None) or {}
        messages = response.get('messages', []) if isinstance(response, dict) else []
        already_sent = any(
            send_marker in str(msg.get('content', ''))
            for msg in messages
            if msg.get('type') == 'tool_use'
        )
        if already_sent:
            return {}
        return {
            "systemMessage": (
                f"Reminder: If you haven't sent the user an update via {send_marker} yet, "
                "do so now to keep them informed of your progress or completion."
            )
        }

    @staticmethod
    def _summarize_tool_input(tool_name: str, tool_input: dict) -> str | None:
        """Build a short human-readable summary of tool input for sdk_events payload."""
        if not tool_input:
            return None
        try:
            if tool_name == "Bash":
                cmd = tool_input.get("command", "")
                desc = tool_input.get("description", "")
                return desc or cmd
            elif tool_name in ("Read", "Write"):
                path = tool_input.get("file_path", "")
                # Show just filename
                return path.rsplit("/", 1)[-1] if "/" in path else path
            elif tool_name in ("Edit",):
                path = tool_input.get("file_path", "")
                return path.rsplit("/", 1)[-1] if "/" in path else path
            elif tool_name in ("Grep",):
                pattern = tool_input.get("pattern", "")
                path = tool_input.get("path", "")
                short_path = path.rsplit("/", 1)[-1] if "/" in path else (path or ".")
                return f'"{pattern}" in {short_path}'
            elif tool_name in ("Glob",):
                return tool_input.get("pattern", "")
            elif tool_name == "Agent":
                return tool_input.get("description", "")
            elif tool_name == "WebSearch":
                return tool_input.get("query", "")
            elif tool_name == "WebFetch":
                url = tool_input.get("url", "")
                return url or None
            else:
                # Generic: show first string value
                for v in tool_input.values():
                    if isinstance(v, str) and v:
                        return v
                return None
        except Exception:
            return None

    async def _handle_message(self, message):
        """Handle messages from client.receive_response().

        NOTE: We do NOT auto-send text output as SMS. Claude calls send-sms
        explicitly via Bash tool when it wants to message the user.
        Auto-sending caused massive SMS spam (every intermediate TextBlock
        became a separate message).
        """
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    self._log.info(f"OUT | {block.text}")

                    if block.text:
                        self.last_assistant_text = block.text
                        if _AUTH_FAILURE_RE.search(block.text):
                            self._note_auth_failure(block.text)

                    # Detect block limit messages from the API
                    self._detect_block_limit(block.text)

                    # Emit text event so thinking indicator shows "Responding"
                    if self._producer and hasattr(self._producer, 'send_sdk_event'):
                        preview = block.text if block.text else None
                        self._producer.send_sdk_event(
                            session_name=self._session_name,
                            chat_id=self.chat_id,
                            event_type="text",
                            payload=preview,
                        )
                elif isinstance(block, ToolUseBlock):
                    self._log.info(f"TOOL_USE | {block.name}")
                    # Track tool start time for performance logging
                    self._pending_tools[block.id] = (
                        time.perf_counter(),
                        block.input if isinstance(block.input, dict) else {},
                        block.name,
                    )
                    # Populate sdk_events for tool-level tracing
                    if self._producer and hasattr(self._producer, 'send_sdk_event'):
                        # Build a short summary of tool input for the payload
                        tool_input = block.input if isinstance(block.input, dict) else {}
                        input_summary = self._summarize_tool_input(block.name, tool_input)
                        self._producer.send_sdk_event(
                            session_name=self._session_name,
                            chat_id=self.chat_id,
                            event_type="tool_use",
                            tool_name=block.name,
                            tool_use_id=block.id,
                            payload=input_summary,
                        )

        elif isinstance(message, UserMessage):
            # UserMessage contains tool results - track completion timing
            for block in (message.content if isinstance(message.content, list) else []):
                if isinstance(block, ToolResultBlock):
                    self.last_tool_activity_at = datetime.now()
                    tool_use_id = block.tool_use_id
                    if tool_use_id in self._pending_tools:
                        start_time, tool_input, tool_name = self._pending_tools.pop(tool_use_id)
                        duration_ms = (time.perf_counter() - start_time) * 1000
                        session_name = self._session_name
                        perf.log_tool_execution(
                            session=session_name,
                            tool=tool_name,
                            tool_input=tool_input,
                            duration_ms=duration_ms,
                            is_error=block.is_error or False,
                            session_type=self.session_type,
                        )
                        # Populate sdk_events for tool-level tracing
                        if self._producer and hasattr(self._producer, 'send_sdk_event'):
                            self._producer.send_sdk_event(
                                session_name=session_name,
                                chat_id=self.chat_id,
                                event_type="tool_result",
                                tool_name=tool_name,
                                tool_use_id=tool_use_id,
                                duration_ms=duration_ms,
                                is_error=block.is_error or False,
                            )
                        # Detect outbound message sends for e2e latency measurement
                        if tool_name == "Bash" and tool_input and not (block.is_error or False):
                            cmd = tool_input.get("command", "")
                            if _is_send_command(cmd):
                                if self._producer:
                                    produce_event(self._producer, "messages", "message.sent", {
                                        "chat_id": self.chat_id,
                                        "command": cmd,
                                        "tool_use_id": tool_use_id,
                                        "duration_ms": duration_ms,
                                    }, key=f"{self.source}:{self.chat_id}", source="sdk_session")
                    else:
                        self._log.warning(f"TOOL_RESULT_ORPHAN | tool_use_id={tool_use_id}")

        elif isinstance(message, ResultMessage):
            self.turn_count += message.num_turns or 0
            if message.session_id:
                self.session_id = message.session_id
            self.last_activity = datetime.now()
            self.last_response_at = datetime.now()  # Track for stuck detection
            if message.is_error:
                self._consecutive_error_turns += 1
            else:
                self._consecutive_error_turns = 0
                self._clear_auth_failure_flag()
            self._log.info(
                f"TURN | #{self.turn_count} | "
                f"duration={message.duration_ms}ms | error={message.is_error} | "
                f"sid={message.session_id}"
            )
            session_name = self._session_name
            produce_event(self._producer, "system", "sdk.turn_complete", {
                "session_name": session_name,
                "chat_id": self.chat_id,
                "contact_name": self.contact_name,
                "tier": self.tier,
                "duration_ms": message.duration_ms,
                "num_turns": message.num_turns,
                "is_error": message.is_error,
                "total_turns": self.turn_count,
            }, key=session_name, source="sdk")
            # Populate sdk_events table for tool-level observability
            if self._producer and hasattr(self._producer, 'send_sdk_event'):
                self._producer.send_sdk_event(
                    session_name=session_name,
                    chat_id=self.chat_id,
                    event_type="result",
                    duration_ms=message.duration_ms,
                    is_error=message.is_error or False,
                    num_turns=message.num_turns,
                )

        elif isinstance(message, SystemMessage):
            # Capture session_id from init message
            if hasattr(message, 'data') and isinstance(message.data, dict):
                sid = message.data.get('session_id')
                if sid and not self.session_id:
                    self.session_id = sid
            self._log.info(f"SYSTEM | {getattr(message, 'subtype', 'unknown')}")
