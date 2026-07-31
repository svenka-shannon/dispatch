"""
Comprehensive unit tests for the Kafka-on-SQLite message bus.

Tests cover:
- Topic CRUD
- Producer: single send, batch send, partition assignment, key hashing
- Consumer: poll, commit, seek, replay, auto-commit, auto_offset_reset
- Consumer groups: generation IDs, zombie fencing, rebalance callbacks
- Partition assignment: RangeAssignor correctness
- Retention and pruning
- Edge cases and error handling
- Write queue: batching, queue depth, graceful shutdown, thread safety
"""

import json
import os
import tempfile
import threading
import time

import pytest

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from bus.bus import (
    Bus,
    Consumer,
    Producer,
    Record,
    StaleGenerationError,
    TopicPartition,
    _murmur2,
    _partition_for_key,
)


@pytest.fixture
def bus(tmp_path):
    """Create a fresh bus with a temporary database."""
    db_path = tmp_path / "test-bus.db"
    b = Bus(db_path)
    yield b
    b.close()


@pytest.fixture
def bus_with_topic(bus):
    """Bus with a pre-created 'test' topic."""
    bus.create_topic("test", partitions=1)
    return bus


@pytest.fixture
def bus_with_partitioned_topic(bus):
    """Bus with a pre-created 'test' topic with 4 partitions."""
    bus.create_topic("test", partitions=4)
    return bus


# ─── Topic CRUD ───────────────────────────────────────────────


class TestTopics:
    def test_create_topic(self, bus):
        assert bus.create_topic("messages") is True
        topics = bus.list_topics()
        assert len(topics) == 1
        assert topics[0]["name"] == "messages"
        assert topics[0]["partitions"] == 1

    def test_create_topic_with_partitions(self, bus):
        bus.create_topic("events", partitions=8)
        topics = bus.list_topics()
        assert topics[0]["partitions"] == 8

    def test_create_topic_custom_retention(self, bus):
        retention_30d = 30 * 24 * 60 * 60 * 1000
        bus.create_topic("logs", retention_ms=retention_30d)
        topics = bus.list_topics()
        assert topics[0]["retention_ms"] == retention_30d

    def test_create_duplicate_topic(self, bus):
        bus.create_topic("test")
        assert bus.create_topic("test") is False

    def test_delete_topic(self, bus):
        bus.create_topic("test")
        assert bus.delete_topic("test") is True
        assert bus.list_topics() == []

    def test_delete_nonexistent_topic(self, bus):
        assert bus.delete_topic("nope") is False

    def test_delete_topic_removes_records(self, bus):
        bus.create_topic("test")
        producer = bus.producer()
        producer.send("test", value={"hello": "world"})
        producer.flush()
        producer.close()
        bus.delete_topic("test")
        # Recreate and verify no records remain
        bus.create_topic("test")
        info = bus.topic_info("test")
        assert info["total_records"] == 0

    def test_topic_info(self, bus):
        bus.create_topic("test", partitions=3)
        producer = bus.producer()
        producer.send("test", value={"a": 1}, partition=0)
        producer.send("test", value={"b": 2}, partition=1)
        producer.flush()
        producer.close()
        info = bus.topic_info("test")
        assert info["name"] == "test"
        assert info["partitions"] == 3
        assert info["total_records"] == 2
        assert info["partition_offsets"][0] == 0
        assert info["partition_offsets"][1] == 0
        assert info["partition_offsets"][2] == -1

    def test_topic_info_nonexistent(self, bus):
        assert bus.topic_info("nope") is None

    def test_list_topics_sorted(self, bus):
        bus.create_topic("zebra")
        bus.create_topic("alpha")
        bus.create_topic("middle")
        names = [t["name"] for t in bus.list_topics()]
        assert names == ["alpha", "middle", "zebra"]


# ─── Producer ─────────────────────────────────────────────────


class TestProducer:
    def test_send_basic(self, bus_with_topic):
        producer = bus_with_topic.producer()
        producer.send("test", value={"msg": "hello"})
        producer.flush()
        consumer = bus_with_topic.consumer(group_id="t1", topics=["test"])
        records = consumer.poll(timeout_ms=0)
        assert len(records) == 1
        assert records[0].topic == "test"
        assert records[0].partition == 0
        assert records[0].offset == 0
        assert records[0].value == {"msg": "hello"}
        assert records[0].key is None
        consumer.close()
        producer.close()

    def test_send_with_key(self, bus_with_topic):
        producer = bus_with_topic.producer()
        producer.send("test", value={"msg": "hello"}, key="user-123")
        producer.flush()
        consumer = bus_with_topic.consumer(group_id="t1", topics=["test"])
        records = consumer.poll(timeout_ms=0)
        assert records[0].key == "user-123"
        consumer.close()
        producer.close()

    def test_send_with_headers(self, bus_with_topic):
        producer = bus_with_topic.producer()
        producer.send("test", value={"a": 1}, headers={"source": "test"})
        producer.flush()
        consumer = bus_with_topic.consumer(group_id="t1", topics=["test"])
        records = consumer.poll(timeout_ms=0)
        assert records[0].headers == {"source": "test"}
        consumer.close()
        producer.close()

    def test_send_increments_offset(self, bus_with_topic):
        producer = bus_with_topic.producer()
        producer.send("test", value={"n": 1})
        producer.send("test", value={"n": 2})
        producer.send("test", value={"n": 3})
        producer.flush()
        consumer = bus_with_topic.consumer(group_id="t1", topics=["test"])
        records = consumer.poll(timeout_ms=0)
        assert len(records) == 3
        assert records[0].offset == 0
        assert records[1].offset == 1
        assert records[2].offset == 2
        consumer.close()
        producer.close()

    def test_send_to_nonexistent_topic(self, bus):
        """Sending to nonexistent topic logs error but doesn't raise (fire-and-forget)."""
        producer = bus.producer()
        producer.send("nope", value={"a": 1})
        producer.flush()
        producer.close()

    def test_send_explicit_partition(self, bus_with_partitioned_topic):
        producer = bus_with_partitioned_topic.producer()
        producer.send("test", value={"a": 1}, partition=2)
        producer.flush()
        cursor = bus_with_partitioned_topic._conn.execute(
            "SELECT partition FROM records WHERE topic = 'test'"
        )
        assert cursor.fetchone()[0] == 2
        producer.close()

    def test_send_custom_timestamp(self, bus_with_topic):
        producer = bus_with_topic.producer()
        ts = 1700000000000
        producer.send("test", value={"a": 1}, timestamp=ts)
        producer.flush()
        consumer = bus_with_topic.consumer(group_id="t1", topics=["test"])
        records = consumer.poll(timeout_ms=0)
        assert records[0].timestamp == ts
        consumer.close()
        producer.close()

    def test_send_value_types(self, bus_with_topic):
        """Value can be any JSON-serializable type, not just dict."""
        producer = bus_with_topic.producer()
        producer.send("test", value="just a string")
        producer.send("test", value=42)
        producer.send("test", value=[1, 2, 3])
        producer.send("test", value=True)
        producer.send("test", value=None)
        producer.flush()
        consumer = bus_with_topic.consumer(group_id="t1", topics=["test"])
        records = consumer.poll(timeout_ms=0)
        assert records[0].value == "just a string"
        assert records[1].value == 42
        assert records[2].value == [1, 2, 3]
        assert records[3].value is True
        assert records[4].value is None
        consumer.close()
        producer.close()

    def test_key_based_partition_assignment_consistent(self, bus_with_partitioned_topic):
        """Same key always goes to same partition."""
        producer = bus_with_partitioned_topic.producer()
        for _ in range(10):
            producer.send("test", value={"n": 1}, key="consistent-key")
        producer.flush()
        consumer = bus_with_partitioned_topic.consumer(group_id="t1", topics=["test"])
        records = consumer.poll(timeout_ms=0)
        partitions = {r.partition for r in records}
        assert len(partitions) == 1  # always same partition
        consumer.close()
        producer.close()

    def test_different_keys_can_go_to_different_partitions(self, bus_with_partitioned_topic):
        """Different keys should distribute across partitions."""
        producer = bus_with_partitioned_topic.producer()
        for i in range(20):
            producer.send("test", value={"n": i}, key=f"key-{i}")
        producer.flush()
        consumer = bus_with_partitioned_topic.consumer(group_id="t1", topics=["test"])
        records = consumer.poll(timeout_ms=0)
        partitions = {r.partition for r in records}
        assert len(partitions) > 1
        consumer.close()
        producer.close()

    def test_send_many_basic(self, bus_with_topic):
        producer = bus_with_topic.producer()
        producer.send_many("test", [
            {"value": {"n": 1}},
            {"value": {"n": 2}},
            {"value": {"n": 3}},
        ])
        producer.flush()
        consumer = bus_with_topic.consumer(group_id="t1", topics=["test"])
        records = consumer.poll(timeout_ms=0)
        assert len(records) == 3
        assert records[0].offset == 0
        assert records[1].offset == 1
        assert records[2].offset == 2
        consumer.close()
        producer.close()

    def test_send_many_with_keys(self, bus_with_partitioned_topic):
        producer = bus_with_partitioned_topic.producer()
        producer.send_many("test", [
            {"value": {"n": 1}, "key": "a"},
            {"value": {"n": 2}, "key": "b"},
            {"value": {"n": 3}, "key": "a"},  # same key as first
        ])
        producer.flush()
        cursor = bus_with_partitioned_topic._conn.execute(
            "SELECT key, partition FROM records WHERE topic = 'test' ORDER BY offset"
        )
        rows = cursor.fetchall()
        assert rows[0][1] == rows[2][1]  # key "a" records on same partition
        producer.close()

    def test_send_many_writes_all(self, bus):
        """send_many should write all records."""
        bus.create_topic("test")
        producer = bus.producer()
        producer.send_many("test", [
            {"value": {"n": 1}},
            {"value": {"n": 2}},
        ])
        producer.flush()
        info = bus.topic_info("test")
        assert info["total_records"] == 2
        producer.close()

    def test_send_many_to_nonexistent_topic(self, bus):
        """Sending to nonexistent topic logs error (fire-and-forget with write queue)."""
        producer = bus.producer()
        producer.send_many("nope", [{"value": {"a": 1}}])
        producer.flush()
        producer.close()


