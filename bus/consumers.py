"""
Consumer Framework: Declarative consumers with pluggable actions.

A consumer is: topic + filter + action (+ optional batching).

Usage:
    from bus import Bus
    from bus.consumers import ConsumerRunner, ConsumerConfig, actions

    bus = Bus()

    runner = ConsumerRunner(bus, [
        ConsumerConfig(
            topic="messages",
            group="chat-router",
            filter=lambda r: r.type == "message.in",
            action=actions.call_function(lambda records: print(f"Got {len(records)} messages")),
        ),
        ConsumerConfig(
            topic="properties",
            group="listing-alerts",
            filter=lambda r: r.payload.get("price", 999999) < 500000,
            batch=BatchConfig(window_seconds=60),
            action=actions.call_function(lambda records: print(f"{len(records)} cheap listings")),
        ),
    ])

    runner.run_once()  # process one round
    runner.run_forever()  # poll loop until interrupted
"""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .bus import Bus, Record, StaleGenerationError

logger = logging.getLogger("bus.consumers")


# ─── Actions ──────────────────────────────────────────────────
#
# Actions are callables that receive a list of Records and do something.
# They are the "what happens after filtering" layer.
#
Action = Callable[[list[Record]], None]


class actions:
    """Built-in action factories. Each returns an Action (callable)."""

    @staticmethod
    def call_function(fn: Callable[[list[Record]], Any]) -> Action:
        """
        Call a Python function with the filtered records.
        The simplest action — total flexibility.

        Usage:
            action=actions.call_function(lambda records: process(records))
        """
        def _action(records: list[Record]) -> None:
            fn(records)
        return _action

    @staticmethod
    def produce(bus: Bus, topic: str, transform: Callable[[Record], dict]) -> Action:
        """
        Produce to another topic (stream processing / chaining).
        Transform function maps each input record to an output value.

        Usage:
            action=actions.produce(bus, "enriched", lambda r: {**r.value, "enriched": True})
        """
        def _action(records: list[Record]) -> None:
            producer = bus.producer()
            for record in records:
                output_value = transform(record)
                producer.send(topic, value=output_value, key=record.key)
            producer.flush()
            producer.close()
        return _action

    @staticmethod
    def produce_batch(bus: Bus, topic: str, transform: Callable[[list[Record]], list[dict]]) -> Action:
        """
        Produce batch to another topic. Transform receives all records and returns
        a list of output values.

        Usage:
            action=actions.produce_batch(bus, "summary", lambda rs: [{"count": len(rs)}])
        """
        def _action(records: list[Record]) -> None:
            producer = bus.producer()
            output_values = transform(records)
            producer.send_many(topic, [{"value": v} for v in output_values])
            producer.flush()
            producer.close()
        return _action

    @staticmethod
    def log(level: str = "info", template: Callable[[Record], str] | None = None) -> Action:
        """
        Log each record. Useful for debugging/monitoring.

        Usage:
            action=actions.log()
            action=actions.log(template=lambda r: f"Got {r.key}: {r.value}")
        """
        log_fn = getattr(logger, level, logger.info)

        def _action(records: list[Record]) -> None:
            for record in records:
                if template:
                    log_fn(template(record))
                else:
                    log_fn(
                        "Record %s[%d]@%d key=%s type=%s source=%s payload=%s",
                        record.topic, record.partition, record.offset,
                        record.key, record.type, record.source,
                        json.dumps(record.payload),
                    )
        return _action

    @staticmethod
    def multi(*action_list: Action) -> Action:
        """
        Run multiple actions in sequence.

        Usage:
            action=actions.multi(
                actions.log(),
                actions.call_function(process),
                actions.produce(bus, "output", transform),
            )
        """
        def _action(records: list[Record]) -> None:
            for act in action_list:
                act(records)
        return _action

    @staticmethod
    def noop() -> Action:
        """
        Do nothing — just acknowledge/commit the records.
        Useful for skipping records you want to mark as consumed.
        """
        def _action(records: list[Record]) -> None:
            pass
        return _action

    @staticmethod
    def dead_letter(bus: Bus, topic: str = "dead-letters") -> Action:
        """
        Send records to a dead-letter topic. Used as error_action for failed processing.

        Usage:
            error_action=actions.dead_letter(bus)
        """
        def _action(records: list[Record]) -> None:
            producer = bus.producer()
            bus.create_topic(topic)  # ensure exists (idempotent)
            for record in records:
                producer.send(
                    topic,
                    payload={
                        "original_topic": record.topic,
                        "original_partition": record.partition,
                        "original_offset": record.offset,
                        "original_key": record.key,
                        "original_type": record.type,
                        "original_source": record.source,
                        "original_payload": record.payload,
                        "error_time": int(time.time() * 1000),
                    },
                    key=record.key,
                    type="dead_letter",
                    source=record.source,
                )
            producer.flush()
            producer.close()
            logger.warning("Sent %d record(s) to dead-letter topic '%s'", len(records), topic)
        return _action


