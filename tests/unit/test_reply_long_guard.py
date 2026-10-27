"""--long is gated by a token bucket AND a graded justification.

Eric's design, 2026-09-23, after two flat caps failed: at 4/day the budget was
spent within the hour; at 1/day it was just a wall. Making the override cost
effort and submitting it to an independent judge is the part that cannot be
rationalised past.

These exist because the guard was verified BY HAND when it shipped — the same
hand-checking that let two silent bugs through the package watcher the day
before.
"""
import importlib.machinery
import importlib.util
import json
import time
from pathlib import Path

import pytest

REPLY = Path.home() / ".claude/skills/sms-assistant/scripts/reply"


def _load():
    loader = importlib.machinery.SourceFileLoader("reply_mod", str(REPLY))
    spec = importlib.util.spec_from_loader("reply_mod", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def rp(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "BURST_STATE_DIR", tmp_path)
    return mod


class TestTokenBucket:
    def test_refills_one_token_per_two_hours(self, rp, tmp_path):
        p = tmp_path / "b.json"
        p.write_text(json.dumps({"tokens": 0.0,
                                 "last_refill": time.time() - 2 * 3600}))
        assert rp._long_bucket(p)["tokens"] == pytest.approx(1.0, abs=0.01)

    def test_accrues_but_caps_at_four(self, rp, tmp_path):
        p = tmp_path / "b.json"
        p.write_text(json.dumps({"tokens": 0.0,
                                 "last_refill": time.time() - 100 * 3600}))
        assert rp._long_bucket(p)["tokens"] == rp.LONG_MAX_TOKENS

    def test_missing_state_starts_full(self, rp, tmp_path):
        assert rp._long_bucket(tmp_path / "nope.json")["tokens"] == rp.LONG_MAX_TOKENS


class TestGate:
    def _long_path(self, tmp_path):
        return tmp_path / "s.longbucket.json"

    def test_short_message_never_touches_the_gate(self, rp):
        # allow_long=False must return immediately, no bucket, no grader.
        rp.check_long("s", False, "", "hi")

    def test_empty_bucket_blocks(self, rp, tmp_path, monkeypatch):
        self._long_path(tmp_path).write_text(
            json.dumps({"tokens": 0.0, "last_refill": time.time()}))
        monkeypatch.setattr(rp, "_grade_justification",
                            lambda *a: (True, "ok"))
        with pytest.raises(SystemExit) as e:
            rp.check_long("s", True, "word " * 300, "msg")
        assert e.value.code == 4

    def test_short_justification_blocks_before_grading(self, rp, tmp_path, monkeypatch):
        called = []
        monkeypatch.setattr(rp, "_grade_justification",
                            lambda *a: called.append(1) or (True, "ok"))
        with pytest.raises(SystemExit):
            rp.check_long("s", True, "too short", "msg")
        assert not called, "must not spend grader calls on an under-length justification"

    def test_grader_fail_blocks_and_spends_no_token(self, rp, tmp_path, monkeypatch):
        p = self._long_path(tmp_path)
        p.write_text(json.dumps({"tokens": 2.0, "last_refill": time.time()}))
        monkeypatch.setattr(rp, "_grade_justification",
                            lambda *a: (False, "self-narration"))
        with pytest.raises(SystemExit):
            rp.check_long("s", True, "word " * 300, "msg")
        # The whole point: a rejected request must not cost a token, or the
        # cheapest path becomes spamming the grader until it relents.
        assert json.loads(p.read_text())["tokens"] == pytest.approx(2.0, abs=0.01)

    def test_grader_pass_spends_exactly_one(self, rp, tmp_path, monkeypatch):
        p = self._long_path(tmp_path)
        p.write_text(json.dumps({"tokens": 3.0, "last_refill": time.time()}))
        monkeypatch.setattr(rp, "_grade_justification", lambda *a: (True, "fine"))
        rp.check_long("s", True, "word " * 300, "msg")
        assert json.loads(p.read_text())["tokens"] == pytest.approx(2.0, abs=0.01)


class TestFailsClosed:
    def test_unreachable_grader_is_a_refusal_not_a_free_pass(self, rp, monkeypatch):
        def boom(*a, **k):
            raise OSError("claude binary missing")
        monkeypatch.setattr(rp.subprocess, "run", boom)
        ok, reason = rp._grade_justification("w " * 300, "m")
        assert ok is False
        assert "failing closed" in reason

    def test_empty_grader_output_is_a_refusal(self, rp, monkeypatch):
        class R:
            stdout = b""
            stderr = b""
        monkeypatch.setattr(rp.subprocess, "run", lambda *a, **k: R())
        ok, _ = rp._grade_justification("w " * 300, "m")
        assert ok is False

    def test_verdict_parsed_from_first_line(self, rp, monkeypatch):
        class R:
            stdout = b"PASS\nevery line is actionable"
            stderr = b""
        monkeypatch.setattr(rp.subprocess, "run", lambda *a, **k: R())
        ok, reason = rp._grade_justification("w " * 300, "m")
        assert ok is True
        assert "actionable" in reason
