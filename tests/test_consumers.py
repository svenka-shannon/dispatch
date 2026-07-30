"""
Unit tests for the consumer framework: ConsumerConfig, ConsumerRunner, actions, batching.

Tests cover:
- Actions: call_function, produce, produce_batch, log, multi, noop, dead_letter
- ConsumerConfig: filter, batch config validation
- ConsumerRunner: run_once, batching (time + count), retry, error_action, stop/flush
- Integration: end-to-end pipeline with filter → batch → action
"""

import json
import logging
import time

import pytest

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from bus.bus import Bus, Record
from bus.consumers import (
    BatchConfig,
    ConsumerConfig,
    ConsumerRunner,
    actions,
)


@pytest.fixture
def bus(tmp_path):
    db_path = tmp_path / "test-bus.db"
    b = Bus(db_path)
    yield b
    b.close()


@pytest.fixture
def bus_with_topic(bus):
    bus.create_topic("test", partitions=1)
    return bus


def _make_record(topic="test", partition=0, offset=0, key=None, value=None, payload=None, timestamp=None, type=None, source=None):
    """Helper to create a Record for testing actions directly."""
    return Record(
        topic=topic,
        partition=partition,
        offset=offset,
        timestamp=timestamp or int(time.time() * 1000),
        key=key,
        type=type,
        source=source,
        payload=payload or value or {},
    )


# ─── Actions ──────────────────────────────────────────────────


class TestCallFunction:
    def test_basic(self):
        results = []
        action = actions.call_function(lambda records: results.extend(records))
        records = [_make_record(value={"n": 1}), _make_record(value={"n": 2})]
        action(records)
        assert len(results) == 2
        assert results[0].value == {"n": 1}

    def test_receives_list(self):
        """Action always receives a list of records, even for single record."""
        received = []
        action = actions.call_function(lambda rs: received.append(len(rs)))
        action([_make_record()])
        assert received == [1]


class TestProduce:
    def test_produces_to_topic(self, bus_with_topic):
        bus_with_topic.create_topic("output")
        action = actions.produce(
            bus_with_topic, "output",
            transform=lambda r: {"enriched": True, **r.value},
        )
        records = [
            _make_record(value={"n": 1}, key="k1"),
            _make_record(value={"n": 2}, key="k2"),
        ]
        action(records)

        # Verify records were produced to output topic
        consumer = bus_with_topic.consumer(group_id="verify", topics=["output"])
        output = consumer.poll(timeout_ms=0)
        assert len(output) == 2
        assert output[0].value == {"enriched": True, "n": 1}
        assert output[0].key == "k1"
        assert output[1].value == {"enriched": True, "n": 2}
        consumer.close()

    def test_preserves_key(self, bus_with_topic):
        bus_with_topic.create_topic("output")
        action = actions.produce(bus_with_topic, "output", transform=lambda r: r.value)
        action([_make_record(value={"x": 1}, key="my-key")])

        consumer = bus_with_topic.consumer(group_id="verify", topics=["output"])
        output = consumer.poll(timeout_ms=0)
        assert output[0].key == "my-key"
        consumer.close()


class TestProduceBatch:
    def test_batch_transform(self, bus_with_topic):
        bus_with_topic.create_topic("summary")
        action = actions.produce_batch(
            bus_with_topic, "summary",
            transform=lambda rs: [{"count": len(rs), "total": sum(r.value["n"] for r in rs)}],
        )
        records = [
            _make_record(value={"n": 10}),
            _make_record(value={"n": 20}),
            _make_record(value={"n": 30}),
        ]
        action(records)

        consumer = bus_with_topic.consumer(group_id="verify", topics=["summary"])
        output = consumer.poll(timeout_ms=0)
        assert len(output) == 1
        assert output[0].value == {"count": 3, "total": 60}
        consumer.close()