# ─── Batching ─────────────────────────────────────────────────


@dataclass
class BatchConfig:
    """
    Batching configuration for a consumer.

    window_seconds: Accumulate records for this many seconds, then dispatch.
    window_count: Dispatch after accumulating this many records (0 = no count limit).
    Either or both can be set. Dispatch happens when the first threshold is met.
    """
    window_seconds: int = 0
    window_count: int = 0

    def __post_init__(self):
        if self.window_seconds <= 0 and self.window_count <= 0:
            raise ValueError("BatchConfig must have window_seconds > 0 or window_count > 0")


# ─── Consumer Config ──────────────────────────────────────────


@dataclass
class ConsumerConfig:
    """
    Declarative consumer configuration.

    topic: Topic to consume from
    group: Consumer group ID
    filter: Python function to filter records (return True to keep)
    action: What to do with filtered records
    batch: Optional batching config (accumulate before dispatch)
    max_retries: Number of retries on action failure before giving up
    error_action: Action to run on failure (e.g., dead-letter)
    """
    topic: str
    group: str
    action: Action
    filter: Callable[[Record], bool] | None = None
    batch: BatchConfig | None = None
    max_retries: int = 0
    error_action: Action | None = None
    commit_interval_s: float = 0  # 0 = commit every poll. >0 = batch commits (reduces write lock contention)


# ─── Consumer Runner ──────────────────────────────────────────


@dataclass
class _BatchState:
    """Internal state for batch accumulation."""
    records: list[Record] = field(default_factory=list)
    window_start: float = 0.0  # time.monotonic()

    def reset(self):
        self.records = []
        self.window_start = time.monotonic()


