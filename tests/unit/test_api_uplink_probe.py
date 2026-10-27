"""Unit tests for the api_uplink dependency probe (degraded-uplink detector).

The probe uploads a 500KB control and a 2MB test to speed.cloudflare.com/__up:
  - control fails            → SKIP  (total loss / third-party outage — don't page)
  - control OK, 2MB fails    → DEGRADED (the signature failure mode) with the
                               specific fix + measured speed in the detail
  - both OK                  → OK with the large-upload speed in the detail

All subprocess/curl activity is mocked — no real network traffic.
"""
from __future__ import annotations

import asyncio
import subprocess
from unittest.mock import MagicMock

from assistant import dependency_checks
from assistant.dependency_health import (
    DependencyCheck,
    DependencyHealthRunner,
    DepState,
    ProbeStatus,
)


def _curl_result(rc=0, http_code="200", speed_bps=3_000_000.0, stderr="", timed_out=False):
    return {"rc": rc, "http_code": http_code, "speed_bps": speed_bps,
            "stderr": stderr, "timed_out": timed_out}


def _patch_uploads(monkeypatch, small, large=None):
    """Route _run_curl_upload by payload size: small control vs large test."""
    calls: list[tuple[int, int]] = []

    def fake(nbytes, max_time):
        calls.append((nbytes, max_time))
        if nbytes == dependency_checks.UPLINK_SMALL_BYTES:
            return small
        assert nbytes == dependency_checks.UPLINK_LARGE_BYTES
        assert large is not None, "large upload should not have been attempted"
        return large

    monkeypatch.setattr(dependency_checks, "_run_curl_upload", fake)
    return calls


