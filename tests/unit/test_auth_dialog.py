"""Tests for AuthDialogMonitor — Phases 1-4: parsers, config, queue, classification, resolution."""

import asyncio
import hashlib
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from assistant.auth_dialog import (
    AuthDialogConfig,
    AuthDialogMonitor,
    Confidence,
    Decision,
    DecisionAction,
    DialogContext,
    DialogQueue,
    RateLimiter,
    RateLimitConfig,
    ResolutionConfig,
    Rule,
    TraceResult,
    classify_dialog_type,
    compute_dialog_id,
    load_default_config,
    parse_action,
    parse_app_name,
    parse_buttons,
)


# ---------------------------------------------------------------------------
# Mock AX Backend for resolution tests
# ---------------------------------------------------------------------------

class MockAXBackend:
    """Mock AXBackend for testing resolution without real PyObjC."""

    def __init__(self, dismiss_after_press: bool = True):
        self._dismiss_after_press = dismiss_after_press
        self._pressed = False
        self._value_set: str | None = None
        self._set_value_error = 0  # 0 = success

    def find_security_agent(self) -> Any | None:
        return {"mock": "app"}

    def get_ax_child(self, element: Any, role: str, index: int = 0,
                     title: str | None = None) -> Any | None:
        if element is None:
            return None
        if role == "AXWindow":
            return {"mock": "window"}
        if role == "AXTextField":
            return {"mock": f"textfield_{index}"}
        if role == "AXButton":
            return {"mock": f"button_{title or index}"}
        return None

    def set_value(self, element: Any, value: str) -> int:
        self._value_set = value
        return self._set_value_error

    def set_focused(self, element: Any, focused: bool) -> int:
        return 0

    def press(self, element: Any) -> int:
        self._pressed = True
        return 0


# ---------------------------------------------------------------------------
# Fixtures: real axctl tree SecurityAgent output patterns
# ---------------------------------------------------------------------------

FIXTURE_BREW_PASSWORD = """
AXApplication 'SecurityAgent'
  AXWindow 'SecurityAgent'
    AXStaticText AXValue='Homebrew'
    AXStaticText AXValue='brew wants to make changes.'
    AXTextField AXValue='sven'
    AXTextField AXValue='\uf79a\uf79a\uf79a\uf79a\uf79a'
    AXButton AXTitle='Cancel'
    AXButton AXTitle='OK'
"""

FIXTURE_RPI_IMAGER_PASSWORD = """
AXApplication 'SecurityAgent'
  AXWindow 'SecurityAgent'
    AXStaticText AXValue='Raspberry Pi Imager'
    AXStaticText AXValue='Raspberry Pi Imager is trying to access the disk'
    AXTextField AXValue='sven'
    AXTextField AXValue='\uf79a\uf79a\uf79a'
    AXButton AXTitle='Cancel'
    AXButton AXTitle='OK'
"""

FIXTURE_ALLOW_DENY = """
AXApplication 'SecurityAgent'
  AXWindow 'SecurityAgent'
    AXStaticText AXValue='Google Chrome'
    AXStaticText AXValue='Google Chrome wants to access your location'
    AXButton AXTitle='Deny'
    AXButton AXTitle='Allow'
"""

FIXTURE_NON_PASSWORD_WITH_TEXTFIELD = """
AXApplication 'SecurityAgent'
  AXWindow 'SecurityAgent'
    AXStaticText AXValue='System Preferences'
    AXStaticText AXValue='Enter a name for this profile'
    AXTextField AXValue=''
    AXButton AXTitle='Cancel'
    AXButton AXTitle='Save'
"""

FIXTURE_PASSWORD_PROMPT_NO_BULLET = """
AXApplication 'SecurityAgent'
  AXWindow 'SecurityAgent'
    AXStaticText AXValue='App Store'
    AXStaticText AXValue='Enter your password to install updates'
    AXTextField AXValue=''
    AXButton AXTitle='Cancel'
    AXButton AXTitle='OK'
"""

FIXTURE_SINGLE_FIELD_PASSWORD = """
AXApplication 'SecurityAgent'
  AXWindow 'SecurityAgent'
    AXStaticText AXValue='macOS'
    AXStaticText AXValue='Enter your password'
    AXTextField AXValue='\uf79a\uf79a'
    AXButton AXTitle='Cancel'
    AXButton AXTitle='OK'
"""


# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------

class TestParseAppName:
    def test_brew(self):
        assert parse_app_name(FIXTURE_BREW_PASSWORD) == "Homebrew"

    def test_rpi_imager(self):
        assert parse_app_name(FIXTURE_RPI_IMAGER_PASSWORD) == "Raspberry Pi Imager"

    def test_allow_deny(self):
        assert parse_app_name(FIXTURE_ALLOW_DENY) == "Google Chrome"

    def test_empty_tree(self):
        assert parse_app_name("") == ""

    def test_non_password(self):
        assert parse_app_name(FIXTURE_NON_PASSWORD_WITH_TEXTFIELD) == "System Preferences"


class TestParseAction:
    def test_brew(self):
        assert parse_action(FIXTURE_BREW_PASSWORD) == "brew wants to make changes."

    def test_rpi_imager(self):
        assert parse_action(FIXTURE_RPI_IMAGER_PASSWORD) == "Raspberry Pi Imager is trying to access the disk"

    def test_allow_deny(self):
        assert parse_action(FIXTURE_ALLOW_DENY) == "Google Chrome wants to access your location"

    def test_single_static_text(self):
        tree = "AXStaticText AXValue='Only one'"
        assert parse_action(tree) == ""


