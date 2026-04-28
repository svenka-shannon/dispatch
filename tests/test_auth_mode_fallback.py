"""Integration tests for OAuth quota fallback in SDKSession.

Verifies that when an SDK session encounters an OAuth quota-exhausted error,
it:
  1. Detects it via auth_mode.is_oauth_quota_error()
  2. Promotes ANTHROPIC_API_KEY_FALLBACK into ANTHROPIC_API_KEY
  3. Writes state/auth_mode.json with mode=api_key
  4. Returns True so the caller can mark the session for restart

Uses the FakeClaudeSDKClient mock from tests/conftest.py.
"""
from __future__ import annotations

import json
import os

import pytest

from assistant import auth_mode


@pytest.fixture
def isolated_auth_state(tmp_path, monkeypatch):
    """Point AUTH_MODE_FILE at a temp path so tests don't touch real state."""
    monkeypatch.setattr(auth_mode, "AUTH_MODE_FILE", tmp_path / "auth_mode.json")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY_FALLBACK", "sk-ant-test-fallback-12345")
    yield tmp_path


class TestSDKSessionQuotaFallback:
    """Cover SDKSession._maybe_handle_oauth_quota_error end-to-end."""

    def test_returns_false_for_unrelated_errors(self, sdk_session, isolated_auth_state):
        assert sdk_session._maybe_handle_oauth_quota_error("buffer overflow") is False
        assert "ANTHROPIC_API_KEY" not in os.environ
        assert not auth_mode.AUTH_MODE_FILE.exists()

    def test_returns_false_when_already_in_api_key_mode(self, sdk_session, isolated_auth_state):
        # Pre-existing api_key state — second quota error should not re-fire.
        auth_mode.write_mode("api_key", reason="prior_error")
        assert sdk_session._maybe_handle_oauth_quota_error("usage limit reached") is False

    def test_oauth_quota_error_promotes_and_persists(self, sdk_session, isolated_auth_state):
        err = "Claude usage limit reached. Resets at 3pm (PT)."
        assert sdk_session._maybe_handle_oauth_quota_error(err) is True

        # Env promoted
        assert os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-test-fallback-12345"

        # Persisted to disk
        assert auth_mode.AUTH_MODE_FILE.exists()
        info = json.loads(auth_mode.AUTH_MODE_FILE.read_text())
        assert info["mode"] == "api_key"
        assert info["reason"] == "oauth_quota_exhausted"
        assert info["triggered_by_session"] == sdk_session._session_name
        assert err.startswith(info["error_text"][:30])

    def test_quota_error_without_fallback_records_no_fallback_reason(
        self, sdk_session, isolated_auth_state, monkeypatch
    ):
        monkeypatch.delenv("ANTHROPIC_API_KEY_FALLBACK", raising=False)
        # Still classified as a quota error and returns True so the session
        # exits the loop, but stays on oauth (no fallback to promote).
        assert sdk_session._maybe_handle_oauth_quota_error("usage limit reached") is True
        assert "ANTHROPIC_API_KEY" not in os.environ

        info = json.loads(auth_mode.AUTH_MODE_FILE.read_text())
        # write_mode is called with mode='oauth' and a reason that flags the
        # missing fallback so the watchdog/admin can see what happened.
        assert info["mode"] == "oauth"
        assert info["reason"] == "oauth_quota_exhausted_no_fallback"
