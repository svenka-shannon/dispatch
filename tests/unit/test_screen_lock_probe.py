"""Unit tests for the screen_lock dependency probe/recovery (bin/screen-lock is mocked)."""
import subprocess

import pytest

from assistant import dependency_checks
from assistant.dependency_health import ProbeStatus

UNLOCKED = {
    "locked": False, "screenlock_setting": "300", "screensaver_idle_s": None,
    "display_sleep_min": 10, "password_stored": True, "hardened": False,
}
LOCKED = {**UNLOCKED, "locked": True}
HARDENED = {**UNLOCKED, "screenlock_setting": "off", "hardened": True}


class FakeCli:
    """Scripted responses for _run_screen_lock(*args): {subcommand: (rc, payload, stderr)}."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, *args, timeout=None):
        self.calls.append(args[0])
        r = self.responses[args[0]]
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def cli(monkeypatch):
    def install(responses, harden=False):
        fake = FakeCli(responses)
        monkeypatch.setattr(dependency_checks, "_run_screen_lock", fake)
        monkeypatch.setattr(dependency_checks, "_screen_lock_harden_enabled", lambda: harden)
        monkeypatch.setattr(dependency_checks, "SCREEN_LOCK_CLI", dependency_checks.Path(__file__))
        return fake
    return install


class TestProbe:
    def test_ok_when_unlocked_and_harden_off(self, cli):
        cli({"status": (0, UNLOCKED, "")})
        r = dependency_checks._screen_lock_probe()
        assert r.status == ProbeStatus.OK
        assert "harden=off" in r.detail
        assert r.diagnostics["screenlock_setting"] == "300"

    def test_down_when_locked_with_password(self, cli):
        cli({"status": (2, LOCKED, "")})
        r = dependency_checks._screen_lock_probe()
        assert r.status == ProbeStatus.DOWN
        assert not r.escalate_now
        assert "LOCKED" in r.detail

    def test_locked_without_password_escalates_immediately(self, cli):
        cli({"status": (2, {**LOCKED, "password_stored": False}, "")})
        r = dependency_checks._screen_lock_probe()
        assert r.status == ProbeStatus.DOWN
        assert r.escalate_now
        assert "mac-login-password" in r.detail

    def test_degraded_when_harden_on_and_lock_armed(self, cli):
        cli({"status": (0, UNLOCKED, "")}, harden=True)
        r = dependency_checks._screen_lock_probe()
        assert r.status == ProbeStatus.DEGRADED
        assert not r.escalate_now

    def test_degraded_without_password_escalates(self, cli):
        cli({"status": (0, {**UNLOCKED, "password_stored": False}, "")}, harden=True)
        r = dependency_checks._screen_lock_probe()
        assert r.status == ProbeStatus.DEGRADED
        assert r.escalate_now

    def test_ok_when_hardened(self, cli):
        cli({"status": (0, HARDENED, "")}, harden=True)
        assert dependency_checks._screen_lock_probe().status == ProbeStatus.OK

    def test_skip_when_state_unknown(self, cli):
        cli({"status": (0, {"locked": None}, "no session")})
        assert dependency_checks._screen_lock_probe().status == ProbeStatus.SKIP

    def test_timeout_is_down(self, cli):
        cli({"status": subprocess.TimeoutExpired("screen-lock", 30)})
        r = dependency_checks._screen_lock_probe()
        assert r.status == ProbeStatus.DOWN
        assert r.diagnostics["probe_timed_out"]


class TestRecover:
    def test_unlocks_when_locked(self, cli):
        fake = cli({"status": (2, LOCKED, ""),
                    "unlock": (0, {"locked": False, "detail": "unlocked via cgevent on attempt 1",
                                   "method": "cgevent", "attempts": 1}, "")})
        r = dependency_checks._screen_lock_recover()
        assert r.success
        assert "cgevent" in r.detail
        assert fake.calls == ["status", "unlock"]

    def test_no_password_escalates_now(self, cli):
        cli({"status": (2, LOCKED, ""), "unlock": (3, {"locked": True, "password_stored": False}, "")})
        r = dependency_checks._screen_lock_recover()
        assert not r.success and r.escalate_now
        assert "mac-login-password" in r.detail

    def test_unlock_failure_carries_specific_ask(self, cli):
        cli({"status": (2, LOCKED, ""),
             "unlock": (1, {"locked": True, "detail": "still locked after 2 attempts", "method": "system-events",
                            "attempts": 2}, "")})
        r = dependency_checks._screen_lock_recover()
        assert not r.success and not r.escalate_now
        assert "Accessibility" in r.detail
        assert r.diagnostics["unlock_attempts"] == 2

    def test_noop_when_already_unlocked_and_harden_off(self, cli):
        fake = cli({"status": (0, UNLOCKED, "")})
        r = dependency_checks._screen_lock_recover()
        assert r.success
        assert fake.calls == ["status"]

    def test_hardens_after_unlock_when_enabled(self, cli):
        fake = cli({"status": (2, LOCKED, ""),
                    "unlock": (0, {"locked": False, "detail": "unlocked", "method": "cgevent", "attempts": 1}, ""),
                    "harden": (0, {"hardened": True, "changed": 2, "items": []}, "")}, harden=True)
        r = dependency_checks._screen_lock_recover()
        assert r.success
        assert r.diagnostics["harden_changed"] == 2
        assert fake.calls == ["status", "unlock", "harden"]

    def test_harden_failure_names_the_setting(self, cli):
        cli({"status": (0, UNLOCKED, ""),
             "harden": (1, {"hardened": False, "changed": 0, "items": [
                 {"name": "screenlock_require_password", "value": "300", "status": "FAIL", "error": "sysadminctl exited 1"},
                 {"name": "screensaver_idle", "value": None, "status": "OK"}]}, "")}, harden=True)
        r = dependency_checks._screen_lock_recover()
        assert not r.success
        assert "screenlock_require_password" in r.detail
        assert "Lock Screen" in r.detail


def test_registered_in_default_registry():
    class _Mgr:
        _producer = None

        def _send_sms(self, phone, msg):
            pass

    runner = dependency_checks.build_default_registry(_Mgr())
    assert "screen_lock" in runner.names()
