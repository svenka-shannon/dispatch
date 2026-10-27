"""Burst guard resets on every inbound message.

2026-10-20: the guard counted every send in a 15-min window, so in a live
back-and-forth the answer to a NEW question was blocked by replies to the
previous one (7 --burst overrides in one day). Now only sends made AFTER the
newest `message.received` for the chat count; the window is an upper bound.
"""
import importlib.machinery
import importlib.util
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

REPLY = Path(os.environ.get(
    "REPLY_CLI_PATH",
    Path.home() / ".claude/skills/sms-assistant/scripts/reply"))

CHAT = "+15550001111"


def _load():
    loader = importlib.machinery.SourceFileLoader("reply_burst_mod", str(REPLY))
    spec = importlib.util.spec_from_loader("reply_burst_mod", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _make_bus(path: Path) -> None:
    conn = sqlite3.connect(path)
    for table, extra in (("records", ""), ("records_archive", ", archived_at INTEGER")):
        conn.execute(
            f"CREATE TABLE {table} (topic TEXT NOT NULL, partition INTEGER NOT NULL, "
            f"offset INTEGER NOT NULL, timestamp INTEGER NOT NULL, key TEXT, type TEXT, "
            f"source TEXT, payload TEXT NOT NULL, headers TEXT{extra}, "
            f"PRIMARY KEY (topic, partition, offset))"
        )
    conn.commit()
    conn.close()


_offset = [0]


def _inbound(path: Path, ts_sec: float, chat_id: str = CHAT,
             table: str = "records", type_: str = "message.received") -> None:
    _offset[0] += 1
    conn = sqlite3.connect(path)
    cols = "topic, partition, offset, timestamp, key, type, source, payload"
    vals = ["messages", 0, _offset[0], int(ts_sec * 1000), f"imessage/{chat_id}",
            type_, "imessage", json.dumps({"chat_id": chat_id, "text": "hi"})]
    if table == "records_archive":
        cols += ", archived_at"
        vals.append(int(time.time() * 1000))
    conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({','.join('?' * len(vals))})", vals)
    conn.commit()
    conn.close()


@pytest.fixture
def rp(tmp_path, monkeypatch):
    mod = _load()
    state = tmp_path / "state"
    monkeypatch.setattr(mod, "BURST_STATE_DIR", state)
    bus = tmp_path / "bus.db"
    _make_bus(bus)
    monkeypatch.setattr(mod, "BUS_DB_PATH", bus)
    mod._test_bus = bus
    mod._test_state = state
    return mod


def _stamps(rp, session, stamps):
    rp._test_state.mkdir(parents=True, exist_ok=True)
    (rp._test_state / f"{session}.json").write_text(json.dumps(stamps))


def _read_stamps(rp, session):
    return json.loads((rp._test_state / f"{session}.json").read_text())


class TestCounting:
    def test_only_sends_after_inbound_count(self, rp):
        now = 10_000.0
        stamps = [now - 300, now - 200, now - 100, now - 10]
        assert rp.count_burst_sends(stamps, now, int((now - 50) * 1000)) == 1

    def test_window_still_caps_without_inbound(self, rp):
        now = 10_000.0
        stamps = [now - 2000, now - 1000, now - 100]
        assert rp.count_burst_sends(stamps, now, 0) == 1

    def test_last_inbound_reads_both_tables_and_filters_chat(self, rp):
        now = time.time()
        _inbound(rp._test_bus, now - 400, table="records_archive")
        _inbound(rp._test_bus, now - 600)
        _inbound(rp._test_bus, now - 5, chat_id="+19998887777")  # other chat
        _inbound(rp._test_bus, now - 3, type_="message.sent")    # not inbound
        got = rp.last_inbound_ms([CHAT], int((now - 900) * 1000))
        assert got == int((now - 400) * 1000)

    def test_spawn_session_matches_parent_chat(self, rp):
        ids = rp.burst_chat_ids(f"{CHAT}-spawn-6c2279fd")
        assert CHAT in ids


class TestGuard:
    S = "imessage___15550001111"

    def test_blocks_when_no_new_inbound(self, rp):
        now = time.time()
        _inbound(rp._test_bus, now - 800)
        _stamps(rp, self.S, [now - 300, now - 200, now - 100])
        with pytest.raises(SystemExit) as e:
            rp.check_burst(self.S, False, chat_id=CHAT)
        assert e.value.code == 3

    def test_new_inbound_resets_the_count(self, rp):
        now = time.time()
        _stamps(rp, self.S, [now - 300, now - 200, now - 100])
        _inbound(rp._test_bus, now - 50)  # owner asks a NEW question
        rp.check_burst(self.S, False, chat_id=CHAT)  # must not raise
        assert len(_read_stamps(rp, self.S)) == 4

    def test_fails_open_when_bus_unreadable(self, rp, monkeypatch):
        now = time.time()
        monkeypatch.setattr(rp, "BUS_DB_PATH", rp._test_state / "missing" / "bus.db")
        _stamps(rp, self.S, [now - 300, now - 200, now - 100])
        rp.check_burst(self.S, False, chat_id=CHAT)  # must not raise

    def test_burst_override_not_counted_and_reason_logged(self, rp):
        now = time.time()
        _stamps(rp, self.S, [now - 300, now - 200, now - 100])
        rp.check_burst(self.S, True, chat_id=CHAT, why="owner asked for 3 links", message="x")
        assert len(_read_stamps(rp, self.S)) == 3
        log = (rp._test_state / f"{self.S}.burst-why.jsonl").read_text().splitlines()
        assert json.loads(log[-1])["why"] == "owner asked for 3 links"
