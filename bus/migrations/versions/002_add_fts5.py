"""002_add_fts5 — Add FTS5 full-text search indexes.
Creates virtual tables, INSERT/DELETE triggers on all 4 source tables,
and backfills existing data.

Revision ID: 002
Create Date: 2026-03-18
"""
from alembic import op
from bus.search import payload_text_sql, sdk_payload_text_sql

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def _create_fts_tables_and_triggers(connection):
    """Create FTS5 virtual tables and all triggers. Shared by migration and fts-rebuild.

    Args:
        connection: A raw sqlite3.Connection (NOT SQLAlchemy Connection).
                    Callers in Alembic context use op.get_bind().connection.dbapi_connection.
                    Callers in Bus context use self._conn directly.
    """

    # Check FTS5 is available
    opts = [r[0] for r in connection.execute("PRAGMA compile_options").fetchall()]
    if "ENABLE_FTS5" not in opts:
        raise RuntimeError("SQLite compiled without FTS5 support")

    text_expr = payload_text_sql("NEW.payload", "NEW.type")
    sdk_text_expr = sdk_payload_text_sql("NEW.payload")

    connection.executescript(f"""
        -- FTS5 virtual tables
        CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(
            topic, key, type, source, payload_text,
            timestamp UNINDEXED, partition UNINDEXED, offset_val UNINDEXED,
            tokenize='porter unicode61'
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS sdk_events_fts USING fts5(
            session_name, event_type, tool_name, payload_text,
            chat_id UNINDEXED, timestamp UNINDEXED, source_id UNINDEXED,
            tokenize='porter unicode61'
        );

        -- records: INSERT trigger (hot)
        CREATE TRIGGER IF NOT EXISTS records_fts_ai AFTER INSERT ON records
        BEGIN
            INSERT INTO records_fts(topic, key, type, source, payload_text, timestamp, partition, offset_val)
            VALUES (NEW.topic, NEW.key, NEW.type, NEW.source,
                    {text_expr}, NEW.timestamp, NEW.partition, NEW.offset);
        END;

        -- records: DELETE trigger (hot) — MIN(rowid) removes older entry during prune
        CREATE TRIGGER IF NOT EXISTS records_fts_ad AFTER DELETE ON records
        BEGIN
            DELETE FROM records_fts WHERE rowid = (
                SELECT MIN(rowid) FROM records_fts
                WHERE topic = OLD.topic AND partition = OLD.partition AND offset_val = OLD.offset
            );
        END;

        -- records_archive: INSERT trigger
        CREATE TRIGGER IF NOT EXISTS records_archive_fts_ai AFTER INSERT ON records_archive
        BEGIN
            INSERT INTO records_fts(topic, key, type, source, payload_text, timestamp, partition, offset_val)
            VALUES (NEW.topic, NEW.key, NEW.type, NEW.source,
                    {text_expr}, NEW.timestamp, NEW.partition, NEW.offset);
        END;

        -- records_archive: DELETE trigger (defense in depth)
        CREATE TRIGGER IF NOT EXISTS records_archive_fts_ad AFTER DELETE ON records_archive
        BEGIN
            DELETE FROM records_fts WHERE rowid = (
                SELECT MIN(rowid) FROM records_fts
                WHERE topic = OLD.topic AND partition = OLD.partition AND offset_val = OLD.offset
            );
        END;

        -- sdk_events: INSERT trigger (hot)
        CREATE TRIGGER IF NOT EXISTS sdk_events_fts_ai AFTER INSERT ON sdk_events
        BEGIN
            INSERT INTO sdk_events_fts(session_name, event_type, tool_name, payload_text, chat_id, timestamp, source_id)
            VALUES (NEW.session_name, NEW.event_type, NEW.tool_name,
                    {sdk_text_expr},
                    NEW.chat_id, NEW.timestamp, NEW.id);
        END;

        -- sdk_events: DELETE trigger (hot) — MIN(rowid) removes older entry during prune
        CREATE TRIGGER IF NOT EXISTS sdk_events_fts_ad AFTER DELETE ON sdk_events
        BEGIN
            DELETE FROM sdk_events_fts WHERE rowid = (
                SELECT MIN(rowid) FROM sdk_events_fts WHERE source_id = OLD.id
            );
        END;

        -- sdk_events_archive: INSERT trigger
        CREATE TRIGGER IF NOT EXISTS sdk_events_archive_fts_ai AFTER INSERT ON sdk_events_archive
        BEGIN
            INSERT INTO sdk_events_fts(session_name, event_type, tool_name, payload_text, chat_id, timestamp, source_id)
            VALUES (NEW.session_name, NEW.event_type, NEW.tool_name,
                    {sdk_text_expr},
                    NEW.chat_id, NEW.timestamp, NEW.id);
        END;

        -- sdk_events_archive: DELETE trigger (defense in depth)
        CREATE TRIGGER IF NOT EXISTS sdk_events_archive_fts_ad AFTER DELETE ON sdk_events_archive
        BEGIN
            DELETE FROM sdk_events_fts WHERE rowid = (
                SELECT MIN(rowid) FROM sdk_events_fts WHERE source_id = OLD.id
            );
        END;
    """)


