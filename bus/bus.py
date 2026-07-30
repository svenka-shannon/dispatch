"""
Kafka-on-SQLite: A local message bus with full Kafka semantics.

Core concepts (1:1 with Kafka):
- Topics: named categories of events with N partitions
- Partitions: ordered, append-only sequences within a topic
- Producers: write records with key-based partition assignment (murmur2 hash)
- Consumer Groups: coordinate to split partitions, track offsets independently
- Offsets: monotonic per partition, committed after processing
- Timestamps: ALL timestamps in bus.db are Unix milliseconds (ms since epoch)
- Retention: time-based pruning, NOT consumption-based deletion
- Generation IDs: epoch fencing prevents zombie consumers from corrupting offsets
- Rebalance callbacks: onPartitionsRevoked/onPartitionsAssigned

Usage:
    from bus import Bus

    bus = Bus()  # defaults to ~/dispatch/state/bus.db

    # Create topics
    bus.create_topic("messages", partitions=1)
    bus.create_topic("properties", partitions=4)

    # Produce
    producer = bus.producer()
    producer.send("messages", key="+15555550100", value={"type": "message.in", "text": "hello"})

    # Consume
    consumer = bus.consumer(group_id="message-router", topics=["messages"])
    for record in consumer.poll(timeout_ms=100):
        process(record)
        consumer.commit()

    # Replay from beginning
    consumer2 = bus.consumer(group_id="replay", topics=["messages"], auto_offset_reset="earliest")
    consumer2.seek_to_beginning()
"""

import json
import logging
import queue
import sqlite3
import struct
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("bus")

DEFAULT_DB_PATH = Path.home() / "dispatch" / "state" / "bus.db"
DEFAULT_RETENTION_MS = 7 * 24 * 60 * 60 * 1000  # 7 days
DEFAULT_POLL_TIMEOUT_MS = 100
DEFAULT_MAX_POLL_RECORDS = 500
HEARTBEAT_TIMEOUT_MS = 300_000  # 5 minutes (all consumers are in-process, heartbeats are mainly for stale cleanup)
AUTO_PRUNE_INTERVAL = 1000  # prune every N produces
# Archive retention is infinite — archive records are never auto-pruned.
# If manual cleanup is needed, use SQL directly on records_archive/sdk_events_archive.


@dataclass
class Record:
    """A single record from the bus (mirrors Kafka ConsumerRecord + enrichments).

    Kafka-identical fields: topic, partition, offset, timestamp, key, headers
    Our additions: type, source, payload (replaces Kafka's opaque 'value')
    """
    topic: str
    partition: int
    offset: int
    timestamp: int  # unix ms
    key: str | None
    type: str | None  # event type (e.g. "message.in", "session.restart")
    source: str | None  # origin system (e.g. "imessage", "signal", "daemon")
    payload: Any  # JSON-serializable, heterogeneous per type
    headers: dict[str, str] | None = None

    @property
    def value(self) -> Any:
        """Backwards-compatible alias for payload."""
        return self.payload


@dataclass
class TopicPartition:
    """A topic + partition pair."""
    topic: str
    partition: int


# Type alias for rebalance listener callbacks
RebalanceListener = Callable[[list[TopicPartition]], None]


