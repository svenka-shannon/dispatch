"""Unit tests for assistant.commitments — marker store + commitment heuristic.

Pure I/O against a tmp STATE_DIR for the marker store; pure-function tests for
looks_like_commitment().
"""
import importlib
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def commitments_mod(tmp_path, monkeypatch):
    """Import assistant.commitments with COMMITMENTS_DIR redirected to tmp_path."""
    from assistant import commitments as mod
    importlib.reload(mod)
    monkeypatch.setattr(mod, "COMMITMENTS_DIR", tmp_path / "commitments")
    return mod


# ── Marker store ────────────────────────────────────────────────────────

def test_set_get_clear_roundtrip(commitments_mod):
    m = commitments_mod
    assert m.get_commitment("imessage/_15555550100") is None
    m.set_commitment("imessage/_15555550100", "waiting on the user to reload the extension")
    c = m.get_commitment("imessage/_15555550100")
    assert c is not None
    assert c["text"] == "waiting on the user to reload the extension"
    assert c["session_name"] == "imessage/_15555550100"
    assert "set_at" in c
    assert m.clear_commitment("imessage/_15555550100") is True
    assert m.get_commitment("imessage/_15555550100") is None
    assert m.clear_commitment("imessage/_15555550100") is False  # already gone


def test_set_overwrites(commitments_mod):
    m = commitments_mod
    m.set_commitment("test/x", "first")
    m.set_commitment("test/x", "second")
    assert m.get_commitment("test/x")["text"] == "second"


def test_safe_name_handles_slashes_and_plus(commitments_mod):
    m = commitments_mod
    # session names contain "/" and chat_ids contain "+" — must not blow up the path
    m.set_commitment("signal/+15555550100", "blocked on signal-cli restart")
    c = m.get_commitment("signal/+15555550100")
    assert c is not None and c["text"] == "blocked on signal-cli restart"


def test_get_commitment_ignores_garbage_file(commitments_mod, tmp_path):
    m = commitments_mod
    m.COMMITMENTS_DIR.mkdir(parents=True, exist_ok=True)
    p = m._path_for("test/y")
    p.write_text("{not json")
    assert m.get_commitment("test/y") is None
    # empty/text-less payload also ignored
    p.write_text('{"session_name": "test/y"}')
    assert m.get_commitment("test/y") is None


def test_commitment_age_seconds(commitments_mod):
    m = commitments_mod
    fresh = {"set_at": datetime.now(timezone.utc).isoformat()}
    age = m.commitment_age_seconds(fresh)
    assert age is not None and 0 <= age < 5
    old = {"set_at": (datetime.now(timezone.utc) - timedelta(hours=8)).isoformat()}
    assert m.commitment_age_seconds(old) > 7 * 3600
    assert m.commitment_age_seconds({"set_at": "not-a-date"}) is None
    assert m.commitment_age_seconds({}) is None


# ── Heuristic: looks_like_commitment ────────────────────────────────────

@pytest.mark.parametrize("text", [
    "On it! Trying to reset chrome-control now...",
    "I'll get that fixed and report back.",
    "I will check the deploy and let you know.",
    "Working on it — found the files, processing now.",
    "Waiting on you to click the extension toolbar icon.",
    "Once you reload chrome at chrome://extensions I'll continue.",
    "Hang tight, re-running the build.",
    "Give me a sec, retrying the blocked step.",
    "Let me know when you've reloaded it and I'll pick it up.",
    "Stand by while I retry that.",
])
def test_heuristic_matches_commitments(text):
    from assistant.commitments import looks_like_commitment
    snippet = looks_like_commitment(text)
    assert snippet, f"expected a commitment match for: {text!r}"
    assert isinstance(snippet, str) and snippet


@pytest.mark.parametrize("text", [
    None,
    "",
    "ok",
    "Done! Here's what I found: the file was empty.",
    "All sorted — the build passed.",
    "I couldn't reproduce that, seems fine now.",
    "Escalated to admin: native_host PID 11522 spinning; need an extension reload.",  # already escalated
    "Sure, here's the answer: 42.",
    "That's a great question — the capital of France is Paris.",
    "Thanks! Let me know if you need anything else.",  # closing pleasantry, not a commitment
])
def test_heuristic_rejects_non_commitments(text):
    from assistant.commitments import looks_like_commitment
    assert looks_like_commitment(text) is None, f"false positive for: {text!r}"


def test_heuristic_scans_only_the_tail():
    """A commitment phrase buried deep in a huge body is ignored (conservative)."""
    from assistant.commitments import looks_like_commitment
    body = ("I'll get back to you. " + "lorem ipsum " * 200
            + "here is the final answer with no promise in it.")
    assert looks_like_commitment(body) is None