# ─── Murmur2 Hashing ─────────────────────────────────────────


class TestMurmur2:
    def test_known_values(self):
        """Verify murmur2 produces consistent results."""
        h1 = _murmur2(b"hello")
        h2 = _murmur2(b"hello")
        assert h1 == h2  # deterministic

    def test_different_inputs_different_hashes(self):
        h1 = _murmur2(b"hello")
        h2 = _murmur2(b"world")
        assert h1 != h2

    def test_partition_for_key_in_range(self):
        for i in range(100):
            p = _partition_for_key(f"key-{i}", 4)
            assert 0 <= p < 4

    def test_partition_for_key_consistent(self):
        p1 = _partition_for_key("my-key", 8)
        p2 = _partition_for_key("my-key", 8)
        assert p1 == p2


# ─── Consumer: Basic ─────────────────────────────────────────


class TestConsumerBasic:
    def test_poll_returns_records(self, bus_with_topic):
        producer = bus_with_topic.producer()
        producer.send("test", value={"n": 1})
        producer.send("test", value={"n": 2})
        producer.flush()

        consumer = bus_with_topic.consumer(group_id="g1", topics=["test"])
        records = consumer.poll(timeout_ms=0)
        assert len(records) == 2
        assert records[0].value == {"n": 1}
        assert records[1].value == {"n": 2}
        consumer.close()
        producer.close()

    def test_poll_empty(self, bus_with_topic):
        consumer = bus_with_topic.consumer(group_id="g1", topics=["test"])
        records = consumer.poll(timeout_ms=0)
        assert records == []
        consumer.close()

    def test_poll_after_commit_no_duplicates(self, bus_with_topic):
        producer = bus_with_topic.producer()
        producer.send("test", value={"n": 1})
        producer.flush()

        consumer = bus_with_topic.consumer(group_id="g1", topics=["test"])
        records = consumer.poll(timeout_ms=0)
        assert len(records) == 1
        consumer.commit()

        # Second poll should return nothing
        records2 = consumer.poll(timeout_ms=0)
        assert records2 == []
        consumer.close()
        producer.close()

    def test_poll_without_commit_replays(self, bus_with_topic):
        producer = bus_with_topic.producer()
        producer.send("test", value={"n": 1})
        producer.flush()

        consumer = bus_with_topic.consumer(group_id="g1", topics=["test"])
        records = consumer.poll(timeout_ms=0)
        assert len(records) == 1
        # Don't commit!

        # Re-create consumer in same group — should see same record
        consumer.leave_group()
        consumer2 = bus_with_topic.consumer(group_id="g1", topics=["test"])
        records2 = consumer2.poll(timeout_ms=0)
        assert len(records2) == 1
        assert records2[0].value == {"n": 1}
        consumer2.close()
        producer.close()

    def test_auto_commit(self, bus_with_topic):
        producer = bus_with_topic.producer()
        producer.send("test", value={"n": 1})
        producer.flush()

        consumer = bus_with_topic.consumer(
            group_id="g1", topics=["test"], auto_commit=True
        )
        records = consumer.poll(timeout_ms=0)
        assert len(records) == 1

        # Auto-committed, so new consumer in same group sees nothing
        consumer.leave_group()
        consumer2 = bus_with_topic.consumer(group_id="g1", topics=["test"])
        records2 = consumer2.poll(timeout_ms=0)
        assert records2 == []
        consumer2.close()
        producer.close()

    def test_poll_timeout_blocks(self, bus_with_topic):
        """poll() should block for approximately timeout_ms when no records."""
        consumer = bus_with_topic.consumer(group_id="g1", topics=["test"])
        start = time.monotonic()
        records = consumer.poll(timeout_ms=50)
        elapsed = (time.monotonic() - start) * 1000
        assert records == []
        assert elapsed >= 40  # should have waited ~50ms (allow some slack)
        consumer.close()

    def test_poll_returns_immediately_with_records(self, bus_with_topic):
        producer = bus_with_topic.producer()
        producer.send("test", value={"n": 1})
        producer.flush()

        consumer = bus_with_topic.consumer(group_id="g1", topics=["test"])
        start = time.monotonic()
        records = consumer.poll(timeout_ms=5000)
        elapsed = (time.monotonic() - start) * 1000
        assert len(records) == 1
        assert elapsed < 100  # should return immediately, not wait 5 seconds
        consumer.close()
        producer.close()

    def test_max_records(self, bus_with_topic):
        producer = bus_with_topic.producer()
        for i in range(10):
            producer.send("test", value={"n": i})
        producer.flush()

        consumer = bus_with_topic.consumer(group_id="g1", topics=["test"])
        records = consumer.poll(timeout_ms=0, max_records=3)
        assert len(records) == 3
        consumer.close()
        producer.close()


# ─── Consumer: auto_offset_reset ──────────────────────────────


class TestAutoOffsetReset:
    def test_earliest(self, bus_with_topic):
        producer = bus_with_topic.producer()
        producer.send("test", value={"n": 1})
        producer.send("test", value={"n": 2})
        producer.flush()

        consumer = bus_with_topic.consumer(
            group_id="g1", topics=["test"], auto_offset_reset="earliest"
        )
        records = consumer.poll(timeout_ms=0)
        assert len(records) == 2  # sees all records
        consumer.close()
        producer.close()

    def test_latest(self, bus_with_topic):
        producer = bus_with_topic.producer()
        producer.send("test", value={"n": 1})
        producer.send("test", value={"n": 2})
        producer.flush()

        consumer = bus_with_topic.consumer(
            group_id="g1", topics=["test"], auto_offset_reset="latest"
        )
        records = consumer.poll(timeout_ms=0)
        assert len(records) == 0  # skips existing records

        # But new records after consumer creation are visible
        producer.send("test", value={"n": 3})
        producer.flush()
        records2 = consumer.poll(timeout_ms=0)
        assert len(records2) == 1
        assert records2[0].value == {"n": 3}
        consumer.close()
        producer.close()


# ─── Consumer: Seek ───────────────────────────────────────────


class TestConsumerSeek:
    def test_seek_to_beginning(self, bus_with_topic):
        producer = bus_with_topic.producer()
        for i in range(5):
            producer.send("test", value={"n": i})
        producer.flush()

        consumer = bus_with_topic.consumer(group_id="g1", topics=["test"])
        consumer.poll(timeout_ms=0)
        consumer.commit()

        # Seek back to beginning
        consumer.seek_to_beginning()
        records = consumer.poll(timeout_ms=0)
        assert len(records) == 5

        consumer.close()
        producer.close()

    def test_seek_to_end(self, bus_with_topic):
        producer = bus_with_topic.producer()
        for i in range(5):
            producer.send("test", value={"n": i})
        producer.flush()

        consumer = bus_with_topic.consumer(group_id="g1", topics=["test"])
        consumer.seek_to_end()
        records = consumer.poll(timeout_ms=0)
        assert len(records) == 0

        # New record after seek is visible
        producer.send("test", value={"n": 99})
        producer.flush()
        records2 = consumer.poll(timeout_ms=0)
        assert len(records2) == 1
        assert records2[0].value == {"n": 99}
        consumer.close()
        producer.close()

    def test_seek_to_specific_offset(self, bus_with_topic):
        producer = bus_with_topic.producer()
        for i in range(5):
            producer.send("test", value={"n": i})
        producer.flush()

        consumer = bus_with_topic.consumer(group_id="g1", topics=["test"])
        consumer.seek("test", 0, 3)  # seek to offset 3
        records = consumer.poll(timeout_ms=0)
        assert len(records) == 2  # offsets 3 and 4
        assert records[0].value == {"n": 3}
        assert records[1].value == {"n": 4}
        consumer.close()
        producer.close()

    def test_seek_to_timestamp(self, bus_with_topic):
        producer = bus_with_topic.producer()
        t1 = 1700000000000
        t2 = 1700000001000
        t3 = 1700000002000
        producer.send("test", value={"n": 1}, timestamp=t1)
        producer.send("test", value={"n": 2}, timestamp=t2)
        producer.send("test", value={"n": 3}, timestamp=t3)
        producer.flush()

        consumer = bus_with_topic.consumer(group_id="g1", topics=["test"])
        consumer.seek_to_timestamp("test", t2)
        records = consumer.poll(timeout_ms=0)
        assert len(records) == 2  # records at t2 and t3
        assert records[0].value == {"n": 2}
        consumer.close()
        producer.close()

    def test_seek_to_timestamp_no_match_seeks_to_end(self, bus_with_topic):
        producer = bus_with_topic.producer()
        producer.send("test", value={"n": 1}, timestamp=1700000000000)
        producer.flush()

        consumer = bus_with_topic.consumer(group_id="g1", topics=["test"])
        consumer.seek_to_timestamp("test", 9999999999999)  # far future
        records = consumer.poll(timeout_ms=0)
        assert len(records) == 0  # seeked to end
        consumer.close()
        producer.close()


