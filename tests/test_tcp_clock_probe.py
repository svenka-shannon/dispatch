"""tcp_clock probe: macOS 49.7-day tcp_now freeze detector (POSTMORTEMS #24-#26)."""
from __future__ import annotations

from unittest import mock

from assistant import dependency_checks as dc
from assistant.dependency_health import ProbeStatus

DAY = 86400


def test_healthy_box_is_ok():
    r = dc._tcp_clock_verdict(1 * DAY, 29)
    assert r.status == ProbeStatus.OK


def test_frozen_signature_is_down_regardless_of_uptime():
    # 2026-09-24 incident: 19,536 TIME_WAIT at ~56d uptime.
    r = dc._tcp_clock_verdict(56 * DAY, 19536)
    assert r.status == ProbeStatus.DOWN
    assert "Reboot" in r.detail
    # Also DOWN if uptime were somehow misread as low — TIME_WAIT is the direct signal.
    assert dc._tcp_clock_verdict(3 * DAY, 1001).status == ProbeStatus.DOWN


def test_threshold_edges():
    assert dc._tcp_clock_verdict(1 * DAY, 1000).status == ProbeStatus.OK
    assert dc._tcp_clock_verdict(44.99 * DAY, 10).status == ProbeStatus.OK
    assert dc._tcp_clock_verdict(45 * DAY, 10).status == ProbeStatus.DEGRADED


def test_degraded_says_time_left_then_past():
    r = dc._tcp_clock_verdict(46 * DAY, 10)
    assert "hits in 3.7d" in r.detail
    r = dc._tcp_clock_verdict(50 * DAY, 10)
    assert "is past" in r.detail


def test_alert_text_is_phone_sized():
    # Alerts go to a human's phone (2026-09-21 lesson: 686-char SMS was the incident).
    for up, tw in [(56 * DAY, 19536), (46 * DAY, 10), (50 * DAY, 10)]:
        assert len(dc._tcp_clock_verdict(up, tw).detail) <= 220


def test_probe_parses_real_formats():
    boot = "{ sec = 1790284064, usec = 572082 } Thu Sep 24 14:07:44 2026\n"
    netstat = (
        "Active Internet connections (including servers)\n"
        "tcp4  0 0 10.0.0.140.53322  162.159.140.220.443  TIME_WAIT  \n"
        "tcp6  0 0 2601::1.53294     2606:4700:7::da.443  TIME_WAIT\n"
        "tcp4  0 0 10.0.0.140.5000   *.*                  LISTEN\n"
    )

    def fake_run(cmd, **kw):
        out = boot if cmd[0] == "sysctl" else netstat
        return mock.Mock(stdout=out, returncode=0)

    with mock.patch.object(dc.subprocess, "run", side_effect=fake_run), \
         mock.patch.object(dc.time, "time", return_value=1790284064 + 2 * DAY):
        r = dc._tcp_clock_probe()
    assert r.status == ProbeStatus.OK
    assert r.diagnostics["time_wait"] == 2
    assert r.diagnostics["uptime_days"] == 2.0


def test_probe_skips_on_unparseable_boottime():
    with mock.patch.object(dc.subprocess, "run",
                           return_value=mock.Mock(stdout="garbage", returncode=0)):
        assert dc._tcp_clock_probe().status == ProbeStatus.SKIP


def test_registered_in_default_registry():
    runner = dc.build_default_registry(mock.MagicMock())
    names = runner.names() if callable(getattr(runner, "names", None)) else runner.names
    assert "tcp_clock" in names
