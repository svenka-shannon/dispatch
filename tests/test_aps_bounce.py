"""Gating tests for Manager._aps_bounce_if_stalled (Wi-Fi bounce for stuck inbound iMessage)."""
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from assistant import config
from assistant.manager import Manager

NOW = time.time()


def make_self(last_in_ago, last_out_ago, busy=False, hid_idle=900.0, last_bounce=0.0):
    fake = SimpleNamespace(
        _last_aps_bounce=last_bounce,
        _chat_reader=SimpleNamespace(_executor=ThreadPoolExecutor(1)),
        sessions=SimpleNamespace(sessions={"imessage/_1": SimpleNamespace(is_busy=busy)}),
        _producer=None,
    )
    fake._chatdb_last_in_out = lambda: (NOW - last_in_ago, NOW - last_out_ago)
    fake._hid_idle_seconds = lambda: hid_idle
    return fake


@pytest.fixture
def bounces(monkeypatch):
    calls: list[tuple] = []

    class Proc:
        returncode = 0

        async def communicate(self):
            return b"", b""

        async def wait(self):
            return 0

    async def fake_exec(*argv, **kw):
        calls.append(argv)
        return Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(config, "get", lambda key, default=None: default)
    return calls


def networksetup_calls(calls):
    return [c for c in calls if c[0] == "networksetup"]


@pytest.mark.asyncio
async def test_bounces_when_agent_waiting_and_inbound_quiet(bounces):
    fake = make_self(last_in_ago=900, last_out_ago=600)
    await Manager._aps_bounce_if_stalled(fake)
    assert networksetup_calls(bounces) == [
        ("networksetup", "-setairportpower", "en1", "off"),
        ("networksetup", "-setairportpower", "en1", "on"),
    ]
    assert fake._last_aps_bounce > 0


@pytest.mark.parametrize("kwargs", [
    dict(last_in_ago=900, last_out_ago=950),          # user had the last word
    dict(last_in_ago=120, last_out_ago=60),           # inbound not quiet long enough
    dict(last_in_ago=4000, last_out_ago=3600),        # conversation not active
    dict(last_in_ago=900, last_out_ago=600, busy=True),       # session mid-turn
    dict(last_in_ago=900, last_out_ago=600, hid_idle=30.0),   # someone at the Mac
    dict(last_in_ago=900, last_out_ago=600, hid_idle=None),   # idle unknown → be safe
    dict(last_in_ago=900, last_out_ago=600, last_bounce=NOW - 60),  # cooldown
])
@pytest.mark.asyncio
async def test_does_not_bounce(bounces, kwargs):
    await Manager._aps_bounce_if_stalled(make_self(**kwargs))
    assert networksetup_calls(bounces) == []