class TestLog:
    def test_log_default(self, caplog):
        action = actions.log()
        with caplog.at_level(logging.INFO, logger="bus.consumers"):
            action([_make_record(value={"msg": "hello"}, key="k1")])
        assert "k1" in caplog.text
        assert "hello" in caplog.text

    def test_log_custom_template(self, caplog):
        action = actions.log(template=lambda r: f"Custom: {r.key}")
        with caplog.at_level(logging.INFO, logger="bus.consumers"):
            action([_make_record(key="my-key")])
        assert "Custom: my-key" in caplog.text


class TestMulti:
    def test_runs_all_actions(self):
        results_a = []
        results_b = []
        action = actions.multi(
            actions.call_function(lambda rs: results_a.extend(rs)),
            actions.call_function(lambda rs: results_b.extend(rs)),
        )
        records = [_make_record(value={"n": 1})]
        action(records)
        assert len(results_a) == 1
        assert len(results_b) == 1

    def test_runs_in_order(self):
        order = []
        action = actions.multi(
            actions.call_function(lambda rs: order.append("first")),
            actions.call_function(lambda rs: order.append("second")),
            actions.call_function(lambda rs: order.append("third")),
        )
        action([_make_record()])
        assert order == ["first", "second", "third"]


class TestNoop:
    def test_does_nothing(self):
        action = actions.noop()
        # Should not raise
        action([_make_record(), _make_record()])


class TestDeadLetter:
    def test_sends_to_dead_letter_topic(self, bus_with_topic):
        action = actions.dead_letter(bus_with_topic, topic="dlq")
        records = [
            _make_record(topic="source", partition=2, offset=42, key="k1", value={"bad": True}),
        ]
        action(records)

        consumer = bus_with_topic.consumer(group_id="verify", topics=["dlq"])
        output = consumer.poll(timeout_ms=0)
        assert len(output) == 1
        assert output[0].payload["original_topic"] == "source"
        assert output[0].payload["original_partition"] == 2
        assert output[0].payload["original_offset"] == 42
        assert output[0].payload["original_key"] == "k1"
        assert output[0].payload["original_payload"] == {"bad": True}
        assert "error_time" in output[0].payload
        consumer.close()

    def test_creates_topic_if_not_exists(self, bus):
        action = actions.dead_letter(bus, topic="new-dlq")
        action([_make_record()])
        # Verify topic was created
        topics = [t["name"] for t in bus.list_topics()]
        assert "new-dlq" in topics


# ─── BatchConfig ──────────────────────────────────────────────


class TestBatchConfig:
    def test_window_seconds(self):
        config = BatchConfig(window_seconds=60)
        assert config.window_seconds == 60

    def test_window_count(self):
        config = BatchConfig(window_count=10)
        assert config.window_count == 10

    def test_both(self):
        config = BatchConfig(window_seconds=60, window_count=100)
        assert config.window_seconds == 60
        assert config.window_count == 100

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="must have"):
            BatchConfig(window_seconds=0, window_count=0)

    def test_negative_raises(self):
        with pytest.raises(ValueError, match="must have"):
            BatchConfig(window_seconds=-1)


# ─── ConsumerRunner: Basic ────────────────────────────────────


