"""Tests for bus migration 004_fast_fts_prune (latency FIX-001).

Migration 004 drops the archive FTS insert triggers and guards the hot FTS
delete triggers so auto-prune (archive + delete) no longer full-scans the FTS
index once per deleted row. These tests check that every delete path still
keeps FTS in sync with hot + archive rows and that search results survive.
"""

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from bus.bus import Bus

migration_004 = importlib.import_module("bus.migrations.versions.004_fast_fts_prune")

OLD_TS = 1000  # far past every retention window


@pytest.fixture
def bus(tmp_path):
    b = Bus(tmp_path / "test-bus.db")
    yield b
    b.close()


def _alembic(bus: Bus, action: str, revision: str):
    """Run an alembic upgrade/downgrade against the bus DB (daemon code path)."""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine

    cfg = Config(str(Path(__file__).parent.parent / "bus" / "alembic.ini"))
    engine = create_engine(f"sqlite:///{bus.db_path}", connect_args={"timeout": 5})
    try:
        with engine.connect() as conn:
            cfg.attributes["connection"] = conn
            getattr(command, action)(cfg, revision)
    finally:
        engine.dispose()


def _triggers(bus: Bus) -> dict[str, str]:
    return dict(bus._conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
    ).fetchall())


def _produce(bus: Bus, topic: str, values: list[dict], timestamp: int | None = OLD_TS):
    p = bus.producer()
    for v in values:
        p.send(topic, value=v, key="k", timestamp=timestamp)
    p.flush()
    p.close()


def _produce_sdk(bus: Bus, payloads: list[str], backdate: bool = True):
    p = bus.producer()
    for text in payloads:
        p.send_sdk_event("sess/one", "chat-1", "tool_use", tool_name="Bash", payload=text)
    p.flush()
    p.close()
    if backdate:
        bus._conn.execute("UPDATE sdk_events SET timestamp = ?", (OLD_TS,))


def _assert_fts_healthy(bus: Bus):
    status = bus.fts_status()
    assert status["records"]["healthy"], status
    assert status["sdk_events"]["healthy"], status


def _fts_rows_for(bus: Bus, topic: str, partition: int, offset: int) -> int:
    return bus._conn.execute(
        "SELECT COUNT(*) FROM records_fts WHERE topic = ? AND partition = ? AND offset_val = ?",
        (topic, partition, offset),
    ).fetchone()[0]


class TestSchema:
    def test_head_is_004(self, bus):
        assert bus._conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "004"

    def test_archive_insert_triggers_dropped(self, bus):
        trig = _triggers(bus)
        assert "records_archive_fts_ai" not in trig
        assert "sdk_events_archive_fts_ai" not in trig
        # Kept: hot inserts and archive deletes (defense in depth)
        for name in ("records_fts_ai", "sdk_events_fts_ai",
                     "records_archive_fts_ad", "sdk_events_archive_fts_ad"):
            assert name in trig

    def test_hot_delete_triggers_guarded(self, bus):
        trig = _triggers(bus)
        assert "WHEN NOT EXISTS" in trig["records_fts_ad"]
        assert "records_archive" in trig["records_fts_ad"]
        assert "WHEN NOT EXISTS" in trig["sdk_events_fts_ad"]
        assert "sdk_events_archive" in trig["sdk_events_fts_ad"]

    def test_sdk_archive_id_index_used(self, bus):
        plan = " ".join(str(r) for r in bus._conn.execute(
            "EXPLAIN QUERY PLAN SELECT 1 FROM sdk_events_archive WHERE id = ?", (1,)
        ).fetchall())
        assert "idx_sdk_archive_id" in plan

    def test_records_guard_uses_primary_key(self, bus):
        plan = " ".join(str(r) for r in bus._conn.execute(
            "EXPLAIN QUERY PLAN SELECT 1 FROM records_archive "
            "WHERE topic = ? AND partition = ? AND offset = ? AND timestamp = ?",
            ("t", 0, 0, 0),
        ).fetchall())
        assert "PRIMARY KEY" in plan


