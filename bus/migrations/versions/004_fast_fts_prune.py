"""004_fast_fts_prune — Stop auto-prune from full-scanning the FTS index.

Problem (latency scan 2026-10-26, FIX-001): the hot DELETE triggers from 002
(records_fts_ad / sdk_events_fts_ad) locate the FTS row to delete via
UNINDEXED columns, so every deleted row full-scans records_fts (~570K rows,
~80ms/row). Auto-prune archives+deletes ~1-2K rows in one BEGIN IMMEDIATE,
holding the bus.db write lock for 70-150s every ~4h.

Fix: when a row moves hot -> archive, its FTS row is byte-identical before and
after, so keep the hot-insert FTS row instead of adding a new one and deleting
the old one:
  * drop records_archive_fts_ai / sdk_events_archive_fts_ai (archive inserts no
    longer add FTS rows);
  * guard records_fts_ad / sdk_events_fts_ad with an indexed "was just
    archived" check so they skip archived rows (no scan, no delete);
  * index sdk_events_archive(id) so the sdk guard is an index lookup.

Non-archived deletes (delete_topic, archive=0 topics) still run the original
MIN(rowid) delete body unchanged. records_archive_fts_ad and
sdk_events_archive_fts_ad stay (archives are never pruned; defense in depth).

The records guard also matches on timestamp, so a hot row that collides with an
older, different archive row on (topic, partition, offset) (INSERT OR IGNORE
skipped it) falls back to the original delete and keeps FTS counts in sync.
sdk_events ids are AUTOINCREMENT and never reused, so id alone is exact.

Revision ID: 004
Create Date: 2026-10-26
"""
import importlib

from alembic import op

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def _apply_fast_prune_triggers(connection):
    """Replace 002's archive/delete FTS triggers with the fast-prune variants.

    Shared by upgrade() and Bus.fts_rebuild() (which recreates 002's triggers
    first and must then call this, or the slow triggers come back).

    Args:
        connection: A raw sqlite3.Connection (NOT SQLAlchemy Connection).
    """
    # Delete bodies are verbatim from 002 — only the WHEN guard is new.
    connection.executescript("""
        CREATE INDEX IF NOT EXISTS idx_sdk_archive_id ON sdk_events_archive(id);

        DROP TRIGGER IF EXISTS records_archive_fts_ai;
        DROP TRIGGER IF EXISTS sdk_events_archive_fts_ai;

        DROP TRIGGER IF EXISTS records_fts_ad;
        CREATE TRIGGER records_fts_ad AFTER DELETE ON records
        WHEN NOT EXISTS (
            SELECT 1 FROM records_archive
            WHERE topic = OLD.topic AND partition = OLD.partition
              AND offset = OLD.offset AND timestamp = OLD.timestamp
        )
        BEGIN
            DELETE FROM records_fts WHERE rowid = (
                SELECT MIN(rowid) FROM records_fts
                WHERE topic = OLD.topic AND partition = OLD.partition AND offset_val = OLD.offset
            );
        END;

        DROP TRIGGER IF EXISTS sdk_events_fts_ad;
        CREATE TRIGGER sdk_events_fts_ad AFTER DELETE ON sdk_events
        WHEN NOT EXISTS (
            SELECT 1 FROM sdk_events_archive WHERE id = OLD.id
        )
        BEGIN
            DELETE FROM sdk_events_fts WHERE rowid = (
                SELECT MIN(rowid) FROM sdk_events_fts WHERE source_id = OLD.id
            );
        END;
    """)


def _restore_002_triggers(connection):
    """Undo _apply_fast_prune_triggers: back to 002's original trigger set."""
    connection.executescript("""
        DROP TRIGGER IF EXISTS records_fts_ad;
        DROP TRIGGER IF EXISTS sdk_events_fts_ad;
        DROP INDEX IF EXISTS idx_sdk_archive_id;
    """)
    migration_002 = importlib.import_module("bus.migrations.versions.002_add_fts5")
    # IF NOT EXISTS: recreates only the 4 triggers dropped above / by upgrade.
    migration_002._create_fts_tables_and_triggers(connection)


def upgrade():
    """Run via Alembic. Gets raw DBAPI connection for executescript() support."""
    conn = op.get_bind().connection.dbapi_connection
    _apply_fast_prune_triggers(conn)


def downgrade():
    # FTS content is one row per source row in both schemes, so no rebuild needed.
    conn = op.get_bind().connection.dbapi_connection
    _restore_002_triggers(conn)
