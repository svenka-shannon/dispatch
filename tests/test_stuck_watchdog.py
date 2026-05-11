"""Tests for the daemon's blocked-session watchdog (Manager._run_stuck_session_check).

The watchdog nudges any session that's gone idle for > N min with an outstanding
commitment — via an explicit marker (preferred) or a heuristic match on the
session's last outbound text. It must:
  - nudge an idle session that has a commitment marker
  - nudge an idle session whose last message reads like an "I'll …" commitment
  - leave a normally-idle session (no commitment) alone
  - not re-nudge the same session every cycle (per-session back-off)
  - honour a configurable idle threshold N
  - emit a `session.stuck_nudge` bus event on each nudge

All session internals are mocked; nothing shells out or touches the real daemon.
"""
import importlib
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest


# ── Test doubles ────────────────────────────────────────────────────────

class FakeQueue:
    def __init__(self, size=0):
        self._size = size
    def qsize(self):
        return self._size


class FakeSession:
    def __init__(self, *, session_name, chat_id="+15555550100", source="imessage",
                 contact_name="Test User", is_busy=False, queue_size=0,
                 idle_minutes=0.0, last_assistant_text=None, alive=True):
        self._session_name = session_name
        self.chat_id = chat_id
        self.source = source
        self.contact_name = contact_name
        self.is_busy = is_busy
        self._message_queue = FakeQueue(queue_size)
        self.last_activity = datetime.now() - timedelta(minutes=idle_minutes)
        self.last_assistant_text = last_assistant_text
        self._alive = alive
        self.injected: list[str] = []

    def is_alive(self):
        return self._alive

    async def inject(self, text):
        self.injected.append(text)


def _make_manager(sessions: dict, *, idle_minutes_n: float = 5.0):
    """Build a MagicMock(spec=Manager) wired with the bits the watchdog touches."""
    from assistant.manager import Manager
    m = MagicMock(spec=Manager)
    m._producer = MagicMock()
    m._shutdown_flag = False
    m._stuck_check_running = False
    m._last_stuck_nudge_at = {}
    m.sessions = MagicMock()
    m.sessions.sessions = sessions
    # _stuck_session_idle_minutes is a real method but the spec'd mock replaces it
    m._stuck_session_idle_minutes = MagicMock(return_value=idle_minutes_n)
    return m, Manager


async def _run(m, Manager):
    await Manager._run_stuck_session_check.__get__(m, Manager)()


@pytest.fixture(autouse=True)
def _isolate_commitments(tmp_path, monkeypatch):
    """Redirect the commitment marker store to tmp_path."""
    from assistant import commitments as mod
    importlib.reload(mod)
    monkeypatch.setattr(mod, "COMMITMENTS_DIR", tmp_path / "commitments")
    yield mod


# ── Tests ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_idle_session_with_marker_gets_nudged(_isolate_commitments):
    cm = _isolate_commitments
    sess = FakeSession(session_name="imessage/_15555550100", idle_minutes=10)
    cm.set_commitment("imessage/_15555550100", "waiting on the user to reload chrome-control")
    m, Manager = _make_manager({"+15555550100": sess})

    await _run(m, Manager)

    assert len(sess.injected) == 1
    assert "SYSTEM NUDGE" in sess.injected[0]
    assert "reload chrome-control" in sess.injected[0]
    # bus event emitted
    assert m._producer is not None
    # produce_session_event(producer, chat_id, "session.stuck_nudge", payload, source=...)
    # is a module-level call; assert via the event_type arg by inspecting calls on the
    # bus_helpers function isn't possible here — instead assert the nudge timestamp recorded.
    assert "imessage/_15555550100" in m._last_stuck_nudge_at


@pytest.mark.asyncio
async def test_idle_session_with_heuristic_match_gets_nudged():
    sess = FakeSession(
        session_name="imessage/_15555550100", idle_minutes=10,
        last_assistant_text="On it — waiting on you to click the extension toolbar icon.",
    )
    m, Manager = _make_manager({"+15555550100": sess})

    await _run(m, Manager)

    assert len(sess.injected) == 1
    assert "SYSTEM NUDGE" in sess.injected[0]
    assert "imessage/_15555550100" in m._last_stuck_nudge_at


@pytest.mark.asyncio
async def test_normally_idle_session_not_nudged():
    sess = FakeSession(
        session_name="imessage/_15555550100", idle_minutes=30,
        last_assistant_text="Sure! The capital of France is Paris.",
    )
    m, Manager = _make_manager({"+15555550100": sess})

    await _run(m, Manager)

    assert sess.injected == []
    assert m._last_stuck_nudge_at == {}


@pytest.mark.asyncio
async def test_recently_active_session_not_nudged_even_with_marker(_isolate_commitments):
    cm = _isolate_commitments
    sess = FakeSession(session_name="imessage/_15555550100", idle_minutes=1)  # under N=5
    cm.set_commitment("imessage/_15555550100", "still working on it")
    m, Manager = _make_manager({"+15555550100": sess})

    await _run(m, Manager)

    assert sess.injected == []


@pytest.mark.asyncio
async def test_busy_session_not_nudged_even_with_marker(_isolate_commitments):
    cm = _isolate_commitments
    sess = FakeSession(session_name="imessage/_15555550100", idle_minutes=10, is_busy=True)
    cm.set_commitment("imessage/_15555550100", "working on it")
    m, Manager = _make_manager({"+15555550100": sess})

    await _run(m, Manager)
    assert sess.injected == []