class TestConsumerRunnerBasic:
    def test_run_once_processes_records(self, bus_with_topic):
        producer = bus_with_topic.producer()
        producer.send("test", value={"n": 1})
        producer.send("test", value={"n": 2})
        producer.flush()

        processed = []
        runner = ConsumerRunner(bus_with_topic, [
            ConsumerConfig(
                topic="test",
                group="g1",
                action=actions.call_function(lambda rs: processed.extend(rs)),
            ),
        ])

        results = runner.run_once()
        assert results["g1"] == 2
        assert len(processed) == 2
        assert processed[0].value == {"n": 1}
        runner.stop()
        producer.close()

    def test_run_once_no_records(self, bus_with_topic):
        runner = ConsumerRunner(bus_with_topic, [
            ConsumerConfig(
                topic="test",
                group="g1",
                action=actions.noop(),
            ),
        ])
        results = runner.run_once()
        assert results["g1"] == 0
        runner.stop()

    def test_run_once_commits_offsets(self, bus_with_topic):
        producer = bus_with_topic.producer()
        producer.send("test", value={"n": 1})
        producer.flush()

        runner = ConsumerRunner(bus_with_topic, [
            ConsumerConfig(
                topic="test",
                group="g1",
                action=actions.noop(),
            ),
        ])
        runner.run_once()

        # Second run should see nothing (offsets committed)
        results = runner.run_once()
        assert results["g1"] == 0
        runner.stop()
        producer.close()

    def test_multiple_consumers(self, bus):
        bus.create_topic("topic-a")
        bus.create_topic("topic-b")
        producer = bus.producer()
        producer.send("topic-a", value={"from": "a"})
        producer.send("topic-b", value={"from": "b"})
        producer.flush()

        results_a = []
        results_b = []
        runner = ConsumerRunner(bus, [
            ConsumerConfig(
                topic="topic-a",
                group="consumer-a",
                action=actions.call_function(lambda rs: results_a.extend(rs)),
            ),
            ConsumerConfig(
                topic="topic-b",
                group="consumer-b",
                action=actions.call_function(lambda rs: results_b.extend(rs)),
            ),
        ])

        runner.run_once()
        assert len(results_a) == 1
        assert results_a[0].value == {"from": "a"}
        assert len(results_b) == 1
        assert results_b[0].value == {"from": "b"}
        runner.stop()
        producer.close()


# ─── ConsumerRunner: Filter ───────────────────────────────────


class TestConsumerRunnerFilter:
    def test_filter_keeps_matching(self, bus_with_topic):
        producer = bus_with_topic.producer()
        producer.send("test", value={"type": "good", "n": 1})
        producer.send("test", value={"type": "bad", "n": 2})
        producer.send("test", value={"type": "good", "n": 3})
        producer.flush()

        processed = []
        runner = ConsumerRunner(bus_with_topic, [
            ConsumerConfig(
                topic="test",
                group="g1",
                filter=lambda r: r.value.get("type") == "good",
                action=actions.call_function(lambda rs: processed.extend(rs)),
            ),
        ])

        results = runner.run_once()
        assert results["g1"] == 2
        assert len(processed) == 2
        assert all(r.value["type"] == "good" for r in processed)
        runner.stop()
        producer.close()

    def test_filter_all_rejected_commits_offsets(self, bus_with_topic):
        producer = bus_with_topic.producer()
        producer.send("test", value={"type": "bad"})
        producer.send("test", value={"type": "bad"})
        producer.flush()

        processed = []
        runner = ConsumerRunner(bus_with_topic, [
            ConsumerConfig(
                topic="test",
                group="g1",
                filter=lambda r: r.value.get("type") == "good",
                action=actions.call_function(lambda rs: processed.extend(rs)),
            ),
        ])

        runner.run_once()
        assert len(processed) == 0

        # Offsets should still be committed (filtered records are consumed)
        results = runner.run_once()
        assert results["g1"] == 0
        runner.stop()
        producer.close()

    def test_no_filter_passes_all(self, bus_with_topic):
        producer = bus_with_topic.producer()
        producer.send("test", value={"n": 1})
        producer.send("test", value={"n": 2})
        producer.flush()

        processed = []
        runner = ConsumerRunner(bus_with_topic, [
            ConsumerConfig(
                topic="test",
                group="g1",
                action=actions.call_function(lambda rs: processed.extend(rs)),
            ),
        ])

        runner.run_once()
        assert len(processed) == 2
        runner.stop()
        producer.close()


# ─── ConsumerRunner: Batching ─────────────────────────────────


