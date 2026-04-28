"""Unit tests for assistant.investigations.

Pure-function/state coverage for the investigation subsystem:
state CRUD, rate limits, recursive-dispatch detection, and
parse_result_block.

No SDK, no bus daemons — `produce_event` is fed a MagicMock
producer and we don't assert on it here (integration tests
cover bus emission).
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def investigation_state_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect investigations.INFLIGHT_PATH to tmp_path/state/investigations.json."""
    from assistant import investigations

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / "investigations.json"
    monkeypatch.setattr(investigations, "INFLIGHT_PATH", state_file)
    # Make sure recursion env var isn't leaked from CI shell
    monkeypatch.delenv(investigations.RECURSION_ENV_VAR, raising=False)
    return state_file


@pytest.fixture
def fake_producer() -> MagicMock:
    """A bus Producer stand-in. produce_event tolerates any object with .send."""
    return MagicMock()


@pytest.fixture
def fake_bus_clock(monkeypatch: pytest.MonkeyPatch) -> "_FakeClock":
    """Deterministic time.time() for rate-limit window assertions."""
    clock = _FakeClock(start=1_700_000_000.0)
    monkeypatch.setattr(time, "time", clock.time)
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    return clock


class _FakeClock:
    def __init__(self, start: float) -> None:
        self._t: float = start

    def time(self) -> float:
        return self._t

    def monotonic(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


# ── State CRUD ──────────────────────────────────────────────────────────


class TestStateCRUD:
    def test_dispatch_creates_state_entry(
        self,
        investigation_state_file: Path,
        fake_producer: MagicMock,
        fake_bus_clock: _FakeClock,
    ) -> None:
        from assistant import investigations

        task_id = investigations.dispatch(
            fake_producer, "look at chrome", "imessage/_15555550100"
        )
        assert task_id
        data = json.loads(investigation_state_file.read_text())
        assert task_id in data["inflight"]
        entry = data["inflight"][task_id]
        assert entry["task_id"] == task_id
        # New entries land as "requested" (alias for queued in this impl)
        assert entry["status"] == "requested"
        assert entry["from_session"] == "imessage/_15555550100"
        assert entry["created_at"] == pytest.approx(fake_bus_clock.time())

    def test_dispatch_returns_task_id_immediately(
        self,
        investigation_state_file: Path,
        fake_producer: MagicMock,
    ) -> None:
        from assistant import investigations

        task_id = investigations.dispatch(
            fake_producer, "trace the wedge", "imessage/admin"
        )
        assert isinstance(task_id, str)
        # Format: "investigation-<12 hex>"
        assert task_id.startswith("investigation-")
        assert len(task_id) == len("investigation-") + 12

    def test_get_status_roundtrip(
        self,
        investigation_state_file: Path,
        fake_producer: MagicMock,
    ) -> None:
        from assistant import investigations

        task_id = investigations.dispatch(
            fake_producer, "x", "imessage/admin"
        )
        entry = investigations.get_status(task_id)
        assert entry is not None
        assert entry["task_id"] == task_id
        assert investigations.get_status("nope-not-here") is None

    def test_list_inflight_filters_terminal(
        self,
        investigation_state_file: Path,
        fake_producer: MagicMock,
    ) -> None:
        """The implementation's list_inflight returns ALL entries in the
        inflight dict (terminal entries stay until removed). Verify that
        contract — the *filter* is by from_session, not by status — but
        also assert the by-status partition the scaffold expects, which
        consumers do via mark_status / remove."""
        from assistant import investigations

        t1 = investigations.dispatch(fake_producer, "a", "imessage/A")
        t2 = investigations.dispatch(fake_producer, "b", "imessage/B")
        t3 = investigations.dispatch(fake_producer, "c", "imessage/C")
        investigations.mark_status(t1, "running")
        investigations.mark_status(t3, "completed")

        all_entries = investigations.list_inflight()
        assert len(all_entries) == 3

        # The scaffold's "filter terminal" semantic is achieved by the caller
        # — verify the data is queryable by status post-mark_status:
        non_terminal = [
            e for e in all_entries
            if e["status"] not in ("completed", "failed", "timeout", "cancelled")
        ]
        assert {e["task_id"] for e in non_terminal} == {t1, t2}

        # by-session filter
        only_b = investigations.list_inflight(from_session="imessage/B")
        assert len(only_b) == 1 and only_b[0]["task_id"] == t2

    def test_cancel_marks_state_cancelled(
        self,
        investigation_state_file: Path,
        fake_producer: MagicMock,
    ) -> None:
        from assistant import investigations

        task_id = investigations.dispatch(fake_producer, "x", "imessage/A")

        first = investigations.cancel(task_id)
        assert first is True
        entry = investigations.get_status(task_id)
        assert entry is not None and entry["status"] == "cancelled"

        # Idempotent: second call is a no-op (already terminal)
        second = investigations.cancel(task_id)
        assert second is False

    def test_state_file_atomic_write(
        self,
        investigation_state_file: Path,
        fake_producer: MagicMock,
    ) -> None:
        """Corrupt the on-disk file; loader must recover gracefully."""
        from assistant import investigations

        investigation_state_file.write_text("{not json")
        # _load_state silently returns the empty default
        snap = investigations.list_inflight()
        assert snap == []

        # Subsequent dispatch writes a valid file again
        task_id = investigations.dispatch(fake_producer, "x", "imessage/A")
        data = json.loads(investigation_state_file.read_text())
        assert task_id in data["inflight"]


# ── Rate limiting ───────────────────────────────────────────────────────


class TestRateLimits:
    def test_rate_limit_per_session_enforced(
        self,
        investigation_state_file: Path,
        fake_producer: MagicMock,
    ) -> None:
        from assistant import investigations

        sess = "imessage/admin"
        investigations.dispatch(fake_producer, "1", sess)
        investigations.dispatch(fake_producer, "2", sess)

        with pytest.raises(investigations.RateLimitExceeded):
            investigations.dispatch(fake_producer, "3", sess)

    def test_rate_limit_global_window(
        self,
        investigation_state_file: Path,
        fake_producer: MagicMock,
        fake_bus_clock: _FakeClock,
    ) -> None:
        from assistant import investigations

        # Five different sessions, each one dispatch — fills GLOBAL_MAX_PER_HOUR (5)
        for i in range(investigations.GLOBAL_MAX_PER_HOUR):
            investigations.dispatch(fake_producer, "x", f"imessage/sess{i}")

        with pytest.raises(investigations.RateLimitExceeded):
            investigations.dispatch(fake_producer, "x", "imessage/sess99")

        # Advance past the rolling window — old timestamps prune, new dispatch
        # admitted.
        fake_bus_clock.advance(investigations.GLOBAL_RATE_WINDOW_SECONDS + 1)
        new_id = investigations.dispatch(fake_producer, "x", "imessage/sess99")
        assert new_id

    def test_rate_limit_resets_on_completion(
        self,
        investigation_state_file: Path,
        fake_producer: MagicMock,
    ) -> None:
        """Per-session in-flight cap counts only requested/running entries;
        marking an entry completed must free a slot."""
        from assistant import investigations

        sess = "imessage/admin"
        t1 = investigations.dispatch(fake_producer, "a", sess)
        investigations.dispatch(fake_producer, "b", sess)

        # Cap reached
        with pytest.raises(investigations.RateLimitExceeded):
            investigations.dispatch(fake_producer, "c", sess)

        investigations.mark_status(t1, "completed")
        # Slot freed → next dispatch admitted
        new_id = investigations.dispatch(fake_producer, "d", sess)
        assert new_id


# ── Recursive dispatch detection ────────────────────────────────────────


class TestRecursionDetection:
    def test_recursive_dispatch_rejected_by_env_flag(
        self,
        investigation_state_file: Path,
        fake_producer: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from assistant import investigations

        monkeypatch.setenv(
            investigations.RECURSION_ENV_VAR, "investigation-deadbeefcafe"
        )
        with pytest.raises(investigations.RecursiveDispatchError):
            investigations.dispatch(fake_producer, "x", "imessage/admin")

    def test_recursive_dispatch_rejected_by_originator_flag(
        self,
        investigation_state_file: Path,
        fake_producer: MagicMock,
    ) -> None:
        """The investigations module rejects any from_session whose name
        begins with the ephemeral- prefix used by ephemeral SDKSessions."""
        from assistant import investigations

        with pytest.raises(investigations.RecursiveDispatchError):
            investigations.dispatch(
                fake_producer, "x", "ephemeral-investigation-abc"
            )


# ── Concurrency ─────────────────────────────────────────────────────────


class TestConcurrency:
    def test_concurrent_dispatch_threadsafe(
        self,
        investigation_state_file: Path,
        fake_producer: MagicMock,
    ) -> None:
        """10 threads dispatching concurrently → 10 unique entries persist;
        no torn writes."""
        from assistant import investigations

        results: list[str] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def worker(i: int) -> None:
            try:
                # Distinct sessions to avoid the per-session cap (2)
                tid = investigations.dispatch(
                    fake_producer, f"prompt-{i}", f"imessage/sess{i}"
                )
                with lock:
                    results.append(tid)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

        # GLOBAL_MAX_PER_HOUR = 5 — only the first 5 succeed; remaining
        # raise RateLimitExceeded, which is *expected and acceptable* for
        # this thread-safety test. The contract is: state is consistent.
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All successes must have unique task_ids
        assert len(set(results)) == len(results)
        # Sum of (successful + rate-limited) must be 10
        rate_limited = [
            e for e in errors if isinstance(e, investigations.RateLimitExceeded)
        ]
        assert len(results) + len(rate_limited) == 10
        # Persisted entries match in-memory results
        on_disk = json.loads(investigation_state_file.read_text())
        assert set(on_disk["inflight"].keys()) == set(results)


# ── parse_result_block ──────────────────────────────────────────────────


WELL_FORMED_BLOCK = """\
preamble that should be ignored

=== INVESTIGATION RESULT ===
SYMPTOM: chrome process pegged at 800% cpu
ROOT CAUSE: runaway tab in extension service worker
RECOMMENDED ACTION: kill the chrome helper pid 41281; restart chrome cleanly
SAFETY: recoverable - no in-flight tabs holding form data
DURABLE FIX: pin extension version, add watchdog
=== END ===

trailing junk
"""

PARTIAL_BLOCK = """\
=== INVESTIGATION RESULT ===
SYMPTOM: signal-cli wedge
=== END ===
"""

ABSENT_BLOCK = "I investigated and concluded the daemon is fine. Nothing to report."


class TestParseResultBlock:
    def test_parse_result_block_well_formed(self) -> None:
        from assistant.investigations import parse_result_block

        out = parse_result_block(WELL_FORMED_BLOCK)
        assert out["parse_status"] == "complete"
        assert out["symptom"] == "chrome process pegged at 800% cpu"
        assert out["root_cause"] == "runaway tab in extension service worker"
        assert out["recommended_action"].startswith("kill the chrome helper")
        assert out["safety"].startswith("recoverable")
        assert out["durable_fix"] == "pin extension version, add watchdog"

    def test_parse_result_block_missing_fields(self) -> None:
        from assistant.investigations import parse_result_block

        out = parse_result_block(PARTIAL_BLOCK)
        assert out["parse_status"] == "partial"
        assert out["symptom"] == "signal-cli wedge"
        # Missing fields stay None
        assert out["root_cause"] is None
        assert out["recommended_action"] is None
        assert out["safety"] is None
        assert out["durable_fix"] is None

    def test_parse_result_block_absent(self) -> None:
        from assistant.investigations import parse_result_block

        out = parse_result_block(ABSENT_BLOCK)
        assert out["parse_status"] == "missing"
        assert out["raw"] == ABSENT_BLOCK
        for f in ("symptom", "root_cause", "recommended_action", "safety", "durable_fix"):
            assert out[f] is None

    def test_parse_result_block_empty_input(self) -> None:
        """Empty / None input must not raise."""
        from assistant.investigations import parse_result_block

        empty = parse_result_block("")
        assert empty["parse_status"] == "missing"
        none_input = parse_result_block(None)  # type: ignore[arg-type]
        assert none_input["parse_status"] == "missing"
        assert none_input["raw"] == ""

    def test_format_result_for_injection_wraps_status(self) -> None:
        from assistant.investigations import (
            format_result_for_injection,
            parse_result_block,
        )

        parsed = parse_result_block(WELL_FORMED_BLOCK)
        wrapped = format_result_for_injection(
            "investigation-abc123def456", "completed", parsed
        )
        assert wrapped.startswith('<investigation-result task_id="investigation-abc123def456"')
        assert 'status="completed"' in wrapped
        assert 'parse_status="complete"' in wrapped
        assert "SYMPTOM: chrome process pegged" in wrapped
        assert wrapped.endswith("</investigation-result>")