class TestClassifyDialogType:
    def test_brew_password(self):
        assert classify_dialog_type(FIXTURE_BREW_PASSWORD) == "password_auth"

    def test_rpi_imager_password(self):
        assert classify_dialog_type(FIXTURE_RPI_IMAGER_PASSWORD) == "password_auth"

    def test_allow_deny(self):
        assert classify_dialog_type(FIXTURE_ALLOW_DENY) == "allow_deny"

    def test_non_password_with_textfield(self):
        """Critical: empty text field WITHOUT password signals should not classify as password_auth."""
        result = classify_dialog_type(FIXTURE_NON_PASSWORD_WITH_TEXTFIELD)
        assert result != "password_auth"
        assert result == "unknown"

    def test_password_prompt_no_bullet(self):
        """Password prompt without bullet glyph → possible_password_auth (escalates)."""
        assert classify_dialog_type(FIXTURE_PASSWORD_PROMPT_NO_BULLET) == "possible_password_auth"

    def test_single_field_password(self):
        """Single password field with bullet glyph should classify as password_auth."""
        assert classify_dialog_type(FIXTURE_SINGLE_FIELD_PASSWORD) == "password_auth"


class TestParseButtons:
    def test_brew_buttons(self):
        assert parse_buttons(FIXTURE_BREW_PASSWORD) == ["Cancel", "OK"]

    def test_allow_deny_buttons(self):
        assert parse_buttons(FIXTURE_ALLOW_DENY) == ["Deny", "Allow"]


class TestComputeDialogId:
    def test_stable_id(self):
        """Same inputs → same ID."""
        id1 = compute_dialog_id("App", "action", "type", ["OK"])
        id2 = compute_dialog_id("App", "action", "type", ["OK"])
        assert id1 == id2

    def test_different_inputs(self):
        """Different inputs → different ID."""
        id1 = compute_dialog_id("App1", "action", "type", ["OK"])
        id2 = compute_dialog_id("App2", "action", "type", ["OK"])
        assert id1 != id2

    def test_length(self):
        """ID is 16 hex chars."""
        dialog_id = compute_dialog_id("A", "B", "C", ["D"])
        assert len(dialog_id) == 16


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------

class TestAuthDialogConfig:
    def test_default_config(self):
        cfg = AuthDialogConfig()
        assert cfg.dry_run is True
        assert cfg.enabled is True
        assert cfg.poll_interval_seconds == 3.0

    def test_from_dict(self):
        d = {
            "enabled": True,
            "dry_run": False,
            "poll_interval_seconds": 5,
            "auto_approve": [
                {"name": "test_rule", "app_pattern": "TestApp", "action": "approve"},
            ],
            "always_escalate": [
                {"name": "keychain", "action_pattern": "keychain"},
            ],
        }
        cfg = AuthDialogConfig.from_dict(d)
        assert cfg.dry_run is False
        assert len(cfg.auto_approve) == 1
        assert cfg.auto_approve[0].name == "test_rule"

    def test_validate_good(self):
        cfg = AuthDialogConfig()
        cfg.validate()  # Should not raise

    def test_validate_bad_rate_limit(self):
        cfg = AuthDialogConfig()
        cfg.rate_limit.max_approvals_per_minute = 0
        with pytest.raises(ValueError, match="max_approvals_per_minute"):
            cfg.validate()

    def test_validate_possible_password_auth(self):
        cfg = AuthDialogConfig()
        cfg.auto_approve = [Rule(name="bad_rule", dialog_type="possible_password_auth")]
        with pytest.raises(ValueError, match="possible_password_auth"):
            cfg.validate()


# ---------------------------------------------------------------------------
# Rule tests
# ---------------------------------------------------------------------------

class TestRule:
    def test_match_app(self):
        rule = Rule(name="test", app_pattern="brew|Homebrew")
        dialog = DialogContext(
            ax_tree_snapshot="", dialog_id="", app_name="Homebrew",
            action="", dialog_type="", buttons=[], detected_at=0,
        )
        assert rule.matches(dialog)

    def test_no_match(self):
        rule = Rule(name="test", app_pattern="brew")
        dialog = DialogContext(
            ax_tree_snapshot="", dialog_id="", app_name="Firefox",
            action="", dialog_type="", buttons=[], detected_at=0,
        )
        assert not rule.matches(dialog)

    def test_match_dialog_type(self):
        rule = Rule(name="test", dialog_type="password_auth")
        dialog = DialogContext(
            ax_tree_snapshot="", dialog_id="", app_name="",
            action="", dialog_type="password_auth", buttons=[], detected_at=0,
        )
        assert rule.matches(dialog)

    def test_match_action_pattern(self):
        rule = Rule(name="test", action_pattern=r"keychain|credential")
        dialog = DialogContext(
            ax_tree_snapshot="", dialog_id="", app_name="",
            action="access keychain", dialog_type="", buttons=[], detected_at=0,
        )
        assert rule.matches(dialog)


# ---------------------------------------------------------------------------
# RateLimiter tests
# ---------------------------------------------------------------------------

class TestRateLimiter:
    def test_not_exceeded_initially(self):
        rl = RateLimiter(RateLimitConfig())
        assert not rl.exceeded()

    def test_exceeded_per_minute(self):
        rl = RateLimiter(RateLimitConfig(max_approvals_per_minute=2))
        rl.record_approval()
        rl.record_approval()
        assert rl.exceeded()

    def test_cooldown_after_escalation(self):
        rl = RateLimiter(RateLimitConfig(cooldown_after_escalation_seconds=10))
        rl.record_escalation()
        assert rl.exceeded()

    def test_first_hit_in_window(self):
        rl = RateLimiter(RateLimitConfig(max_approvals_per_minute=1))
        rl.record_approval()
        assert rl.exceeded()
        assert rl.first_hit_in_window()
        assert not rl.first_hit_in_window()  # second call returns False