class TestApiUplinkProbe:
    def test_ok_when_both_pass(self, monkeypatch):
        _patch_uploads(
            monkeypatch,
            small=_curl_result(speed_bps=3_072_000.0),
            large=_curl_result(speed_bps=2_048_000.0),
        )
        result = dependency_checks._api_uplink_probe()
        assert result.status == ProbeStatus.OK
        # Detail carries the measured large-upload speed (2048000 B/s = 2000KB/s).
        assert "2000KB/s" in result.detail
        assert result.diagnostics["speed_upload_bps"] == 2_048_000.0
        assert result.diagnostics["small_speed_upload_bps"] == 3_072_000.0

    def test_degraded_when_large_fails(self, monkeypatch):
        # The verified real-world signature: 500KB passes at ~3MB/s, 2MB
        # collapses to ~130KB/s and gets RST (curl rc 56, recv reset).
        _patch_uploads(
            monkeypatch,
            small=_curl_result(speed_bps=3_072_000.0),
            large=_curl_result(rc=56, http_code="000", speed_bps=133_120.0,
                               stderr="curl: (56) Recv failure: Connection reset by peer"),
        )
        result = dependency_checks._api_uplink_probe()
        assert result.status == ProbeStatus.DEGRADED
        # The specific escalation ask is in the detail (default escalator wraps it).
        # 2026-09-21 (later): the escalation text is delivered to a HUMAN as an
        # SMS. An earlier fix today moved the full diagnostic narrative into it
        # ("re-test a NON-Cloudflare host", "Do NOT advise rebooting the
        # router"). That guidance was correct but aimed at the wrong audience:
        # it paged the admin 16x in two days with a 686-char wall of text, and
        # he asked for it to stop. The narrative now lives in code comments and
        # probe diagnostics; the SMS carries only what the human can act on.
        # These assertions preserve the ORIGINAL lesson — never send the
        # operator at the ISP/router/hardware — without the wall of text.
        assert len(result.detail) < 400, "escalation SMS must stay short"
        assert "autosndbufmax" in result.detail, "must name the real mitigation"
        # 2026-09-24: was `assert "Not an ISP or router fault" in result.detail`.
        # The ORIGINAL lesson -- never send the operator to the router -- is
        # preserved below. The CAUSAL claim it was worded as ("verified") was
        # not supported: it came off netstat's retransmit counter, which is
        # dead on macOS 26.2. Assert the guidance, not the unproven cause.
        # 2026-09-24: must not assert a cause, and must not steer the operator
        # away from one either. "No router or ISP action needed" was the same
        # unverified claim with "verified" deleted -- if the path eats the
        # burst, router/ISP action is exactly what's needed.
        assert "verified" not in result.detail.lower(), (
            "must not claim a verified cause -- pcap has not been run"
        )
        assert "not yet isolated" in result.detail.lower(), (
            "must state the cause is open"
        )
        for steer in ("not an isp", "no router", "no isp"):
            assert steer not in result.detail.lower(), (
                f"must not tell the operator where NOT to look: {steer!r}"
            )
        # Ethernet is not an option in this deployment; never advise it.
        assert "thernet" not in result.detail
        assert "reboot" not in result.detail.lower()
        # Measured speeds are in the detail and diagnostics (133120 B/s = 130KB/s).
        assert "130KB/s" in result.detail
        assert result.diagnostics["speed_upload_bps"] == 133_120.0
        assert result.diagnostics["probe_rc"] == 56
        assert result.diagnostics["probe_timed_out"] is False

    def test_degraded_when_large_hits_max_time(self, monkeypatch):
        # curl rc 28 = --max-time cut the stalled transfer.
        _patch_uploads(
            monkeypatch,
            small=_curl_result(),
            large=_curl_result(rc=28, http_code="000", speed_bps=None,
                               stderr="curl: (28) Operation timed out", timed_out=True),
        )
        result = dependency_checks._api_uplink_probe()
        assert result.status == ProbeStatus.DEGRADED
        assert "stalled/RST" in result.detail
        assert result.diagnostics["probe_timed_out"] is True
        assert result.diagnostics["speed_upload_bps"] is None

    def test_degraded_when_large_gets_http_error(self, monkeypatch):
        _patch_uploads(
            monkeypatch,
            small=_curl_result(),
            large=_curl_result(rc=0, http_code="503", speed_bps=100_000.0),
        )
        result = dependency_checks._api_uplink_probe()
        assert result.status == ProbeStatus.DEGRADED

    def test_skip_when_small_fails(self, monkeypatch):
        # Total network loss / Cloudflare down → SKIP, never page the admin,
        # and never attempt the 2MB upload.
        calls = _patch_uploads(
            monkeypatch,
            small=_curl_result(rc=6, http_code="", speed_bps=None,
                               stderr="curl: (6) Could not resolve host"),
            large=None,
        )
        result = dependency_checks._api_uplink_probe()
        assert result.status == ProbeStatus.SKIP
        assert "500KB" in result.detail
        assert "rc=6" in result.detail
        assert len(calls) == 1  # large upload was not attempted

    def test_skip_when_small_wedges_past_subprocess_timeout(self, monkeypatch):
        _patch_uploads(
            monkeypatch,
            small={"rc": None, "http_code": "", "speed_bps": None,
                   "stderr": "curl wedged past --max-time (killed after 30s)",
                   "timed_out": True},
            large=None,
        )
        result = dependency_checks._api_uplink_probe()
        assert result.status == ProbeStatus.SKIP