class TestConsumerRunnerBatching:
    def test_count_based_batch(self, bus_with_topic):
        producer = bus_with_topic.producer()
        for i in range(5):
            producer.send("test", value={"n": i})
        producer.flush()

        batches = []
        runner = ConsumerRunner(bus_with_topic, [
            ConsumerConfig(
                topic="test",
                group="g1",
                batch=BatchConfig(window_count=3),
                action=actions.call_function(lambda rs: batches.append(list(rs))),
            ),
        ])

        # First run: 5 records, batch size 3 → should flush one batch of 5
        # (all 5 polled at once, 5 >= 3 threshold)
        runner.run_once()
        assert len(batches) == 1
        assert len(batches[0]) == 5
        runner.stop()
        producer.close()

    def test_count_based_batch_accumulates(self, bus_with_topic):
        """Records accumulate until count threshold is met."""
        batches = []
        runner = ConsumerRunner(bus_with_topic, [
            ConsumerConfig(
                topic="test",
                group="g1",
                batch=BatchConfig(window_count=3),
                action=actions.call_function(lambda rs: batches.append(list(rs))),
            ),
        ])

        producer = bus_with_topic.producer()

        # Send 2 records — below threshold
        producer.send("test", value={"n": 1})
        producer.send("test", value={"n": 2})
        producer.flush()
        runner.run_once()
        assert len(batches) == 0  # not yet flushed

        # Send 1 more — hits threshold
        producer.send("test", value={"n": 3})
        producer.flush()
        runner.run_once()
        assert len(batches) == 1
        assert len(batches[0]) == 3
        runner.stop()
        producer.close()

    def test_time_based_batch(self, bus_with_topic):
        """Records accumulate until time window expires."""
        producer = bus_with_topic.producer()
        producer.send("test", value={"n": 1})
        producer.send("test", value={"n": 2})
        producer.flush()

        batches = []
        runner = ConsumerRunner(bus_with_topic, [
            ConsumerConfig(
                topic="test",
                group="g1",
                batch=BatchConfig(window_seconds=1),
                action=actions.call_function(lambda rs: batches.append(list(rs))),
            ),
        ])

        # First run: accumulates but window hasn't expired
        runner.run_once()
        assert len(batches) == 0

        # Wait for window to expire
        time.sleep(1.1)

        # Now it should flush
        runner.run_once()
        assert len(batches) == 1
        assert len(batches[0]) == 2
        runner.stop()
        producer.close()

    def test_batch_with_filter(self, bus_with_topic):
        """Filter applies before batching."""
        producer = bus_with_topic.producer()
        producer.send("test", value={"type": "good", "n": 1})
        producer.send("test", value={"type": "bad", "n": 2})
        producer.send("test", value={"type": "good", "n": 3})
        producer.flush()

        batches = []
        runner = ConsumerRunner(bus_with_topic, [
            ConsumerConfig(
                topic="test",
                group="g1",
                filter=lambda r: r.value.get("type") == "good",
                batch=BatchConfig(window_count=2),
                action=actions.call_function(lambda rs: batches.append(list(rs))),
            ),
        ])

        runner.run_once()
        assert len(batches) == 1
        assert len(batches[0]) == 2
        assert all(r.value["type"] == "good" for r in batches[0])
        runner.stop()
        producer.close()

    def test_stop_flushes_pending_batch(self, bus_with_topic):
        """stop() should flush any accumulated batch records."""
        producer = bus_with_topic.producer()
        producer.send("test", value={"n": 1})
        producer.flush()

        batches = []
        runner = ConsumerRunner(bus_with_topic, [
            ConsumerConfig(
                topic="test",
                group="g1",
                batch=BatchConfig(window_count=100),  # high threshold, won't flush normally
                action=actions.call_function(lambda rs: batches.append(list(rs))),
            ),
        ])

        runner.run_once()
        assert len(batches) == 0  # not flushed yet

        runner.stop()  # should flush pending
        assert len(batches) == 1
        assert len(batches[0]) == 1
        producer.close()


# ─── ConsumerRunner: Retry & Error Handling ───────────────────