class Bus:
    """
    The central bus. Creates the database, manages topics, and spawns
    producers/consumers.

    Thread safety: Each Bus instance owns a single SQLite connection.
    For multi-threaded use, create separate Bus instances (separate connections).
    For multi-process use, each process should create its own Bus instance.
    """

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = self._connect()
        self._run_migrations()
        self._init_schema()
        self._produce_count = 0

    def _run_migrations(self):
        """Run pending Alembic migrations. Called on every daemon start."""
        try:
            from alembic.config import Config
            from alembic import command
            from sqlalchemy import create_engine

            alembic_cfg = Config(str(Path(__file__).parent / "alembic.ini"))
            engine = create_engine(
                f"sqlite:///{self.db_path}",
                connect_args={"timeout": 5},
            )

            # Pass the engine connection to env.py (avoids creating a second connection)
            with engine.connect() as conn:
                alembic_cfg.attributes["connection"] = conn
                command.upgrade(alembic_cfg, "head")

        except Exception as e:
            logger.error(f"Migration failed (non-fatal, continuing without FTS): {e}")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.db_path),
            isolation_level=None,  # autocommit by default, explicit BEGIN for transactions
            check_same_thread=False,
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")  # 30s to survive prune/vacuum locks
        conn.execute("PRAGMA cache_size=-2000")  # 2MB cache (DB is ~2MB, 64MB was 218x overkill)
        conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        return conn

    def _init_schema(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS topics (
                name TEXT PRIMARY KEY,
                partitions INTEGER NOT NULL DEFAULT 1,
                retention_ms INTEGER NOT NULL DEFAULT 604800000,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS records (
                topic TEXT NOT NULL,
                partition INTEGER NOT NULL,
                offset INTEGER NOT NULL,
                timestamp INTEGER NOT NULL,
                key TEXT,
                type TEXT,
                source TEXT,
                payload TEXT NOT NULL,
                headers TEXT,
                PRIMARY KEY (topic, partition, offset)
            ) WITHOUT ROWID;

            CREATE TABLE IF NOT EXISTS consumer_offsets (
                group_id TEXT NOT NULL,
                topic TEXT NOT NULL,
                partition INTEGER NOT NULL,
                committed_offset INTEGER NOT NULL,
                generation INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (group_id, topic, partition)
            ) WITHOUT ROWID;

            CREATE TABLE IF NOT EXISTS consumer_members (
                group_id TEXT NOT NULL,
                consumer_id TEXT NOT NULL,
                generation INTEGER NOT NULL DEFAULT 0,
                assigned_partitions TEXT,
                last_heartbeat INTEGER NOT NULL,
                PRIMARY KEY (group_id, consumer_id)
            ) WITHOUT ROWID;

            CREATE TABLE IF NOT EXISTS consumer_groups (
                group_id TEXT PRIMARY KEY,
                generation INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_records_topic_ts
                ON records(topic, timestamp);

            CREATE INDEX IF NOT EXISTS idx_records_key
                ON records(topic, key) WHERE key IS NOT NULL;
        """)

        # Migrate existing databases: add type, source columns and rename value -> payload
        self._migrate_schema()

        # Create indexes on new columns (after migration ensures they exist)
        self._conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_records_type
                ON records(topic, type) WHERE type IS NOT NULL;

            CREATE INDEX IF NOT EXISTS idx_records_source
                ON records(topic, source) WHERE source IS NOT NULL;
        """)

        # SDK events auxiliary table (tier 2: tool_use, tool_result, text, result, error)
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS sdk_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER NOT NULL,
                session_name TEXT NOT NULL,
                chat_id TEXT,
                event_type TEXT NOT NULL,
                tool_name TEXT,
                tool_use_id TEXT,
                duration_ms REAL,
                is_error INTEGER DEFAULT 0,
                payload TEXT,
                num_turns INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_sdk_session
                ON sdk_events(session_name, timestamp);
            CREATE INDEX IF NOT EXISTS idx_sdk_type
                ON sdk_events(event_type);
            CREATE INDEX IF NOT EXISTS idx_sdk_tool
                ON sdk_events(tool_name) WHERE tool_name IS NOT NULL;
        """)

        # Archive tables — prune moves records here instead of deleting.
        # Per-topic archive control via topics.archive column (default=1).
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS records_archive (
                topic TEXT NOT NULL,
                partition INTEGER NOT NULL,
                offset INTEGER NOT NULL,
                timestamp INTEGER NOT NULL,
                key TEXT,
                type TEXT,
                source TEXT,
                payload TEXT NOT NULL,
                headers TEXT,
                archived_at INTEGER NOT NULL,
                PRIMARY KEY (topic, partition, offset)
            ) WITHOUT ROWID;

            CREATE INDEX IF NOT EXISTS idx_archive_topic_ts
                ON records_archive(topic, timestamp);
            CREATE INDEX IF NOT EXISTS idx_archive_type
                ON records_archive(topic, type) WHERE type IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_archive_key
                ON records_archive(topic, key) WHERE key IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_archive_archived_at
                ON records_archive(archived_at);

            CREATE TABLE IF NOT EXISTS sdk_events_archive (
                id INTEGER NOT NULL,
                timestamp INTEGER NOT NULL,
                session_name TEXT NOT NULL,
                chat_id TEXT,
                event_type TEXT NOT NULL,
                tool_name TEXT,
                tool_use_id TEXT,
                duration_ms REAL,
                is_error INTEGER DEFAULT 0,
                payload TEXT,
                num_turns INTEGER,
                archived_at INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_sdk_archive_session
                ON sdk_events_archive(session_name, timestamp);
            CREATE INDEX IF NOT EXISTS idx_sdk_archive_tool
                ON sdk_events_archive(tool_name) WHERE tool_name IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_sdk_archive_archived_at
                ON sdk_events_archive(archived_at);

            CREATE TABLE IF NOT EXISTS session_states (
                session_name TEXT PRIMARY KEY,
                is_busy INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL
            ) WITHOUT ROWID;

            -- Durable per-topic-partition offset counter. Offsets must NEVER
            -- be reused: deriving next-offset from MAX(records.offset) breaks
            -- once retention archives every row of a topic (offsets restart at
            -- 0 while consumer committed offsets stay high, deafening every
            -- consumer of that topic). This counter only moves forward.
            CREATE TABLE IF NOT EXISTS topic_offsets (
                topic TEXT NOT NULL,
                partition INTEGER NOT NULL,
                next_offset INTEGER NOT NULL,
                PRIMARY KEY (topic, partition)
            ) WITHOUT ROWID;
        """)

        # Migration: add archive column to topics table (defaults to 1 = enabled)
        cursor = self._conn.execute("PRAGMA table_info(topics)")
        topic_columns = {row[1] for row in cursor.fetchall()}
        if "archive" not in topic_columns:
            self._conn.execute("ALTER TABLE topics ADD COLUMN archive INTEGER NOT NULL DEFAULT 1")

    def _migrate_schema(self):
        """Migrate existing databases to the latest schema."""
        # Check if records table has the old 'value' column (needs rename to 'payload')
        cursor = self._conn.execute("PRAGMA table_info(records)")
        columns = {row[1] for row in cursor.fetchall()}

        if "value" in columns and "payload" not in columns:
            # Old schema: rename value -> payload, add type and source columns
            # SQLite doesn't support ALTER COLUMN RENAME, so we recreate the table
            logger.info("Migrating bus schema: value -> payload, adding type/source columns")
            self._conn.executescript("""
                ALTER TABLE records RENAME TO records_old;

                CREATE TABLE records (
                    topic TEXT NOT NULL,
                    partition INTEGER NOT NULL,
                    offset INTEGER NOT NULL,
                    timestamp INTEGER NOT NULL,
                    key TEXT,
                    type TEXT,
                    source TEXT,
                    payload TEXT NOT NULL,
                    headers TEXT,
                    PRIMARY KEY (topic, partition, offset)
                ) WITHOUT ROWID;

                INSERT INTO records (topic, partition, offset, timestamp, key, type, source, payload, headers)
                    SELECT topic, partition, offset, timestamp, key,
                           json_extract(value, '$.type'),
                           json_extract(value, '$.source'),
                           value, headers
                    FROM records_old;

                DROP TABLE records_old;

                CREATE INDEX IF NOT EXISTS idx_records_topic_ts ON records(topic, timestamp);
                CREATE INDEX IF NOT EXISTS idx_records_key ON records(topic, key) WHERE key IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_records_type ON records(topic, type) WHERE type IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_records_source ON records(topic, source) WHERE source IS NOT NULL;
            """)
            logger.info("Bus schema migration complete")
        elif "type" not in columns and "payload" not in columns:
            # Fresh table with no records — schema is already correct from CREATE TABLE
            pass
        elif "type" not in columns and "payload" in columns:
            # Has payload but missing type/source — add them
            self._conn.execute("ALTER TABLE records ADD COLUMN type TEXT")
            self._conn.execute("ALTER TABLE records ADD COLUMN source TEXT")

    def create_topic(self, name: str, partitions: int = 1, retention_ms: int = DEFAULT_RETENTION_MS,
                     archive: bool = True) -> bool:
        """
        Create a topic. Returns True if created, False if already exists.
        Mirrors: kafka-topics --create --topic <name> --partitions <n>
        archive: whether pruned records should be moved to records_archive (default True).
        """
        try:
            self._conn.execute(
                "INSERT INTO topics (name, partitions, retention_ms, created_at, archive) VALUES (?, ?, ?, ?, ?)",
                (name, partitions, retention_ms, _now_ms(), 1 if archive else 0),
            )
            logger.info("Created topic '%s' with %d partition(s)", name, partitions)
            return True
        except sqlite3.IntegrityError:
            # Topic exists — update retention_ms if it changed
            cursor = self._conn.execute(
                "SELECT retention_ms FROM topics WHERE name = ?", (name,)
            )
            row = cursor.fetchone()
            if row and row[0] != retention_ms:
                self._conn.execute(
                    "UPDATE topics SET retention_ms = ? WHERE name = ?",
                    (retention_ms, name),
                )
                logger.info("Updated topic '%s' retention_ms: %d -> %d", name, row[0], retention_ms)
            return False

    def delete_topic(self, name: str) -> bool:
        """Delete a topic and all its records atomically. Returns True if deleted."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = self._conn.execute("DELETE FROM topics WHERE name = ?", (name,))
            if cursor.rowcount > 0:
                self._conn.execute("DELETE FROM records WHERE topic = ?", (name,))
                self._conn.execute("DELETE FROM consumer_offsets WHERE topic = ?", (name,))
                self._conn.execute("COMMIT")
                logger.info("Deleted topic '%s'", name)
                return True
            self._conn.execute("COMMIT")
            return False
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def list_topics(self) -> list[dict[str, Any]]:
        """List all topics with metadata."""
        cursor = self._conn.execute(
            "SELECT name, partitions, retention_ms, created_at, archive FROM topics ORDER BY name"
        )
        return [
            {
                "name": row[0],
                "partitions": row[1],
                "retention_ms": row[2],
                "created_at": row[3],
                "archive": bool(row[4]),
            }
            for row in cursor.fetchall()
        ]

    def topic_info(self, name: str) -> dict[str, Any] | None:
        """Get topic info including partition offsets."""
        cursor = self._conn.execute(
            "SELECT name, partitions, retention_ms, created_at, archive FROM topics WHERE name = ?",
            (name,),
        )
        row = cursor.fetchone()
        if not row:
            return None

        info = {
            "name": row[0],
            "partitions": row[1],
            "retention_ms": row[2],
            "created_at": row[3],
            "archive": bool(row[4]),
            "partition_offsets": {},
            "total_records": 0,
        }

        for p in range(row[1]):
            cursor2 = self._conn.execute(
                "SELECT MAX(offset), COUNT(*) FROM records WHERE topic = ? AND partition = ?",
                (name, p),
            )
            max_offset, count = cursor2.fetchone()
            info["partition_offsets"][p] = max_offset if max_offset is not None else -1
            info["total_records"] += count or 0

        return info

    def topic_partition_count(self, name: str) -> int:
        """Get the number of partitions for a topic.

        Raises ValueError if the topic does not exist (prevents silently
        masking a misconfigured topic name as 'all partitions assigned').
        """
        cursor = self._conn.execute(
            "SELECT partitions FROM topics WHERE name = ?", (name,)
        )
        row = cursor.fetchone()
        if not row:
            raise ValueError(f"Topic '{name}' does not exist")
        return row[0]

    def list_consumer_groups(self) -> list[dict[str, Any]]:
        """List all consumer groups with members and assignments."""
        cursor = self._conn.execute(
            "SELECT group_id, generation FROM consumer_groups ORDER BY group_id"
        )
        groups = []
        for group_id, generation in cursor.fetchall():
            cursor2 = self._conn.execute(
                "SELECT consumer_id, generation, assigned_partitions, last_heartbeat "
                "FROM consumer_members WHERE group_id = ? ORDER BY consumer_id",
                (group_id,),
            )
            members = []
            for cid, gen, partitions_json, heartbeat in cursor2.fetchall():
                members.append({
                    "consumer_id": cid,
                    "generation": gen,
                    "assigned_partitions": json.loads(partitions_json) if partitions_json else [],
                    "last_heartbeat": heartbeat,
                    "alive": heartbeat > _now_ms() - HEARTBEAT_TIMEOUT_MS,
                })
            groups.append({
                "group_id": group_id,
                "generation": generation,
                "members": members,
            })
        return groups

    def update_offset(self, group_id: str, topic: str, partition: int, offset: int):
        """
        Directly update a consumer group's committed offset.
        Used by CLI seek command without needing a full consumer.
        """
        self._conn.execute(
            "INSERT OR REPLACE INTO consumer_offsets "
            "(group_id, topic, partition, committed_offset, generation, updated_at) "
            "VALUES (?, ?, ?, ?, 0, ?)",
            (group_id, topic, partition, offset, _now_ms()),
        )

    def producer(self) -> "Producer":
        """Create a producer for this bus. Each producer uses the bus's connection."""
        return Producer(self)

    def consumer(
        self,
        group_id: str,
        topics: list[str],
        auto_commit: bool = False,
        auto_offset_reset: str = "earliest",
        consumer_id: str | None = None,
        on_partitions_revoked: RebalanceListener | None = None,
        on_partitions_assigned: RebalanceListener | None = None,
        exclusive: bool = False,
    ) -> "Consumer":
        """
        Create a consumer in a consumer group.

        Args:
            group_id: Consumer group ID
            topics: List of topics to subscribe to
            auto_commit: Auto-commit offsets after poll()
            auto_offset_reset: What to do when no committed offset exists:
                "earliest" (default) - start from beginning
                "latest" - start from end (only see new records)
            consumer_id: Unique consumer ID (auto-generated if not provided)
            on_partitions_revoked: Callback when partitions are revoked during rebalance
            on_partitions_assigned: Callback when partitions are assigned during rebalance
            exclusive: If True, purge ALL other members of this group before joining.
                Use for single-consumer groups (e.g. message-router) to prevent
                partition splitting from zombie consumers during rapid restarts.

        Mirrors: KafkaConsumer(group_id=..., topics=[...], auto_offset_reset=...)
        """
        return Consumer(
            bus=self,
            group_id=group_id,
            topics=topics,
            auto_commit=auto_commit,
            auto_offset_reset=auto_offset_reset,
            consumer_id=consumer_id or f"consumer-{uuid.uuid4().hex[:8]}",
            on_partitions_revoked=on_partitions_revoked,
            on_partitions_assigned=on_partitions_assigned,
            exclusive=exclusive,
        )

    def prune(self) -> int:
        """
        Archive and delete records past their topic's retention period.
        Records are moved to records_archive before deletion (if topic.archive=1).
        Archive tables have infinite retention (never auto-pruned).
        Returns number of records deleted from hot tables.
        """
        now = _now_ms()
        cursor = self._conn.execute("SELECT name, retention_ms, archive FROM topics")
        total_deleted = 0
        total_archived = 0
        for topic_name, retention_ms, archive in cursor.fetchall():
            if retention_ms <= 0:
                continue  # retention_ms=0 means infinite retention (no pruning)
            cutoff = now - retention_ms

            # Archive before delete (in implicit transaction per executescript,
            # but we use BEGIN IMMEDIATE for atomicity)
            if archive:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    archived = self._conn.execute(
                        "INSERT OR IGNORE INTO records_archive "
                        "SELECT topic, partition, offset, timestamp, key, type, source, payload, headers, ? "
                        "FROM records WHERE topic = ? AND timestamp < ?",
                        (now, topic_name, cutoff),
                    ).rowcount
                    total_archived += archived
                    self._conn.execute(
                        "DELETE FROM records WHERE topic = ? AND timestamp < ?",
                        (topic_name, cutoff),
                    )
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise
            else:
                result = self._conn.execute(
                    "DELETE FROM records WHERE topic = ? AND timestamp < ?",
                    (topic_name, cutoff),
                )
            total_deleted += self._conn.execute(
                "SELECT changes()"
            ).fetchone()[0] if not archive else archived

        # Archive and prune sdk_events with 3-day retention
        sdk_retention_ms = 3 * 24 * 60 * 60 * 1000
        sdk_cutoff = now - sdk_retention_ms
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            sdk_archived = self._conn.execute(
                "INSERT INTO sdk_events_archive "
                "SELECT id, timestamp, session_name, chat_id, event_type, tool_name, "
                "tool_use_id, duration_ms, is_error, payload, num_turns, ? "
                "FROM sdk_events WHERE timestamp < ?",
                (now, sdk_cutoff),
            ).rowcount
            sdk_deleted = self._conn.execute(
                "DELETE FROM sdk_events WHERE timestamp < ?",
                (sdk_cutoff,),
            ).rowcount
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        total_deleted += sdk_deleted

        # Archive retention is infinite — no pruning of archive tables.

        # Prune stale consumer groups: members with no heartbeat for >1 hour
        # and groups with zero live members. Prevents unbounded metadata growth
        # from CLI peek/debug/tail commands that create one-shot consumers.
        stale_cutoff = now - (60 * 60 * 1000)  # 1 hour
        stale_members = self._conn.execute(
            "DELETE FROM consumer_members WHERE last_heartbeat < ?",
            (stale_cutoff,),
        )
        stale_member_count = stale_members.rowcount

        # Find groups with no remaining members and clean up their offsets
        orphan_groups = self._conn.execute(
            "SELECT group_id FROM consumer_groups "
            "WHERE group_id NOT IN (SELECT DISTINCT group_id FROM consumer_members)"
        ).fetchall()
        orphan_count = 0
        for (group_id,) in orphan_groups:
            self._conn.execute("DELETE FROM consumer_offsets WHERE group_id = ?", (group_id,))
            self._conn.execute("DELETE FROM consumer_groups WHERE group_id = ?", (group_id,))
            orphan_count += 1

        if stale_member_count > 0 or orphan_count > 0:
            logger.info("Pruned %d stale consumer member(s), %d orphan group(s)",
                        stale_member_count, orphan_count)

        if total_deleted > 0 or orphan_count > 0 or total_archived > 0:
            self._conn.execute("PRAGMA incremental_vacuum")
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            logger.info("Pruned %d record(s) (incl. %d sdk_events), archived %d, checkpointed WAL",
                        total_deleted, sdk_deleted, total_archived + sdk_archived)

        # Optimize FTS indexes (merge b-tree segments for query performance)
        # Runs every prune cycle (~every 1000 produces). Fast: <100ms for our data volume.
        try:
            self._conn.execute("INSERT INTO records_fts(records_fts) VALUES('optimize')")
            self._conn.execute("INSERT INTO sdk_events_fts(sdk_events_fts) VALUES('optimize')")
        except Exception:
            pass  # FTS not available yet (pre-migration)

        return total_deleted

    def query_sdk_events(
        self,
        session_name: str | None = None,
        chat_id: str | None = None,
        event_type: str | None = None,
        limit: int = 100,
        since_hours: float = 24,
    ) -> list[dict[str, Any]]:
        """Query sdk_events for debugging and observability.

        Args:
            session_name: Filter by session name.
            chat_id: Filter by chat_id.
            event_type: Filter by event type.
            limit: Max number of results.
            since_hours: Only return events from the last N hours.

        Returns:
            List of event dicts, most recent first.
        """
        cutoff = _now_ms() - int(since_hours * 60 * 60 * 1000)
        conditions = ["timestamp > ?"]
        params: list[Any] = [cutoff]

        if session_name is not None:
            conditions.append("session_name = ?")
            params.append(session_name)
        if chat_id is not None:
            conditions.append("chat_id = ?")
            params.append(chat_id)
        if event_type is not None:
            conditions.append("event_type = ?")
            params.append(event_type)

        where = " AND ".join(conditions)
        params.append(limit)

        cursor = self._conn.execute(
            f"SELECT id, timestamp, session_name, chat_id, event_type, tool_name, "
            f"tool_use_id, duration_ms, is_error, payload, num_turns "
            f"FROM sdk_events WHERE {where} ORDER BY timestamp DESC LIMIT ?",
            params,
        )
        return [
            {
                "id": row[0],
                "timestamp": row[1],
                "session_name": row[2],
                "chat_id": row[3],
                "event_type": row[4],
                "tool_name": row[5],
                "tool_use_id": row[6],
                "duration_ms": row[7],
                "is_error": bool(row[8]),
                "payload": row[9],
                "num_turns": row[10],
            }
            for row in cursor.fetchall()
        ]

    def search(self, query: str, **kwargs) -> list:
        """Full-text search across bus records (hot + archive).
        Delegates to search.search_records(). See that function for kwargs."""
        from bus.search import search_records
        return search_records(self._conn, query, **kwargs)

    def search_sdk(self, query: str, **kwargs) -> list:
        """Full-text search across SDK events (hot + archive).
        Delegates to search.search_sdk_events(). See that function for kwargs."""
        from bus.search import search_sdk_events
        return search_sdk_events(self._conn, query, **kwargs)

    def fts_status(self) -> dict:
        """Compare FTS row counts against source tables to detect drift."""
        records_hot = self._conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        records_archive = self._conn.execute("SELECT COUNT(*) FROM records_archive").fetchone()[0]
        records_fts = self._conn.execute("SELECT COUNT(*) FROM records_fts").fetchone()[0]

        sdk_hot = self._conn.execute("SELECT COUNT(*) FROM sdk_events").fetchone()[0]
        sdk_archive = self._conn.execute("SELECT COUNT(*) FROM sdk_events_archive").fetchone()[0]
        sdk_fts = self._conn.execute("SELECT COUNT(*) FROM sdk_events_fts").fetchone()[0]

        return {
            "records": {
                "hot": records_hot, "archive": records_archive, "fts": records_fts,
                "expected": records_hot + records_archive,
                "drift": records_fts - (records_hot + records_archive),
                "healthy": records_fts == records_hot + records_archive,
            },
            "sdk_events": {
                "hot": sdk_hot, "archive": sdk_archive, "fts": sdk_fts,
                "expected": sdk_hot + sdk_archive,
                "drift": sdk_fts - (sdk_hot + sdk_archive),
                "healthy": sdk_fts == sdk_hot + sdk_archive,
            },
        }

    def fts_rebuild(self):
        """Drop and recreate FTS indexes from scratch.
        Uses the same logic as migration 002 for table creation + backfill.

        IMPORTANT: Must be run with daemon stopped, or acquires exclusive lock
        to prevent concurrent writes from creating duplicates during backfill.
        """
        import importlib
        migration_002 = importlib.import_module("bus.migrations.versions.002_add_fts5")

        # Drop existing triggers and tables first (outside transaction since
        # executescript auto-commits). These are safe to drop without a transaction.
        for trigger in ["records_fts_ai", "records_fts_ad", "records_archive_fts_ai",
                        "records_archive_fts_ad", "sdk_events_fts_ai", "sdk_events_fts_ad",
                        "sdk_events_archive_fts_ai", "sdk_events_archive_fts_ad"]:
            self._conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        self._conn.execute("DROP TABLE IF EXISTS records_fts")
        self._conn.execute("DROP TABLE IF EXISTS sdk_events_fts")

        # Recreate tables and triggers (uses executescript which auto-commits)
        migration_002._create_fts_tables_and_triggers(self._conn)

        # Backfill within an exclusive transaction to prevent concurrent writes
        # from creating duplicates during the backfill window.
        self._conn.execute("BEGIN EXCLUSIVE")
        try:
            migration_002._backfill_fts(self._conn)
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def close(self):
        """Close the database connection."""
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


_SENTINEL = object()  # sentinel for distinguishing "not provided" from None


class Producer:
    """
    Writes records to topics. Handles partition assignment via key hashing.
    Mirrors: KafkaProducer.send()

    Uses murmur2 hashing for key-based partition assignment (same as Kafka).

    Write queue architecture: send() and send_sdk_event() enqueue items to an
    in-memory queue (~microsecond fire-and-forget). A background writer thread
    drains the queue and batches commits for efficiency.
    """

    def __init__(self, bus: Bus):
        self._bus = bus
        # Producer gets its OWN connection to avoid cross-thread corruption.
        # The writer thread does BEGIN IMMEDIATE which conflicts with any
        # main-thread usage of bus._conn (topic queries, prune, etc.).
        self._conn = bus._connect()
        self._lock = threading.Lock()
        # Cache topic metadata to avoid per-record queries
        self._topic_cache: dict[str, int] = {}  # topic_name -> partition_count
        # Cache next offset per (topic, partition) to avoid MAX queries
        self._offset_cache: dict[tuple[str, int], int] = {}  # (topic, partition) -> next_offset
        # Prune guard: prevent concurrent prune threads
        self._prune_running = False

        # Write queue: thread-safe, drained by background writer
        self._write_queue: queue.Queue = queue.Queue()
        self._running = True
        self._batch_count = 0
        self._writer_thread = threading.Thread(
            target=self._writer_loop, daemon=True, name="bus-writer"
        )
        self._writer_thread.start()

    @property
    def queue_depth(self) -> int:
        """Current number of items waiting in the write queue."""
        return self._write_queue.qsize()

    def _writer_loop(self):
        """Background thread that drains the write queue and batches commits."""
        while self._running or not self._write_queue.empty():
            batch: list[dict] = []
            try:
                # Block for first item (with timeout so we can check _running)
                item = self._write_queue.get(timeout=0.1)
                batch.append(item)
                # Drain any additional queued items (non-blocking)
                while not self._write_queue.empty() and len(batch) < 100:
                    try:
                        batch.append(self._write_queue.get_nowait())
                    except queue.Empty:
                        break
                # Write entire batch in one transaction
                self._write_batch(batch)
                self._batch_count += 1
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"Bus writer error: {e}")

    def _write_batch(self, batch: list[dict]):
        """Write a batch of events in a single transaction."""
        with self._lock:
            # Clear offset cache at batch boundary — offsets are only valid
            # within a single BEGIN IMMEDIATE transaction where we hold the write lock.
            # Another producer could have written between batches.
            self._offset_cache.clear()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for item in batch:
                    if item["table"] == "records":
                        self._write_record_item(item)
                    elif item["table"] == "sdk_events":
                        self._write_sdk_event_item(item)
                    elif item["table"] == "session_states":
                        self._write_session_state_item(item)
                self._conn.execute("COMMIT")
            except Exception:
                self._offset_cache.clear()  # Invalidate on error too
                try:
                    self._conn.execute("ROLLBACK")
                except Exception as rb_err:
                    logger.error(f"ROLLBACK failed (connection may be corrupt): {rb_err}")
                raise

        # Auto-prune periodically — run in a separate thread to avoid
        # blocking the writer. Prune does DELETEs + VACUUM + WAL checkpoint
        # which can take hundreds of ms.
        record_count = sum(1 for item in batch if item["table"] == "records")
        if record_count > 0:
            self._bus._produce_count += record_count
            if self._bus._produce_count % AUTO_PRUNE_INTERVAL < record_count:
                if not self._prune_running:
                    self._prune_running = True
                    threading.Thread(
                        target=self._background_prune,
                        daemon=True,
                        name="bus-prune",
                    ).start()

    def _background_prune(self):
        """Run prune in a background thread with its own connection.

        Uses a fresh connection instead of bus._conn to avoid cross-thread
        SQLite corruption. Also guards against concurrent prune threads via
        the _prune_running flag.

        Archives records before deleting (if topic.archive=1).
        """
        try:
            # Create a dedicated connection for pruning — bus._conn is used
            # by other threads (consumers, topic queries) and is not thread-safe
            prune_conn = self._bus._connect()
            try:
                now_ms = int(time.time() * 1000)
                cursor = prune_conn.execute("SELECT name, retention_ms, archive FROM topics")
                total_deleted = 0
                total_archived = 0
                for topic_name, retention_ms, archive in cursor.fetchall():
                    if retention_ms <= 0:
                        continue
                    cutoff = now_ms - retention_ms

                    if archive:
                        # Archive + delete in a single transaction for atomicity
                        prune_conn.execute("BEGIN IMMEDIATE")
                        try:
                            archived = prune_conn.execute(
                                "INSERT OR IGNORE INTO records_archive "
                                "SELECT topic, partition, offset, timestamp, key, type, source, payload, headers, ? "
                                "FROM records WHERE topic = ? AND timestamp < ?",
                                (now_ms, topic_name, cutoff),
                            ).rowcount
                            total_archived += archived
                            prune_conn.execute(
                                "DELETE FROM records WHERE topic = ? AND timestamp < ?",
                                (topic_name, cutoff),
                            )
                            prune_conn.execute("COMMIT")
                        except Exception:
                            prune_conn.execute("ROLLBACK")
                            raise
                        total_deleted += archived
                    else:
                        result = prune_conn.execute(
                            "DELETE FROM records WHERE topic = ? AND timestamp < ?",
                            (topic_name, cutoff),
                        )
                        total_deleted += result.rowcount

                # Archive + prune sdk_events with 3-day retention
                sdk_cutoff = now_ms - (3 * 24 * 60 * 60 * 1000)
                prune_conn.execute("BEGIN IMMEDIATE")
                try:
                    sdk_archived = prune_conn.execute(
                        "INSERT INTO sdk_events_archive "
                        "SELECT id, timestamp, session_name, chat_id, event_type, tool_name, "
                        "tool_use_id, duration_ms, is_error, payload, num_turns, ? "
                        "FROM sdk_events WHERE timestamp < ?",
                        (now_ms, sdk_cutoff),
                    ).rowcount
                    prune_conn.execute(
                        "DELETE FROM sdk_events WHERE timestamp < ?",
                        (sdk_cutoff,),
                    )
                    prune_conn.execute("COMMIT")
                except Exception:
                    prune_conn.execute("ROLLBACK")
                    raise
                total_deleted += sdk_archived
                total_archived += sdk_archived

                # Archive retention is infinite — no pruning of archive tables.

                if total_deleted > 0:
                    prune_conn.execute("PRAGMA incremental_vacuum")
                    prune_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    logger.info("Auto-prune: deleted %d record(s), archived %d",
                                total_deleted, total_archived)

                # Optimize FTS indexes after prune (merge b-tree segments)
                try:
                    prune_conn.execute("INSERT INTO records_fts(records_fts) VALUES('optimize')")
                    prune_conn.execute("INSERT INTO sdk_events_fts(sdk_events_fts) VALUES('optimize')")
                except Exception:
                    pass  # FTS not available yet (pre-migration)
            finally:
                prune_conn.close()
        except Exception as e:
            logger.warning(f"Auto-prune failed: {e}")
        finally:
            self._prune_running = False

    def _get_topic_partitions(self, topic: str) -> int | None:
        """Get partition count for a topic, using cache to avoid per-record queries."""
        if topic in self._topic_cache:
            return self._topic_cache[topic]
        cursor = self._conn.execute(
            "SELECT partitions FROM topics WHERE name = ?", (topic,)
        )
        row = cursor.fetchone()
        if not row:
            return None
        self._topic_cache[topic] = row[0]
        return row[0]

    def _get_next_offset(self, topic: str, partition: int) -> int:
        """Get next offset for a topic+partition, using cache to avoid DB queries.

        Backed by the durable topic_offsets counter so offsets are never reused
        after retention archives a topic's live rows. On first use (or for DBs
        predating the counter) it is seeded from the max of: live records,
        archived records, and any committed consumer offset — so it can never
        regress below what a consumer has already seen.
        """
        cache_key = (topic, partition)
        if cache_key in self._offset_cache:
            offset = self._offset_cache[cache_key]
            self._offset_cache[cache_key] = offset + 1
            return offset
        # First call: read the durable counter, seeding it if absent.
        cursor = self._conn.execute(
            "SELECT next_offset FROM topic_offsets WHERE topic = ? AND partition = ?",
            (topic, partition),
        )
        row = cursor.fetchone()
        if row is not None:
            next_offset = row[0]
        else:
            cursor = self._conn.execute(
                """
                SELECT MAX(
                    COALESCE((SELECT MAX(offset) FROM records
                              WHERE topic = :t AND partition = :p), -1),
                    COALESCE((SELECT MAX(offset) FROM records_archive
                              WHERE topic = :t AND partition = :p), -1),
                    COALESCE((SELECT MAX(committed_offset) FROM consumer_offsets
                              WHERE topic = :t AND partition = :p), -1)
                ) + 1
                """,
                {"t": topic, "p": partition},
            )
            next_offset = cursor.fetchone()[0]
        self._offset_cache[cache_key] = next_offset + 1
        return next_offset

    def _write_record_item(self, item: dict):
        """Write a single record item within an active transaction."""
        topic = item["topic"]
        key = item["key"]
        partition = item.get("partition")

        # Get topic metadata (cached)
        num_partitions = self._get_topic_partitions(topic)
        if num_partitions is None:
            logger.error(f"Topic '{topic}' does not exist, dropping record")
            return

        # Determine partition
        if partition is not None:
            if partition < 0 or partition >= num_partitions:
                logger.error(f"Partition {partition} out of range [0, {num_partitions}), dropping record")
                return
            target_partition = partition
        elif key is not None:
            target_partition = _partition_for_key(key, num_partitions)
        else:
            # Round-robin: pick partition with fewest records
            cursor2 = self._conn.execute(
                "SELECT partition, COUNT(*) as cnt FROM records WHERE topic = ? GROUP BY partition",
                (topic,),
            )
            counts = {p: 0 for p in range(num_partitions)}
            for p, cnt in cursor2.fetchall():
                counts[p] = cnt
            target_partition = min(counts, key=counts.get)

        # Get next offset (cached)
        next_offset = self._get_next_offset(topic, target_partition)

        self._conn.execute(
            "INSERT INTO records (topic, partition, offset, timestamp, key, type, source, payload, headers) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (topic, target_partition, next_offset, item["timestamp"], key,
             item["type"], item["source"], item["payload_json"], item["headers_json"]),
        )

        # Persist the durable offset counter in the same transaction as the
        # record insert. Monotonic guard: never move the counter backwards
        # (e.g. a second producer with a colder cache).
        self._conn.execute(
            "INSERT INTO topic_offsets (topic, partition, next_offset) VALUES (?, ?, ?) "
            "ON CONFLICT(topic, partition) DO UPDATE SET next_offset = excluded.next_offset "
            "WHERE excluded.next_offset > topic_offsets.next_offset",
            (topic, target_partition, next_offset + 1),
        )

        logger.debug("Produced %s[%d]@%d key=%s type=%s", topic, target_partition, next_offset, key, item["type"])

    def _write_sdk_event_item(self, item: dict):
        """Write a single SDK event item within an active transaction."""
        self._conn.execute(
            "INSERT INTO sdk_events "
            "(timestamp, session_name, chat_id, event_type, tool_name, "
            "tool_use_id, duration_ms, is_error, payload, num_turns) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item["timestamp"], item["session_name"], item["chat_id"],
             item["event_type"], item["tool_name"], item["tool_use_id"],
             item["duration_ms"], 1 if item["is_error"] else 0,
             item["payload"], item["num_turns"]),
        )
        logger.debug("SDK event: %s/%s tool=%s", item["session_name"], item["event_type"], item["tool_name"])

    def _write_session_state_item(self, item: dict):
        """Upsert session busy state within an active transaction."""
        self._conn.execute(
            "INSERT INTO session_states (session_name, is_busy, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(session_name) DO UPDATE SET is_busy=excluded.is_busy, updated_at=excluded.updated_at",
            (item["session_name"], 1 if item["is_busy"] else 0, item["updated_at"]),
        )

    def send(
        self,
        topic: str,
        *,
        payload: Any = None,
        key: str | None = None,
        type: str | None = None,
        source: str | None = None,
        headers: dict[str, str] | None = None,
        partition: int | None = None,
        timestamp: int | None = None,
        # Backwards compatibility: accept 'value' as alias for 'payload'
        value: Any = _SENTINEL,
    ) -> None:
        """
        Enqueue a record for writing to a topic. Returns immediately (~microseconds).

        The background writer thread handles the actual SQLite write.
        JSON serialization happens here (in the caller's thread) to keep payloads immutable.

        Note: Previously returned a Record with offset. Now returns None since the
        write is deferred. Callers should not depend on the returned offset.
        """
        # Handle backwards-compatible 'value' parameter
        if value is not _SENTINEL:
            if payload is not None:
                raise ValueError("Cannot specify both 'value' and 'payload'")
            payload = value
        elif payload is None:
            raise ValueError("'payload' is required")

        ts = timestamp or _now_ms()
        payload_json = json.dumps(payload, separators=(",", ":"))
        headers_json = json.dumps(headers, separators=(",", ":")) if headers else None

        self._write_queue.put({
            "table": "records",
            "topic": topic,
            "key": key,
            "type": type,
            "source": source,
            "payload_json": payload_json,
            "headers_json": headers_json,
            "partition": partition,
            "timestamp": ts,
        })

        depth = self._write_queue.qsize()
        if depth > 1000:
            logger.warning(f"Bus write queue depth {depth} — events produced faster than written")

    def send_many(
        self,
        topic: str,
        records: list[dict[str, Any]],
    ) -> None:
        """
        Enqueue multiple records for writing to a topic.
        Each record dict should have: payload or value (required), key/type/source/headers (optional).

        The background writer thread will naturally batch these into a single transaction.
        """
        ts = _now_ms()

        for rec in records:
            payload = rec.get("payload", rec.get("value"))
            headers = rec.get("headers")
            payload_json = json.dumps(payload, separators=(",", ":"))
            headers_json = json.dumps(headers, separators=(",", ":")) if headers else None

            self._write_queue.put({
                "table": "records",
                "topic": topic,
                "key": rec.get("key"),
                "type": rec.get("type"),
                "source": rec.get("source"),
                "payload_json": payload_json,
                "headers_json": headers_json,
                "partition": None,
                "timestamp": ts,
            })

    def send_sdk_event(
        self,
        session_name: str,
        chat_id: str | None,
        event_type: str,
        tool_name: str | None = None,
        tool_use_id: str | None = None,
        duration_ms: float | None = None,
        is_error: bool = False,
        payload: str | None = None,
        num_turns: int | None = None,
    ) -> None:
        """Enqueue an SDK event for writing to the sdk_events table.

        Returns immediately (~microseconds).
        """
        self._write_queue.put({
            "table": "sdk_events",
            "timestamp": _now_ms(),
            "session_name": session_name,
            "chat_id": chat_id,
            "event_type": event_type,
            "tool_name": tool_name,
            "tool_use_id": tool_use_id,
            "duration_ms": duration_ms,
            "is_error": is_error,
            "payload": payload,
            "num_turns": num_turns,
        })

    def set_session_busy(self, session_name: str, is_busy: bool) -> None:
        """Update session busy state in session_states table. Fire-and-forget."""
        self._write_queue.put({
            "table": "session_states",
            "session_name": session_name,
            "is_busy": is_busy,
            "updated_at": _now_ms(),
        })

    def flush(self, timeout: float = 5.0) -> bool:
        """Block until the write queue is fully drained (for testing).

        Returns True if the queue was drained before timeout, False otherwise.
        """
        deadline = time.monotonic() + timeout
        while not self._write_queue.empty():
            if time.monotonic() > deadline:
                return False
            time.sleep(0.01)
        # One more sleep to let the writer finish processing the last batch
        time.sleep(0.02)
        return True

    def close(self):
        """Graceful shutdown — drain queue before closing."""
        self._running = False
        self._writer_thread.join(timeout=5.0)
        if self._writer_thread.is_alive():
            logger.warning("Bus writer thread did not stop within 5 seconds")
        remaining = self._write_queue.qsize()
        if remaining > 0:
            logger.warning(f"Bus writer closed with {remaining} events still in queue")
        # Close our own connection (separate from bus._conn)
        try:
            self._conn.close()
        except Exception:
            pass