class TestPruneKeepsFtsConsistent:
    def test_prune_archives_and_search_still_finds(self, bus):
        bus.create_topic("t", retention_ms=1)
        _produce(bus, "t", [{"text": f"zebrafish number{i}"} for i in range(5)])
        _assert_fts_healthy(bus)

        assert bus.prune() == 5
        assert bus._conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
        assert bus._conn.execute("SELECT COUNT(*) FROM records_archive").fetchone()[0] == 5
        _assert_fts_healthy(bus)

        results = bus.search("zebrafish", topic="t")
        assert sorted((r.partition, r.offset) for r in results) == [(0, i) for i in range(5)]
        hit = bus.search("number3", topic="t")
        assert [(r.partition, r.offset) for r in hit] == [(0, 3)]
        # Exactly one FTS row per archived record
        for i in range(5):
            assert _fts_rows_for(bus, "t", 0, i) == 1

    def test_background_prune_path(self, bus):
        bus.create_topic("t", retention_ms=1)
        _produce(bus, "t", [{"text": "platypus"} for _ in range(3)])
        _produce_sdk(bus, ["walrus tool output", "walrus again"])

        producer = bus.producer()
        try:
            producer._prune_running = True
            producer._background_prune()
            assert producer._prune_running is False
        finally:
            producer.close()

        assert bus._conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
        assert bus._conn.execute("SELECT COUNT(*) FROM sdk_events").fetchone()[0] == 0
        _assert_fts_healthy(bus)
        assert len(bus.search("platypus", topic="t")) == 3
        assert len(bus.search_sdk("walrus")) == 2

    def test_sdk_events_prune(self, bus):
        _produce_sdk(bus, ["narwhal alpha", "narwhal beta", "narwhal gamma"])
        ids = [r[0] for r in bus._conn.execute("SELECT id FROM sdk_events ORDER BY id")]
        bus.prune()
        assert bus._conn.execute("SELECT COUNT(*) FROM sdk_events").fetchone()[0] == 0
        assert bus._conn.execute("SELECT COUNT(*) FROM sdk_events_archive").fetchone()[0] == 3
        _assert_fts_healthy(bus)
        assert sorted(r.source_id for r in bus.search_sdk("narwhal")) == ids

    def test_recent_rows_untouched(self, bus):
        bus.create_topic("t", retention_ms=60_000)
        _produce(bus, "t", [{"text": "recent okapi"}], timestamp=None)
        _produce_sdk(bus, ["recent okapi sdk"], backdate=False)
        bus.prune()
        _assert_fts_healthy(bus)
        assert len(bus.search("okapi", topic="t")) == 1
        assert len(bus.search_sdk("okapi")) == 1

    def test_non_archive_topic_prune_removes_fts(self, bus):
        bus.create_topic("ephemeral", retention_ms=1, archive=False)
        _produce(bus, "ephemeral", [{"text": "axolotl"} for _ in range(3)])
        bus.prune()
        assert bus._conn.execute("SELECT COUNT(*) FROM records_archive").fetchone()[0] == 0
        _assert_fts_healthy(bus)
        assert bus.search("axolotl") == []

    def test_delete_topic_removes_hot_fts_keeps_archived(self, bus):
        bus.create_topic("t", retention_ms=1)
        _produce(bus, "t", [{"text": "archived quokka"}])
        bus.prune()  # offset 0 -> archive
        _produce(bus, "t", [{"text": "hot quokka"}], timestamp=None)  # offset 1, hot
        _assert_fts_healthy(bus)

        assert bus.delete_topic("t")
        _assert_fts_healthy(bus)
        assert [(r.offset) for r in bus.search("quokka", topic="t")] == [0]

    def test_manual_hot_delete_still_removes_fts(self, bus):
        bus.create_topic("t")
        _produce(bus, "t", [{"text": "pangolin"}], timestamp=None)
        bus._conn.execute("DELETE FROM records WHERE topic = 't'")
        _assert_fts_healthy(bus)
        assert bus.search("pangolin") == []

    def test_offset_collision_with_older_archive_row(self, bus):
        """A hot row whose key already exists in the archive (different row; the
        prune's INSERT OR IGNORE skips it) must not leave an extra FTS row."""
        bus.create_topic("t", retention_ms=1)
        _produce(bus, "t", [{"text": "first"}])
        bus.prune()  # (t, 0, 0) archived
        # Simulate a reused offset: a new, different hot row with the same key.
        bus._conn.execute(
            "INSERT INTO records (topic, partition, offset, timestamp, key, type, source, payload) "
            "VALUES ('t', 0, 0, 2000, 'k', NULL, NULL, '{\"text\": \"second\"}')"
        )
        _assert_fts_healthy(bus)
        bus.prune()
        assert bus._conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
        assert bus._conn.execute("SELECT COUNT(*) FROM records_archive").fetchone()[0] == 1
        _assert_fts_healthy(bus)
        assert _fts_rows_for(bus, "t", 0, 0) == 1