# ---------------------------------------------------------------------------
# DialogQueue tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestDialogQueue:
    async def test_enqueue_dequeue(self):
        q = DialogQueue(max_size=3, ttl_seconds=30)
        dialog = DialogContext(
            ax_tree_snapshot="", dialog_id="abc123",
            app_name="Test", action="test", dialog_type="unknown",
            buttons=[], detected_at=time.monotonic(),
        )
        result = await q.check_and_enqueue(dialog)
        assert result is True
        assert "abc123" in q._seen_ids

    async def test_dedup(self):
        q = DialogQueue(max_size=3, ttl_seconds=30)
        dialog = DialogContext(
            ax_tree_snapshot="", dialog_id="abc123",
            app_name="Test", action="test", dialog_type="unknown",
            buttons=[], detected_at=time.monotonic(),
        )
        await q.check_and_enqueue(dialog)
        result = await q.check_and_enqueue(dialog)
        assert result is False  # Deduped

    async def test_overflow_escalates(self):
        escalated = []

        async def mock_escalate(msg, **kwargs):
            escalated.append(msg)

        q = DialogQueue(max_size=1, ttl_seconds=30, escalate_fn=mock_escalate)

        d1 = DialogContext(
            ax_tree_snapshot="", dialog_id="d1",
            app_name="App1", action="a1", dialog_type="unknown",
            buttons=[], detected_at=time.monotonic(),
        )
        d2 = DialogContext(
            ax_tree_snapshot="", dialog_id="d2",
            app_name="App2", action="a2", dialog_type="unknown",
            buttons=[], detected_at=time.monotonic(),
        )

        await q.check_and_enqueue(d1)  # Fills queue
        result = await q.check_and_enqueue(d2)  # Overflow
        assert result is False
        assert len(escalated) == 2  # d1 and d2 both escalated

    async def test_prune_suppressed(self):
        q = DialogQueue()
        q._suppress_until["test:key"] = time.monotonic() - 1  # expired
        q._suppress_until["test:key2"] = time.monotonic() + 100  # still valid
        q.prune_suppressed()
        assert "test:key" not in q._suppress_until
        assert "test:key2" in q._suppress_until


# ---------------------------------------------------------------------------
# Classification tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestClassification:
    async def _make_monitor(self):
        cfg = AuthDialogConfig()
        cfg.always_escalate = [
            Rule(name="keychain", action_pattern="keychain"),
            Rule(name="possible_pw", dialog_type="possible_password_auth"),
        ]
        cfg.auto_approve = [
            Rule(name="brew", app_pattern="brew|Homebrew",
                 action_pattern="make changes", dialog_type="password_auth",
                 action="approve_with_password"),
        ]
        return AuthDialogMonitor(config=cfg)

    async def test_always_escalate_first(self):
        """Dialog matching both always_escalate and auto_approve → escalate."""
        monitor = await self._make_monitor()
        dialog = DialogContext(
            ax_tree_snapshot="", dialog_id="", app_name="Homebrew",
            action="access keychain", dialog_type="password_auth",
            buttons=[], detected_at=0,
        )
        provenance = TraceResult(confidence=Confidence.HIGH, method="test")
        decision = await monitor.classify(dialog, provenance)
        assert decision.action == DecisionAction.ESCALATE
        assert "keychain" in decision.reason

    async def test_auto_approve_with_provenance(self):
        monitor = await self._make_monitor()
        dialog = DialogContext(
            ax_tree_snapshot="", dialog_id="", app_name="Homebrew",
            action="brew wants to make changes.", dialog_type="password_auth",
            buttons=[], detected_at=0,
        )
        provenance = TraceResult(confidence=Confidence.HIGH, method="test")
        decision = await monitor.classify(dialog, provenance)
        assert decision.action == DecisionAction.APPROVE_WITH_PASSWORD

    async def test_no_provenance_escalates(self):
        monitor = await self._make_monitor()
        dialog = DialogContext(
            ax_tree_snapshot="", dialog_id="", app_name="Homebrew",
            action="brew wants to make changes.", dialog_type="password_auth",
            buttons=[], detected_at=0,
        )
        provenance = TraceResult(confidence=Confidence.NONE, method="no_match")
        decision = await monitor.classify(dialog, provenance)
        assert decision.action == DecisionAction.ESCALATE
        assert "no_provenance" in decision.reason

    async def test_possible_password_escalates(self):
        monitor = await self._make_monitor()
        dialog = DialogContext(
            ax_tree_snapshot="", dialog_id="", app_name="SomeApp",
            action="something", dialog_type="possible_password_auth",
            buttons=[], detected_at=0,
        )
        provenance = TraceResult(confidence=Confidence.HIGH, method="test")
        decision = await monitor.classify(dialog, provenance)
        assert decision.action == DecisionAction.ESCALATE

    async def test_no_matching_rule_escalates(self):
        monitor = await self._make_monitor()
        dialog = DialogContext(
            ax_tree_snapshot="", dialog_id="", app_name="UnknownApp",
            action="something else", dialog_type="unknown",
            buttons=[], detected_at=0,
        )
        provenance = TraceResult(confidence=Confidence.HIGH, method="test")
        decision = await monitor.classify(dialog, provenance)
        assert decision.action == DecisionAction.ESCALATE
        assert "no_matching_rule" in decision.reason