class TestConsumerRunnerRetry:
    def test_retry_on_failure(self, bus_with_topic):
        producer = bus_with_topic.producer()
        producer.send("test", value={"n": 1})
        producer.flush()

        call_count = [0]

        def flaky_action(records):
            call_count[0] += 1
            if call_count[0] < 3:
                raise RuntimeError("transient failure")
            # succeeds on 3rd try

        runner = ConsumerRunner(bus_with_topic, [
            ConsumerConfig(
                topic="test",
                group="g1",
                action=flaky_action,
                max_retries=2,  # 1 initial + 2 retries = 3 attempts
            ),
        ])

        runner.run_once()
        assert call_count[0] == 3  # tried 3 times, succeeded on 3rd
        runner.stop()
        producer.close()

    def test_error_action_on_exhausted_retries(self, bus_with_topic):
        producer = bus_with_topic.producer()
        producer.send("test", value={"n": 1})
        producer.flush()

        error_records = []

        def always_fail(records):
            raise RuntimeError("permanent failure")

        runner = ConsumerRunner(bus_with_topic, [
            ConsumerConfig(
                topic="test",
                group="g1",
                action=always_fail,
                max_retries=1,
                error_action=actions.call_function(lambda rs: error_records.extend(rs)),
            ),
        ])

        runner.run_once()
        assert len(error_records) == 1  # error_action received the failed records
        runner.stop()
        producer.close()

    def test_dead_letter_on_failure(self, bus_with_topic):
        producer = bus_with_topic.producer()
        producer.send("test", value={"important": True}, key="k1")
        producer.flush()

        def always_fail(records):
            raise RuntimeError("crash")

        runner = ConsumerRunner(bus_with_topic, [
            ConsumerConfig(
                topic="test",
                group="g1",
                action=always_fail,
                max_retries=0,
                error_action=actions.dead_letter(bus_with_topic, topic="dlq"),
            ),
        ])

        runner.run_once()

        # Verify dead letter topic has the record
        consumer = bus_with_topic.consumer(group_id="verify", topics=["dlq"])
        output = consumer.poll(timeout_ms=0)
        assert len(output) == 1
        assert output[0].payload["original_payload"] == {"important": True}
        consumer.close()
        runner.stop()
        producer.close()

    def test_no_retry_by_default(self, bus_with_topic):
        producer = bus_with_topic.producer()
        producer.send("test", value={"n": 1})
        producer.flush()

        call_count = [0]

        def always_fail(records):
            call_count[0] += 1
            raise RuntimeError("fail")

        runner = ConsumerRunner(bus_with_topic, [
            ConsumerConfig(
                topic="test",
                group="g1",
                action=always_fail,
            ),
        ])

        runner.run_once()
        assert call_count[0] == 1  # no retries
        runner.stop()
        producer.close()


# ─── Integration: End-to-End Pipeline ─────────────────────────