def _backfill_fts(connection):
    """Backfill FTS tables from existing data. Shared by migration and fts-rebuild.
    IMPORTANT: Hot tables are backfilled BEFORE archive tables to ensure hot entries
    get lower rowids. This guarantees MIN(rowid) DELETE triggers work correctly.
    Since 004 the archive copy keeps the hot row's FTS entry (no archive insert
    trigger, guarded hot delete), so MIN(rowid) only matters for non-archived
    deletes (delete_topic, archive=0 topics)."""
    text_expr = payload_text_sql("payload", "type")
    sdk_text = sdk_payload_text_sql("payload")

    # Backfill records (hot)
    connection.execute(f"""
        INSERT INTO records_fts(topic, key, type, source, payload_text, timestamp, partition, offset_val)
        SELECT topic, key, type, source, {text_expr}, timestamp, partition, offset
        FROM records
    """)

    # Backfill records_archive
    connection.execute(f"""
        INSERT INTO records_fts(topic, key, type, source, payload_text, timestamp, partition, offset_val)
        SELECT topic, key, type, source, {text_expr}, timestamp, partition, offset
        FROM records_archive
    """)

    # Backfill sdk_events (hot)
    connection.execute(f"""
        INSERT INTO sdk_events_fts(session_name, event_type, tool_name, payload_text, chat_id, timestamp, source_id)
        SELECT session_name, event_type, tool_name, {sdk_text},
            chat_id, timestamp, id
        FROM sdk_events
    """)

    # Backfill sdk_events_archive
    connection.execute(f"""
        INSERT INTO sdk_events_fts(session_name, event_type, tool_name, payload_text, chat_id, timestamp, source_id)
        SELECT session_name, event_type, tool_name, {sdk_text},
            chat_id, timestamp, id
        FROM sdk_events_archive
    """)

    # Optimize after bulk insert
    connection.execute("INSERT INTO records_fts(records_fts) VALUES('optimize')")
    connection.execute("INSERT INTO sdk_events_fts(sdk_events_fts) VALUES('optimize')")


def upgrade():
    """Run via Alembic. Gets raw DBAPI connection for executescript() support."""
    conn = op.get_bind().connection.dbapi_connection
    _create_fts_tables_and_triggers(conn)
    _backfill_fts(conn)


def downgrade():
    op.execute("DROP TRIGGER IF EXISTS records_fts_ai")
    op.execute("DROP TRIGGER IF EXISTS records_fts_ad")
    op.execute("DROP TRIGGER IF EXISTS records_archive_fts_ai")
    op.execute("DROP TRIGGER IF EXISTS records_archive_fts_ad")
    op.execute("DROP TRIGGER IF EXISTS sdk_events_fts_ai")
    op.execute("DROP TRIGGER IF EXISTS sdk_events_fts_ad")
    op.execute("DROP TRIGGER IF EXISTS sdk_events_archive_fts_ai")
    op.execute("DROP TRIGGER IF EXISTS sdk_events_archive_fts_ad")
    op.execute("DROP TABLE IF EXISTS records_fts")
    op.execute("DROP TABLE IF EXISTS sdk_events_fts")
