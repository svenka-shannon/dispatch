"""Ephemeral task idle-cleanup vs. in-flight work, and failed-vs-completed verdicts.

Regression for 2026-10-18 nightly skillify: last ResultMessage (TURN #22) at
23:17:34, then the CLI woke the model on its own after background subagents
finished (task_notification -> init -> tool calls). None of that bumped
last_activity or marked the session busy, so the task supervisor idle-killed
it at 23:19:40 mid-pipeline. It never sent its SMS, yet emitted task.completed.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from assistant import sdk_session as sdk_mod
from assistant.sdk_session import SDKSession


# ─── helpers ────────────────────────────────────────────────────────


def _sys(subtype: str, task_id: str | None = None):
    m = sdk_mod.SystemMessage(subtype)
    m.data = {"task_id": task_id} if task_id else {}
    return m


def _make_session(tmp_path) -> SDKSession:
    cwd = str(tmp_path / "eph")
    os.makedirs(cwd, exist_ok=True)
    return SDKSession(
        chat_id="ephemeral-nightly-skillify",
        contact_name="Task: Nightly skillify",
        tier="admin",
        cwd=cwd,
        session_type="ephemeral",
        source="test",
    )


class _ScriptedClient:
    """Fake client whose receive_messages yields from an asyncio.Queue."""

    def __init__(self):
        self.q: asyncio.Queue = asyncio.Queue()

    async def receive_messages(self):
        while True:
            yield await self.q.get()


async def _feed(session, client, *messages):
    for m in messages:
        await client.q.put(m)
    for _ in range(50):
        await asyncio.sleep(0.01)
        if client.q.empty():
            break
    await asyncio.sleep(0.02)


# ─── (1) SDKSession activity + in-flight tracking ───────────────────


@pytest.mark.asyncio
async def test_every_sdk_message_bumps_last_activity(tmp_path):
    s = _make_session(tmp_path)
    client = _ScriptedClient()
    s._client = client
    task = asyncio.create_task(s._receive_loop())
    try:
        stale = datetime.now() - timedelta(minutes=5)
        s.last_activity = stale
        # Background wake-up: no queue inject, no ResultMessage yet.
        await _feed(s, client, _sys("task_notification", "bg-1"))
        assert s.last_activity > stale + timedelta(minutes=4)

        s.last_activity = stale
        await _feed(s, client, sdk_mod.AssistantMessage([]))
        assert s.last_activity > stale + timedelta(minutes=4)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cli_initiated_turn_counts_as_inflight(tmp_path):
    s = _make_session(tmp_path)
    client = _ScriptedClient()
    s._client = client
    task = asyncio.create_task(s._receive_loop())
    try:
        await _feed(s, client, _sys("init"))
        assert s.is_busy is False          # queue counter can't see it...
        assert s.has_inflight_work is True  # ...but the turn is in progress

        await _feed(s, client, sdk_mod.ResultMessage())
        assert s.has_inflight_work is False
        assert s.last_result_is_error is False
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_background_tasks_keep_session_inflight_across_turn_end(tmp_path):
    s = _make_session(tmp_path)
    client = _ScriptedClient()
    s._client = client
    task = asyncio.create_task(s._receive_loop())
    try:
        await _feed(s, client, _sys("init"), _sys("task_started", "sub-a"),
                    _sys("task_started", "sub-b"), sdk_mod.ResultMessage())
        assert s.active_background_tasks == 2
        assert s.has_inflight_work is True

        await _feed(s, client, _sys("task_notification", "sub-a"))
        assert s.active_background_tasks == 1
        await _feed(s, client, _sys("task_notification", "sub-b"))
        # task_notification arrives before the wake-up turn's init
        await _feed(s, client, _sys("init"), sdk_mod.ResultMessage())
        assert s.active_background_tasks == 0
        assert s.has_inflight_work is False
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_stale_background_task_does_not_pin_busy(tmp_path):
    s = _make_session(tmp_path)
    s._background_tasks["lost"] = time.monotonic() - (s.BACKGROUND_TASK_STALE_SECS + 5)
    assert s.active_background_tasks == 0
    assert s.has_inflight_work is False


def test_error_result_recorded(tmp_path):
    s = _make_session(tmp_path)
    asyncio.run(s._handle_message(sdk_mod.ResultMessage(is_error=True)))
    assert s.last_result_is_error is True


# ─── (2) failed-vs-completed verdict ────────────────────────────────


class _Sess:
    def __init__(self, *, alive=True, inflight=False, err=False, sent=True,
                 idle_secs=300):
        self._alive = alive
        self.has_inflight_work = inflight
        self.active_background_tasks = 1 if inflight else 0
        self.last_result_is_error = err
        self.last_send_at = datetime.now() if sent else None
        self.last_activity = datetime.now() - timedelta(seconds=idle_secs)
        self.is_busy = False
        self._pending_queries = 0
        self._error_count = 0

    def is_alive(self):
        return self._alive


INFO = {"notify": True, "notify_explicit": True, "investigation": False}


class TestFailureReason:
    def _r(self, session, **info):
        from assistant.manager import _ephemeral_task_failure_reason
        return _ephemeral_task_failure_reason(session, {**INFO, **info})

    def test_clean_finish_with_send_is_completed(self):
        assert self._r(_Sess()) is None

    def test_no_notify_keeps_old_behaviour(self):
        assert self._r(None, notify=False) is None
        assert self._r(_Sess(sent=False), notify=False) is None

    def test_investigation_excluded(self):
        assert self._r(None, investigation=True) is None

    def test_vanished_session_fails(self):
        assert "vanished" in self._r(None)

    def test_inflight_work_fails(self):
        assert "in flight" in self._r(_Sess(inflight=True))

    def test_error_turn_fails(self):
        assert "error" in self._r(_Sess(err=True))

    def test_dead_session_fails(self):
        assert "died" in self._r(_Sess(alive=False))

    def test_explicit_notify_without_send_fails(self):
        assert "without sending" in self._r(_Sess(sent=False))

    def test_default_notify_without_send_is_completed(self):
        # notify defaulted (not explicitly true) -> runner's own notice suffices
        assert self._r(_Sess(sent=False), notify_explicit=False) is None


# ─── supervisor integration (real _supervise_ephemeral_tasks) ───────


async def _run_supervisor_once(session, info_overrides=None):
    from assistant.manager import Manager

    m = MagicMock(spec=Manager)
    m.sessions = MagicMock()
    m.sessions.sessions = {"ephemeral-nightly-skillify": session}
    m.sessions.kill_ephemeral_session = AsyncMock(return_value=True)
    m._producer = MagicMock()
    m._shutdown_flag = False
    m._completed_task_traces = {}
    m._handle_investigation_completed = AsyncMock()
    m._notify_task_event = AsyncMock()
    m._ephemeral_tasks = {
        "nightly-skillify": {
            "session_key": "ephemeral-nightly-skillify",
            "started_at": time.time() - 1200,
            "timeout_minutes": 90,
            "requested_by": "+15555550100",
            "title": "Nightly skillify analysis",
            "notify": True,
            "notify_explicit": True,
            "investigation": False,
            **(info_overrides or {}),
        }
    }
    events = []

    def _capture(producer, topic, etype, payload, **kw):
        events.append((etype, payload))

    real = Manager._supervise_ephemeral_tasks.__get__(m, Manager)
    with patch("assistant.manager.produce_event", side_effect=_capture), \
         patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        async def stop(*a):
            m._shutdown_flag = True
        mock_sleep.side_effect = stop
        await real()
    return m, events


@pytest.mark.asyncio
async def test_supervisor_skips_idle_cleanup_while_inflight():
    sess = _Sess(inflight=True, idle_secs=600)
    m, events = await _run_supervisor_once(sess)
    m.sessions.kill_ephemeral_session.assert_not_awaited()
    assert "nightly-skillify" in m._ephemeral_tasks
    assert events == []


@pytest.mark.asyncio
async def test_supervisor_idle_cleanup_without_send_emits_failed():
    sess = _Sess(sent=False, idle_secs=600)
    m, events = await _run_supervisor_once(sess)
    m.sessions.kill_ephemeral_session.assert_awaited_once_with("nightly-skillify")
    types = [e[0] for e in events]
    assert types == ["task.failed"]
    payload = events[0][1]
    assert payload["ended_by"] == "idle_cleanup"
    assert "without sending" in payload["error"]


@pytest.mark.asyncio
async def test_supervisor_idle_cleanup_after_send_emits_completed():
    sess = _Sess(sent=True, idle_secs=600)
    m, events = await _run_supervisor_once(sess)
    assert [e[0] for e in events] == ["task.completed"]


@pytest.mark.asyncio
async def test_supervisor_dead_session_emits_failed():
    sess = _Sess(alive=False)
    m, events = await _run_supervisor_once(sess)
    assert [e[0] for e in events] == ["task.failed"]
    assert events[0][1]["ended_by"] == "session_gone"