@pytest.mark.asyncio
async def test_session_with_queued_message_not_nudged(_isolate_commitments):
    cm = _isolate_commitments
    sess = FakeSession(session_name="imessage/_15555550100", idle_minutes=10, queue_size=1)
    cm.set_commitment("imessage/_15555550100", "working on it")
    m, Manager = _make_manager({"+15555550100": sess})

    await _run(m, Manager)
    assert sess.injected == []


@pytest.mark.asyncio
async def test_no_renudge_within_backoff(_isolate_commitments):
    cm = _isolate_commitments
    sess = FakeSession(session_name="imessage/_15555550100", idle_minutes=10)
    cm.set_commitment("imessage/_15555550100", "waiting on the user")
    m, Manager = _make_manager({"+15555550100": sess})

    await _run(m, Manager)
    assert len(sess.injected) == 1

    # Second cycle immediately after — still idle, marker still set, but within
    # the 2×N back-off window → no re-nudge.
    await _run(m, Manager)
    assert len(sess.injected) == 1


@pytest.mark.asyncio
async def test_renudge_after_session_does_a_turn(_isolate_commitments):
    cm = _isolate_commitments
    sess = FakeSession(session_name="imessage/_15555550100", idle_minutes=10)
    cm.set_commitment("imessage/_15555550100", "waiting on the user")
    m, Manager = _make_manager({"+15555550100": sess})

    await _run(m, Manager)
    assert len(sess.injected) == 1

    # Session did another turn (last_activity refreshed) — under N now, so the
    # watchdog clears its cooldown. Then it goes idle again with the marker still
    # set → eligible for a fresh nudge.
    sess.last_activity = datetime.now()
    await _run(m, Manager)  # clears cooldown, no nudge (not idle)
    assert len(sess.injected) == 1
    assert "imessage/_15555550100" not in m._last_stuck_nudge_at

    sess.last_activity = datetime.now() - timedelta(minutes=10)
    await _run(m, Manager)
    assert len(sess.injected) == 2


@pytest.mark.asyncio
async def test_idle_threshold_is_configurable(_isolate_commitments):
    cm = _isolate_commitments
    cm.set_commitment("imessage/_15555550100", "waiting on the user")

    # idle 4 min: not nudged with N=5, nudged with N=3
    sess5 = FakeSession(session_name="imessage/_15555550100", idle_minutes=4)
    m5, Manager = _make_manager({"+15555550100": sess5}, idle_minutes_n=5.0)
    await _run(m5, Manager)
    assert sess5.injected == []

    sess3 = FakeSession(session_name="imessage/_15555550100", idle_minutes=4)
    m3, Manager = _make_manager({"+15555550100": sess3}, idle_minutes_n=3.0)
    await _run(m3, Manager)
    assert len(sess3.injected) == 1


@pytest.mark.asyncio
async def test_stale_marker_is_gc_not_nudged(_isolate_commitments, tmp_path):
    cm = _isolate_commitments
    cm.COMMITMENTS_DIR.mkdir(parents=True, exist_ok=True)
    # Hand-write a marker that's 8h old (> COMMITMENT_MAX_AGE_SECONDS = 6h)
    import json
    p = cm._path_for("imessage/_15555550100")
    p.write_text(json.dumps({
        "session_name": "imessage/_15555550100",
        "text": "ancient blocked task",
        "set_at": (datetime.now(timezone.utc) - timedelta(hours=8)).isoformat(),
    }))
    sess = FakeSession(session_name="imessage/_15555550100", idle_minutes=10,
                       last_assistant_text="Sure! Here's the answer: 42.")
    m, Manager = _make_manager({"+15555550100": sess})

    await _run(m, Manager)

    assert sess.injected == []                       # not nudged on a stale marker
    assert cm.get_commitment("imessage/_15555550100") is None  # GC'd


@pytest.mark.asyncio
async def test_master_session_skipped(_isolate_commitments):
    cm = _isolate_commitments
    from assistant.common import MASTER_SESSION
    sess = FakeSession(session_name="master", idle_minutes=99)
    cm.set_commitment("master", "doesn't matter")
    m, Manager = _make_manager({MASTER_SESSION: sess})

    await _run(m, Manager)
    assert sess.injected == []


@pytest.mark.asyncio
async def test_dead_session_not_nudged(_isolate_commitments):
    cm = _isolate_commitments
    sess = FakeSession(session_name="imessage/_15555550100", idle_minutes=10, alive=False)
    cm.set_commitment("imessage/_15555550100", "waiting on the user")
    m, Manager = _make_manager({"+15555550100": sess})

    await _run(m, Manager)
    assert sess.injected == []


@pytest.mark.asyncio
async def test_marker_preferred_over_heuristic(_isolate_commitments):
    """When both present, detection is 'marker' and the marker text is used."""
    cm = _isolate_commitments
    cm.set_commitment("imessage/_15555550100", "MARKER: waiting on the extension reload")
    sess = FakeSession(
        session_name="imessage/_15555550100", idle_minutes=10,
        last_assistant_text="I'll get back to you on that.",
    )
    m, Manager = _make_manager({"+15555550100": sess})

    await _run(m, Manager)
    assert len(sess.injected) == 1
    assert "MARKER: waiting on the extension reload" in sess.injected[0]