# ---------------------------------------------------------------------------
# Monitor integration tests (mocked axctl)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestMonitorDetection:
    async def test_poll_detects_security_agent(self):
        """When axctl apps shows SecurityAgent, gather_context is called."""
        call_count = 0
        async def mock_run(cmd):
            nonlocal call_count
            call_count += 1
            if "apps" in cmd:
                return "SecurityAgent (pid: 123)\nFinder (pid: 456)\n"
            elif "tree" in cmd:
                return FIXTURE_BREW_PASSWORD
            return ""

        cfg = AuthDialogConfig(enabled=True)
        monitor = AuthDialogMonitor(config=cfg, run_cmd=mock_run)
        monitor.enabled = True
        await monitor.poll()

        assert call_count == 2  # apps + tree
        assert not monitor.queue._queue.empty()

    async def test_poll_no_security_agent(self):
        """When no SecurityAgent, nothing queued."""
        async def mock_run(cmd):
            return "Finder (pid: 456)\n"

        cfg = AuthDialogConfig(enabled=True)
        monitor = AuthDialogMonitor(config=cfg, run_cmd=mock_run)
        await monitor.poll()
        assert monitor.queue._queue.empty()

    async def test_poll_empty_result_disables(self):
        """Empty axctl result disables monitor (AX permission lost)."""
        escalated = []
        async def mock_run(cmd):
            return ""
        async def mock_escalate(msg, **kwargs):
            escalated.append(msg)

        cfg = AuthDialogConfig(enabled=True)
        monitor = AuthDialogMonitor(config=cfg, run_cmd=mock_run, escalate_fn=mock_escalate)
        await monitor.poll()
        assert not monitor.enabled
        assert len(escalated) == 1

    async def test_keychain_in_progress_suppresses_poll(self):
        """Poll does nothing when _keychain_in_progress is True."""
        calls = []
        async def mock_run(cmd):
            calls.append(cmd)
            return "SecurityAgent (pid: 123)\n"

        cfg = AuthDialogConfig(enabled=True)
        monitor = AuthDialogMonitor(config=cfg, run_cmd=mock_run)
        monitor._keychain_in_progress = True
        await monitor.poll()
        assert len(calls) == 0  # No axctl calls made


# ---------------------------------------------------------------------------
# SIGHUP reload tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestConfigReload:
    async def test_reload_config(self):
        """_reload_config updates monitor state from disk."""
        cfg = AuthDialogConfig(dry_run=True)
        cfg.always_escalate = [Rule(name="test", action_pattern="test")]
        monitor = AuthDialogMonitor(config=cfg)

        # Mock load_default_config to return new config
        new_cfg = AuthDialogConfig(dry_run=False)
        new_cfg.always_escalate = [
            Rule(name="possible_password", dialog_type="possible_password_auth"),
        ]
        new_cfg.auto_approve = [
            Rule(name="new_rule", app_pattern="NewApp"),
        ]
        with patch("assistant.auth_dialog.load_default_config", return_value=new_cfg):
            await monitor._reload_config()

        assert monitor.config.dry_run is False
        assert len(monitor.config.auto_approve) == 1
        assert monitor.config.auto_approve[0].name == "new_rule"

    async def test_reload_config_failure_keeps_old(self):
        """Failed reload keeps existing config."""
        cfg = AuthDialogConfig(dry_run=True)
        escalated = []
        async def mock_esc(msg, **kwargs):
            escalated.append(msg)

        monitor = AuthDialogMonitor(config=cfg, escalate_fn=mock_esc)

        with patch("assistant.auth_dialog.load_default_config", side_effect=ValueError("bad config")):
            await monitor._reload_config()

        assert monitor.config.dry_run is True  # unchanged
        assert len(escalated) == 1
        assert "reload failed" in escalated[0]


# ---------------------------------------------------------------------------
# DialogQueue.process_next tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestProcessNext:
    async def test_process_next_calls_handler(self):
        """process_next passes dialog to handler function."""
        handled = []
        async def handler(dialog):
            handled.append(dialog)

        q = DialogQueue(max_size=3, ttl_seconds=30)
        dialog = DialogContext(
            ax_tree_snapshot="", dialog_id="pn1",
            app_name="Test", action="test", dialog_type="unknown",
            buttons=[], detected_at=time.monotonic(),
        )
        await q.check_and_enqueue(dialog)
        result = await q.process_next(handler)
        assert result is True
        assert len(handled) == 1
        assert handled[0].dialog_id == "pn1"
        # seen_ids cleaned up
        assert "pn1" not in q._seen_ids

    async def test_process_next_discards_stale(self):
        """Stale dialogs are discarded without calling handler."""
        handled = []
        async def handler(dialog):
            handled.append(dialog)

        q = DialogQueue(max_size=3, ttl_seconds=1)
        dialog = DialogContext(
            ax_tree_snapshot="", dialog_id="stale1",
            app_name="Test", action="test", dialog_type="unknown",
            buttons=[], detected_at=time.monotonic() - 10,  # 10s old, TTL=1s
        )
        await q.check_and_enqueue(dialog)
        result = await q.process_next(handler)
        assert result is False
        assert len(handled) == 0

    async def test_process_next_timeout_returns_false(self):
        """Empty queue times out and returns False."""
        async def handler(dialog):
            pass

        q = DialogQueue(max_size=3, ttl_seconds=30)
        result = await q.process_next(handler)
        assert result is False


# ---------------------------------------------------------------------------
# Adversarial parser tests
# ---------------------------------------------------------------------------