class TestRunCurlUpload:
    def test_command_shape_and_in_memory_payload(self, monkeypatch):
        seen = {}

        def fake_run(cmd, input=None, capture_output=None, timeout=None):
            seen.update(cmd=cmd, input=input, timeout=timeout)
            p = MagicMock()
            p.returncode = 0
            p.stdout = b"200 3072000.000"
            p.stderr = b""
            return p

        monkeypatch.setattr(dependency_checks.subprocess, "run", fake_run)
        r = dependency_checks._run_curl_upload(500 * 1024, 20)
        assert r == {"rc": 0, "http_code": "200", "speed_bps": 3_072_000.0,
                     "stderr": "", "timed_out": False}
        cmd = seen["cmd"]
        assert cmd[0] == "curl"
        assert dependency_checks.UPLINK_TEST_URL in cmd
        assert "--max-time" in cmd and cmd[cmd.index("--max-time") + 1] == "20"
        # Payload is piped through stdin — no temp files on disk.
        assert "@-" in cmd
        assert seen["input"] == b"\x00" * (500 * 1024)
        assert seen["timeout"] == 30  # max_time + 10 subprocess slack

    def test_subprocess_timeout_maps_to_timed_out(self, monkeypatch):
        def fake_run(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 0))

        monkeypatch.setattr(dependency_checks.subprocess, "run", fake_run)
        r = dependency_checks._run_curl_upload(1024, 20)
        assert r["timed_out"] is True
        assert r["rc"] is None

    def test_unparseable_writeout_yields_no_speed(self, monkeypatch):
        def fake_run(cmd, **kw):
            p = MagicMock()
            p.returncode = 56
            p.stdout = b"000 garbage"
            p.stderr = b"curl: (56) reset"
            return p

        monkeypatch.setattr(dependency_checks.subprocess, "run", fake_run)
        r = dependency_checks._run_curl_upload(1024, 20)
        assert r["rc"] == 56
        assert r["speed_bps"] is None


class TestRegistration:
    def test_registered_probe_only_with_degraded_escalation(self):
        runner = dependency_checks.build_default_registry(MagicMock())
        assert "api_uplink" in runner.names()
        check = runner._checks["api_uplink"].check  # noqa: SLF001 — inspecting the descriptor
        assert check.recover is None, "nothing local can fix an uplink — must be probe-only"
        assert check.recover_on_degraded is True, (
            "DEGRADED must take the actionable path so recover=None escalates immediately"
        )
        assert check.interval_s == 900, "probe costs real bandwidth — keep it infrequent"
        # Probe timeout must cover both sequential curls (20s + 45s --max-time).
        assert check.probe_timeout_s > (
            dependency_checks.UPLINK_SMALL_MAX_TIME + dependency_checks.UPLINK_LARGE_MAX_TIME
        )

    def test_degraded_uplink_escalates_with_specific_fix(self, monkeypatch):
        """End-to-end through the framework: control passes, 2MB chokes →
        DEGRADED → (no recover) → ESCALATED, and the escalation carries the
        specific fix + measured speed."""
        _patch_uploads(
            monkeypatch,
            small=_curl_result(speed_bps=3_072_000.0),
            large=_curl_result(rc=56, http_code="000", speed_bps=133_120.0,
                               stderr="curl: (56) Recv failure: Connection reset by peer"),
        )
        escalations: list[tuple] = []

        async def fake_sleep(_s):
            return None

        async def inline_to_thread(fn, *a, **kw):
            return fn(*a, **kw)

        runner = DependencyHealthRunner(
            producer=None,
            escalate_default=lambda *a: escalations.append(a),
            sleep=fake_sleep, to_thread=inline_to_thread,
        )
        runner.register(DependencyCheck(
            name="api_uplink",
            probe=dependency_checks._api_uplink_probe,
            recover=None,
            recover_on_degraded=True,
            interval_s=900,
        ))
        asyncio.run(runner.run_check("api_uplink"))
        assert runner.state_of("api_uplink") == DepState.ESCALATED
        assert len(escalations) == 1
        name, _state, detail, diag = escalations[0]
        assert name == "api_uplink"
        # See the note in test_degraded_when_large_fails: the escalation SMS is
        # deliberately short now. Same lesson preserved — no ISP/router/ethernet.
        assert len(detail) < 400, "escalation SMS must stay short"
        assert "autosndbufmax" in detail
        assert "thernet" not in detail
        assert "130KB/s" in detail
        assert diag["speed_upload_bps"] == 133_120.0

    def test_skip_never_escalates(self, monkeypatch):
        _patch_uploads(
            monkeypatch,
            small=_curl_result(rc=6, http_code="", speed_bps=None,
                               stderr="curl: (6) Could not resolve host"),
            large=None,
        )
        escalations: list[tuple] = []

        async def fake_sleep(_s):
            return None

        async def inline_to_thread(fn, *a, **kw):
            return fn(*a, **kw)

        runner = DependencyHealthRunner(
            producer=None,
            escalate_default=lambda *a: escalations.append(a),
            sleep=fake_sleep, to_thread=inline_to_thread,
        )
        runner.register(DependencyCheck(
            name="api_uplink",
            probe=dependency_checks._api_uplink_probe,
            recover=None,
            recover_on_degraded=True,
            interval_s=900,
        ))
        asyncio.run(runner.run_check("api_uplink"))
        assert runner.state_of("api_uplink") == DepState.HEALTHY
        assert escalations == []