class TestFtsRebuild:
    def test_rebuild_keeps_fast_triggers_and_counts(self, bus):
        bus.create_topic("t", retention_ms=1)
        _produce(bus, "t", [{"text": "lemur"} for _ in range(4)])
        _produce_sdk(bus, ["lemur sdk"])
        bus.prune()
        _produce(bus, "t", [{"text": "lemur hot"}], timestamp=None)

        bus.fts_rebuild()

        trig = _triggers(bus)
        assert "records_archive_fts_ai" not in trig
        assert "sdk_events_archive_fts_ai" not in trig
        assert "WHEN NOT EXISTS" in trig["records_fts_ad"]
        assert "WHEN NOT EXISTS" in trig["sdk_events_fts_ad"]
        _assert_fts_healthy(bus)
        assert len(bus.search("lemur", topic="t")) == 5

        # And prune after rebuild stays consistent
        bus.prune()
        _assert_fts_healthy(bus)


class TestMigrationUpDown:
    def test_upgrade_from_003_with_existing_data(self, bus):
        _alembic(bus, "downgrade", "003")
        trig = _triggers(bus)
        assert "records_archive_fts_ai" in trig
        assert "WHEN" not in trig["records_fts_ad"]
        assert bus._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'idx_sdk_archive_id'"
        ).fetchone() is None

        # Data pruned under the old (003) triggers
        bus.create_topic("t", retention_ms=1)
        _produce(bus, "t", [{"text": "old-scheme capybara"} for _ in range(3)])
        _produce_sdk(bus, ["old-scheme capybara sdk"])
        bus.prune()
        _assert_fts_healthy(bus)

        _alembic(bus, "upgrade", "head")
        assert bus._conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "004"
        assert "records_archive_fts_ai" not in _triggers(bus)

        # Data pruned under the new triggers
        _produce(bus, "t", [{"text": "new-scheme capybara"} for _ in range(2)])
        _produce_sdk(bus, ["new-scheme capybara sdk"])
        bus.prune()
        _assert_fts_healthy(bus)
        assert len(bus.search("capybara", topic="t")) == 5
        assert len(bus.search_sdk("capybara")) == 2

    def test_downgrade_keeps_fts_consistent(self, bus):
        bus.create_topic("t", retention_ms=1)
        _produce(bus, "t", [{"text": "ibex"} for _ in range(2)])
        bus.prune()
        _alembic(bus, "downgrade", "003")
        _assert_fts_healthy(bus)
        _produce(bus, "t", [{"text": "ibex"}])
        bus.prune()  # old triggers: archive insert + MIN(rowid) hot delete
        _assert_fts_healthy(bus)
        assert len(bus.search("ibex", topic="t")) == 3

    def test_migration_is_idempotent(self, bus):
        migration_004._apply_fast_prune_triggers(bus._conn)
        migration_004._apply_fast_prune_triggers(bus._conn)
        assert "records_archive_fts_ai" not in _triggers(bus)


class TestRunMigrationsWithoutAlembic:
    def test_missing_alembic_is_skipped_quietly(self, tmp_path, monkeypatch, caplog):
        """Standalone uv-script CLIs open the bus without alembic installed:
        Bus() must still work and must not log a 'Migration failed' error."""
        monkeypatch.setitem(sys.modules, "alembic", None)
        monkeypatch.setitem(sys.modules, "alembic.config", None)
        with caplog.at_level("DEBUG", logger="bus"):
            b = Bus(tmp_path / "no-alembic.db")
        try:
            assert not [r for r in caplog.records if r.levelname == "ERROR"]
            b.create_topic("t")
            _produce(b, "t", [{"text": "hello"}], timestamp=None)
            assert b.topic_info("t")["total_records"] == 1
        finally:
            b.close()