class TestAdversarialParsers:
    """Malformed, empty, and edge-case AX trees."""

    def test_empty_tree(self):
        assert parse_app_name("") == ""
        assert parse_action("") == ""
        assert classify_dialog_type("") == "unknown"
        assert parse_buttons("") == []

    def test_no_static_text(self):
        tree = "AXApplication 'SecurityAgent'\n  AXWindow 'SecurityAgent'\n  AXButton AXTitle='OK'\n"
        assert parse_app_name(tree) == ""
        assert parse_action(tree) == ""
        assert parse_buttons(tree) == ["OK"]

    def test_single_static_text_no_action(self):
        tree = "AXStaticText AXValue='OnlyOne'\nAXButton AXTitle='Done'\n"
        assert parse_app_name(tree) == "OnlyOne"
        assert parse_action(tree) == ""

    def test_special_chars_in_app_name(self):
        tree = "AXStaticText AXValue='App (v2.0) — Beta'\nAXStaticText AXValue='wants access'\n"
        assert parse_app_name(tree) == "App (v2.0) — Beta"
        assert parse_action(tree) == "wants access"

    def test_unicode_in_action(self):
        tree = "AXStaticText AXValue='App'\nAXStaticText AXValue='需要访问权限'\n"
        assert parse_app_name(tree) == "App"
        assert parse_action(tree) == "需要访问权限"

    def test_many_static_texts(self):
        """Only first two matter — additional ones ignored."""
        tree = (
            "AXStaticText AXValue='First'\n"
            "AXStaticText AXValue='Second'\n"
            "AXStaticText AXValue='Third'\n"
            "AXStaticText AXValue='Fourth'\n"
        )
        assert parse_app_name(tree) == "First"
        assert parse_action(tree) == "Second"

    def test_textfield_with_only_whitespace(self):
        """Whitespace-only text field shouldn't classify as password."""
        tree = "AXStaticText AXValue='App'\nAXTextField AXValue='   '\nAXButton AXTitle='OK'\n"
        assert classify_dialog_type(tree) == "unknown"

    def test_password_in_app_name_not_action(self):
        """'password' in app name but not action — still triggers possible_password_auth
        if there's a text field, because we search the whole tree for 'password'."""
        tree = (
            "AXStaticText AXValue='Password Manager'\n"
            "AXStaticText AXValue='wants to sync'\n"
            "AXTextField AXValue=''\n"
            "AXButton AXTitle='OK'\n"
        )
        # 'password' appears in the tree, text field present → possible_password_auth
        assert classify_dialog_type(tree) == "possible_password_auth"

    def test_dialog_id_with_empty_fields(self):
        """Empty fields still produce a stable ID."""
        did = compute_dialog_id("", "", "", [])
        assert len(did) == 16
        did2 = compute_dialog_id("", "", "", [])
        assert did == did2

    def test_dialog_id_button_order_matters(self):
        """Different button order → different ID."""
        id1 = compute_dialog_id("App", "act", "type", ["OK", "Cancel"])
        id2 = compute_dialog_id("App", "act", "type", ["Cancel", "OK"])
        assert id1 != id2