class ConsumerRunner:
    """
    Runs multiple consumers in a single poll loop.

    For each configured consumer:
    1. Poll for new records
    2. Apply python filter
    3. If batching: accumulate until window expires or count threshold
    4. Dispatch to action
    5. Commit offsets after successful dispatch
    6. On failure: retry up to max_retries, then run error_action
    """

    def __init__(self, bus: Bus, configs: list[ConsumerConfig]):
        self.bus = bus
        self.configs = configs
        self._consumers: dict[str, Any] = {}  # group -> Consumer
        self._batch_states: dict[str, _BatchState] = {}  # group -> batch state
        self._last_commit: dict[str, float] = {}  # group -> monotonic time of last commit
        self._pending_commit: set[str] = set()  # groups with uncommitted offsets
        self._running = False
        self._init_consumers()

    def _init_consumers(self):
        now = time.monotonic()
        for config in self.configs:
            consumer = self.bus.consumer(
                group_id=config.group,
                topics=[config.topic],
            )
            self._consumers[config.group] = consumer
            # Initialize last commit time so first commit respects the interval
            if config.commit_interval_s > 0:
                self._last_commit[config.group] = now

            if config.batch:
                self._batch_states[config.group] = _BatchState()
                self._batch_states[config.group].reset()

    def run_once(self) -> dict[str, int]:
        """
        Process one round of polling across all consumers.
        Returns dict of {group: records_processed}.
        """
        results = {}
        for config in self.configs:
            count = self._process_consumer(config)
            results[config.group] = count
        return results

    def run_forever(self, poll_interval_ms: int = 100):
        """
        Run the poll loop forever until interrupted.
        Calls run_once() in a loop with sleep between rounds.
        """
        self._running = True
        logger.info("ConsumerRunner starting with %d consumer(s)", len(self.configs))
        try:
            while self._running:
                self.run_once()
                time.sleep(poll_interval_ms / 1000)
        except KeyboardInterrupt:
            logger.info("ConsumerRunner interrupted")
        finally:
            self.stop()

    def stop(self):
        """Stop the runner and close all consumers."""
        self._running = False
        # Flush any pending batches and deferred commits
        for config in self.configs:
            consumer = self._consumers.get(config.group)
            if not consumer:
                continue
            if config.batch and config.group in self._batch_states:
                state = self._batch_states[config.group]
                if state.records:
                    self._dispatch(config, state.records)
                    try:
                        consumer.commit()
                    except StaleGenerationError:
                        pass  # fenced during shutdown — records re-poll next start
                    state.reset()
            # Flush any deferred commits on shutdown
            if config.group in self._pending_commit:
                try:
                    consumer.commit()
                except Exception:
                    pass  # best-effort on shutdown
                self._pending_commit.discard(config.group)

        for consumer in self._consumers.values():
            consumer.close()
        self._consumers.clear()
        self._batch_states.clear()
        self._pending_commit.clear()
        logger.info("ConsumerRunner stopped")

    def _should_commit(self, config: ConsumerConfig) -> bool:
        """Check if this consumer should commit now based on commit_interval_s."""
        if config.commit_interval_s <= 0:
            return True  # commit every poll (legacy behavior)
        now = time.monotonic()
        last = self._last_commit.get(config.group, 0)
        return (now - last) >= config.commit_interval_s

    def _do_commit(self, config: ConsumerConfig, consumer: Any):
        """Commit offsets and update tracking."""
        consumer.commit()
        self._last_commit[config.group] = time.monotonic()
        self._pending_commit.discard(config.group)

    def _rejoin_consumer(self, config: ConsumerConfig):
        """Replace a fenced consumer with a fresh group member.

        A one-shot CLI consumer (bus tail/peek/debug) joining the same group —
        or a second runner instance — bumps the group generation and fences our
        member. Fencing one consumer must NOT crash the whole runner: rejoin
        just that group and carry on. Uncommitted records re-poll under the new
        member, so any accumulated batch state is dropped (not lost — re-read).
        """
        old = self._consumers.pop(config.group, None)
        if old is not None:
            try:
                old.close()  # close() tolerates a fenced commit internally
            except Exception:
                pass
        self._consumers[config.group] = self.bus.consumer(
            group_id=config.group,
            topics=[config.topic],
        )
        if config.batch and config.group in self._batch_states:
            # Batch mode commits at poll time, so accumulated records may
            # already be past the committed offset — dropping them here would
            # lose them for good. Flush them now instead. (If the fence beat
            # the commit, the fresh member re-polls them → possible duplicate
            # dispatch, which batch consumers must tolerate anyway.)
            state = self._batch_states[config.group]
            if state.records:
                try:
                    self._dispatch(config, state.records)
                except Exception:
                    logger.exception(
                        "Batch flush during '%s' rejoin failed — %d record(s) dropped",
                        config.group, len(state.records),
                    )
            state.reset()
        self._pending_commit.discard(config.group)
        logger.warning(
            "Consumer group '%s' fenced by rebalance — rejoined with a fresh member",
            config.group,
        )

    def _process_consumer(self, config: ConsumerConfig) -> int:
        """Process one consumer. Returns number of records dispatched.

        StaleGenerationError (from poll or commit) is handled here by rejoining
        THIS consumer only — it must never crash the whole runner.
        """
        try:
            return self._process_consumer_inner(config)
        except StaleGenerationError:
            self._rejoin_consumer(config)
            return 0

    def _process_consumer_inner(self, config: ConsumerConfig) -> int:
        consumer = self._consumers.get(config.group)
        if not consumer:
            return 0

        records = consumer.poll(timeout_ms=0)
        if not records:
            # Check if batch window expired even without new records
            if config.batch and config.group in self._batch_states:
                self._check_batch_flush(config)
            # Flush pending commits on interval even when idle
            if config.group in self._pending_commit and self._should_commit(config):
                self._do_commit(config, consumer)
            return 0

        # Apply filter
        if config.filter:
            filtered = [r for r in records if config.filter(r)]
        else:
            filtered = records

        if not filtered:
            # Even if all filtered out, commit the offsets (maybe deferred)
            self._pending_commit.add(config.group)
            if self._should_commit(config):
                self._do_commit(config, consumer)
            return 0

        # Batching mode
        if config.batch:
            state = self._batch_states[config.group]
            state.records.extend(filtered)
            self._pending_commit.add(config.group)
            if self._should_commit(config):
                self._do_commit(config, consumer)

            dispatched = self._check_batch_flush(config)
            return dispatched

        # Non-batching: dispatch immediately
        self._dispatch(config, filtered)
        self._pending_commit.add(config.group)
        if self._should_commit(config):
            self._do_commit(config, consumer)
        return len(filtered)

    def _check_batch_flush(self, config: ConsumerConfig) -> int:
        """Check if batch should be flushed. Returns records dispatched (0 if not flushed)."""
        state = self._batch_states[config.group]
        batch = config.batch

        if not state.records:
            return 0

        should_flush = False

        # Check count threshold
        if batch.window_count > 0 and len(state.records) >= batch.window_count:
            should_flush = True

        # Check time threshold
        if batch.window_seconds > 0:
            elapsed = time.monotonic() - state.window_start
            if elapsed >= batch.window_seconds:
                should_flush = True

        if should_flush:
            count = len(state.records)
            self._dispatch(config, state.records)
            state.reset()
            return count

        return 0

    def _dispatch(self, config: ConsumerConfig, records: list[Record]):
        """Dispatch records to the action with retry logic."""
        last_error = None
        for attempt in range(1 + config.max_retries):
            try:
                config.action(records)
                if attempt > 0:
                    logger.info(
                        "Consumer '%s' action succeeded on attempt %d",
                        config.group, attempt + 1,
                    )
                return  # success
            except Exception as e:
                last_error = e
                logger.warning(
                    "Consumer '%s' action failed (attempt %d/%d): %s",
                    config.group, attempt + 1, 1 + config.max_retries, e,
                )

        # All retries exhausted
        logger.error(
            "Consumer '%s' action failed after %d attempt(s): %s",
            config.group, 1 + config.max_retries, last_error,
        )
        if config.error_action:
            try:
                config.error_action(records)
            except Exception as e:
                logger.error("Consumer '%s' error_action also failed: %s", config.group, e)
