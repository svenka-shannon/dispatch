"""
Auth Dialog Auto-Resolver — Full Implementation (Phases 1-4).

Detects macOS auth dialogs (SecurityAgent), traces provenance to SDK sessions,
classifies safety via deterministic rule engine, and auto-resolves safe ones.

Also detects macOS permission (TCC) prompts that are NOT owned by SecurityAgent,
e.g. '"python3.14" is requesting to bypass the system private window picker and
directly access your screen and audio' (owner: UserNotificationCenter). These
steal keyboard focus and are NEVER auto-approved — always escalated to admin.

Phase 1: Detection + AX tree parsing + bus logging
Phase 2: Provenance tracing via process tree
Phase 3: Rule engine + config + SIGHUP reload
Phase 4: Resolution via PyObjC AXUIElement (in-process, password stays in-process)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import resource
import signal
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Protocol

from assistant.common import SKILLS_DIR, STATE_DIR

log = logging.getLogger(__name__)

# axctl path
AXCTL = SKILLS_DIR / "axctl" / "scripts" / "axctl"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class Confidence(Enum):
    HIGH = auto()
    NONE = auto()


class DecisionAction(Enum):
    APPROVE = auto()
    APPROVE_WITH_PASSWORD = auto()
    ESCALATE = auto()


@dataclass
class DialogContext:
    """Snapshot of a detected auth dialog."""
    ax_tree_snapshot: str
    dialog_id: str
    app_name: str
    action: str
    dialog_type: str
    buttons: list[str]
    detected_at: float  # time.monotonic()
    decision: Decision | None = None
    owner: str = "SecurityAgent"  # process that owns the dialog window


@dataclass
class TraceResult:
    session_id: str = ""
    confidence: Confidence = Confidence.NONE
    method: str = ""
    details: str = ""


@dataclass
class Decision:
    action: DecisionAction
    reason: str


@dataclass
class ResolutionResult:
    success: bool
    method: str = ""
    reason: str | None = None
    duration_ms: float = 0


@dataclass
class Rule:
    """Pattern matching — all regex is case-insensitive."""
    name: str
    app_pattern: str = ""
    action_pattern: str = ""
    dialog_type: str = ""
    action: str = "approve"

    def __post_init__(self):
        self._app_re = re.compile(self.app_pattern, re.IGNORECASE) if self.app_pattern else None
        self._action_re = re.compile(self.action_pattern, re.IGNORECASE) if self.action_pattern else None
        self._dialog_type = self.dialog_type

    def matches(self, dialog: DialogContext) -> bool:
        if self._app_re and not self._app_re.search(dialog.app_name):
            return False
        if self._action_re and not self._action_re.search(dialog.action):
            return False
        if self._dialog_type and dialog.dialog_type != self._dialog_type:
            return False
        return True


# ---------------------------------------------------------------------------
# AX tree parsers
# ---------------------------------------------------------------------------

def parse_app_name(tree: str) -> str:
    """First AXStaticText is the app name (validated against fixtures)."""
    m = re.search(r"AXStaticText AXValue='([^']+)'", tree)
    return m.group(1) if m else ""


def parse_action(tree: str) -> str:
    """Second AXStaticText is the action description."""
    matches = re.findall(r"AXStaticText AXValue='([^']+)'", tree)
    return matches[1] if len(matches) > 1 else ""


def classify_dialog_type(tree: str) -> str:
    """Detect dialog type from field structure.

    Password auth detection requires the bullet glyph (\\uf79a) in a text field.
    Password prompt text alone → 'possible_password_auth' which always escalates.
    Phase 1 fixture tests MUST include at least one non-password dialog with
    an empty text field to validate this classifier doesn't over-classify.
    """
    has_bullet_field = bool(re.search(r"AXTextField AXValue='[^']*\uf79a", tree))
    has_password_prompt = bool(re.search(r"password", tree, re.IGNORECASE))
    num_text_fields = len(re.findall(r"AXTextField", tree))

    # Strong signal: bullet glyphs in a text field (definite password field)
    if has_bullet_field and num_text_fields >= 1:
        return "password_auth"
    # Weak signal: password prompt text without bullet glyph → escalate
    elif has_password_prompt and num_text_fields >= 1:
        return "possible_password_auth"
    elif re.search(r"AXButton AXTitle='(Allow|Deny)'", tree):
        return "allow_deny"
    return "unknown"


def parse_buttons(tree: str) -> list[str]:
    return re.findall(r"AXButton AXTitle='([^']+)'", tree)


# ---------------------------------------------------------------------------
# Permission (TCC) prompts owned by processes other than SecurityAgent
# ---------------------------------------------------------------------------

# Window owners that show permission/consent prompts. Seen 2026-10-19:
# UserNotificationCenter showed the screen-recording "bypass the system private
# window picker" prompt for python3.14 (Allow / Open System Settings).
# universalAccessAuthWarn = "X would like to control this computer" (Accessibility),
# CoreServicesUIAgent = Gatekeeper "downloaded from the Internet" prompts.
PERMISSION_PROMPT_OWNERS = (
    "UserNotificationCenter",
    "universalAccessAuthWarn",
    "CoreServicesUIAgent",
)
PERMISSION_PROMPT_TYPE = "permission_prompt"


def axctl_tree_cmd(owner: str) -> str:
    """axctl tree command with wrapping disabled (rich wraps long AXValues at 80 cols)."""
    return f'COLUMNS=4000 {AXCTL} tree "{owner}"'


def parse_permission_prompt(tree: str, owner: str) -> dict | None:
    """Parse the first AXWindow of an `axctl tree <owner>` dump.

    Returns {"owner", "requesting_app", "texts", "prompt_text", "buttons"} or None
    if there is no window with text (ignores the app's AXMenuBar).
    """
    lines = tree.splitlines()
    start = next((i for i, l in enumerate(lines) if l.lstrip().startswith("AXWindow")), None)
    if start is None:
        return None
    indent = len(lines[start]) - len(lines[start].lstrip())
    section = [lines[start]]
    for line in lines[start + 1:]:
        stripped = line.lstrip()
        # A sibling of the window (e.g. AXMenuBar) ends the section. Wrapped
        # continuation lines start at column 0 but are not AX elements.
        if stripped.startswith("AX") and len(line) - len(stripped) <= indent:
            break
        section.append(line)
    body = "\n".join(section)
    texts = [
        re.sub(r"\s+", " ", t).strip()
        for t in re.findall(r"AXStaticText AXValue='(.*?)'(?=\s*\n\s*AX|\s*$)", body, re.S)
    ]
    texts = [t for t in texts if t]
    if not texts:
        return None
    buttons = re.findall(r"AXButton AXTitle='([^']+)'", body)
    m = re.search(r"[“\"]([^”\"]+)[”\"]", texts[0])
    requesting = m.group(1) if m else (texts[0].split(" ")[0] if texts else owner)
    return {
        "owner": owner,
        "requesting_app": requesting or owner,
        "texts": texts,
        "prompt_text": " ".join(texts),
        "buttons": buttons,
    }


def compute_dialog_id(app_name: str, action: str, dialog_type: str, buttons: list[str]) -> str:
    """Stable dialog identity — excludes transient AX properties.
    Single source of truth for gather_context() and verify_dialog_identity()."""
    stable_content = f"{app_name}|{action}|{dialog_type}|{'|'.join(buttons)}"
    return hashlib.sha256(stable_content.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class RateLimitConfig:
    max_approvals_per_minute: int = 3
    max_approvals_per_hour: int = 20
    cooldown_after_escalation_seconds: int = 300
    anomaly_alert_multiplier: int = 3
    anomaly_min_observation_days: int = 7
    anomaly_min_events: int = 20
    silent_failure_min_detections: int = 5


@dataclass
class ResolutionConfig:
    password_keychain_entry: str = "mac-admin-password"
    escalation_chat_id: str = ""
    digest_chat_id: str = ""


@dataclass
class AuthDialogConfig:
    enabled: bool = True
    dry_run: bool = True  # Phase 1-3: detect + log only
    poll_interval_seconds: float = 3.0
    auto_approve: list[Rule] = field(default_factory=list)
    always_escalate: list[Rule] = field(default_factory=list)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    resolution: ResolutionConfig = field(default_factory=ResolutionConfig)
    max_pending_dialogs: int = 3
    dialog_verify_wait_seconds: float = 0.5
    dialog_verify_max_attempts: int = 5
    subprocess_timeout_seconds: int = 10
    resolution_max_seconds: int = 30
    queue_ttl_seconds: int = 30
    ax_tree_retention_days: int = 30
    # Permission (TCC) prompts: which window owners to watch, and how long to wait
    # before re-escalating the same prompt if it is still on screen.
    permission_prompt_owners: list[str] = field(default_factory=lambda: list(PERMISSION_PROMPT_OWNERS))
    permission_prompt_reescalate_seconds: int = 21600

    @classmethod
    def from_dict(cls, d: dict) -> AuthDialogConfig:
        """Load from config dict (e.g. config.get('auth_dialog_monitor'))."""
        if not d:
            return cls()

        auto_approve = [
            Rule(**r) for r in d.get("auto_approve", [])
        ]
        always_escalate = [
            Rule(**r) for r in d.get("always_escalate", [])
        ]
        rl = RateLimitConfig(**d.get("rate_limit", {})) if d.get("rate_limit") else RateLimitConfig()
        res = ResolutionConfig(**d.get("resolution", {})) if d.get("resolution") else ResolutionConfig()

        return cls(
            enabled=d.get("enabled", True),
            dry_run=d.get("dry_run", True),
            poll_interval_seconds=d.get("poll_interval_seconds", 3.0),
            auto_approve=auto_approve,
            always_escalate=always_escalate,
            rate_limit=rl,
            resolution=res,
            max_pending_dialogs=d.get("max_pending_dialogs", 3),
            dialog_verify_wait_seconds=d.get("dialog_verify_wait_seconds", 0.5),
            dialog_verify_max_attempts=d.get("dialog_verify_max_attempts", 5),
            subprocess_timeout_seconds=d.get("subprocess_timeout_seconds", 10),
            resolution_max_seconds=d.get("resolution_max_seconds", 30),
            queue_ttl_seconds=d.get("queue_ttl_seconds", 30),
            ax_tree_retention_days=d.get("ax_tree_retention_days", 30),
            permission_prompt_owners=list(d.get("permission_prompt_owners", PERMISSION_PROMPT_OWNERS)),
            permission_prompt_reescalate_seconds=d.get("permission_prompt_reescalate_seconds", 21600),
        )

    def validate(self):
        """Raises ValueError on invalid config."""
        rl = self.rate_limit
        if rl.max_approvals_per_minute < 1:
            raise ValueError(f"max_approvals_per_minute must be >= 1, got {rl.max_approvals_per_minute}")
        if rl.max_approvals_per_hour < rl.max_approvals_per_minute:
            raise ValueError("max_approvals_per_hour must be >= max_approvals_per_minute")
        if self.poll_interval_seconds < 1:
            raise ValueError("poll_interval_seconds must be >= 1")
        if self.queue_ttl_seconds < self.poll_interval_seconds:
            raise ValueError("queue_ttl_seconds must be >= poll_interval_seconds")
        # Safety invariant: possible_password_auth and permission (TCC) prompts
        # must never be auto-approved
        for rule in self.auto_approve:
            if rule._dialog_type == PERMISSION_PROMPT_TYPE:
                raise ValueError(
                    f"auto_approve rule '{rule.name}' targets {PERMISSION_PROMPT_TYPE} — "
                    f"permission prompts are escalation only"
                )
            if rule._dialog_type == "possible_password_auth":
                raise ValueError(
                    f"auto_approve rule '{rule.name}' targets possible_password_auth — "
                    f"this type is reserved for escalation only"
                )


def load_default_config() -> AuthDialogConfig:
    """Load config with sensible defaults for the auth dialog monitor."""
    from assistant import config as app_config
    raw = app_config.get("auth_dialog_monitor", {})
    cfg = AuthDialogConfig.from_dict(raw or {})

    # Add default always_escalate rules if none configured
    if not cfg.always_escalate:
        cfg.always_escalate = [
            Rule(name="keychain_access", action_pattern=r"keychain|credential|certificate"),
            Rule(name="filevault", action_pattern=r"FileVault|encryption"),
            Rule(name="privilege_escalation", action_pattern=r"admin.*(privilege|access)"),
            Rule(name="root_sudo", action_pattern=r"\bsudo\b|\broot\b.*(access|privilege|permission)"),
            Rule(name="empty_app", app_pattern=r"^\s*$"),
            Rule(name="possible_password", dialog_type="possible_password_auth"),
        ]

    cfg.validate()
    return cfg


# ---------------------------------------------------------------------------
# Rate Limiter
# ---------------------------------------------------------------------------

class RateLimiter:
    """Per-minute and per-hour rate limiting with cooldown after escalation."""

    def __init__(self, config: RateLimitConfig):
        self._config = config
        self._minute_approvals: list[float] = []  # timestamps
        self._hour_approvals: list[float] = []
        self._last_escalation: float = 0
        self._first_hit_sent = False

    def record_approval(self):
        now = time.monotonic()
        self._minute_approvals.append(now)
        self._hour_approvals.append(now)

    def record_escalation(self):
        self._last_escalation = time.monotonic()
        self._first_hit_sent = False

    def exceeded(self) -> bool:
        now = time.monotonic()
        # Cooldown after escalation
        if now - self._last_escalation < self._config.cooldown_after_escalation_seconds:
            return True
        # Prune old entries
        cutoff_min = now - 60
        cutoff_hr = now - 3600
        self._minute_approvals = [t for t in self._minute_approvals if t > cutoff_min]
        self._hour_approvals = [t for t in self._hour_approvals if t > cutoff_hr]
        # Check limits
        if len(self._minute_approvals) >= self._config.max_approvals_per_minute:
            return True
        if len(self._hour_approvals) >= self._config.max_approvals_per_hour:
            return True
        return False

    def first_hit_in_window(self) -> bool:
        """Returns True only on the first exceeded() call per cooldown window."""
        if not self._first_hit_sent:
            self._first_hit_sent = True
            return True
        return False


# ---------------------------------------------------------------------------
# Dialog Queue
# ---------------------------------------------------------------------------

class DialogQueue:
    """Serial processing with bounded queue, dedup, and TTL."""

    def __init__(self, max_size: int = 3, ttl_seconds: int = 30,
                 escalate_fn: Callable | None = None):
        self._queue: asyncio.Queue[DialogContext] = asyncio.Queue(maxsize=max_size)
        self._lock = asyncio.Lock()
        self._ttl = ttl_seconds
        self._seen_ids: set[str] = set()
        # Unified suppression dict with namespaced keys:
        # "overflow:{dialog_id}" → suppress_until
        # "escalation:{dialog_id}:{reason}" → suppress_until
        self._suppress_until: dict[str, float] = {}
        self._escalate_fn = escalate_fn or _noop_escalate

    async def check_and_enqueue(self, dialog: DialogContext) -> bool:
        """Atomic check-and-enqueue under lock. Escalation calls outside lock."""
        overflow_items: list[DialogContext] = []
        overflow_new: DialogContext | None = None

        async with self._lock:
            if dialog.dialog_id in self._seen_ids:
                return False
            # Check overflow suppression
            suppress_key = f"overflow:{dialog.dialog_id}"
            suppress_until = self._suppress_until.get(suppress_key)
            if suppress_until and time.monotonic() < suppress_until:
                return False
            self._suppress_until.pop(suppress_key, None)

            if self._queue.full():
                # Collect items to escalate — actual escalation outside lock
                while not self._queue.empty():
                    d = self._queue.get_nowait()
                    overflow_items.append(d)
                    self._seen_ids.discard(d.dialog_id)
                overflow_new = dialog
                self._suppress_until[suppress_key] = time.monotonic() + 60
            else:
                self._seen_ids.add(dialog.dialog_id)
                await self._queue.put(dialog)
                return True

        # Escalate OUTSIDE lock — no deadlock risk
        for d in overflow_items:
            await self._escalate_dialog(d, "queue_overflow")
        if overflow_new:
            await self._escalate_dialog(overflow_new, "queue_overflow")
        return False

    async def process_next(self, handler_fn) -> bool:
        """Wait for next dialog, process via handler_fn, return True if handled.

        Args:
            handler_fn: async callable(DialogContext) — the full pipeline handler.
        """
        try:
            dialog = await asyncio.wait_for(self._queue.get(), timeout=10)
        except asyncio.TimeoutError:
            return False

        # Check TTL under lock, but release before handler to avoid blocking enqueue
        async with self._lock:
            age = time.monotonic() - dialog.detected_at
            if age > self._ttl:
                log.info(f"Discarding stale dialog {dialog.dialog_id} (age={age:.1f}s)")
                self._seen_ids.discard(dialog.dialog_id)
                return False

        try:
            await handler_fn(dialog)
            return True
        finally:
            async with self._lock:
                self._seen_ids.discard(dialog.dialog_id)

    async def _escalate_dialog(self, dialog: DialogContext, reason: str):
        """Escalate with dedup by (dialog_id, reason) within 60s."""
        dedup_key = f"escalation:{dialog.dialog_id}:{reason}"
        now = time.monotonic()
        if dedup_key in self._suppress_until and now < self._suppress_until[dedup_key]:
            log.debug(f"Suppressed duplicate escalation: {dedup_key}")
            return
        self._suppress_until[dedup_key] = now + 60
        await self._escalate_fn(
            f"Auth dialog escalated ({reason}): {dialog.app_name} — {dialog.action}"
        )

    def prune_suppressed(self):
        """Remove expired suppression entries. Called hourly."""
        now = time.monotonic()
        self._suppress_until = {k: v for k, v in self._suppress_until.items() if v > now}


async def _noop_escalate(msg: str, dialog_info: dict | None = None):
    log.warning(f"[escalate-noop] {msg}")


# ---------------------------------------------------------------------------
# AXBackend Protocol + PyObjC Implementation
# ---------------------------------------------------------------------------

class AXBackend(Protocol):
    """Injectable AX interaction layer. Production uses PyObjC; tests use mocks."""
    def find_security_agent(self) -> Any | None: ...
    def get_ax_child(self, element: Any, role: str, index: int = 0,
                     title: str | None = None) -> Any | None: ...
    def set_value(self, element: Any, value: str) -> int: ...
    def set_focused(self, element: Any, focused: bool) -> int: ...
    def press(self, element: Any) -> int: ...


def _get_ax_attr(element: Any, attr: str) -> Any:
    """Get an AX attribute value, returning None on error."""
    import ApplicationServices as AS
    err, value = AS.AXUIElementCopyAttributeValue(element, attr, None)
    return value if err == 0 else None


class PyObjCAXBackend:
    """Production AX backend using PyObjC (in-process, no shell)."""

    def find_security_agent(self) -> Any | None:
        import ApplicationServices as AS
        import importlib
        _quartz = importlib.import_module("Quartz")
        CGWindowListCopyWindowInfo = getattr(_quartz, "CGWindowListCopyWindowInfo")
        kCGWindowListOptionAll = getattr(_quartz, "kCGWindowListOptionAll")
        kCGNullWindowID = getattr(_quartz, "kCGNullWindowID")
        for app_info in CGWindowListCopyWindowInfo(kCGWindowListOptionAll, kCGNullWindowID) or []:
            if app_info.get("kCGWindowOwnerName") == "SecurityAgent":
                pid = app_info.get("kCGWindowOwnerPID")
                if pid:
                    return AS.AXUIElementCreateApplication(pid)
        return None

    def get_ax_child(self, element: Any, role: str, index: int = 0,
                     title: str | None = None) -> Any | None:
        if element is None:
            return None
        children = _get_ax_attr(element, "AXChildren")
        if not children:
            return None
        matches = [c for c in children if _get_ax_attr(c, "AXRole") == role
                   and (title is None or _get_ax_attr(c, "AXTitle") == title)]
        if title is not None:
            return matches[0] if matches else None
        return matches[index] if index < len(matches) else None

    def set_value(self, element: Any, value: str) -> int:
        import ApplicationServices as AS
        return AS.AXUIElementSetAttributeValue(element, "AXValue", value)

    def set_focused(self, element: Any, focused: bool) -> int:
        import ApplicationServices as AS
        return AS.AXUIElementSetAttributeValue(element, "AXFocused", focused)

    def press(self, element: Any) -> int:
        import ApplicationServices as AS
        return AS.AXUIElementPerformAction(element, "AXPress")


# ---------------------------------------------------------------------------
# AuthDialogMonitor
# ---------------------------------------------------------------------------

class AuthDialogMonitor:
    """Main monitor — integrates detection, provenance, classification, resolution."""

    def __init__(
        self,
        config: AuthDialogConfig,
        producer: Any = None,
        session_pid_map: dict[int, str] | None = None,
        run_cmd: Callable | None = None,
        escalate_fn: Callable | None = None,
        ax_backend: AXBackend | None = None,
    ):
        self.config = config
        self.enabled = config.enabled
        self._producer = producer
        self._run = run_cmd or self._default_run
        self._session_pid_map: dict[int, str] = session_pid_map or {}
        self._escalate_fn = escalate_fn or _noop_escalate
        self._ax: AXBackend = ax_backend or PyObjCAXBackend()
        self.queue = DialogQueue(
            config.max_pending_dialogs,
            config.queue_ttl_seconds,
            escalate_fn=self._escalate_fn,
        )
        self.rate_limiter = RateLimiter(config.rate_limit)
        self._rule_hit_counters: Counter = Counter()
        self._consecutive_persist_failures = 0
        self._keychain_in_progress = False
        # Note: _keychain_in_progress is safe without a lock because asyncio is single-threaded.
        self._loop: asyncio.AbstractEventLoop | None = None

        # Persisted daily date for digest
        self._state_path = STATE_DIR / "auth_monitor_state.json"
        self._last_daily_date = self._load_last_daily_date()

    # --- Shell execution ---

    async def _default_run(self, cmd: str) -> str:
        """Run a shell command (for axctl). Returns stdout."""
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=self.config.subprocess_timeout_seconds,
        )
        return stdout.decode() if stdout else ""

    # --- Lifecycle ---

    async def start(self):
        """Start the monitor. Called from daemon main."""
        # Prevent core dumps from leaking plaintext passwords during resolution.
        # NOTE: This is process-wide — affects the entire daemon.
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

        self._loop = asyncio.get_running_loop()
        self._install_sighup_handler()
        await self._startup_check()

        if self.enabled:
            asyncio.create_task(self._poll_loop(), name="auth-dialog-poll")
            asyncio.create_task(self._process_loop(), name="auth-dialog-process")
            asyncio.create_task(self._run_periodic_tasks(), name="auth-dialog-periodic")
            log.info("AuthDialogMonitor started (dry_run=%s)", self.config.dry_run)

    def _install_sighup_handler(self):
        """Install SIGHUP handler for config hot-reload."""
        loop = self._loop
        if loop is None:
            return

        def _on_sighup():
            loop.create_task(self._reload_config())

        try:
            loop.add_signal_handler(signal.SIGHUP, _on_sighup)
            log.info("AuthDialogMonitor SIGHUP handler installed")
        except (NotImplementedError, OSError) as e:
            log.warning(f"Could not install SIGHUP handler: {e}")

    async def _reload_config(self):
        """Reload config from disk on SIGHUP."""
        try:
            new_cfg = load_default_config()
            old_dry_run = self.config.dry_run
            self.config = new_cfg
            self.rate_limiter = RateLimiter(new_cfg.rate_limit)
            self.queue._ttl = new_cfg.queue_ttl_seconds
            log.info(
                "AuthDialogMonitor config reloaded (dry_run=%s→%s, %d auto_approve, %d always_escalate)",
                old_dry_run, new_cfg.dry_run,
                len(new_cfg.auto_approve), len(new_cfg.always_escalate),
            )
        except Exception as e:
            log.error(f"Config reload failed (keeping current config): {e}")
            await self._escalate_fn(f"AuthDialogMonitor config reload failed: {e}")

    async def _startup_check(self):
        """Verify AX permissions on daemon startup."""
        if not AXCTL.exists():
            log.error(f"axctl not found at {AXCTL} — AuthDialogMonitor disabled")
            await self._escalate_fn("AuthDialogMonitor disabled: axctl not found")
            self.enabled = False
            return

        try:
            result = await self._run(f"{AXCTL} apps")
        except (asyncio.TimeoutError, Exception) as e:
            log.error(f"axctl startup check failed: {e}")
            await self._escalate_fn(f"AuthDialogMonitor disabled: axctl failed: {e}")
            self.enabled = False
            return

        if not result.strip():
            log.error("AX permission denied — AuthDialogMonitor disabled. "
                      "Grant Accessibility access in System Settings.")
            await self._escalate_fn("AuthDialogMonitor disabled: no AX permission")
            self.enabled = False
            return

        log.info("AuthDialogMonitor AX permission confirmed")

    # --- Detection ---

    async def poll(self):
        """Single poll cycle: check for SecurityAgent."""
        if not self.enabled or self._keychain_in_progress:
            return

        result = await self._run(f"{AXCTL} apps")
        if not result.strip():
            log.error("axctl apps returned empty — AX permission likely lost")
            await self._escalate_fn("AuthDialogMonitor: AX permission lost")
            self.enabled = False
            return

        if "SecurityAgent" in result:
            dialog = await self._gather_context()
            if dialog:
                await self.queue.check_and_enqueue(dialog)

        # Permission (TCC) prompts owned by other processes (never auto-approved)
        for owner in self.config.permission_prompt_owners:
            if re.search(rf"^{re.escape(owner)} \(pid", result, re.M):
                dialog = await self._gather_permission_prompt(owner)
                if dialog:
                    await self.queue.check_and_enqueue(dialog)

    async def _gather_permission_prompt(self, owner: str) -> DialogContext | None:
        """Gather context for a permission prompt window owned by `owner`."""
        try:
            tree = await self._run(axctl_tree_cmd(owner))
        except (asyncio.TimeoutError, Exception) as e:
            log.warning(f"Failed to get {owner} tree: {e}")
            return None
        info = parse_permission_prompt(tree or "", owner)
        if not info:
            return None
        buttons = info["buttons"]
        dialog_id = compute_dialog_id(
            info["requesting_app"], info["prompt_text"], PERMISSION_PROMPT_TYPE, buttons)
        return DialogContext(
            ax_tree_snapshot=tree,
            dialog_id=dialog_id,
            app_name=info["requesting_app"],
            action=info["prompt_text"],
            dialog_type=PERMISSION_PROMPT_TYPE,
            buttons=buttons,
            detected_at=time.monotonic(),
            owner=owner,
        )

    async def _gather_context(self) -> DialogContext | None:
        """Gather AX tree context for SecurityAgent dialog."""
        try:
            tree = await self._run(f"{AXCTL} tree SecurityAgent")
        except (asyncio.TimeoutError, Exception) as e:
            log.warning(f"Failed to get SecurityAgent tree: {e}")
            return None

        if not tree.strip():
            return None

        app_name = parse_app_name(tree)
        action = parse_action(tree)
        dialog_type = classify_dialog_type(tree)
        buttons = parse_buttons(tree)
        dialog_id = compute_dialog_id(app_name, action, dialog_type, buttons)

        return DialogContext(
            ax_tree_snapshot=tree,
            dialog_id=dialog_id,
            app_name=app_name,
            action=action,
            dialog_type=dialog_type,
            buttons=buttons,
            detected_at=time.monotonic(),
        )

    # --- Provenance ---

    async def trace_provenance(self, dialog: DialogContext) -> TraceResult:
        """Trace dialog to SDK session via process tree."""
        app_name = dialog.app_name
        if not app_name:
            return TraceResult(confidence=Confidence.NONE, method="no_app_name")

        try:
            proc = await asyncio.create_subprocess_exec(
                "pgrep", "-f", app_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            pids_raw = stdout.decode()
        except (asyncio.TimeoutError, Exception):
            return TraceResult(confidence=Confidence.NONE, method="pgrep_failed")

        pids = [int(p) for p in pids_raw.strip().split('\n') if p.strip().isdigit()]
        if len(pids) > 1:
            log.warning(f"pgrep returned {len(pids)} PIDs for '{app_name}' — potential name collision")

        for pid in pids:
            visited: set[int] = set()
            current = pid
            for _ in range(10):
                if current in visited or current <= 1:
                    break
                visited.add(current)
                if current in self._session_pid_map:
                    return TraceResult(
                        session_id=self._session_pid_map[current],
                        confidence=Confidence.HIGH,
                        method="process_tree",
                        details=f"PID chain: {sorted(visited)}",
                    )
                current = await self._get_ppid(current)

        return TraceResult(confidence=Confidence.NONE, method="no_match",
                           details=f"Checked PIDs {pids}")

    async def _get_ppid(self, pid: int) -> int:
        """Get parent PID."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "ps", "-o", "ppid=", "-p", str(pid),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            return int(stdout.strip())
        except (asyncio.TimeoutError, ValueError, Exception):
            return 0

    # --- Classification ---

    async def classify(self, dialog: DialogContext, provenance: TraceResult) -> Decision:
        """Deterministic rule engine. always_escalate checked first."""
        # Permission (TCC) prompts are NEVER auto-approved, regardless of rules.
        if dialog.dialog_type == PERMISSION_PROMPT_TYPE:
            self._rule_hit_counters["escalate:permission_prompt"] += 1
            return Decision(DecisionAction.ESCALATE, "permission_prompt: never auto-approved")

        # IMPORTANT: always_escalate first — safety > convenience
        for rule in self.config.always_escalate:
            if rule.matches(dialog):
                self._rule_hit_counters[f"escalate:{rule.name}"] += 1
                return Decision(DecisionAction.ESCALATE, f"always_escalate: {rule.name}")

        if provenance.confidence != Confidence.HIGH:
            return Decision(DecisionAction.ESCALATE, "no_provenance")

        if self.rate_limiter.exceeded():
            if self.rate_limiter.first_hit_in_window():
                await self._escalate_fn(
                    "Rate limit triggered — auth dialog burst detected. "
                    "Check for runaway sessions."
                )
            return Decision(DecisionAction.ESCALATE, "rate_limit_exceeded")

        for rule in self.config.auto_approve:
            if rule.matches(dialog):
                self._rule_hit_counters[f"approve:{rule.name}"] += 1
                action_map = {
                    "approve": DecisionAction.APPROVE,
                    "approve_with_password": DecisionAction.APPROVE_WITH_PASSWORD,
                }
                return Decision(
                    action_map.get(rule.action, DecisionAction.APPROVE),
                    f"auto_approve: {rule.name}",
                )

        return Decision(DecisionAction.ESCALATE, "no_matching_rule")

    # --- Resolution ---

    async def resolve(self, dialog: DialogContext, decision: Decision) -> ResolutionResult:
        """Dispatch to resolution method. Tracks consecutive failures."""
        start = time.monotonic()

        if decision.action == DecisionAction.ESCALATE:
            dialog_info = {
                "dialog_id": dialog.dialog_id,
                "app_name": dialog.app_name,
                "action": dialog.action,
                "dialog_type": dialog.dialog_type,
                "buttons": dialog.buttons,
                "detected_at_wall": time.time(),
                "owner": dialog.owner,
            }
            if dialog.dialog_type == PERMISSION_PROMPT_TYPE:
                await self._escalate_fn(
                    f"macOS PERMISSION PROMPT on screen (window owner: {dialog.owner}) — "
                    f"requesting app: {dialog.app_name}. Prompt: \"{dialog.action}\" "
                    f"Buttons: {', '.join(dialog.buttons) or 'none'}. It steals keyboard focus "
                    f"until answered. Never auto-approved: ask the admin. 'approve' clicks Allow, "
                    f"'deny' clicks Don't Allow/Deny if present; otherwise "
                    f"`auth-dialog-approve press {dialog.dialog_id} \"<button>\"`. "
                    f"Do NOT use 'always' (refused for permission prompts).",
                    dialog_info=dialog_info,
                )
                return ResolutionResult(success=True, method="escalated", reason=decision.reason)
            await self._escalate_fn(
                f"Auth dialog needs attention: {dialog.app_name} — {dialog.action} "
                f"(reason: {decision.reason})",
                dialog_info=dialog_info,
            )
            return ResolutionResult(success=True, method="escalated", reason=decision.reason)

        if self.config.dry_run:
            log.info(f"DRY RUN: would resolve {dialog.app_name} via {decision.action.name}")
            return ResolutionResult(success=True, method="dry_run")

        # Actual resolution
        if decision.action == DecisionAction.APPROVE_WITH_PASSWORD:
            result = await self._enter_password(dialog)
        elif decision.action == DecisionAction.APPROVE:
            result = await self._click_allow(dialog)
        else:
            result = ResolutionResult(success=False, method="unknown",
                                      reason=f"unknown_action:{decision.action}")

        # Track timing
        elapsed = (time.monotonic() - start) * 1000
        result.duration_ms = elapsed

        # Track consecutive dialog_persisted failures for broken-AX detection
        if not result.success and result.reason == "dialog_persisted":
            self._consecutive_persist_failures += 1
            if self._consecutive_persist_failures > 3:
                await self._escalate_fn(
                    f"AX interaction may be broken: {self._consecutive_persist_failures} "
                    f"consecutive dialog_persisted failures."
                )
        else:
            self._consecutive_persist_failures = 0

        return result

    async def _enter_password(self, dialog: DialogContext) -> ResolutionResult:
        """Enter password via PyObjC AXValue set, with keystroke fallback."""
        if not await self.verify_dialog_identity(dialog.dialog_id):
            return ResolutionResult(success=False, reason="dialog_changed")

        password_bytes = await self._get_password_from_keychain()
        if password_bytes is None:
            return ResolutionResult(success=False, reason="keychain_unavailable")

        # Use bytearray so we can zero it. The finally block ALWAYS runs.
        pw_buf = bytearray(password_bytes)
        password_str: str | None = None
        result: ResolutionResult | None = None
        try:
            password_str = pw_buf.decode("utf-8")

            app = self._ax.find_security_agent()
            if not app:
                return ResolutionResult(success=False, reason="security_agent_not_found")

            window = self._ax.get_ax_child(app, role="AXWindow", index=0)
            # Password field is typically index=1 (after username). Fall back to index=0.
            password_field = (
                self._ax.get_ax_child(window, role="AXTextField", index=1)
                or self._ax.get_ax_child(window, role="AXTextField", index=0)
            )
            ok_button = self._ax.get_ax_child(window, role="AXButton", title="OK")

            if not password_field or not ok_button:
                return ResolutionResult(success=False, reason="dialog_elements_not_found")

            # Try PyObjC direct AXValue set
            self._ax.set_focused(password_field, True)
            self._ax.set_value(password_field, "")
            err = self._ax.set_value(password_field, password_str)

            if err != 0:
                # AXValue failed on secure field → use keystroke fallback
                # pw_buf zeroing happens in outer finally
                result = await self._enter_password_fallback(dialog, pw_buf)
            else:
                self._ax.press(ok_button)
                dismissed = await self._verify_dismissed()
                result = ResolutionResult(
                    success=dismissed,
                    method="pyobjc_axvalue",
                    reason=None if dismissed else "dialog_persisted",
                )
        finally:
            # Best-effort zeroing: zero the bytearray in-place.
            # Python str (password_str) is immutable — deleted and GC'd.
            # For single-user desktop, admin-owned machine, this is acceptable.
            for i in range(len(pw_buf)):
                pw_buf[i] = 0
            if password_str is not None:
                del password_str

        assert result is not None, "unreachable: all code paths set result"
        return result

    async def _enter_password_fallback(self, dialog: DialogContext,
                                        pw_buf: bytearray) -> ResolutionResult:
        """Fallback when AXValue fails on secure text fields.
        Password via stdin pipe — never touches env vars, CLI args, or clipboard."""
        # TOCTOU guard: dialog may have changed
        if not await self.verify_dialog_identity(dialog.dialog_id):
            return ResolutionResult(success=False, reason="dialog_changed_before_fallback")

        helper = SKILLS_DIR / "axctl" / "scripts" / "axctl"
        # Minimal env — no password in env (eliminates ps eww exposure)
        env = {
            "HOME": os.environ.get("HOME", ""),
            "PATH": os.environ.get("PATH", ""),
        }
        proc = await asyncio.create_subprocess_exec(
            str(helper), "type", "SecurityAgent", "--stdin",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            await asyncio.wait_for(
                proc.communicate(input=bytes(pw_buf)),
                timeout=self.config.subprocess_timeout_seconds,
            )
        except asyncio.TimeoutError:
            proc.kill()
            return ResolutionResult(success=False, reason="helper_timeout")

        if proc.returncode != 0:
            return ResolutionResult(success=False, reason=f"helper_exit_{proc.returncode}")

        # Now click OK
        app = self._ax.find_security_agent()
        if app:
            window = self._ax.get_ax_child(app, role="AXWindow", index=0)
            ok_button = self._ax.get_ax_child(window, role="AXButton", title="OK")
            if ok_button:
                self._ax.press(ok_button)

        if not await self._verify_dismissed():
            return ResolutionResult(success=False, reason="dialog_persisted")
        return ResolutionResult(success=True, method="keystroke_fallback")

    async def _click_allow(self, dialog: DialogContext) -> ResolutionResult:
        """Click OK/Allow button for allow/deny dialogs."""
        if not await self.verify_dialog_identity(dialog.dialog_id):
            return ResolutionResult(success=False, reason="dialog_changed")

        app = self._ax.find_security_agent()
        if not app:
            return ResolutionResult(success=False, reason="security_agent_not_found")

        window = self._ax.get_ax_child(app, role="AXWindow", index=0)
        if window:
            btn = (
                self._ax.get_ax_child(window, role="AXButton", title="OK")
                or self._ax.get_ax_child(window, role="AXButton", title="Allow")
            )
            if btn:
                self._ax.press(btn)

        if not await self._verify_dismissed():
            # PyObjC AXPress failed — try axctl fallback
            if not await self.verify_dialog_identity(dialog.dialog_id):
                return ResolutionResult(success=False, reason="dialog_changed_before_fallback")
            await self._run(f'{AXCTL} click "SecurityAgent" --title "OK"')
            if not await self._verify_dismissed():
                return ResolutionResult(success=False, reason="dialog_persisted")
            return ResolutionResult(success=True, method="axctl_fallback")

        return ResolutionResult(success=True, method="pyobjc_axpress")

    async def _get_password_from_keychain(self) -> bytes | None:
        """Retrieve password from macOS Keychain.
        Sets _keychain_in_progress to suppress dialog detection during this call,
        because `security find-generic-password` may itself trigger a SecurityAgent
        dialog asking to allow keychain access."""
        self._keychain_in_progress = True
        try:
            proc = await asyncio.create_subprocess_exec(
                "security", "find-generic-password",
                "-l", self.config.resolution.password_keychain_entry, "-w",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(),
                timeout=self.config.subprocess_timeout_seconds,
            )
            return stdout.strip() if proc.returncode == 0 else None
        except asyncio.TimeoutError:
            proc.kill()  # Ensure subprocess doesn't linger
            return None
        except Exception:
            return None
        finally:
            self._keychain_in_progress = False

    async def _verify_dismissed(self) -> bool:
        """Polling retry to verify dialog dismissed."""
        for _ in range(self.config.dialog_verify_max_attempts):
            await asyncio.sleep(self.config.dialog_verify_wait_seconds)
            if not await self.dialog_still_exists():
                return True
        return False

    # --- Dialog checks ---

    async def dialog_still_exists(self) -> bool:
        result = await self._run(f"{AXCTL} apps")
        return "SecurityAgent" in result

    async def verify_dialog_identity(self, expected_id: str) -> bool:
        """Verify current dialog matches the one we classified."""
        try:
            tree = await self._run(f"{AXCTL} tree SecurityAgent")
        except (asyncio.TimeoutError, Exception):
            return False
        if not tree.strip():
            return False
        current_id = compute_dialog_id(
            parse_app_name(tree), parse_action(tree),
            classify_dialog_type(tree), parse_buttons(tree),
        )
        return current_id == expected_id

    # --- Bus logging ---

    def _log_event(self, dialog: DialogContext, provenance: TraceResult,
                   decision: Decision, result: ResolutionResult):
        """Log auth dialog event to bus."""
        from assistant.bus_helpers import produce_event
        event = {
            "dialog": {
                "app": dialog.app_name,
                "action": dialog.action,
                "dialog_type": dialog.dialog_type,
                "dialog_id": dialog.dialog_id,
                "ax_tree_snapshot": dialog.ax_tree_snapshot,
            },
            "provenance": {
                "session_id": provenance.session_id,
                "confidence": provenance.confidence.name,
                "method": provenance.method,
                "details": provenance.details,
            },
            "classification": {
                "decision": decision.action.name,
                "reason": decision.reason,
            },
            "resolution": {
                "method": result.method,
                "success": result.success,
                "reason": result.reason,
                "duration_ms": result.duration_ms,
                "primary_failed": result.method == "keystroke_fallback",
            },
            "dry_run": self.config.dry_run,
        }
        try:
            produce_event(
                self._producer, "system", "auth_dialog.event", event,
                source="auth-dialog-monitor",
            )
        except Exception as e:
            log.error(f"Bus produce failed for auth_dialog event: {e}")
            self._write_dead_letter(event)

    def _write_dead_letter(self, event: dict):
        """Sync dead-letter write (called from non-async context too)."""
        path = STATE_DIR / "auth_dialog_dead_letter.jsonl"
        try:
            with open(path, "a") as f:
                f.write(json.dumps(event) + "\n")
        except Exception as e:
            log.error(f"Dead-letter write failed: {e}")

    # --- Main pipeline ---

    async def _handle_dialog(self, dialog: DialogContext):
        """Full pipeline: trace → classify → resolve → log."""
        provenance = await self.trace_provenance(dialog)
        decision = await self.classify(dialog, provenance)
        dialog.decision = decision

        result = await self.resolve(dialog, decision)
        self._log_event(dialog, provenance, decision, result)

        if decision.action in (DecisionAction.APPROVE, DecisionAction.APPROVE_WITH_PASSWORD):
            self.rate_limiter.record_approval()
        elif decision.action == DecisionAction.ESCALATE:
            self.rate_limiter.record_escalation()
            # Suppress re-detection of this dialog for cooldown period
            # Prevents escalation spam for dialogs that stay on screen
            cooldown = self.config.rate_limit.cooldown_after_escalation_seconds
            if dialog.dialog_type == PERMISSION_PROMPT_TYPE:
                cooldown = self.config.permission_prompt_reescalate_seconds
            self.queue._suppress_until[f"overflow:{dialog.dialog_id}"] = (
                time.monotonic() + cooldown
            )

    # --- Loops ---

    async def _poll_loop(self):
        """Main detection loop."""
        while self.enabled:
            try:
                # 2x subprocess timeout: poll may call axctl apps + axctl tree
                await asyncio.wait_for(
                    self.poll(),
                    timeout=self.config.subprocess_timeout_seconds * 2,
                )
            except asyncio.TimeoutError:
                log.error("poll() timed out (axctl may be hung)")
            except Exception as e:
                log.error(f"Poll error: {e}")
            await asyncio.sleep(self.config.poll_interval_seconds)

    async def _process_loop(self):
        """Process queued dialogs via DialogQueue.process_next()."""
        while self.enabled:
            try:
                await self.queue.process_next(self._handle_dialog)
            except Exception as e:
                log.error(f"Process error: {e}")
                await asyncio.sleep(1)

    async def _run_periodic_tasks(self):
        """Hourly + daily periodic tasks."""
        next_run = self._loop.time() if self._loop else 0
        while self.enabled:
            # Individual try/except per task
            for name, coro_fn in [
                ("check_ax_permission", self._check_ax_permission),
            ]:
                try:
                    await asyncio.wait_for(coro_fn(), timeout=60)
                except asyncio.TimeoutError:
                    log.error(f"Periodic task {name} timed out")
                except Exception as e:
                    log.error(f"Periodic task {name} error: {e}")

            self.queue.prune_suppressed()

            # Daily checks
            today = date.today().isoformat()
            if today != self._last_daily_date:
                self._last_daily_date = today
                self._save_last_daily_date()
                # Future: anomaly check, silent failure, digest, pruning

            if self._loop:
                next_run += 3600
                await asyncio.sleep(max(0, next_run - self._loop.time()))
            else:
                await asyncio.sleep(3600)

    async def _check_ax_permission(self):
        """Periodic check that AX access still works."""
        try:
            result = await self._run(f"{AXCTL} apps")
        except (asyncio.TimeoutError, Exception):
            result = ""
        if not result.strip():
            log.error("AX permission appears lost")
            await self._escalate_fn("AuthDialogMonitor: AX permission lost")
            self.enabled = False

    # --- State persistence ---

    def _load_last_daily_date(self) -> str:
        try:
            data = json.loads(self._state_path.read_text())
            return data.get("last_daily_date", "")
        except (FileNotFoundError, json.JSONDecodeError):
            return ""

    def _save_last_daily_date(self):
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps({"last_daily_date": self._last_daily_date}))
        except OSError as e:
            log.warning(f"Failed to persist last_daily_date: {e}")

    # --- Escalation helper ---

    async def escalate(self, msg: str):
        """Public escalation method."""
        await self._escalate_fn(msg)