# ---------------------------------------------------------------------------
# Dry-run guard and resolution tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestResolution:
    async def test_dry_run_does_not_resolve(self):
        """In dry_run mode, APPROVE decisions log but don't actually resolve."""
        cfg = AuthDialogConfig(dry_run=True)
        monitor = AuthDialogMonitor(config=cfg)
        dialog = DialogContext(
            ax_tree_snapshot="", dialog_id="dr1",
            app_name="Test", action="test", dialog_type="unknown",
            buttons=[], detected_at=time.monotonic(),
        )
        decision = Decision(DecisionAction.APPROVE, "test_rule")
        result = await monitor.resolve(dialog, decision)
        assert result.success is True
        assert result.method == "dry_run"

    async def test_dry_run_approve_with_password(self):
        """APPROVE_WITH_PASSWORD in dry_run also just logs."""
        cfg = AuthDialogConfig(dry_run=True)
        monitor = AuthDialogMonitor(config=cfg)
        dialog = DialogContext(
            ax_tree_snapshot="", dialog_id="dr2",
            app_name="Test", action="test", dialog_type="password_auth",
            buttons=[], detected_at=time.monotonic(),
        )
        decision = Decision(DecisionAction.APPROVE_WITH_PASSWORD, "brew")
        result = await monitor.resolve(dialog, decision)
        assert result.success is True
        assert result.method == "dry_run"

    async def test_escalation_always_works(self):
        """ESCALATE decisions call escalate_fn regardless of dry_run."""
        escalated = []
        async def mock_esc(msg, **kwargs):
            escalated.append(msg)

        cfg = AuthDialogConfig(dry_run=True)
        monitor = AuthDialogMonitor(config=cfg, escalate_fn=mock_esc)
        dialog = DialogContext(
            ax_tree_snapshot="", dialog_id="esc1",
            app_name="Test", action="test", dialog_type="unknown",
            buttons=[], detected_at=time.monotonic(),
        )
        decision = Decision(DecisionAction.ESCALATE, "test_reason")
        result = await monitor.resolve(dialog, decision)
        assert result.success is True
        assert result.method == "escalated"
        assert len(escalated) == 1

    async def test_non_dry_run_click_allow_succeeds(self):
        """With dry_run=False, APPROVE calls _click_allow with mock AX backend."""
        mock_ax = MockAXBackend(dismiss_after_press=True)

        async def mock_run(cmd):
            if "apps" in cmd:
                # After press, dialog should be dismissed
                return "Finder (pid: 456)\n" if mock_ax._pressed else "SecurityAgent (pid: 123)\nFinder\n"
            elif "tree" in cmd:
                return FIXTURE_ALLOW_DENY
            return ""

        cfg = AuthDialogConfig(dry_run=False, dialog_verify_wait_seconds=0.01)
        monitor = AuthDialogMonitor(config=cfg, run_cmd=mock_run, ax_backend=mock_ax)
        dialog = DialogContext(
            ax_tree_snapshot=FIXTURE_ALLOW_DENY, dialog_id=compute_dialog_id(
                "Google Chrome", "Google Chrome wants to access your location",
                "allow_deny", ["Deny", "Allow"]),
            app_name="Google Chrome",
            action="Google Chrome wants to access your location",
            dialog_type="allow_deny",
            buttons=["Deny", "Allow"],
            detected_at=time.monotonic(),
        )
        decision = Decision(DecisionAction.APPROVE, "test_rule")
        result = await monitor.resolve(dialog, decision)
        assert result.success is True
        assert result.method == "pyobjc_axpress"

    async def test_non_dry_run_enter_password(self):
        """With dry_run=False, APPROVE_WITH_PASSWORD enters password via mock AX."""
        mock_ax = MockAXBackend(dismiss_after_press=True)

        async def mock_run(cmd):
            if "apps" in cmd:
                return "Finder (pid: 456)\n" if mock_ax._pressed else "SecurityAgent (pid: 123)\nFinder\n"
            elif "tree" in cmd:
                return FIXTURE_BREW_PASSWORD
            return ""

        cfg = AuthDialogConfig(dry_run=False, dialog_verify_wait_seconds=0.01)
        monitor = AuthDialogMonitor(config=cfg, run_cmd=mock_run, ax_backend=mock_ax)
        # Mock keychain access
        monitor._get_password_from_keychain = AsyncMock(return_value=b"testpass123")

        dialog = DialogContext(
            ax_tree_snapshot=FIXTURE_BREW_PASSWORD,
            dialog_id=compute_dialog_id(
                "Homebrew", "brew wants to make changes.", "password_auth", ["Cancel", "OK"]),
            app_name="Homebrew",
            action="brew wants to make changes.",
            dialog_type="password_auth",
            buttons=["Cancel", "OK"],
            detected_at=time.monotonic(),
        )
        decision = Decision(DecisionAction.APPROVE_WITH_PASSWORD, "brew_install")
        result = await monitor.resolve(dialog, decision)
        assert result.success is True
        assert result.method == "pyobjc_axvalue"

    async def test_dialog_changed_aborts_resolution(self):
        """If dialog identity changes before resolution, abort."""
        async def mock_run(cmd):
            if "apps" in cmd:
                return "SecurityAgent (pid: 123)\nFinder\n"
            elif "tree" in cmd:
                # Return a DIFFERENT dialog than expected
                return FIXTURE_ALLOW_DENY
            return ""

        cfg = AuthDialogConfig(dry_run=False)
        monitor = AuthDialogMonitor(config=cfg, run_cmd=mock_run)
        dialog = DialogContext(
            ax_tree_snapshot="", dialog_id="definitely_wrong_id",
            app_name="Test", action="test", dialog_type="unknown",
            buttons=[], detected_at=time.monotonic(),
        )
        decision = Decision(DecisionAction.APPROVE, "test_rule")
        result = await monitor.resolve(dialog, decision)
        assert result.success is False
        assert result.reason == "dialog_changed"

    async def test_keychain_unavailable(self):
        """If keychain returns None, password entry fails gracefully."""
        async def mock_run(cmd):
            if "apps" in cmd:
                return "SecurityAgent (pid: 123)\nFinder\n"
            elif "tree" in cmd:
                return FIXTURE_BREW_PASSWORD
            return ""

        cfg = AuthDialogConfig(dry_run=False)
        monitor = AuthDialogMonitor(config=cfg, run_cmd=mock_run)
        monitor._get_password_from_keychain = AsyncMock(return_value=None)

        dialog = DialogContext(
            ax_tree_snapshot=FIXTURE_BREW_PASSWORD,
            dialog_id=compute_dialog_id(
                "Homebrew", "brew wants to make changes.", "password_auth", ["Cancel", "OK"]),
            app_name="Homebrew",
            action="brew wants to make changes.",
            dialog_type="password_auth",
            buttons=["Cancel", "OK"],
            detected_at=time.monotonic(),
        )
        decision = Decision(DecisionAction.APPROVE_WITH_PASSWORD, "brew_install")
        result = await monitor.resolve(dialog, decision)
        assert result.success is False
        assert result.reason == "keychain_unavailable"

    async def test_consecutive_persist_failures_escalate(self):
        """3+ consecutive dialog_persisted failures trigger escalation."""
        escalated = []
        async def mock_esc(msg, **kwargs):
            escalated.append(msg)

        mock_ax = MockAXBackend(dismiss_after_press=False)  # Never dismisses

        async def mock_run(cmd):
            if "apps" in cmd:
                return "SecurityAgent (pid: 123)\nFinder\n"
            elif "tree" in cmd:
                return FIXTURE_ALLOW_DENY
            return ""

        cfg = AuthDialogConfig(dry_run=False, dialog_verify_wait_seconds=0.01,
                               dialog_verify_max_attempts=1)
        monitor = AuthDialogMonitor(config=cfg, run_cmd=mock_run,
                                     escalate_fn=mock_esc, ax_backend=mock_ax)

        dialog_id = compute_dialog_id(
            "Google Chrome", "Google Chrome wants to access your location",
            "allow_deny", ["Deny", "Allow"])

        for i in range(4):
            dialog = DialogContext(
                ax_tree_snapshot=FIXTURE_ALLOW_DENY, dialog_id=dialog_id,
                app_name="Google Chrome",
                action="Google Chrome wants to access your location",
                dialog_type="allow_deny", buttons=["Deny", "Allow"],
                detected_at=time.monotonic(),
            )
            decision = Decision(DecisionAction.APPROVE, "test_rule")
            await monitor.resolve(dialog, decision)

        # Should have escalated about broken AX after 4th failure
        ax_broken_msgs = [m for m in escalated if "AX interaction may be broken" in m]
        assert len(ax_broken_msgs) == 1

    async def test_axvalue_failure_triggers_keystroke_fallback(self):
        """When AXValue set fails (err != 0), falls back to keystroke helper."""
        mock_ax = MockAXBackend(dismiss_after_press=True)
        mock_ax._set_value_error = -25211  # Simulates AXValue failure on secure field
        fallback_called = False

        async def mock_run(cmd):
            nonlocal fallback_called
            if "apps" in cmd:
                # After fallback, dialog dismissed
                return "Finder (pid: 456)\n" if fallback_called else "SecurityAgent (pid: 123)\nFinder\n"
            elif "tree" in cmd:
                return FIXTURE_BREW_PASSWORD
            elif "type" in cmd or "axctl" in cmd:
                fallback_called = True
                return ""
            return ""

        cfg = AuthDialogConfig(dry_run=False, dialog_verify_wait_seconds=0.01)
        monitor = AuthDialogMonitor(config=cfg, run_cmd=mock_run, ax_backend=mock_ax)
        monitor._get_password_from_keychain = AsyncMock(return_value=b"testpass123")

        # Mock the fallback to simulate successful keystroke entry
        async def mock_fallback(dialog, pw_buf):
            from assistant.auth_dialog import ResolutionResult
            return ResolutionResult(success=True, method="keystroke_fallback")

        monitor._enter_password_fallback = mock_fallback

        dialog = DialogContext(
            ax_tree_snapshot=FIXTURE_BREW_PASSWORD,
            dialog_id=compute_dialog_id(
                "Homebrew", "brew wants to make changes.", "password_auth", ["Cancel", "OK"]),
            app_name="Homebrew",
            action="brew wants to make changes.",
            dialog_type="password_auth",
            buttons=["Cancel", "OK"],
            detected_at=time.monotonic(),
        )
        decision = Decision(DecisionAction.APPROVE_WITH_PASSWORD, "brew_install")
        result = await monitor.resolve(dialog, decision)
        assert result.success is True
        assert result.method == "keystroke_fallback"

    async def test_click_allow_axctl_fallback(self):
        """When PyObjC AXPress fails to dismiss, falls back to axctl click."""
        mock_ax = MockAXBackend(dismiss_after_press=False)  # PyObjC press doesn't dismiss
        axctl_click_called = False

        async def mock_run(cmd):
            nonlocal axctl_click_called
            if "apps" in cmd:
                # After axctl click, dialog is dismissed
                return "Finder (pid: 456)\n" if axctl_click_called else "SecurityAgent (pid: 123)\nFinder\n"
            elif "tree" in cmd:
                return FIXTURE_ALLOW_DENY
            elif "click" in cmd:
                axctl_click_called = True
                return ""
            return ""

        cfg = AuthDialogConfig(dry_run=False, dialog_verify_wait_seconds=0.01,
                               dialog_verify_max_attempts=1)
        monitor = AuthDialogMonitor(config=cfg, run_cmd=mock_run, ax_backend=mock_ax)

        dialog = DialogContext(
            ax_tree_snapshot=FIXTURE_ALLOW_DENY,
            dialog_id=compute_dialog_id(
                "Google Chrome", "Google Chrome wants to access your location",
                "allow_deny", ["Deny", "Allow"]),
            app_name="Google Chrome",
            action="Google Chrome wants to access your location",
            dialog_type="allow_deny",
            buttons=["Deny", "Allow"],
            detected_at=time.monotonic(),
        )
        decision = Decision(DecisionAction.APPROVE, "test_rule")
        result = await monitor.resolve(dialog, decision)
        assert result.success is True
        assert result.method == "axctl_fallback"
        assert axctl_click_called

    async def test_password_bytearray_zeroed_after_resolution(self):
        """Verify password bytearray is zeroed in finally block."""
        mock_ax = MockAXBackend(dismiss_after_press=True)
        captured_pw_buf = [None]

        original_enter = AuthDialogMonitor._enter_password

        async def capturing_enter(self_monitor, dialog):
            # Intercept to capture the pw_buf state after resolution
            result = await original_enter(self_monitor, dialog)
            return result

        async def mock_run(cmd):
            if "apps" in cmd:
                return "Finder (pid: 456)\n" if mock_ax._pressed else "SecurityAgent (pid: 123)\nFinder\n"
            elif "tree" in cmd:
                return FIXTURE_BREW_PASSWORD
            return ""

        cfg = AuthDialogConfig(dry_run=False, dialog_verify_wait_seconds=0.01)
        monitor = AuthDialogMonitor(config=cfg, run_cmd=mock_run, ax_backend=mock_ax)
        monitor._get_password_from_keychain = AsyncMock(return_value=b"testpass123")

        dialog = DialogContext(
            ax_tree_snapshot=FIXTURE_BREW_PASSWORD,
            dialog_id=compute_dialog_id(
                "Homebrew", "brew wants to make changes.", "password_auth", ["Cancel", "OK"]),
            app_name="Homebrew",
            action="brew wants to make changes.",
            dialog_type="password_auth",
            buttons=["Cancel", "OK"],
            detected_at=time.monotonic(),
        )
        decision = Decision(DecisionAction.APPROVE_WITH_PASSWORD, "brew_install")
        result = await monitor.resolve(dialog, decision)
        assert result.success is True
        # The value was set via mock — verify it was called with the password
        assert mock_ax._value_set == "testpass123"


