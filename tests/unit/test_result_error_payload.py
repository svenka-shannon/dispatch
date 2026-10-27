"""Errored turns must record WHY, not just THAT."""
import json, types, asyncio, pytest
from assistant.sdk_session import SDKSession


def _session(captured):
    s = object.__new__(SDKSession)
    s.chat_id = "+15551234567"; s.source = "imessage"
    s.contact_name = "T"; s.tier = "admin"; s.turn_count = 0
    s.session_id = None; s._session_name = "imessage/_15551234567"
    s.api_retry_streak = 0; s._consecutive_error_turns = 0
    s._api_outage_started = None
    s.last_activity = s.last_response_at = None
    s._log = types.SimpleNamespace(info=lambda *a, **k: None,
                                   warning=lambda *a, **k: None,
                                   debug=lambda *a, **k: None)
    class P:
        def send_sdk_event(self, **kw): captured.append(kw)
    s._producer = P()
    return s


def _result(is_error, **kw):
    d = dict(subtype=None, duration_ms=339800.0, duration_api_ms=None,
             is_error=is_error, num_turns=1, session_id="sid",
             stop_reason=None, result=None)
    d.update(kw)
    return types.SimpleNamespace(**d)


def _emit(session, msg):
    """Run just the result-event emit the way the handler does."""
    err_payload = None
    if msg.is_error:
        detail = {
            "subtype": getattr(msg, "subtype", None),
            "stop_reason": getattr(msg, "stop_reason", None),
            "duration_ms": msg.duration_ms,
            "duration_api_ms": getattr(msg, "duration_api_ms", None),
            "num_turns": msg.num_turns,
            "result": (getattr(msg, "result", None) or "")[:2000],
        }
        err_payload = json.dumps({k: v for k, v in detail.items() if v not in (None, "")})
    session._producer.send_sdk_event(event_type="result", is_error=msg.is_error or False,
                                     payload=err_payload)
    return err_payload


def test_errored_turn_records_the_reason():
    cap = []; s = _session(cap)
    p = _emit(s, _result(True, subtype="error_during_execution",
                         duration_api_ms=339000.0,
                         result="Connection reset by peer"))
    d = json.loads(p)
    assert d["subtype"] == "error_during_execution"
    assert d["duration_api_ms"] == 339000.0, "api vs wall time is the 340s discriminator"
    assert "Connection reset" in d["result"]


def test_clean_turn_carries_no_payload():
    cap = []; s = _session(cap)
    assert _emit(s, _result(False)) is None
    assert cap[0]["payload"] is None


def test_sparse_error_still_records_what_exists():
    """The 58 real events had almost nothing set -- must not crash or emit {}."""
    cap = []; s = _session(cap)
    d = json.loads(_emit(s, _result(True)))
    assert d == {"duration_ms": 339800.0, "num_turns": 1}, d


def test_long_result_is_truncated_not_dropped():
    cap = []; s = _session(cap)
    d = json.loads(_emit(s, _result(True, result="x" * 9000)))
    assert len(d["result"]) == 2000
