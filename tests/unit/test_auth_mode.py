"""Tests for assistant.auth_mode — OAuth quota detection and api_key fallback."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from assistant import auth_mode


@pytest.fixture
def tmp_state_dir(tmp_path, monkeypatch):
    """Redirect AUTH_MODE_FILE to a temp path so tests don't touch real state."""
    monkeypatch.setattr(auth_mode, "AUTH_MODE_FILE", tmp_path / "auth_mode.json")
    yield tmp_path


# ---------------------------------------------------------------------------
# is_oauth_quota_error — pattern classifier
# ---------------------------------------------------------------------------

class TestIsOAuthQuotaError:
    def test_matches_usage_limit_reached(self):
        assert auth_mode.is_oauth_quota_error(
            "Claude usage limit reached. Your limit will reset at 3pm (PT)."
        )

    def test_matches_5_hour_limit(self):
        assert auth_mode.is_oauth_quota_error("5-hour usage limit reached")
        assert auth_mode.is_oauth_quota_error("5 hour limit hit")

    def test_matches_7_day_limit(self):
        assert auth_mode.is_oauth_quota_error("7-day usage limit reached")
        assert auth_mode.is_oauth_quota_error("Weekly usage limit reached")

    def test_matches_oauth_rate_limit(self):
        assert auth_mode.is_oauth_quota_error(
            'rate_limit_error: oauth token quota exceeded'
        )

    def test_matches_plan_limit(self):
        assert auth_mode.is_oauth_quota_error("Max plan limit reached")

    def test_no_match_on_unrelated_errors(self):
        assert not auth_mode.is_oauth_quota_error("buffer overflow")
        assert not auth_mode.is_oauth_quota_error("connection refused")
        assert not auth_mode.is_oauth_quota_error("invalid api key")
        assert not auth_mode.is_oauth_quota_error("")

    def test_no_match_on_generic_rate_limit(self):
        # Generic API rate limits are handled by _detect_block_limit, not us.
        assert not auth_mode.is_oauth_quota_error("Too many requests, slow down")


# ---------------------------------------------------------------------------
# current_mode / write_mode / clear — file-backed state
# ---------------------------------------------------------------------------

class TestModeState:
    def test_default_is_oauth_when_no_file(self, tmp_state_dir):
        info = auth_mode.current_mode()
        assert info == {"mode": "oauth"}

    def test_write_then_read(self, tmp_state_dir):
        auth_mode.write_mode("api_key", reason="oauth_quota_exhausted",
                             error_text="usage limit reached",
                             session_name="imessage/_15555550100")
        info = auth_mode.current_mode()
        assert info["mode"] == "api_key"
        assert info["reason"] == "oauth_quota_exhausted"
        assert info["triggered_by_session"] == "imessage/_15555550100"
        assert "since" in info
        assert info["error_text"] == "usage limit reached"

    def test_write_truncates_long_error_text(self, tmp_state_dir):
        long_err = "x" * 1000
        auth_mode.write_mode("api_key", reason="x", error_text=long_err)
        info = auth_mode.current_mode()
        assert len(info["error_text"]) == 500

    def test_clear_removes_file(self, tmp_state_dir):
        auth_mode.write_mode("api_key", reason="x")
        assert auth_mode.AUTH_MODE_FILE.exists()
        assert auth_mode.clear() is True
        assert not auth_mode.AUTH_MODE_FILE.exists()
        # current_mode falls back to oauth
        assert auth_mode.current_mode() == {"mode": "oauth"}

    def test_clear_returns_false_when_already_clean(self, tmp_state_dir):
        assert auth_mode.clear() is False

    def test_corrupt_json_falls_back_to_oauth(self, tmp_state_dir):
        auth_mode.AUTH_MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
        auth_mode.AUTH_MODE_FILE.write_text("{not json")
        assert auth_mode.current_mode() == {"mode": "oauth"}

    def test_is_api_key_mode(self, tmp_state_dir):
        assert not auth_mode.is_api_key_mode()
        auth_mode.write_mode("api_key", reason="x")
        assert auth_mode.is_api_key_mode()


# ---------------------------------------------------------------------------
# promote_fallback_key — env var manipulation
# ---------------------------------------------------------------------------

class TestPromoteFallbackKey:
    def test_promotes_when_fallback_set(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY_FALLBACK", "sk-ant-test-fallback")
        assert auth_mode.promote_fallback_key() is True
        assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-test-fallback"

    def test_returns_false_when_no_fallback(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY_FALLBACK", raising=False)
        assert auth_mode.promote_fallback_key() is False
        assert "ANTHROPIC_API_KEY" not in os.environ


# ---------------------------------------------------------------------------
# apply_at_startup — daemon boot integration
# ---------------------------------------------------------------------------

class TestApplyAtStartup:
    def test_oauth_default_no_promotion(self, tmp_state_dir, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY_FALLBACK", "sk-test")
        assert auth_mode.apply_at_startup() == "oauth"
        assert "ANTHROPIC_API_KEY" not in os.environ

    def test_api_key_mode_promotes_fallback(self, tmp_state_dir, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY_FALLBACK", "sk-ant-test-fb")
        auth_mode.write_mode("api_key", reason="oauth_quota_exhausted")
        assert auth_mode.apply_at_startup() == "api_key"
        assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-test-fb"

    def test_api_key_mode_without_fallback_falls_back_to_oauth(self, tmp_state_dir, monkeypatch):
        # Persisted state says api_key, but no fallback in env — degrade to oauth.
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY_FALLBACK", raising=False)
        auth_mode.write_mode("api_key", reason="x")
        assert auth_mode.apply_at_startup() == "oauth"
        assert "ANTHROPIC_API_KEY" not in os.environ