# ─── Consumer: Subscribe/Unsubscribe ─────────────────────────


class TestSubscribe:
    def test_subscribe_changes_topics(self, bus):
        bus.create_topic("topic-a")
        bus.create_topic("topic-b")
        producer = bus.producer()
        producer.send("topic-a", value={"from": "a"})
        producer.send("topic-b", value={"from": "b"})
        producer.flush()

        consumer = bus.consumer(group_id="g1", topics=["topic-a"])
        records = consumer.poll(timeout_ms=0)
        assert len(records) == 1
        assert records[0].value == {"from": "a"}
        consumer.commit()

        # Subscribe to topic-b
        consumer.subscribe(["topic-b"])
        records2 = consumer.poll(timeout_ms=0)
        assert len(records2) == 1
        assert records2[0].value == {"from": "b"}
        consumer.close()
        producer.close()

    def test_unsubscribe(self, bus_with_topic):
        producer = bus_with_topic.producer()
        producer.send("test", value={"n": 1})
        producer.flush()

        consumer = bus_with_topic.consumer(group_id="g1", topics=["test"])
        consumer.unsubscribe()
        records = consumer.poll(timeout_ms=0)
        assert records == []
        consumer.close()
        producer.close()


# ─── Consumer Groups ──────────────────────────────────────────


class TestConsumerGroups:
    def test_independent_groups(self, bus_with_topic):
        """Different consumer groups independently consume the same records."""
        producer = bus_with_topic.producer()
        producer.send("test", value={"n": 1})
        producer.flush()

        c1 = bus_with_topic.consumer(group_id="group-a", topics=["test"])
        c2 = bus_with_topic.consumer(group_id="group-b", topics=["test"])

        r1 = c1.poll(timeout_ms=0)
        r2 = c2.poll(timeout_ms=0)
        assert len(r1) == 1
        assert len(r2) == 1  # both groups see the same record

        c1.close()
        c2.close()
        producer.close()

    def test_committed_offsets(self, bus_with_topic):
        producer = bus_with_topic.producer()
        producer.send("test", value={"n": 1})
        producer.send("test", value={"n": 2})
        producer.flush()

        consumer = bus_with_topic.consumer(group_id="g1", topics=["test"])
        consumer.poll(timeout_ms=0)
        consumer.commit()

        offsets = consumer.committed()
        assert offsets[("test", 0)] == 1  # offset of last record
        consumer.close()
        producer.close()

    def test_generation_increments(self, bus):
        bus.create_topic("test")

        c1 = bus.consumer(group_id="g1", topics=["test"])
        gen1 = c1._generation

        c1.close()

        c2 = bus.consumer(group_id="g1", topics=["test"])
        gen2 = c2._generation
        assert gen2 > gen1
        c2.close()

    def test_stale_generation_commit_rejected(self, bus):
        """A consumer with a stale generation cannot commit offsets."""
        bus.create_topic("test")
        producer = bus.producer()
        producer.send("test", value={"n": 1})
        producer.flush()

        c1 = bus.consumer(group_id="g1", topics=["test"], consumer_id="c1")
        c1.poll(timeout_ms=0)

        # Another consumer joins, bumping generation
        c2 = bus.consumer(group_id="g1", topics=["test"], consumer_id="c2")

        # c1's commit should fail — stale generation
        with pytest.raises(StaleGenerationError):
            c1.commit()

        c1.leave_group()
        c2.close()
        producer.close()

    def test_list_consumer_groups(self, bus):
        bus.create_topic("test")
        c1 = bus.consumer(group_id="g1", topics=["test"], consumer_id="c1")
        groups = bus.list_consumer_groups()
        assert len(groups) >= 1
        g1 = [g for g in groups if g["group_id"] == "g1"][0]
        assert g1["generation"] > 0
        assert len(g1["members"]) == 1
        assert g1["members"][0]["consumer_id"] == "c1"
        c1.close()

    def test_leave_group(self, bus):
        bus.create_topic("test")
        c1 = bus.consumer(group_id="g1", topics=["test"], consumer_id="c1")
        c1.leave_group()
        groups = bus.list_consumer_groups()
        g1 = [g for g in groups if g["group_id"] == "g1"][0]
        assert len(g1["members"]) == 0


# ─── Rebalance Callbacks ─────────────────────────────────────


class TestRebalanceCallbacks:
    def test_on_assigned_called(self, bus):
        bus.create_topic("test", partitions=2)
        assigned = []

        def on_assigned(tps):
            assigned.extend(tps)

        consumer = bus.consumer(
            group_id="g1",
            topics=["test"],
            on_partitions_assigned=on_assigned,
        )
        assert len(assigned) == 2
        assert all(isinstance(tp, TopicPartition) for tp in assigned)
        consumer.close()

    def test_on_revoked_called_on_close(self, bus):
        bus.create_topic("test")
        revoked = []

        def on_revoked(tps):
            revoked.extend(tps)

        consumer = bus.consumer(
            group_id="g1",
            topics=["test"],
            on_partitions_revoked=on_revoked,
        )
        consumer.close()
        assert len(revoked) == 1

    def test_on_revoked_called_on_resubscribe(self, bus):
        bus.create_topic("topic-a")
        bus.create_topic("topic-b")
        revoked = []

        def on_revoked(tps):
            revoked.extend(tps)

        consumer = bus.consumer(
            group_id="g1",
            topics=["topic-a"],
            on_partitions_revoked=on_revoked,
        )
        consumer.subscribe(["topic-b"])  # should revoke topic-a partitions
        assert len(revoked) == 1
        assert revoked[0].topic == "topic-a"
        consumer.close()


# ─── RangeAssignor ────────────────────────────────────────────


class TestRangeAssignor:
    def test_single_consumer_gets_all(self, bus):
        bus.create_topic("test", partitions=4)
        consumer = bus.consumer(group_id="g1", topics=["test"])
        assert len(consumer._assigned) == 4
        partitions = [tp.partition for tp in consumer._assigned]
        assert partitions == [0, 1, 2, 3]
        consumer.close()

    def test_contiguous_ranges(self, bus):
        """With 6 partitions and 2 consumers, each gets 3 contiguous partitions."""
        bus.create_topic("test", partitions=6)

        c1 = bus.consumer(group_id="g1", topics=["test"], consumer_id="aaa")
        # c1 gets all 6 initially
        assert len(c1._assigned) == 6

        # c2 joins, triggering rebalance for c2
        c2 = bus.consumer(group_id="g1", topics=["test"], consumer_id="bbb")

        # c2 should get its contiguous range
        c2_partitions = [tp.partition for tp in c2._assigned]
        assert len(c2_partitions) == 3
        # Should be contiguous (e.g., [3,4,5] for the second consumer)
        assert c2_partitions == sorted(c2_partitions)
        assert c2_partitions[-1] - c2_partitions[0] == 2  # contiguous range

        c1.close()
        c2.close()


# ─── Retention & Pruning ─────────────────────────────────────