class Consumer:
    """
    Reads records from topics as part of a consumer group.
    Tracks offsets per partition. Supports seek and replay.

    Generation IDs (epoch fencing): Each rebalance increments the group's
    generation. Offset commits include the generation and are rejected if
    they come from a stale generation, preventing zombie consumers from
    corrupting progress.

    Mirrors: KafkaConsumer
    """

    def __init__(
        self,
        bus: Bus,
        group_id: str,
        topics: list[str],
        auto_commit: bool = False,
        auto_offset_reset: str = "earliest",
        consumer_id: str = "",
        on_partitions_revoked: RebalanceListener | None = None,
        on_partitions_assigned: RebalanceListener | None = None,
        exclusive: bool = False,
    ):
        self._bus = bus
        # Consumer gets its OWN connection to avoid transaction conflicts
        # with the Producer's write queue thread (which does BEGIN IMMEDIATE
        # on bus._conn). WAL mode supports concurrent reader + writer.
        self._conn = bus._connect()
        self.group_id = group_id
        self.topics = topics
        self.auto_commit = auto_commit
        self.auto_offset_reset = auto_offset_reset
        self.consumer_id = consumer_id
        self._on_revoked = on_partitions_revoked
        self._on_assigned = on_partitions_assigned
        self._assigned: list[TopicPartition] = []
        self._generation: int = 0
        self._pending_offsets: dict[tuple[str, int], int] = {}
        self._exclusive = exclusive
        # Throttle heartbeats: send at most every 10s (HEARTBEAT_TIMEOUT is 30s)
        self._last_heartbeat: int = 0
        self._heartbeat_interval_ms: int = 60_000  # 60s (reduced write pressure, all consumers in-process)
        self._join_group()

    def _join_group(self):
        """
        Join the consumer group atomically and get partition assignments.
        Increments generation ID to fence zombie consumers.
        Fires rebalance callbacks (onPartitionsRevoked, onPartitionsAssigned).
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            now = _now_ms()

            # Ensure consumer group exists and increment generation
            self._conn.execute(
                "INSERT INTO consumer_groups (group_id, generation) VALUES (?, 1) "
                "ON CONFLICT(group_id) DO UPDATE SET generation = generation + 1",
                (self.group_id,),
            )

            # Read new generation
            cursor = self._conn.execute(
                "SELECT generation FROM consumer_groups WHERE group_id = ?",
                (self.group_id,),
            )
            self._generation = cursor.fetchone()[0]

            # Register this consumer with new generation
            self._conn.execute(
                "INSERT OR REPLACE INTO consumer_members "
                "(group_id, consumer_id, generation, assigned_partitions, last_heartbeat) "
                "VALUES (?, ?, ?, '[]', ?)",
                (self.group_id, self.consumer_id, self._generation, now),
            )

            # Remove stale consumers. In exclusive mode, purge ALL other members
            # regardless of heartbeat freshness — this prevents partition splitting
            # from zombie consumers during rapid daemon restarts (where the old
            # consumer's heartbeat is still fresh but it will never process messages).
            if self._exclusive:
                deleted = self._conn.execute(
                    "DELETE FROM consumer_members WHERE group_id = ? AND consumer_id != ?",
                    (self.group_id, self.consumer_id),
                ).rowcount
                if deleted:
                    logger.warning(
                        "Exclusive consumer purged %d other member(s) from group '%s'",
                        deleted, self.group_id,
                    )
            else:
                # Default: only remove consumers that missed heartbeat timeout
                self._conn.execute(
                    "DELETE FROM consumer_members WHERE group_id = ? AND last_heartbeat < ?",
                    (self.group_id, now - HEARTBEAT_TIMEOUT_MS),
                )

            # Get all live consumers in this group
            cursor = self._conn.execute(
                "SELECT consumer_id FROM consumer_members WHERE group_id = ? ORDER BY consumer_id",
                (self.group_id,),
            )
            members = [row[0] for row in cursor.fetchall()]
            my_index = members.index(self.consumer_id)

            # Collect all topic-partitions
            all_tps: list[TopicPartition] = []
            for topic in self.topics:
                cursor2 = self._conn.execute(
                    "SELECT partitions FROM topics WHERE name = ?", (topic,)
                )
                row = cursor2.fetchone()
                if row:
                    for p in range(row[0]):
                        all_tps.append(TopicPartition(topic, p))

            # Fire onPartitionsRevoked for previously assigned partitions
            old_assigned = self._assigned
            if old_assigned and self._on_revoked:
                self._on_revoked(old_assigned)

            # Range assignment (Kafka RangeAssignor):
            # For each topic, assign contiguous blocks of partitions to consumers
            new_assigned: list[TopicPartition] = []
            tps_by_topic: dict[str, list[TopicPartition]] = {}
            for tp in all_tps:
                tps_by_topic.setdefault(tp.topic, []).append(tp)

            for topic_name in sorted(tps_by_topic.keys()):
                tps = tps_by_topic[topic_name]
                n_partitions = len(tps)
                n_members = len(members)
                partitions_per_consumer = n_partitions // n_members
                extra = n_partitions % n_members
                # Consumers with index < extra get one extra partition
                if my_index < extra:
                    start = my_index * (partitions_per_consumer + 1)
                    count = partitions_per_consumer + 1
                else:
                    start = my_index * partitions_per_consumer + extra
                    count = partitions_per_consumer
                new_assigned.extend(tps[start:start + count])

            self._assigned = new_assigned

            # Update assignment record
            assignment_json = json.dumps(
                [{"topic": tp.topic, "partition": tp.partition} for tp in self._assigned]
            )
            self._conn.execute(
                "UPDATE consumer_members SET assigned_partitions = ?, generation = ? "
                "WHERE group_id = ? AND consumer_id = ?",
                (assignment_json, self._generation, self.group_id, self.consumer_id),
            )

            # Initialize offsets for new assignments
            for tp in self._assigned:
                if self.auto_offset_reset == "latest":
                    default_offset = _latest_offset(self._conn, tp.topic, tp.partition)
                else:  # "earliest"
                    default_offset = -1

                self._conn.execute(
                    "INSERT OR IGNORE INTO consumer_offsets "
                    "(group_id, topic, partition, committed_offset, generation, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (self.group_id, tp.topic, tp.partition, default_offset, self._generation, now),
                )

            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

        # Fire onPartitionsAssigned after transaction commits
        if self._assigned and self._on_assigned:
            self._on_assigned(self._assigned)

        logger.info(
            "Consumer %s joined group '%s' generation %d, assigned %d partition(s)",
            self.consumer_id, self.group_id, self._generation, len(self._assigned),
        )

    @property
    def assigned_partitions(self) -> list[TopicPartition]:
        """Currently assigned partitions (read-only view)."""
        return list(self._assigned)

    def poll(
        self,
        timeout_ms: int = DEFAULT_POLL_TIMEOUT_MS,
        max_records: int = DEFAULT_MAX_POLL_RECORDS,
    ) -> list[Record]:
        """
        Poll for new records across assigned partitions.
        Blocks up to timeout_ms waiting for records (like real Kafka).
        Returns records since last committed offset.

        Uses a single batched query across all assigned partitions instead of
        N separate queries, which is significantly faster for multi-partition topics.

        Mirrors: KafkaConsumer.poll()
        """
        # Validate generation BEFORE reading records to prevent stale consumers
        # from processing events they can never commit (zombie fencing at poll time).
        cursor = self._conn.execute(
            "SELECT generation FROM consumer_groups WHERE group_id = ?",
            (self.group_id,),
        )
        row = cursor.fetchone()
        if row and row[0] != self._generation:
            raise StaleGenerationError(
                f"Consumer generation {self._generation} is stale (current: {row[0]}). "
                "This consumer has been fenced by a rebalance."
            )

        deadline = _now_ms() + timeout_ms
        records: list[Record] = []

        while True:
            # Send heartbeat (throttled to avoid excessive writes)
            now_ms = _now_ms()
            if now_ms - self._last_heartbeat >= self._heartbeat_interval_ms:
                self._conn.execute(
                    "UPDATE consumer_members SET last_heartbeat = ? "
                    "WHERE group_id = ? AND consumer_id = ?",
                    (now_ms, self.group_id, self.consumer_id),
                )
                self._last_heartbeat = now_ms

            if not self._assigned:
                break

            # Batch-fetch committed offsets for all assigned partitions in one query
            offsets_by_tp: dict[tuple[str, int], int] = {}
            if self._assigned:
                placeholders = ",".join(
                    f"(?,?)" for _ in self._assigned
                )
                offset_params: list = [self.group_id]
                for tp in self._assigned:
                    offset_params.extend([tp.topic, tp.partition])
                cursor = self._conn.execute(
                    f"SELECT topic, partition, committed_offset FROM consumer_offsets "
                    f"WHERE group_id = ? AND (topic, partition) IN ({placeholders})",
                    offset_params,
                )
                for topic, partition, committed in cursor.fetchall():
                    offsets_by_tp[(topic, partition)] = committed

            # Build a single UNION ALL query across all partitions
            # This replaces N separate SELECT queries with one round-trip
            union_parts = []
            union_params: list = []
            remaining = max_records
            for tp in self._assigned:
                if remaining <= 0:
                    break
                committed = offsets_by_tp.get((tp.topic, tp.partition), -1)
                union_parts.append(
                    "SELECT topic, partition, offset, timestamp, key, type, source, payload, headers "
                    "FROM records "
                    "WHERE topic = ? AND partition = ? AND offset > ? "
                    "ORDER BY offset ASC LIMIT ?"
                )
                union_params.extend([tp.topic, tp.partition, committed, remaining])

            if union_parts:
                # Wrap each partition query to preserve per-partition ordering and limits,
                # then sort the combined result by timestamp and offset
                full_query = " UNION ALL ".join(
                    f"SELECT * FROM ({part})" for part in union_parts
                )
                full_query += " ORDER BY timestamp ASC, topic ASC, partition ASC, offset ASC"
                if max_records > 0:
                    full_query += f" LIMIT {max_records}"

                cursor2 = self._conn.execute(full_query, union_params)

                for topic, partition, offset, ts, key, rec_type, rec_source, payload_json, headers_json in cursor2.fetchall():
                    records.append(
                        Record(
                            topic=topic,
                            partition=partition,
                            offset=offset,
                            timestamp=ts,
                            key=key,
                            type=rec_type,
                            source=rec_source,
                            payload=json.loads(payload_json),
                            headers=json.loads(headers_json) if headers_json else None,
                        )
                    )
                    self._pending_offsets[(topic, partition)] = offset

            # If we got records, return immediately
            if records:
                break

            # If timeout expired, return empty
            if _now_ms() >= deadline:
                break

            # Sleep briefly and retry (10ms intervals, like Kafka's internal poll loop)
            time.sleep(0.01)

        if self.auto_commit and self._pending_offsets:
            self.commit()

        return records

    def commit(self):
        """
        Commit current offsets for all partitions that have been polled.
        Rejects commits from stale generations (zombie fencing).
        Mirrors: KafkaConsumer.commit()
        """
        now = _now_ms()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            # Verify generation inside transaction to prevent TOCTOU race
            cursor = self._conn.execute(
                "SELECT generation FROM consumer_groups WHERE group_id = ?",
                (self.group_id,),
            )
            row = cursor.fetchone()
            if row and row[0] != self._generation:
                self._conn.execute("ROLLBACK")
                logger.warning(
                    "Consumer %s commit rejected: stale generation %d (current: %d)",
                    self.consumer_id, self._generation, row[0],
                )
                self._pending_offsets.clear()
                raise StaleGenerationError(
                    f"Consumer generation {self._generation} is stale (current: {row[0]}). "
                    "This consumer has been fenced by a rebalance."
                )

            self._conn.executemany(
                "INSERT OR REPLACE INTO consumer_offsets "
                "(group_id, topic, partition, committed_offset, generation, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (self.group_id, topic, partition, offset, self._generation, now)
                    for (topic, partition), offset in self._pending_offsets.items()
                ],
            )
            self._conn.execute("COMMIT")
        except StaleGenerationError:
            raise
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        self._pending_offsets.clear()

    def subscribe(self, topics: list[str]):
        """
        Change topic subscription and trigger rebalance.
        Mirrors: KafkaConsumer.subscribe()
        """
        self.topics = topics
        self._join_group()

    def unsubscribe(self):
        """
        Unsubscribe from all topics.
        Mirrors: KafkaConsumer.unsubscribe()
        """
        self.topics = []
        if self._on_revoked and self._assigned:
            self._on_revoked(self._assigned)
        self._assigned = []

    def seek_to_beginning(self, topic: str | None = None, partition: int | None = None):
        """
        Reset offsets to beginning (will re-read all records).
        Mirrors: KafkaConsumer.seek_to_beginning()
        """
        now = _now_ms()
        if topic and partition is not None:
            self._conn.execute(
                "INSERT OR REPLACE INTO consumer_offsets "
                "(group_id, topic, partition, committed_offset, generation, updated_at) "
                "VALUES (?, ?, ?, -1, ?, ?)",
                (self.group_id, topic, partition, self._generation, now),
            )
        else:
            for tp in self._assigned:
                if topic and tp.topic != topic:
                    continue
                self._conn.execute(
                    "INSERT OR REPLACE INTO consumer_offsets "
                    "(group_id, topic, partition, committed_offset, generation, updated_at) "
                    "VALUES (?, ?, ?, -1, ?, ?)",
                    (self.group_id, tp.topic, tp.partition, self._generation, now),
                )

    def seek_to_end(self, topic: str | None = None, partition: int | None = None):
        """
        Set offsets to end (will only see new records).
        Mirrors: KafkaConsumer.seek_to_end()
        """
        now = _now_ms()
        for tp in self._assigned:
            if topic and tp.topic != topic:
                continue
            if partition is not None and tp.partition != partition:
                continue
            max_offset = _latest_offset(self._conn, tp.topic, tp.partition)
            self._conn.execute(
                "INSERT OR REPLACE INTO consumer_offsets "
                "(group_id, topic, partition, committed_offset, generation, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (self.group_id, tp.topic, tp.partition, max_offset, self._generation, now),
            )

    def seek(self, topic: str, partition: int, offset: int):
        """
        Seek to a specific offset. Next poll() will return records starting at this offset.
        Mirrors: KafkaConsumer.seek()
        """
        self._conn.execute(
            "INSERT OR REPLACE INTO consumer_offsets "
            "(group_id, topic, partition, committed_offset, generation, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (self.group_id, topic, partition, offset - 1, self._generation, _now_ms()),
        )

    def seek_to_timestamp(self, topic: str, timestamp: int, partition: int | None = None):
        """
        Seek to the first record at or after the given timestamp.
        If no records match, seeks to end.
        Mirrors: KafkaConsumer.offsetsForTimes() + seek()
        """
        for tp in self._assigned:
            if tp.topic != topic:
                continue
            if partition is not None and tp.partition != partition:
                continue

            cursor = self._conn.execute(
                "SELECT MIN(offset) - 1 FROM records "
                "WHERE topic = ? AND partition = ? AND timestamp >= ?",
                (tp.topic, tp.partition, timestamp),
            )
            row = cursor.fetchone()
            if row and row[0] is not None:
                self._conn.execute(
                    "INSERT OR REPLACE INTO consumer_offsets "
                    "(group_id, topic, partition, committed_offset, generation, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (self.group_id, tp.topic, tp.partition, row[0], self._generation, _now_ms()),
                )
            else:
                # No records at or after timestamp — seek to end
                logger.info(
                    "No records at or after timestamp %d in %s[%d], seeking to end",
                    timestamp, tp.topic, tp.partition,
                )
                self.seek_to_end(tp.topic, tp.partition)

    def committed(self) -> dict[tuple[str, int], int]:
        """Get committed offsets for all assigned partitions."""
        result = {}
        for tp in self._assigned:
            cursor = self._conn.execute(
                "SELECT committed_offset FROM consumer_offsets "
                "WHERE group_id = ? AND topic = ? AND partition = ?",
                (self.group_id, tp.topic, tp.partition),
            )
            row = cursor.fetchone()
            result[(tp.topic, tp.partition)] = row[0] if row else -1
        return result

    def leave_group(self):
        """Leave the consumer group (triggers rebalance for other members)."""
        if self._on_revoked and self._assigned:
            self._on_revoked(self._assigned)
        self._conn.execute(
            "DELETE FROM consumer_members WHERE group_id = ? AND consumer_id = ?",
            (self.group_id, self.consumer_id),
        )
        logger.info("Consumer %s left group '%s'", self.consumer_id, self.group_id)

    def close(self):
        """Commit pending offsets, leave group, and close own connection."""
        if self._pending_offsets:
            try:
                self.commit()
            except StaleGenerationError:
                pass  # can't commit if fenced, just leave
        self.leave_group()
        try:
            self._conn.close()
        except Exception:
            pass


class StaleGenerationError(Exception):
    """Raised when a consumer tries to commit with a stale generation ID."""
    pass


def _latest_offset(conn, topic: str, partition: int) -> int:
    """Latest allocated offset for a topic+partition (-1 if none ever).

    Prefers the durable topic_offsets counter — MAX(records.offset) regresses
    once retention archives a topic's live rows, which would make "latest"
    rewind and replay/skip incorrectly.
    """
    cursor = conn.execute(
        "SELECT next_offset - 1 FROM topic_offsets WHERE topic = ? AND partition = ?",
        (topic, partition),
    )
    row = cursor.fetchone()
    if row is not None:
        return row[0]
    cursor = conn.execute(
        "SELECT COALESCE(MAX(offset), -1) FROM records WHERE topic = ? AND partition = ?",
        (topic, partition),
    )
    return cursor.fetchone()[0]


def _now_ms() -> int:
    """Current time in milliseconds."""
    return int(time.time() * 1000)


def _murmur2(data: bytes) -> int:
    """
    Murmur2 hash (same algorithm as Kafka's DefaultPartitioner).
    Java-compatible implementation matching kafka.common.utils.Utils.murmur2().
    """
    seed = 0x9747B28C
    m = 0x5BD1E995
    r = 24
    length = len(data)
    h = seed ^ length

    # Process 4-byte chunks
    i = 0
    while i + 4 <= length:
        k = struct.unpack_from("<I", data, i)[0]
        k = (k * m) & 0xFFFFFFFF
        k ^= k >> r
        k = (k * m) & 0xFFFFFFFF
        h = (h * m) & 0xFFFFFFFF
        h ^= k
        i += 4

    # Handle remaining bytes
    remaining = length - i
    if remaining >= 3:
        h ^= data[i + 2] << 16
    if remaining >= 2:
        h ^= data[i + 1] << 8
    if remaining >= 1:
        h ^= data[i]
        h = (h * m) & 0xFFFFFFFF

    h ^= h >> 13
    h = (h * m) & 0xFFFFFFFF
    h ^= h >> 15

    # Convert to signed 32-bit (like Java's int)
    if h >= 0x80000000:
        h -= 0x100000000
    return h


def _partition_for_key(key: str, num_partitions: int) -> int:
    """
    Determine partition from key using murmur2 hashing.
    Same algorithm as Kafka's DefaultPartitioner: toPositive(murmur2(key)) % numPartitions
    """
    h = _murmur2(key.encode("utf-8"))
    return (h & 0x7FFFFFFF) % num_partitions
