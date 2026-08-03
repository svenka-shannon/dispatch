"""Unit tests for the oauth_token dependency probe and session auth-failure flag."""
import json
import time

import pytest

from assistant import dependency_checks
from assistant.dependency_health import ProbeStatus


@pytest.fixture
def flag_path(tmp_path, monkeypatch):
    path = tmp_path / "auth_failure.json"
    monkeypatch.setattr(dependency_checks, "AUTH_FAILURE_FLAG", path)
    return path


class TestOauthProbe:
    def test_ok_when_no_flag(self, flag_path):
        result = dependency_checks._oauth_probe()
        assert result.status == ProbeStatus.OK

    def test_down_when_flagged_and_token_expired(self, flag_path, monkeypatch):
        flag_path.write_text(json.dumps({
            "ts": "2026-08-02T18:21:28-04:00",
            "session_name": "imessage/_15551234567",
            "detail": "Failed to authenticate: OAuth session expired and could not be refreshed",
        }))
        monkeypatch.setattr(
            dependency_checks, "_keychain_token_expires_at_ms",
            lambda: int(time.time() * 1000) - 3_600_000,
        )
        result = dependency_checks._oauth_probe()
        assert result.status == ProbeStatus.DOWN
        assert "/login" in result.detail
        assert result.diagnostics["flagged_by"] == "imessage/_15551234567"

    def test_down_when_flagged_and_keychain_unreadable(self, flag_path, monkeypatch):
        flag_path.write_text("{}")
        monkeypatch.setattr(dependency_checks, "_keychain_token_expires_at_ms", lambda: None)
        result = dependency_checks._oauth_probe()
        assert result.status == ProbeStatus.DOWN

    def test_clears_flag_when_token_valid_again(self, flag_path, monkeypatch):
        flag_path.write_text("{}")
        monkeypatch.setattr(
            dependency_checks, "_keychain_token_expires_at_ms",
            lambda: int(time.time() * 1000) + 3_600_000,
        )
        result = dependency_checks._oauth_probe()
        assert result.status == ProbeStatus.OK
        assert not flag_path.exists()


class TestAuthFailureRegex:
    def test_matches_outage_text(self):
        from assistant.sdk_session import _AUTH_FAILURE_RE
        assert _AUTH_FAILURE_RE.search(
            "Failed to authenticate: OAuth session expired and could not be refreshed"
        )

    def test_does_not_match_normal_text(self):
        from assistant.sdk_session import _AUTH_FAILURE_RE
        assert not _AUTH_FAILURE_RE.search(
            "here's the standings for mrtoolshed — #365 of 1833"
        )
        assert not _AUTH_FAILURE_RE.search(
            "you'll need to authenticate with google first, want me to?"
        )