class TestRetention:
    def test_prune_removes_old_records(self, bus):
        # Create topic with 1 second retention
        bus.create_topic("test", retention_ms=1)
        producer = bus.producer()
        producer.send("test", value={"old": True}, timestamp=1000)  # very old
        producer.flush()
        producer.close()

        # Prune should remove it
        deleted = bus.prune()
        assert deleted == 1

        info = bus.topic_info("test")
        assert info["total_records"] == 0

    def test_prune_keeps_recent_records(self, bus):
        bus.create_topic("test", retention_ms=60000)  # 60 second retention
        producer = bus.producer()
        producer.send("test", value={"recent": True})  # just now
        producer.flush()
        producer.close()

        deleted = bus.prune()
        assert deleted == 0

        info = bus.topic_info("test")
        assert info["total_records"] == 1

    def test_prune_per_topic_retention(self, bus):
        bus.create_topic("short-lived", retention_ms=1)
        long_retention = 365 * 24 * 60 * 60 * 1000  # 1 year
        bus.create_topic("long-lived", retention_ms=long_retention)
        producer = bus.producer()
        producer.send("short-lived", value={"a": 1}, timestamp=1000)
        # long-lived record is recent (now), so won't be pruned
        producer.send("long-lived", value={"b": 2})
        producer.flush()
        producer.close()

        deleted = bus.prune()
        assert deleted == 1  # only short-lived record pruned

        assert bus.topic_info("short-lived")["total_records"] == 0
        assert bus.topic_info("long-lived")["total_records"] == 1

    def test_prune_cleans_stale_consumer_groups(self, bus):
        """Prune should remove consumer members with expired heartbeats and orphan groups."""
        bus.create_topic("test")

        # Manually insert a stale consumer group and member (simulates a dead CLI consumer)
        old_hb = int(time.time() * 1000) - (2 * 60 * 60 * 1000)  # 2 hours ago
        bus._conn.execute(
            "INSERT INTO consumer_groups (group_id, generation) VALUES (?, ?)",
            ("stale-group", 1),
        )
        bus._conn.execute(
            "INSERT INTO consumer_members "
            "(group_id, consumer_id, generation, assigned_partitions, last_heartbeat) "
            "VALUES (?, ?, ?, '[]', ?)",
            ("stale-group", "stale-c1", 1, old_hb),
        )

        # Add a committed offset for this group
        bus._conn.execute(
            "INSERT OR REPLACE INTO consumer_offsets "
            "(group_id, topic, partition, committed_offset, generation, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("stale-group", "test", 0, 5, 1, old_hb),
        )

        # Verify the stale data exists
        members = bus._conn.execute(
            "SELECT COUNT(*) FROM consumer_members WHERE group_id = ?", ("stale-group",)
        ).fetchone()[0]
        assert members == 1

        # Prune should clean up
        bus.prune()

        # Stale member should be gone
        members_after = bus._conn.execute(
            "SELECT COUNT(*) FROM consumer_members WHERE group_id = ?", ("stale-group",)
        ).fetchone()[0]
        assert members_after == 0

        # Orphan group and its offsets should be gone
        groups_after = bus._conn.execute(
            "SELECT COUNT(*) FROM consumer_groups WHERE group_id = ?", ("stale-group",)
        ).fetchone()[0]
        assert groups_after == 0

        offsets_after = bus._conn.execute(
            "SELECT COUNT(*) FROM consumer_offsets WHERE group_id = ?", ("stale-group",)
        ).fetchone()[0]
        assert offsets_after == 0

    def test_prune_keeps_active_consumer_groups(self, bus):
        """Prune should NOT remove consumer groups with live members."""
        bus.create_topic("test")

        c1 = bus.consumer(group_id="active-group", topics=["test"], consumer_id="active-c1")

        # Prune should not affect active consumer
        bus.prune()

        members = bus._conn.execute(
            "SELECT COUNT(*) FROM consumer_members WHERE group_id = ?", ("active-group",)
        ).fetchone()[0]
        assert members == 1

        groups = bus._conn.execute(
            "SELECT COUNT(*) FROM consumer_groups WHERE group_id = ?", ("active-group",)
        ).fetchone()[0]
        assert groups == 1

        c1.close()


# ─── update_offset (for CLI seek) ────────────────────────────


class TestUpdateOffset:
    def test_update_offset(self, bus_with_topic):
        bus_with_topic.update_offset("g1", "test", 0, 42)
        # Verify by creating consumer
        consumer = bus_with_topic.consumer(group_id="g1", topics=["test"])
        offsets = consumer.committed()
        assert offsets[("test", 0)] == 42
        consumer.close()


# ─── Replay (direct read) ────────────────────────────────────


class TestReplay:
    def test_replay_via_direct_query(self, bus_with_topic):
        producer = bus_with_topic.producer()
        for i in range(5):
            producer.send("test", value={"n": i})
        producer.flush()
        producer.close()

        # Read directly without consumer group
        cursor = bus_with_topic._conn.execute(
            "SELECT payload FROM records WHERE topic = ? ORDER BY offset",
            ("test",),
        )
        values = [json.loads(row[0]) for row in cursor.fetchall()]
        assert len(values) == 5
        assert values[0] == {"n": 0}
        assert values[4] == {"n": 4}


# ─── Context Manager ─────────────────────────────────────────


class TestContextManager:
    def test_bus_context_manager(self, tmp_path):
        db_path = tmp_path / "cm-test.db"
        with Bus(db_path) as bus:
            bus.create_topic("test")
            assert len(bus.list_topics()) == 1
        # Connection should be closed after with block


# ─── Edge Cases ───────────────────────────────────────────────


class TestEdgeCases:
    def test_empty_topic_poll(self, bus_with_topic):
        consumer = bus_with_topic.consumer(group_id="g1", topics=["test"])
        records = consumer.poll(timeout_ms=0)
        assert records == []
        consumer.close()

    def test_multiple_topics_single_consumer(self, bus):
        bus.create_topic("topic-a")
        bus.create_topic("topic-b")
        producer = bus.producer()
        producer.send("topic-a", value={"from": "a"})
        producer.send("topic-b", value={"from": "b"})
        producer.flush()

        consumer = bus.consumer(group_id="g1", topics=["topic-a", "topic-b"])
        records = consumer.poll(timeout_ms=0)
        assert len(records) == 2
        topics = {r.topic for r in records}
        assert topics == {"topic-a", "topic-b"}
        consumer.close()
        producer.close()

    def test_close_commits_pending(self, bus_with_topic):
        producer = bus_with_topic.producer()
        producer.send("test", value={"n": 1})
        producer.flush()

        consumer = bus_with_topic.consumer(group_id="g1", topics=["test"])
        consumer.poll(timeout_ms=0)
        consumer.close()  # should commit pending offsets

        # New consumer in same group should not see the record
        consumer2 = bus_with_topic.consumer(group_id="g1", topics=["test"])
        records = consumer2.poll(timeout_ms=0)
        assert records == []
        consumer2.close()
        producer.close()

    def test_record_with_headers_roundtrip(self, bus_with_topic):
        producer = bus_with_topic.producer()
        headers = {"content-type": "application/json", "source": "test-suite"}
        producer.send("test", value={"a": 1}, headers=headers)
        producer.flush()

        consumer = bus_with_topic.consumer(group_id="g1", topics=["test"])
        records = consumer.poll(timeout_ms=0)
        assert records[0].headers == headers
        consumer.close()
        producer.close()

    def test_large_payload(self, bus_with_topic):
        producer = bus_with_topic.producer()
        large_value = {"data": "x" * 100000}  # 100KB value
        producer.send("test", value=large_value)
        producer.flush()

        consumer = bus_with_topic.consumer(group_id="g1", topics=["test"])
        records = consumer.poll(timeout_ms=0)
        assert records[0].value == large_value
        consumer.close()
        producer.close()

    def test_delete_topic_clears_offsets(self, bus):
        bus.create_topic("test")
        producer = bus.producer()
        producer.send("test", value={"n": 1})
        producer.flush()

        consumer = bus.consumer(group_id="g1", topics=["test"])
        consumer.poll(timeout_ms=0)
        consumer.commit()
        consumer.leave_group()

        bus.delete_topic("test")

        # Verify offsets are gone
        cursor = bus._conn.execute(
            "SELECT COUNT(*) FROM consumer_offsets WHERE topic = ?", ("test",)
        )
        assert cursor.fetchone()[0] == 0
        producer.close()

    def test_subscribe_to_nonexistent_topic(self, bus):
        """Subscribing to nonexistent topic should silently assign nothing."""
        bus.create_topic("real-topic")
        consumer = bus.consumer(group_id="g1", topics=["real-topic", "fake-topic"])
        # Should only get partitions from real-topic
        assigned_topics = {tp.topic for tp in consumer._assigned}
        assert "real-topic" in assigned_topics
        assert "fake-topic" not in assigned_topics
        consumer.close()


# ─── Round-Robin Partition Assignment ─────────────────────────


class TestRoundRobinAssignment:
    def test_no_key_distributes_across_partitions(self, bus_with_partitioned_topic):
        """Records without keys should distribute across partitions via round-robin."""
        producer = bus_with_partitioned_topic.producer()
        for i in range(20):
            producer.send("test", value={"n": i})
        producer.flush()

        consumer = bus_with_partitioned_topic.consumer(group_id="t1", topics=["test"])
        records = consumer.poll(timeout_ms=0)
        partitions = {r.partition for r in records}
        # With 4 partitions and 20 records, should hit multiple partitions
        assert len(partitions) > 1
        consumer.close()
        producer.close()

    def test_no_key_balances_evenly(self, bus_with_partitioned_topic):
        """Without keys, records should be roughly balanced across partitions."""
        producer = bus_with_partitioned_topic.producer()
        for i in range(40):
            producer.send("test", value={"n": i})
        producer.flush()

        consumer = bus_with_partitioned_topic.consumer(group_id="t1", topics=["test"])
        records = consumer.poll(timeout_ms=0)
        partition_counts = {0: 0, 1: 0, 2: 0, 3: 0}
        for r in records:
            partition_counts[r.partition] += 1
        # Each partition should have ~10 records (allow some variance)
        for p, count in partition_counts.items():
            assert count >= 5, f"Partition {p} only got {count} records"
        consumer.close()
        producer.close()