class TestIntermittentRetry:
    """2026-09-21: the uplink fault is INTERMITTENT, not binary.

    Capping net.inet.tcp.autosndbufmax to 393216 took 2MB uploads from 0/10 to
    10/10, but a residual ~1-in-10 failure remains. Paging on a single failed
    probe sent the admin 16 wall-of-text SMS across two days for a condition
    that is already mitigated and self-clears within seconds. The probe now
    retries before it is allowed to wake anyone.
    """

    def _seq(self, monkeypatch, large_results):
        """Return the small control + a large upload that yields `large_results`
        in order, so we can simulate flap-then-recover."""
        remaining = list(large_results)
        attempts: list[int] = []

        def fake(nbytes, max_time):
            if nbytes == dependency_checks.UPLINK_SMALL_BYTES:
                return _curl_result(speed_bps=3_072_000.0)
            attempts.append(1)
            return remaining.pop(0)

        monkeypatch.setattr(dependency_checks, "_run_curl_upload", fake)
        monkeypatch.setattr(dependency_checks.time, "sleep", lambda _s: None)
        return attempts

    def _fail(self):
        return _curl_result(rc=56, http_code="000", speed_bps=133_120.0,
                            stderr="curl: (56) Recv failure: Connection reset by peer")

    def test_single_blip_does_not_page(self, monkeypatch):
        """One failure then success = OK. This is the 1-in-10 residual."""
        attempts = self._seq(monkeypatch, [self._fail(),
                                           _curl_result(speed_bps=3_000_000.0)])
        result = dependency_checks._api_uplink_probe()
        assert result.status == ProbeStatus.OK, "a single blip must not page"
        assert len(attempts) == 2, "should have retried exactly once"

    def test_two_failures_then_success_does_not_page(self, monkeypatch):
        attempts = self._seq(monkeypatch, [self._fail(), self._fail(),
                                           _curl_result(speed_bps=3_000_000.0)])
        result = dependency_checks._api_uplink_probe()
        assert result.status == ProbeStatus.OK
        assert len(attempts) == 3

    def test_three_consecutive_failures_does_page(self, monkeypatch):
        """A genuinely dead uplink must still escalate — the guard is a delay,
        not a mute."""
        attempts = self._seq(monkeypatch, [self._fail(), self._fail(), self._fail()])
        result = dependency_checks._api_uplink_probe()
        assert result.status == ProbeStatus.DEGRADED
        assert len(attempts) == 1 + dependency_checks.UPLINK_RETRIES
        assert "failed 3x in a row" in result.detail

    def test_clean_pass_makes_no_extra_attempts(self, monkeypatch):
        attempts = self._seq(monkeypatch, [_curl_result(speed_bps=3_000_000.0)])
        result = dependency_checks._api_uplink_probe()
        assert result.status == ProbeStatus.OK
        assert len(attempts) == 1, "must not retry when the first attempt passes"