class TestIntegration:
    def test_filter_to_action_pipeline(self, bus):
        """Full pipeline: produce → filter → action."""
        bus.create_topic("events")
        producer = bus.producer()
        producer.send("events", value={"type": "click", "page": "/home"})
        producer.send("events", value={"type": "purchase", "amount": 50})
        producer.send("events", value={"type": "click", "page": "/about"})
        producer.send("events", value={"type": "purchase", "amount": 100})
        producer.flush()

        purchases = []
        runner = ConsumerRunner(bus, [
            ConsumerConfig(
                topic="events",
                group="purchase-tracker",
                filter=lambda r: r.value.get("type") == "purchase",
                action=actions.call_function(lambda rs: purchases.extend(rs)),
            ),
        ])

        runner.run_once()
        assert len(purchases) == 2
        assert purchases[0].value["amount"] == 50
        assert purchases[1].value["amount"] == 100
        runner.stop()
        producer.close()

    def test_produce_chain_pipeline(self, bus):
        """Topic chaining: input → enrich → output."""
        bus.create_topic("raw")
        bus.create_topic("enriched")

        producer = bus.producer()
        producer.send("raw", value={"name": "Widget", "price": 10}, key="w1")
        producer.send("raw", value={"name": "Gadget", "price": 20}, key="g1")
        producer.flush()

        runner = ConsumerRunner(bus, [
            ConsumerConfig(
                topic="raw",
                group="enricher",
                action=actions.produce(
                    bus, "enriched",
                    transform=lambda r: {**r.value, "currency": "USD", "in_stock": True},
                ),
            ),
        ])

        runner.run_once()

        # Verify enriched topic
        consumer = bus.consumer(group_id="verify", topics=["enriched"])
        output = consumer.poll(timeout_ms=0)
        assert len(output) == 2
        assert output[0].value["name"] == "Widget"
        assert output[0].value["currency"] == "USD"
        assert output[0].value["in_stock"] is True
        assert output[0].key == "w1"
        consumer.close()
        runner.stop()
        producer.close()

    def test_multi_action_pipeline(self, bus):
        """Multiple actions on same records: log + produce + custom fn."""
        bus.create_topic("input")
        bus.create_topic("output")
        producer = bus.producer()
        producer.send("input", value={"n": 42}, key="k1")
        producer.flush()

        custom_results = []

        runner = ConsumerRunner(bus, [
            ConsumerConfig(
                topic="input",
                group="multi-pipeline",
                action=actions.multi(
                    actions.call_function(lambda rs: custom_results.extend(rs)),
                    actions.produce(bus, "output", transform=lambda r: {"doubled": r.value["n"] * 2}),
                ),
            ),
        ])

        runner.run_once()
        assert len(custom_results) == 1

        consumer = bus.consumer(group_id="verify", topics=["output"])
        output = consumer.poll(timeout_ms=0)
        assert len(output) == 1
        assert output[0].value == {"doubled": 84}
        consumer.close()
        runner.stop()
        producer.close()


# ─── ConsumerRunner: Deferred Commits ────────────────────────


class TestConsumerRunnerDeferredCommits:
    """Tests for commit_interval_s: batching offset commits to reduce write lock contention."""

    def test_commit_deferred_with_interval(self, bus_with_topic):
        """With commit_interval_s > 0, commits should be deferred."""
        producer = bus_with_topic.producer()
        producer.send("test", value={"n": 1})
        producer.flush()

        processed = []
        runner = ConsumerRunner(bus_with_topic, [
            ConsumerConfig(
                topic="test",
                group="deferred-g1",
                action=actions.call_function(lambda rs: processed.extend(rs)),
                commit_interval_s=10,  # 10 second commit interval
            ),
        ])

        runner.run_once()
        assert len(processed) == 1

        # Offset should NOT be committed yet (interval hasn't elapsed)
        assert "deferred-g1" in runner._pending_commit

        runner.stop()
        producer.close()

    def test_commit_immediate_without_interval(self, bus_with_topic):
        """With commit_interval_s=0, commits should happen every poll (legacy behavior)."""
        producer = bus_with_topic.producer()
        producer.send("test", value={"n": 1})
        producer.flush()

        processed = []
        runner = ConsumerRunner(bus_with_topic, [
            ConsumerConfig(
                topic="test",
                group="immediate-g1",
                action=actions.call_function(lambda rs: processed.extend(rs)),
                commit_interval_s=0,
            ),
        ])

        runner.run_once()
        assert len(processed) == 1
        # No pending commits — committed immediately
        assert "immediate-g1" not in runner._pending_commit

        runner.stop()
        producer.close()

    def test_deferred_commits_flushed_on_stop(self, bus_with_topic):
        """Deferred commits should be flushed when the runner stops."""
        producer = bus_with_topic.producer()
        producer.send("test", value={"n": 1})
        producer.flush()

        processed = []
        runner = ConsumerRunner(bus_with_topic, [
            ConsumerConfig(
                topic="test",
                group="flush-g1",
                action=actions.call_function(lambda rs: processed.extend(rs)),
                commit_interval_s=60,  # very long interval
            ),
        ])

        runner.run_once()
        assert len(processed) == 1
        assert "flush-g1" in runner._pending_commit

        # Stop should flush pending commits
        runner.stop()
        assert "flush-g1" not in runner._pending_commit

        # Verify offset was actually committed by creating a new consumer
        consumer = bus_with_topic.consumer(group_id="flush-g1", topics=["test"])
        records = consumer.poll(timeout_ms=0)
        assert len(records) == 0  # no new records = offset was committed
        consumer.close()
        producer.close()

    def test_deferred_commit_fires_after_interval(self, bus_with_topic):
        """After the interval elapses, the next poll should commit."""
        producer = bus_with_topic.producer()
        producer.send("test", value={"n": 1})
        producer.flush()

        processed = []
        runner = ConsumerRunner(bus_with_topic, [
            ConsumerConfig(
                topic="test",
                group="timed-g1",
                action=actions.call_function(lambda rs: processed.extend(rs)),
                commit_interval_s=0.01,  # 10ms interval (will elapse quickly)
            ),
        ])

        runner.run_once()
        assert len(processed) == 1

        # Wait for interval to elapse
        time.sleep(0.02)

        # Next run_once should commit (even with no new records)
        runner.run_once()
        assert "timed-g1" not in runner._pending_commit

        runner.stop()
        producer.close()