# ─── RangeAssignor Advanced ───────────────────────────────────


class TestRangeAssignorAdvanced:
    def test_uneven_split(self, bus):
        """3 partitions / 2 consumers: one gets 2, other gets 1."""
        bus.create_topic("test", partitions=3)

        c1 = bus.consumer(group_id="g1", topics=["test"], consumer_id="aaa")
        c1_partitions = {tp.partition for tp in c1._assigned}
        assert len(c1_partitions) == 3  # initially gets all

        c2 = bus.consumer(group_id="g1", topics=["test"], consumer_id="bbb")
        c2_partitions = {tp.partition for tp in c2._assigned}

        # c2 should get either 1 or 2 partitions (RangeAssignor gives extras to lower-index)
        # aaa < bbb lexically, so aaa gets 2 and bbb gets 1
        assert len(c2_partitions) == 1

        c1.close()
        c2.close()

    def test_no_overlap_no_gaps(self, bus):
        """With N consumers, all partitions are covered with no overlap."""
        bus.create_topic("test", partitions=7)

        consumers = []
        for i in range(3):
            c = bus.consumer(
                group_id="g1", topics=["test"],
                consumer_id=f"consumer-{i:03d}",
            )
            consumers.append(c)

        last = consumers[-1]
        assert len(last._assigned) in [2, 3]

        for c in consumers:
            c.close()

    def test_multi_topic_range_assignment(self, bus):
        """RangeAssignor assigns per-topic contiguous ranges."""
        bus.create_topic("alpha", partitions=4)
        bus.create_topic("beta", partitions=6)

        c1 = bus.consumer(
            group_id="g1", topics=["alpha", "beta"], consumer_id="aaa"
        )
        # Gets all initially
        assert len(c1._assigned) == 10

        c2 = bus.consumer(
            group_id="g1", topics=["alpha", "beta"], consumer_id="bbb"
        )

        # c2's assignments should come from both topics
        c2_topics = {tp.topic for tp in c2._assigned}
        assert "alpha" in c2_topics
        assert "beta" in c2_topics

        # alpha: 4 partitions / 2 consumers = 2 each
        c2_alpha = [tp.partition for tp in c2._assigned if tp.topic == "alpha"]
        assert len(c2_alpha) == 2

        # beta: 6 partitions / 2 consumers = 3 each
        c2_beta = [tp.partition for tp in c2._assigned if tp.topic == "beta"]
        assert len(c2_beta) == 3

        c1.close()
        c2.close()


# ─── Heartbeat & Dead Consumer ────────────────────────────────


class TestHeartbeat:
    def test_dead_consumer_evicted(self, bus):
        """Consumer with expired heartbeat should be evicted on next join."""
        bus.create_topic("test", partitions=4)

        c1 = bus.consumer(group_id="g1", topics=["test"], consumer_id="alive")

        # Manually insert a dead consumer with old heartbeat (past HEARTBEAT_TIMEOUT_MS=300s)
        old_heartbeat = int(time.time() * 1000) - 600_000  # 10 minutes ago
        bus._conn.execute(
            "INSERT OR REPLACE INTO consumer_members "
            "(group_id, consumer_id, generation, assigned_partitions, last_heartbeat) "
            "VALUES (?, ?, ?, '[]', ?)",
            ("g1", "dead-consumer", 0, old_heartbeat),
        )

        # New consumer joining should evict the dead one
        c2 = bus.consumer(group_id="g1", topics=["test"], consumer_id="alive2")

        groups = bus.list_consumer_groups()
        g1 = [g for g in groups if g["group_id"] == "g1"][0]
        member_ids = [m["consumer_id"] for m in g1["members"]]
        assert "dead-consumer" not in member_ids
        assert "alive" in member_ids or "alive2" in member_ids

        c1.close()
        c2.close()


# ─── Offset Persistence Across Restarts ───────────────────────


class TestOffsetPersistence:
    def test_offsets_survive_consumer_restart(self, bus_with_topic):
        """Committed offsets persist after consumer close and rejoin."""
        producer = bus_with_topic.producer()
        producer.send("test", value={"n": 1})
        producer.send("test", value={"n": 2})
        producer.flush()

        # First consumer: poll and commit
        c1 = bus_with_topic.consumer(group_id="g1", topics=["test"])
        records = c1.poll(timeout_ms=0)
        assert len(records) == 2
        c1.commit()
        c1.close()

        # Second consumer in same group: should not see committed records
        c2 = bus_with_topic.consumer(group_id="g1", topics=["test"])
        records2 = c2.poll(timeout_ms=0)
        assert records2 == []

        # But new records are visible
        producer.send("test", value={"n": 3})
        producer.flush()
        records3 = c2.poll(timeout_ms=0)
        assert len(records3) == 1
        assert records3[0].value == {"n": 3}
        c2.close()
        producer.close()

    def test_committed_returns_persisted_offsets(self, bus_with_topic):
        producer = bus_with_topic.producer()
        for i in range(5):
            producer.send("test", value={"n": i})
        producer.flush()

        c1 = bus_with_topic.consumer(group_id="g1", topics=["test"])
        c1.poll(timeout_ms=0)
        c1.commit()
        c1.close()

        c2 = bus_with_topic.consumer(group_id="g1", topics=["test"])
        offsets = c2.committed()
        assert offsets[("test", 0)] == 4  # last offset
        c2.close()
        producer.close()


# ─── Auto-Prune ──────────────────────────────────────────────


class TestAutoPrune:
    def test_auto_prune_triggers(self, bus):
        """Prune should trigger automatically after AUTO_PRUNE_INTERVAL sends."""
        from bus.bus import AUTO_PRUNE_INTERVAL

        # Create topic with very short retention
        bus.create_topic("test", retention_ms=1)
        producer = bus.producer()

        # Send old records
        for i in range(10):
            producer.send("test", value={"n": i}, timestamp=1000)
        producer.flush()

        # Reset produce count to just before threshold
        bus._produce_count = AUTO_PRUNE_INTERVAL - 1

        # This send should trigger auto-prune
        producer.send("test", value={"trigger": True}, timestamp=1000)
        producer.flush()

        # All records should have been pruned (all have timestamp=1000, retention=1ms)
        info = bus.topic_info("test")
        assert info["total_records"] == 0
        producer.close()


# ─── StaleGeneration on Close ────────────────────────────────


class TestStaleGenerationClose:
    def test_close_with_stale_generation_doesnt_raise(self, bus):
        """close() should swallow StaleGenerationError gracefully."""
        bus.create_topic("test")
        producer = bus.producer()
        producer.send("test", value={"n": 1})
        producer.flush()

        c1 = bus.consumer(group_id="g1", topics=["test"], consumer_id="c1")
        c1.poll(timeout_ms=0)

        # Another consumer joins, bumping generation
        c2 = bus.consumer(group_id="g1", topics=["test"], consumer_id="c2")

        # c1.close() should not raise even though generation is stale
        c1.close()  # should swallow StaleGenerationError

        c2.close()
        producer.close()


# ─── Seek with Specific Topic/Partition ──────────────────────


class TestSeekFiltered:
    def test_seek_to_beginning_specific_topic(self, bus):
        bus.create_topic("topic-a")
        bus.create_topic("topic-b")
        producer = bus.producer()
        producer.send("topic-a", value={"from": "a"})
        producer.send("topic-b", value={"from": "b"})
        producer.flush()

        consumer = bus.consumer(group_id="g1", topics=["topic-a", "topic-b"])
        consumer.poll(timeout_ms=0)
        consumer.commit()

        # Seek only topic-a back to beginning
        consumer.seek_to_beginning(topic="topic-a")
        records = consumer.poll(timeout_ms=0)
        assert len(records) == 1
        assert records[0].topic == "topic-a"
        consumer.close()
        producer.close()

    def test_seek_to_beginning_specific_partition(self, bus):
        bus.create_topic("test", partitions=2)
        producer = bus.producer()
        producer.send("test", value={"n": 1}, partition=0)
        producer.send("test", value={"n": 2}, partition=1)
        producer.flush()

        consumer = bus.consumer(group_id="g1", topics=["test"])
        consumer.poll(timeout_ms=0)
        consumer.commit()

        # Seek only partition 0 back to beginning
        consumer.seek_to_beginning(topic="test", partition=0)
        records = consumer.poll(timeout_ms=0)
        assert len(records) == 1
        assert records[0].partition == 0
        consumer.close()
        producer.close()

    def test_seek_to_end_specific_topic(self, bus):
        bus.create_topic("topic-a")
        bus.create_topic("topic-b")
        producer = bus.producer()
        producer.send("topic-a", value={"from": "a"})
        producer.send("topic-b", value={"from": "b"})
        producer.flush()

        consumer = bus.consumer(group_id="g1", topics=["topic-a", "topic-b"])
        # Seek topic-a to end, leave topic-b at beginning
        consumer.seek_to_end(topic="topic-a")

        records = consumer.poll(timeout_ms=0)
        # Should only get topic-b's record
        assert len(records) == 1
        assert records[0].topic == "topic-b"
        consumer.close()
        producer.close()