# ---------------------------------------------------------------------------
# Config validation edge cases
# ---------------------------------------------------------------------------

class TestConfigValidationEdgeCases:
    def test_hour_less_than_minute(self):
        cfg = AuthDialogConfig()
        cfg.rate_limit.max_approvals_per_hour = 1
        cfg.rate_limit.max_approvals_per_minute = 5
        with pytest.raises(ValueError, match="max_approvals_per_hour"):
            cfg.validate()

    def test_poll_interval_too_low(self):
        cfg = AuthDialogConfig(poll_interval_seconds=0.5)
        with pytest.raises(ValueError, match="poll_interval_seconds"):
            cfg.validate()

    def test_queue_ttl_less_than_poll(self):
        cfg = AuthDialogConfig(poll_interval_seconds=5, queue_ttl_seconds=2)
        with pytest.raises(ValueError, match="queue_ttl_seconds"):
            cfg.validate()

    def test_from_dict_empty(self):
        cfg = AuthDialogConfig.from_dict({})
        assert cfg.enabled is True
        assert cfg.dry_run is True

    def test_from_dict_none(self):
        cfg = AuthDialogConfig.from_dict(None)
        assert cfg.enabled is True


# ---------------------------------------------------------------------------
# Concurrent queue operations
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestQueueConcurrency:
    async def test_concurrent_enqueue_dedup(self):
        """Multiple concurrent enqueues of same dialog should only enqueue once."""
        q = DialogQueue(max_size=5, ttl_seconds=30)
        dialog = DialogContext(
            ax_tree_snapshot="", dialog_id="conc1",
            app_name="Test", action="test", dialog_type="unknown",
            buttons=[], detected_at=time.monotonic(),
        )
        # Fire 5 concurrent enqueue attempts
        results = await asyncio.gather(*[
            q.check_and_enqueue(DialogContext(
                ax_tree_snapshot="", dialog_id="conc1",
                app_name="Test", action="test", dialog_type="unknown",
                buttons=[], detected_at=time.monotonic(),
            ))
            for _ in range(5)
        ])
        # Exactly one should succeed
        assert sum(results) == 1, f"Expected 1 enqueue, got {sum(results)}"

    async def test_concurrent_different_dialogs(self):
        """Multiple different dialogs can be enqueued concurrently."""
        q = DialogQueue(max_size=5, ttl_seconds=30)
        results = await asyncio.gather(*[
            q.check_and_enqueue(DialogContext(
                ax_tree_snapshot="", dialog_id=f"diff{i}",
                app_name=f"App{i}", action="test", dialog_type="unknown",
                buttons=[], detected_at=time.monotonic(),
            ))
            for i in range(3)
        ])
        assert sum(results) == 3, f"Expected 3 enqueues, got {sum(results)}"

    async def test_enqueue_during_process(self):
        """Can enqueue new dialogs while processing one."""
        handled = []
        enqueue_result = [None]

        async def slow_handler(dialog):
            handled.append(dialog)
            # Simulate slow processing — enqueue another dialog during this time
            q2_dialog = DialogContext(
                ax_tree_snapshot="", dialog_id="during_process",
                app_name="New", action="new", dialog_type="unknown",
                buttons=[], detected_at=time.monotonic(),
            )
            enqueue_result[0] = await q.check_and_enqueue(q2_dialog)

        q = DialogQueue(max_size=5, ttl_seconds=30)
        first = DialogContext(
            ax_tree_snapshot="", dialog_id="first",
            app_name="First", action="first", dialog_type="unknown",
            buttons=[], detected_at=time.monotonic(),
        )
        await q.check_and_enqueue(first)
        await q.process_next(slow_handler)

        assert len(handled) == 1
        assert enqueue_result[0] is True  # Was able to enqueue during processing