# ─── Fencing resilience (self-fencing crash-loop regression) ────────────────
#
# July 2026 incident: one fenced consumer (StaleGenerationError) crashed the
# ENTIRE ConsumerRunner; the restart wrapper + dependency-health recovery then
# ran two runner instances that fenced each other forever (8.6k crashes).
# The runner must absorb fencing per-consumer: rejoin that group, keep going.


class TestFencingResilience:
    def test_fenced_consumer_rejoins_without_crashing_runner(self, bus):
        bus.create_topic("events")
        producer = bus.producer()
        producer.send("events", value={"n": 1})
        producer.flush()

        processed = []
        runner = ConsumerRunner(bus, [
            ConsumerConfig(
                topic="events",
                group="fence-me",
                action=actions.call_function(lambda rs: processed.extend(rs)),
            ),
        ])
        runner.run_once()
        assert len(processed) == 1

        # An interloper (one-shot CLI consumer) joins the same group,
        # bumping the generation and fencing the runner's member.
        interloper = bus.consumer(group_id="fence-me", topics=["events"])
        interloper.close()

        # run_once must NOT raise — the runner rejoins the group in place.
        old_consumer = runner._consumers["fence-me"]
        runner.run_once()
        assert runner._consumers["fence-me"] is not old_consumer  # rejoined

        # And the rejoined consumer still processes new records.
        producer.send("events", value={"n": 2})
        producer.flush()
        runner.run_once()
        assert [r.value["n"] for r in processed] == [1, 2]

        runner.stop()
        producer.close()

    def test_fenced_batch_consumer_resets_state_and_repolls(self, bus):
        bus.create_topic("events")
        producer = bus.producer()
        producer.send("events", value={"n": 1})
        producer.flush()

        batches = []
        runner = ConsumerRunner(bus, [
            ConsumerConfig(
                topic="events",
                group="fence-batch",
                batch=BatchConfig(window_seconds=9999),
                action=actions.call_function(lambda rs: batches.append(list(rs))),
            ),
        ])
        runner.run_once()  # record accumulates in batch state, not dispatched
        assert runner._batch_states["fence-batch"].records

        interloper = bus.consumer(group_id="fence-batch", topics=["events"])
        interloper.close()

        runner.run_once()  # fenced → rejoin; batch state must be dropped
        assert not batches or all(b for b in batches)
        # Uncommitted record re-polls under the fresh member on a later round.
        deadline = time.time() + 2
        seen = set()
        while time.time() < deadline:
            runner.run_once()
            seen = {r.value["n"] for b in batches for r in b}
            for st in runner._batch_states.values():
                seen |= {r.value["n"] for r in st.records}
            if 1 in seen:
                break
        assert 1 in seen, "record lost after fencing rejoin"

        runner.stop()
        producer.close()