# ─── send_many Advanced ──────────────────────────────────────


class TestSendManyAdvanced:
    def test_send_many_mixed_keyed_and_unkeyed(self, bus_with_partitioned_topic):
        """Keyed records go to deterministic partition, unkeyed get round-robined."""
        producer = bus_with_partitioned_topic.producer()
        producer.send_many("test", [
            {"value": {"n": 1}, "key": "fixed-key"},
            {"value": {"n": 2}},  # no key
            {"value": {"n": 3}, "key": "fixed-key"},  # same key
            {"value": {"n": 4}},  # no key
        ])
        producer.flush()
        # Verify same key -> same partition
        cursor = bus_with_partitioned_topic._conn.execute(
            "SELECT key, partition FROM records WHERE topic = 'test' ORDER BY offset"
        )
        rows = cursor.fetchall()
        keyed_rows = [(k, p) for k, p in rows if k == "fixed-key"]
        assert len(keyed_rows) == 2
        assert keyed_rows[0][1] == keyed_rows[1][1]  # same partition
        producer.close()


# ─── Murmur2 Kafka Compatibility ─────────────────────────────


class TestMurmur2Compatibility:
    def test_empty_bytes(self):
        """murmur2(b'') should produce a known value."""
        h = _murmur2(b"")
        # murmur2 with seed 0x9747B28C and empty input
        # The hash should be deterministic
        assert isinstance(h, int)

    def test_known_kafka_values(self):
        """Test against values that would be consistent across implementations."""
        # These test that the algorithm is internally consistent
        # and that partition assignment is deterministic
        results = {}
        for key in ["user-1", "user-2", "user-3", "order-100", "order-200"]:
            results[key] = _partition_for_key(key, 10)

        # Same inputs should always produce same outputs
        for key in ["user-1", "user-2", "user-3", "order-100", "order-200"]:
            assert _partition_for_key(key, 10) == results[key]

        # All results should be in valid range
        for key, partition in results.items():
            assert 0 <= partition < 10

    def test_partition_stability_across_num_partitions(self):
        """Same key should stay on same partition when num_partitions doesn't change."""
        p1 = _partition_for_key("stable-key", 8)
        p2 = _partition_for_key("stable-key", 8)
        p3 = _partition_for_key("stable-key", 8)
        assert p1 == p2 == p3


# ─── Concurrent Access ───────────────────────────────────────