# ---------------------------------------------------------------------------
# Permission (TCC) prompts owned by non-SecurityAgent processes
# ---------------------------------------------------------------------------

from assistant.auth_dialog import PERMISSION_PROMPT_TYPE, parse_permission_prompt  # noqa: E402

# Real `axctl tree UserNotificationCenter` output (2026-10-19), default 80-col wrap.
FIXTURE_TCC_SCREEN = """AXApplication AXTitle='UserNotificationCenter'
 AXWindow AXTitle=''
  AXImage 
  AXStaticText AXValue='“python3.14” is requesting to bypass the system private 
window picker and directly access your screen and audio.'
  AXStaticText AXValue='This will allow python3.14 to record your screen and 
system audio, including personal or sensitive information that may be visible or
audible.'
  AXButton AXTitle='Allow'
  AXButton AXTitle='Open System Settings'
 AXMenuBar 
  AXMenuBarItem AXTitle='Apple'
   AXMenu 
    AXMenuItem AXTitle='About This Mac'
"""


class TestPermissionPromptParser:
    def test_parses_tcc_prompt(self):
        info = parse_permission_prompt(FIXTURE_TCC_SCREEN, "UserNotificationCenter")
        assert info["requesting_app"] == "python3.14"
        assert info["buttons"] == ["Allow", "Open System Settings"]
        assert "bypass the system private window picker" in info["prompt_text"]
        assert "\n" not in info["prompt_text"]
        assert "About This Mac" not in info["prompt_text"]

    def test_no_window_returns_none(self):
        tree = "AXApplication AXTitle='UserNotificationCenter'\n AXMenuBar \n"
        assert parse_permission_prompt(tree, "UserNotificationCenter") is None

    def test_window_without_text_returns_none(self):
        tree = "AXApplication AXTitle='X'\n AXWindow AXTitle=''\n  AXImage \n"
        assert parse_permission_prompt(tree, "X") is None


@pytest.mark.asyncio
class TestPermissionPromptMonitor:
    async def test_poll_detects_and_never_approves(self):
        async def mock_run(cmd):
            if cmd.endswith(" apps"):
                return "Finder (pid: 1)\nUserNotificationCenter (pid: 9135)\n"
            if "tree" in cmd and "UserNotificationCenter" in cmd:
                return FIXTURE_TCC_SCREEN
            return ""

        escalations = []

        async def mock_escalate(msg, dialog_info=None):
            escalations.append((msg, dialog_info))

        cfg = AuthDialogConfig(enabled=True, dry_run=False)
        # Even a catch-all approve rule must not approve a permission prompt
        cfg.auto_approve = [Rule(name="everything", app_pattern=".*")]
        monitor = AuthDialogMonitor(config=cfg, run_cmd=mock_run, escalate_fn=mock_escalate,
                                    ax_backend=MockAXBackend())
        await monitor.poll()
        dialog = monitor.queue._queue.get_nowait()
        assert dialog.dialog_type == PERMISSION_PROMPT_TYPE
        assert dialog.owner == "UserNotificationCenter"
        assert dialog.app_name == "python3.14"

        decision = await monitor.classify(dialog, TraceResult(confidence=Confidence.HIGH))
        assert decision.action == DecisionAction.ESCALATE

        await monitor._handle_dialog(dialog)
        assert len(escalations) == 1
        msg, info = escalations[0]
        assert "python3.14" in msg and "bypass the system private window picker" in msg
        assert info["owner"] == "UserNotificationCenter"
        assert info["dialog_type"] == PERMISSION_PROMPT_TYPE
        assert not monitor._ax._pressed
        # Long re-escalation suppression for a prompt that stays on screen
        until = monitor.queue._suppress_until[f"overflow:{dialog.dialog_id}"]
        assert until - time.monotonic() > 3600


class TestPermissionPromptConfig:
    def test_validate_rejects_auto_approve_permission_prompt(self):
        cfg = AuthDialogConfig()
        cfg.auto_approve = [Rule(name="bad", dialog_type=PERMISSION_PROMPT_TYPE)]
        with pytest.raises(ValueError):
            cfg.validate()
