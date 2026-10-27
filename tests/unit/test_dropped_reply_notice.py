r"""An errored turn must not silently send the user nothing.

Measured 2026-09-21 across the full bus history for this machine:
    errored turns: 49 sent no outbound message,  9 replied
    clean   turns: 17 sent no outbound message, 88 replied
~84% of errored turns ended in total silence, nothing retried, and the silence
only ended when the human re-pinged -- 23,339s (6.5h) of dead air. That was the
largest single component of the "why are you unresponsive" complaint.
"""

import time
import types
import pytest

from assistant.sdk_session import SDKSession


def _session():
    """A bare SDKSession-like object with only what the helper touches."""
    s = object.__new__(SDKSession)
    s.chat_id = "+15551234567"
    s.source = "imessage"
    s._last_dropped_reply_notice_ts = 0.0
    s._log = types.SimpleNamespace(
        warning=lambda *a, **k: None,
        debug=lambda *a, **k: None,
        info=lambda *a, **k: None,
    )
    return s


def _result(duration_ms=600000):
    return types.SimpleNamespace(duration_ms=duration_ms, is_error=True)


@pytest.mark.asyncio
async def test_debounce_blocks_a_second_notice_within_10_min(monkeypatch):
    s = _session()
    s._last_dropped_reply_notice_ts = time.time() - 60  # 1 min ago
    sent = []
    async def _fail(*a, **k):
        sent.append(a)
        raise AssertionError("should not have attempted a send")
    monkeypatch.setattr("asyncio.create_subprocess_exec", _fail)
    await s._notify_if_reply_dropped(_result())
    assert sent == []


# 2026-09-24: `test_non_phone_chat_ids_are_skipped` used to live here and
# asserted that group chats get NO notice. That was the bug, not the contract.
# A second machine running without the guard sent its sep-17 notices to three
# group chats; groups are where a dead turn goes unnoticed longest, because
# everyone assumes someone else saw it. Groups now get notices, via the chat
# join below.


@pytest.mark.asyncio
async def test_group_chat_uses_the_chat_join_not_the_handle_join(monkeypatch):
    """A hex group id can never equal a handle id.

    Deleting the 1:1 guard without switching the query would have made every
    errored group turn report "nothing was sent" even right after a reply.
    Verified against live chat.db: the handle-join finds nothing for a group
    the session had replied to minutes earlier.
    """
    s = _session()
    s.chat_id = "b3d258b9a4de447ca412eb335c82a077"
    seen = {}

    class _Conn:
        def execute(self, sql, params=None):
            if "chat_identifier" in sql:
                seen["sql"] = "chat_join"
                seen["param"] = params[0]
            elif "handle" in sql:
                seen["sql"] = "handle_join"
            return types.SimpleNamespace(fetchone=lambda: (1,))
        def close(self):
            pass

    monkeypatch.setattr("sqlite3.connect", lambda *a, **k: _Conn())

    async def _fail(*a, **k):
        raise AssertionError("a reply WAS found -- must not notify")
    monkeypatch.setattr("asyncio.create_subprocess_exec", _fail)

    await s._notify_if_reply_dropped(_result())
    assert seen["sql"] == "chat_join", "groups must use the chat join"
    assert seen["param"] == "b3d258b9a4de447ca412eb335c82a077"


@pytest.mark.asyncio
async def test_group_with_no_reply_does_get_a_notice(monkeypatch):
    """The whole point: a dead turn in a group must not stay silent."""
    s = _session()
    s.chat_id = "b3d258b9a4de447ca412eb335c82a077"

    class _Conn:
        def execute(self, sql, params=None):
            return types.SimpleNamespace(fetchone=lambda: None)  # no reply sent
        def close(self):
            pass

    monkeypatch.setattr("sqlite3.connect", lambda *a, **k: _Conn())
    sent = []

    class _Proc:
        returncode = 0
        async def wait(self):
            return 0

    async def _spawn(*a, **k):
        sent.append(a)
        return _Proc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", _spawn)
    await s._notify_if_reply_dropped(_result())
    assert len(sent) == 1, "group with no reply must get a notice"
    assert sent[0][1] == "b3d258b9a4de447ca412eb335c82a077"


@pytest.mark.asyncio
async def test_non_imessage_source_is_skipped(monkeypatch):
    """chat.db is iMessage ground truth only.

    A Signal 1:1 chat_id also starts with "+". Without a source guard the
    lookup finds no row (Signal messages live in signal-cli, not chat.db),
    concludes the reply was dropped, and fires a false notice -- delivered
    over iMessage to a Signal contact.
    """
    s = _session()
    s.source = "signal"
    async def _fail(*a, **k):
        raise AssertionError("must not notify for non-iMessage sources")
    monkeypatch.setattr("asyncio.create_subprocess_exec", _fail)
    await s._notify_if_reply_dropped(_result())


@pytest.mark.asyncio
async def test_no_notice_when_a_reply_did_go_out(monkeypatch):
    """If chat.db shows an outbound inside the turn window, stay quiet."""
    s = _session()
    import sqlite3

    class _Conn:
        def execute(self, *a, **k):
            return types.SimpleNamespace(fetchone=lambda: (1,))
        def close(self): pass

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: _Conn())
    monkeypatch.setattr("pathlib.Path.exists", lambda self: True)
    async def _fail(*a, **k):
        raise AssertionError("replied during the turn; must not notify")
    monkeypatch.setattr("asyncio.create_subprocess_exec", _fail)
    await s._notify_if_reply_dropped(_result())


@pytest.mark.asyncio
async def test_notifies_when_the_turn_errored_and_nothing_was_sent(monkeypatch):
    s = _session()
    import sqlite3

    class _Conn:
        def execute(self, *a, **k):
            return types.SimpleNamespace(fetchone=lambda: None)  # no outbound
        def close(self): pass

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: _Conn())
    monkeypatch.setattr("pathlib.Path.exists", lambda self: True)

    calls = {}
    class _Proc:
        returncode = 0
        async def wait(self): return 0
    async def _spawn(*a, **k):
        calls["argv"] = a
        return _Proc()
    monkeypatch.setattr("asyncio.create_subprocess_exec", _spawn)

    await s._notify_if_reply_dropped(_result(duration_ms=600000))

    assert "argv" in calls, "an errored, reply-less turn must notify the user"
    assert calls["argv"][1] == "+15551234567"
    body = calls["argv"][2]
    assert "10 min" in body            # duration surfaced
    assert "resend" in body.lower()    # actionable
    assert s._last_dropped_reply_notice_ts > 0  # debounce armed