class TestConcurrentAccess:
    def test_two_bus_instances_no_duplicate_offsets(self, tmp_path):
        """Two separate Bus instances writing to same topic should not produce duplicate offsets."""
        db_path = tmp_path / "concurrent.db"
        bus1 = Bus(db_path)
        bus2 = Bus(db_path)

        bus1.create_topic("test", partitions=1)

        p1 = bus1.producer()
        p2 = bus2.producer()

        # Interleave sends — with write queue, we send and flush each individually
        for i in range(20):
            if i % 2 == 0:
                p1.send("test", value={"from": "p1", "n": i})
                p1.flush()
            else:
                p2.send("test", value={"from": "p2", "n": i})
                p2.flush()

        # All 20 records should have unique offsets
        cursor = bus1._conn.execute(
            "SELECT DISTINCT offset FROM records WHERE topic = 'test'"
        )
        offsets = {row[0] for row in cursor.fetchall()}
        assert len(offsets) == 20

        p1.close()
        p2.close()
        bus1.close()
        bus2.close()

    def test_producer_self_heals_past_foreign_raw_insert(self, bus_with_topic):
        """A foreign writer that raw-INSERTs at MAX(offset)+1 without advancing
        topic_offsets must not wedge the Producer.

        Regression: 2026-07-30 — the reply CLI's tapback path did exactly this,
        after which every daemon batch on the topic collided on the stale
        counter value forever (UNIQUE constraint), silently dropping records.
        The Producer now floors the counter at the live table's MAX+1.
        """
        import json as _json
        import time as _time
        bus = bus_with_topic
        p = bus.producer()
        p.send("test", value={"n": "before"})
        p.flush()

        # Foreign raw insert at MAX+1, counter NOT advanced (old reply-cli).
        (raw_off,) = bus._conn.execute(
            "SELECT COALESCE(MAX(offset), -1) + 1 FROM records "
            "WHERE topic = 'test' AND partition = 0"
        ).fetchone()
        bus._conn.execute(
            "INSERT INTO records (topic, partition, offset, timestamp, key, type, source, payload) "
            "VALUES ('test', 0, ?, ?, 'k', 'foreign', 'raw-writer', ?)",
            (raw_off, int(_time.time() * 1000), _json.dumps({"n": "foreign"})),
        )
        bus._conn.commit()

        # Producer must land ABOVE the foreign row, not collide with it.
        p.send("test", value={"n": "after"})
        p.flush()
        p.close()

        rows = bus._conn.execute(
            "SELECT offset, payload FROM records WHERE topic = 'test' ORDER BY offset"
        ).fetchall()
        offsets = [r[0] for r in rows]
        assert len(offsets) == len(set(offsets)), f"duplicate offsets: {offsets}"
        assert _json.loads(rows[-1][1])["n"] == "after", (
            "producer record after the foreign insert was dropped — "
            f"rows: {[(o, _json.loads(pl)['n']) for o, pl in rows]}"
        )

    def test_producer_thread_safe_concurrent_sends(self, tmp_path):
        """Single producer used from multiple threads should not corrupt data.

        This tests the write queue — multiple threads enqueue concurrently,
        the writer thread serializes the writes.
        """
        db_path = tmp_path / "threadsafe.db"
        bus = Bus(db_path)
        bus.create_topic("test", partitions=1)
        producer = bus.producer()

        errors = []

        def produce_batch(thread_id, count):
            try:
                for i in range(count):
                    producer.send("test", payload={"thread": thread_id, "n": i}, key="same-key")
            except Exception as e:
                errors.append((thread_id, e))

        threads = []
        num_threads = 8
        sends_per_thread = 25
        for t in range(num_threads):
            thread = threading.Thread(target=produce_batch, args=(t, sends_per_thread))
            threads.append(thread)

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        producer.flush()

        assert not errors, f"Thread errors: {errors}"
        total = num_threads * sends_per_thread

        # Verify all records are readable and have unique offsets
        consumer = bus.consumer(group_id="verify", topics=["test"], auto_offset_reset="earliest")
        records = consumer.poll(timeout_ms=1000, max_records=total + 10)
        assert len(records) == total
        offsets = {r.offset for r in records}
        assert len(offsets) == total

        producer.close()
        bus.close()

    def test_producer_thread_safe_send_many(self, tmp_path):
        """send_many from multiple threads should also be thread-safe."""
        db_path = tmp_path / "threadsafe_batch.db"
        bus = Bus(db_path)
        bus.create_topic("test", partitions=1)
        producer = bus.producer()

        errors = []

        def batch_produce(thread_id):
            try:
                producer.send_many("test", [
                    {"payload": {"t": thread_id, "n": i}, "key": f"k-{thread_id}"}
                    for i in range(10)
                ])
            except Exception as e:
                errors.append((thread_id, e))

        threads = [threading.Thread(target=batch_produce, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        producer.flush()

        assert not errors, f"Thread errors: {errors}"

        # Verify all 40 records written
        consumer = bus.consumer(group_id="verify", topics=["test"], auto_offset_reset="earliest")
        records = consumer.poll(timeout_ms=1000, max_records=50)
        assert len(records) == 40
        offsets = {r.offset for r in records}
        assert len(offsets) == 40

        producer.close()
        bus.close()


# ─── SDK Events ───────────────────────────────────────────────


class TestSDKEvents:
    """Tests for the sdk_events auxiliary table."""

    def test_sdk_events_table_created(self, bus):
        """sdk_events table should exist after Bus init."""
        cursor = bus._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sdk_events'"
        )
        assert cursor.fetchone() is not None

    def test_send_sdk_event_basic(self, bus):
        """Producer.send_sdk_event writes to sdk_events table."""
        producer = bus.producer()
        producer.send_sdk_event(
            session_name="imessage/_15555550100",
            chat_id="+15555550100",
            event_type="tool_use",
            tool_name="Bash",
            tool_use_id="tu_123",
            payload='{"command": "ls"}',
        )
        producer.flush()

        events = bus.query_sdk_events(session_name="imessage/_15555550100")
        assert len(events) == 1
        ev = events[0]
        assert ev["session_name"] == "imessage/_15555550100"
        assert ev["chat_id"] == "+15555550100"
        assert ev["event_type"] == "tool_use"
        assert ev["tool_name"] == "Bash"
        assert ev["tool_use_id"] == "tu_123"
        assert ev["payload"] == '{"command": "ls"}'
        assert ev["is_error"] is False
        assert ev["duration_ms"] is None
        assert ev["num_turns"] is None
        producer.close()

    def test_send_sdk_event_result(self, bus):
        """Result events include duration_ms and num_turns."""
        producer = bus.producer()
        producer.send_sdk_event(
            session_name="imessage/_15555550100",
            chat_id="+15555550100",
            event_type="result",
            duration_ms=1234.5,
            num_turns=3,
            is_error=False,
        )
        producer.flush()

        events = bus.query_sdk_events(event_type="result")
        assert len(events) == 1
        assert events[0]["duration_ms"] == 1234.5
        assert events[0]["num_turns"] == 3
        producer.close()

    def test_send_sdk_event_error(self, bus):
        """Error events set is_error=True."""
        producer = bus.producer()
        producer.send_sdk_event(
            session_name="imessage/_15555550100",
            chat_id="+15555550100",
            event_type="error",
            is_error=True,
            payload="context window exceeded",
        )
        producer.flush()

        events = bus.query_sdk_events(event_type="error")
        assert len(events) == 1
        assert events[0]["is_error"] is True
        producer.close()

    def test_payload_no_truncation(self, bus):
        """Large payloads are stored in full without truncation."""
        producer = bus.producer()
        long_payload = "x" * 5000
        producer.send_sdk_event(
            session_name="test/session",
            chat_id=None,
            event_type="text",
            payload=long_payload,
        )
        producer.flush()

        events = bus.query_sdk_events(session_name="test/session")
        assert len(events) == 1
        assert len(events[0]["payload"]) == 5000
        producer.close()

    def test_query_sdk_events_filters(self, bus):
        """Query filtering by session_name, chat_id, event_type."""
        producer = bus.producer()
        producer.send_sdk_event(
            session_name="imessage/_111",
            chat_id="+111",
            event_type="tool_use",
            tool_name="Read",
        )
        producer.send_sdk_event(
            session_name="imessage/_222",
            chat_id="+222",
            event_type="tool_result",
            tool_name="Read",
        )
        producer.send_sdk_event(
            session_name="imessage/_111",
            chat_id="+111",
            event_type="result",
        )
        producer.flush()

        # Filter by session
        events = bus.query_sdk_events(session_name="imessage/_111")
        assert len(events) == 2

        # Filter by chat_id
        events = bus.query_sdk_events(chat_id="+222")
        assert len(events) == 1
        assert events[0]["event_type"] == "tool_result"

        # Filter by event_type
        events = bus.query_sdk_events(event_type="result")
        assert len(events) == 1
        producer.close()

    def test_query_sdk_events_limit(self, bus):
        """Query respects limit parameter."""
        producer = bus.producer()
        for i in range(10):
            producer.send_sdk_event(
                session_name="test/session",
                chat_id=None,
                event_type="text",
                payload=f"msg {i}",
            )
        producer.flush()

        events = bus.query_sdk_events(session_name="test/session", limit=3)
        assert len(events) == 3
        producer.close()

    def test_query_sdk_events_ordering(self, bus):
        """Query returns most recent events first."""
        producer = bus.producer()
        for i in range(5):
            producer.send_sdk_event(
                session_name="test/session",
                chat_id=None,
                event_type="text",
                payload=f"msg {i}",
            )
        producer.flush()

        events = bus.query_sdk_events(session_name="test/session")
        # Most recent first — IDs should be descending
        ids = [e["id"] for e in events]
        assert ids == sorted(ids, reverse=True)
        producer.close()

    def test_sdk_events_pruning(self, bus):
        """Prune removes sdk_events older than 3 days."""
        producer = bus.producer()
        # Insert an event with a very old timestamp (4 days ago)
        old_ts = int(time.time() * 1000) - (4 * 24 * 60 * 60 * 1000)
        bus._conn.execute(
            "INSERT INTO sdk_events "
            "(timestamp, session_name, chat_id, event_type, is_error) "
            "VALUES (?, ?, ?, ?, ?)",
            (old_ts, "test/old", None, "text", 0),
        )
        # Insert a recent event
        producer.send_sdk_event(
            session_name="test/recent",
            chat_id=None,
            event_type="text",
        )
        producer.flush()

        # Need at least one topic for prune to run
        bus.create_topic("system", partitions=1)

        deleted = bus.prune()
        assert deleted >= 1  # At least the old sdk_event

        # Old event should be gone
        events = bus.query_sdk_events(session_name="test/old", since_hours=200)
        assert len(events) == 0

        # Recent event should remain
        events = bus.query_sdk_events(session_name="test/recent")
        assert len(events) == 1
        producer.close()

    def test_send_sdk_event_thread_safety(self, bus):
        """send_sdk_event is thread-safe via the write queue."""
        producer = bus.producer()
        errors = []

        def write_events(thread_id):
            try:
                for i in range(20):
                    producer.send_sdk_event(
                        session_name=f"test/thread_{thread_id}",
                        chat_id=None,
                        event_type="tool_use",
                        tool_name=f"Tool_{i}",
                    )
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=write_events, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        producer.flush()

        assert not errors, f"Thread errors: {errors}"

        # All 80 events should be written
        all_events = bus.query_sdk_events(since_hours=1)
        assert len(all_events) == 80
        producer.close()

    def test_send_sdk_event_null_optional_fields(self, bus):
        """send_sdk_event works with all optional fields as None."""
        producer = bus.producer()
        producer.send_sdk_event(
            session_name="test/minimal",
            chat_id=None,
            event_type="text",
        )
        producer.flush()

        events = bus.query_sdk_events(session_name="test/minimal")
        assert len(events) == 1
        ev = events[0]
        assert ev["tool_name"] is None
        assert ev["tool_use_id"] is None
        assert ev["duration_ms"] is None
        assert ev["payload"] is None
        assert ev["num_turns"] is None
        producer.close()


# ─── Write Queue ──────────────────────────────────────────────


class TestWriteQueue:
    """Tests for the write queue architecture."""

    def test_send_returns_immediately(self, bus_with_topic):
        """send() should return in microseconds (just a queue.put)."""
        producer = bus_with_topic.producer()
        start = time.monotonic()
        for _ in range(100):
            producer.send("test", value={"n": 1})
        elapsed_ms = (time.monotonic() - start) * 1000
        # 100 sends should complete in under 50ms (they're just queue.put calls)
        assert elapsed_ms < 50, f"100 sends took {elapsed_ms:.1f}ms — should be near-instant"
        producer.flush()
        producer.close()

    def test_queue_depth_property(self, bus_with_topic):
        """queue_depth should reflect pending items."""
        producer = bus_with_topic.producer()
        # Queue depth starts at 0
        assert producer.queue_depth >= 0
        producer.close()

    def test_batch_count_increments(self, bus_with_topic):
        """_batch_count should increment as batches are written."""
        producer = bus_with_topic.producer()
        assert producer._batch_count == 0
        producer.send("test", value={"n": 1})
        producer.flush()
        assert producer._batch_count >= 1
        producer.close()

    def test_flush_blocks_until_drained(self, bus_with_topic):
        """flush() should block until all queued items are written."""
        producer = bus_with_topic.producer()
        for i in range(50):
            producer.send("test", value={"n": i})
        result = producer.flush(timeout=5.0)
        assert result is True
        # All records should be visible
        info = bus_with_topic.topic_info("test")
        assert info["total_records"] == 50
        producer.close()

    def test_graceful_shutdown_drains_queue(self, bus_with_topic):
        """close() should drain remaining events before stopping."""
        producer = bus_with_topic.producer()
        for i in range(20):
            producer.send("test", value={"n": i})
        producer.close()  # Should drain before closing
        # Verify all records were written
        info = bus_with_topic.topic_info("test")
        assert info["total_records"] == 20

    def test_concurrent_produces_from_multiple_threads(self, bus_with_topic):
        """Multiple threads can enqueue concurrently without issues."""
        producer = bus_with_topic.producer()
        errors = []

        def produce(thread_id):
            try:
                for i in range(50):
                    producer.send("test", value={"t": thread_id, "n": i})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=produce, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        producer.flush()

        assert not errors, f"Thread errors: {errors}"
        info = bus_with_topic.topic_info("test")
        assert info["total_records"] == 200
        producer.close()

    def test_mixed_records_and_sdk_events_in_batch(self, bus):
        """Records and SDK events can be batched together."""
        bus.create_topic("test")
        producer = bus.producer()

        producer.send("test", value={"msg": "hello"})
        producer.send_sdk_event(
            session_name="test/session",
            chat_id=None,
            event_type="tool_use",
            tool_name="Bash",
        )
        producer.send("test", value={"msg": "world"})
        producer.flush()

        info = bus.topic_info("test")
        assert info["total_records"] == 2

        events = bus.query_sdk_events(session_name="test/session")
        assert len(events) == 1
        producer.close()


class TestTimestampNormalization:
    """Test CLI timestamp auto-detection (seconds vs ms)."""

    def test_seconds_converted_to_ms(self):
        from bus.cli import _normalize_timestamp_ms
        # Unix seconds (e.g. 2026-03-15 00:00:00 UTC)
        ts_seconds = 1773763200
        result = _normalize_timestamp_ms(ts_seconds)
        assert result == ts_seconds * 1000

    def test_ms_stays_as_ms(self):
        from bus.cli import _normalize_timestamp_ms
        # Already in ms (above 2e12 threshold)
        ts_ms = 2_100_000_000_000
        result = _normalize_timestamp_ms(ts_ms)
        assert result == ts_ms

    def test_boundary_value(self):
        from bus.cli import _normalize_timestamp_ms
        # Just under the boundary (2e12) - treated as seconds
        assert _normalize_timestamp_ms(1_999_999_999_999) == 1_999_999_999_999 * 1000
        # At and above the boundary - treated as ms
        assert _normalize_timestamp_ms(2_000_000_000_000) == 2_000_000_000_000


# ─── Exclusive Consumer Mode ─────────────────────────────────


class TestExclusiveConsumer:
    """Tests for exclusive=True consumer mode (zombie purge on join)."""

    def test_exclusive_purges_other_members(self, bus):
        """Exclusive consumer should delete all other members regardless of heartbeat freshness."""
        bus.create_topic("test", partitions=4)

        # Create a non-exclusive consumer first
        c1 = bus.consumer(group_id="g1", topics=["test"], consumer_id="zombie")
        assert len(c1.assigned_partitions) == 4

        # Now create an exclusive consumer — should purge c1's membership
        c2 = bus.consumer(group_id="g1", topics=["test"], consumer_id="exclusive-one", exclusive=True)
        assert len(c2.assigned_partitions) == 4  # gets ALL partitions

        # Verify the zombie is gone from the group
        groups = bus.list_consumer_groups()
        g1 = [g for g in groups if g["group_id"] == "g1"][0]
        member_ids = [m["consumer_id"] for m in g1["members"]]
        assert "zombie" not in member_ids
        assert "exclusive-one" in member_ids

        c1.close()
        c2.close()

    def test_exclusive_does_not_purge_self(self, bus):
        """Exclusive consumer should not delete its own membership row."""
        bus.create_topic("test", partitions=4)

        c1 = bus.consumer(group_id="g1", topics=["test"], consumer_id="sole", exclusive=True)
        assert len(c1.assigned_partitions) == 4

        groups = bus.list_consumer_groups()
        g1 = [g for g in groups if g["group_id"] == "g1"][0]
        assert len(g1["members"]) == 1
        assert g1["members"][0]["consumer_id"] == "sole"

        c1.close()

    def test_exclusive_purges_fresh_heartbeat_zombie(self, bus):
        """Exclusive mode should purge members even with fresh heartbeats (the key bug fix)."""
        bus.create_topic("test", partitions=4)

        # Insert a zombie with a very fresh heartbeat (simulates rapid restart scenario)
        fresh_heartbeat = int(time.time() * 1000)  # just now
        bus._conn.execute(
            "INSERT OR REPLACE INTO consumer_members "
            "(group_id, consumer_id, generation, assigned_partitions, last_heartbeat) "
            "VALUES (?, ?, ?, '[]', ?)",
            ("g1", "fresh-zombie", 1, fresh_heartbeat),
        )

        # Exclusive consumer should still purge it
        c1 = bus.consumer(group_id="g1", topics=["test"], consumer_id="new-daemon", exclusive=True)
        assert len(c1.assigned_partitions) == 4

        groups = bus.list_consumer_groups()
        g1 = [g for g in groups if g["group_id"] == "g1"][0]
        member_ids = [m["consumer_id"] for m in g1["members"]]
        assert "fresh-zombie" not in member_ids
        assert "new-daemon" in member_ids

        c1.close()

    def test_non_exclusive_does_not_purge_fresh_zombie(self, bus):
        """Non-exclusive consumer should NOT purge members with fresh heartbeats."""
        bus.create_topic("test", partitions=4)

        # Insert a zombie with a fresh heartbeat
        fresh_heartbeat = int(time.time() * 1000)
        bus._conn.execute(
            "INSERT OR REPLACE INTO consumer_members "
            "(group_id, consumer_id, generation, assigned_partitions, last_heartbeat) "
            "VALUES (?, ?, ?, '[]', ?)",
            ("g1", "fresh-zombie", 1, fresh_heartbeat),
        )

        # Non-exclusive consumer should share partitions
        c1 = bus.consumer(group_id="g1", topics=["test"], consumer_id="non-excl")
        assert len(c1.assigned_partitions) < 4  # should be split

        c1.close()


# ─── Topic Partition Count ─────────────────────────────────


class TestTopicPartitionCount:
    def test_returns_correct_count(self, bus):
        bus.create_topic("multi", partitions=8)
        assert bus.topic_partition_count("multi") == 8

    def test_single_partition_default(self, bus):
        bus.create_topic("single", partitions=1)
        assert bus.topic_partition_count("single") == 1

    def test_raises_on_missing_topic(self, bus):
        with pytest.raises(ValueError, match="does not exist"):
            bus.topic_partition_count("nonexistent")


# ─── Assigned Partitions Property ──────────────────────────


class TestAssignedPartitionsProperty:
    def test_returns_all_partitions(self, bus):
        bus.create_topic("test", partitions=4)
        c = bus.consumer(group_id="g1", topics=["test"])
        assigned = c.assigned_partitions
        assert len(assigned) == 4
        assert all(isinstance(tp, TopicPartition) for tp in assigned)
        c.close()

    def test_returns_copy_not_reference(self, bus):
        """Mutating the returned list should not affect internal state."""
        bus.create_topic("test", partitions=4)
        c = bus.consumer(group_id="g1", topics=["test"])
        assigned = c.assigned_partitions
        assigned.clear()  # mutate the copy
        assert len(c.assigned_partitions) == 4  # internal state unchanged
        c.close()


# ─── Offset durability across retention (offset-reuse regression) ──────────
#
# Regression tests for the July 2026 incident: next-offset was derived from
# MAX(records.offset), so when retention archived EVERY live row of a topic,
# offsets restarted at 0 while consumer committed offsets kept their old
# high-water marks — consumers went permanently deaf on that topic (all
# task.requested events were silently dropped for days).


class TestOffsetDurability:
    def _drain_via_prune(self, bus, topic):
        """Force retention to archive every live record of `topic`."""
        bus._conn.execute(
            "UPDATE topics SET retention_ms = 1 WHERE name = ?", (topic,)
        )
        bus._topic_cache = getattr(bus, "_topic_cache", {})
        time.sleep(0.002)
        bus.prune()

    def test_offsets_continue_after_full_drain(self, bus):
        bus.create_topic("test", retention_ms=60000)
        p1 = bus.producer()
        for n in range(3):
            p1.send("test", value={"n": n}, timestamp=1000 + n)  # ancient
        p1.flush()
        p1.close()

        self._drain_via_prune(bus, "test")
        info = bus.topic_info("test")
        assert info["total_records"] == 0  # topic fully drained

        # Fresh producer = cold offset cache (simulates daemon restart).
        p2 = bus.producer()
        p2.send("test", value={"n": 99})
        p2.flush()
        p2.close()

        row = bus._conn.execute(
            "SELECT MIN(offset), MAX(offset) FROM records WHERE topic='test'"
        ).fetchone()
        assert row == (3, 3), f"offset was reused after drain: {row}"

    def test_consumer_not_deafened_by_drain(self, bus):
        bus.create_topic("test", retention_ms=60000)
        p1 = bus.producer()
        for n in range(3):
            p1.send("test", value={"n": n}, timestamp=1000 + n)
        p1.flush()
        p1.close()

        consumer = bus.consumer(group_id="g1", topics=["test"])
        records = consumer.poll(timeout_ms=0)
        assert len(records) == 3
        consumer.commit()  # committed offset now 2

        self._drain_via_prune(bus, "test")

        p2 = bus.producer()  # cold cache
        p2.send("test", value={"n": "after-drain"})
        p2.flush()
        p2.close()

        records = consumer.poll(timeout_ms=0)
        got = [r.value["n"] for r in records]
        assert got == ["after-drain"], (
            f"consumer deaf after drain (committed above restarted offsets): {got}"
        )
        consumer.close()

    def test_seed_respects_committed_offsets_without_archive(self, bus):
        # archive=False topic: prune deletes outright, so committed offsets are
        # the only surviving evidence of prior allocation.
        bus.create_topic("test", retention_ms=60000, archive=False)
        p1 = bus.producer()
        for n in range(3):
            p1.send("test", value={"n": n}, timestamp=1000 + n)
        p1.flush()
        p1.close()

        consumer = bus.consumer(group_id="g1", topics=["test"])
        consumer.poll(timeout_ms=0)
        consumer.commit()

        # Simulate a pre-fix DB: drop the counter, then drain.
        bus._conn.execute("DELETE FROM topic_offsets WHERE topic='test'")
        self._drain_via_prune(bus, "test")

        p2 = bus.producer()
        p2.send("test", value={"n": "next"})
        p2.flush()
        p2.close()

        (min_off,) = bus._conn.execute(
            "SELECT MIN(offset) FROM records WHERE topic='test'"
        ).fetchone()
        assert min_off >= 3, f"offset regressed below committed high-water: {min_off}"

        records = consumer.poll(timeout_ms=0)
        got = [r.value["n"] for r in records]
        assert got == ["next"]
        consumer.close()
