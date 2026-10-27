"""
Tests for the write-ahead queue (WAL) system.

Covers:
- inject() write-ahead pattern: bus event before memory queue
- inject() graceful degradation when bus unavailable
- Tuple-based queue format (message_id, text)
- Delivery confirmation after query()
- Sentinel bypass (no WAL for __SHUTDOWN__)
- query_undelivered_messages SQL query
- _is_send_command pattern matching
- Replay logic (dedup, at-most-once, error handling)
"""
import asyncio
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio


# ── _is_send_command tests ───────────────────────────────────────────

from assistant.sdk_session import _is_send_command


class TestIsSendCommand:
    """Test send command detection."""

    def test_send_sms(self):
        assert _is_send_command('~/.claude/skills/sms-assistant/scripts/send-sms "+1234" "hello"')

    def test_send_signal(self):
        assert _is_send_command('~/.claude/skills/signal/scripts/send-signal "+1234" "hello"')

    def test_send_signal_group(self):
        assert _is_send_command('~/.claude/skills/signal/scripts/send-signal-group "id" "hello"')

    def test_reply(self):
        assert _is_send_command('~/.claude/skills/sms-assistant/scripts/reply "hello"')

    def test_non_send_command(self):
        assert not _is_send_command("ls -la")

    def test_grep_command(self):
        assert not _is_send_command("grep 'hello' file.txt")

    def test_echo_command(self):
        assert not _is_send_command('echo "hello world"')

    def test_empty_command(self):
        assert not _is_send_command("")

    def test_partial_match_not_at_path(self):
        """send-sms in a non-path context should still match (substring)."""
        # This is acceptable — the pattern is in a path-like location
        assert _is_send_command("/scripts/send-sms foo")

    def test_bash_with_pipe(self):
        assert not _is_send_command("cat file.txt | grep pattern")


# ── query_undelivered_messages tests ─────────────────────────────────

from assistant.bus_helpers import query_undelivered_messages


class TestQueryUndeliveredMessages:
    """Test bus query for undelivered messages."""

    @pytest.fixture
    def bus_db(self, tmp_path):
        """Create an in-memory bus DB with the records table."""
        db_path = str(tmp_path / "test_bus.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE records (
                id INTEGER PRIMARY KEY,
                topic TEXT NOT NULL,
                type TEXT NOT NULL,
                key TEXT,
                payload TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                source TEXT
            )
        """)
        conn.execute("CREATE INDEX idx_records_topic_type ON records(topic, type)")
        conn.execute("CREATE INDEX idx_records_topic_key ON records(topic, key)")
        conn.commit()
        conn.close()
        return db_path

    def _insert_record(self, db_path, topic, type_, key, payload, timestamp_ms):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO records (topic, type, key, payload, timestamp, source) VALUES (?, ?, ?, ?, ?, ?)",
            (topic, type_, key, json.dumps(payload), timestamp_ms, "test"),
        )
        conn.commit()
        conn.close()

    def test_no_records_returns_empty(self, bus_db):
        result = query_undelivered_messages(bus_db, "chat123")
        assert result == []

    def test_queued_without_delivered_returned(self, bus_db):
        now_ms = int(time.time() * 1000)
        self._insert_record(bus_db, "messages", "message.queued", "chat123", {
            "message_id": "msg-001",
            "chat_id": "chat123",
            "text": "hello",
            "source": "imessage",
        }, now_ms)

        result = query_undelivered_messages(bus_db, "chat123")
        assert len(result) == 1
        assert result[0]["message_id"] == "msg-001"
        assert result[0]["text"] == "hello"

    def test_delivered_message_excluded(self, bus_db):
        now_ms = int(time.time() * 1000)
        self._insert_record(bus_db, "messages", "message.queued", "chat123", {
            "message_id": "msg-001",
            "chat_id": "chat123",
            "text": "hello",
            "source": "imessage",
        }, now_ms)
        self._insert_record(bus_db, "messages", "message.delivered", "chat123", {
            "message_id": "msg-001",
        }, now_ms + 100)

        result = query_undelivered_messages(bus_db, "chat123")
        assert result == []

    def test_different_chat_id_excluded(self, bus_db):
        now_ms = int(time.time() * 1000)
        self._insert_record(bus_db, "messages", "message.queued", "other_chat", {
            "message_id": "msg-001",
            "text": "hello",
            "source": "imessage",
        }, now_ms)

        result = query_undelivered_messages(bus_db, "chat123")
        assert result == []

    def test_old_messages_excluded(self, bus_db):
        old_ms = int((time.time() - 25 * 3600) * 1000)  # 25 hours ago
        self._insert_record(bus_db, "messages", "message.queued", "chat123", {
            "message_id": "msg-001",
            "text": "hello",
            "source": "imessage",
        }, old_ms)

        result = query_undelivered_messages(bus_db, "chat123")
        assert result == []

    def test_sentinel_messages_filtered(self, bus_db):
        now_ms = int(time.time() * 1000)
        self._insert_record(bus_db, "messages", "message.queued", "chat123", {
            "message_id": "msg-001",
            "text": "__SHUTDOWN__",
            "source": "imessage",
        }, now_ms)

        result = query_undelivered_messages(bus_db, "chat123")
        assert result == []

    def test_multiple_undelivered_ordered_by_time(self, bus_db):
        now_ms = int(time.time() * 1000)
        self._insert_record(bus_db, "messages", "message.queued", "chat123", {
            "message_id": "msg-002",
            "text": "second",
            "source": "imessage",
        }, now_ms + 100)
        self._insert_record(bus_db, "messages", "message.queued", "chat123", {
            "message_id": "msg-001",
            "text": "first",
            "source": "imessage",
        }, now_ms)

        result = query_undelivered_messages(bus_db, "chat123")
        assert len(result) == 2
        assert result[0]["text"] == "first"
        assert result[1]["text"] == "second"

    def test_partial_delivery_returns_only_undelivered(self, bus_db):
        now_ms = int(time.time() * 1000)
        self._insert_record(bus_db, "messages", "message.queued", "chat123", {
            "message_id": "msg-001",
            "text": "delivered",
            "source": "imessage",
        }, now_ms)
        self._insert_record(bus_db, "messages", "message.queued", "chat123", {
            "message_id": "msg-002",
            "text": "undelivered",
            "source": "imessage",
        }, now_ms + 100)
        self._insert_record(bus_db, "messages", "message.delivered", "chat123", {
            "message_id": "msg-001",
        }, now_ms + 200)

        result = query_undelivered_messages(bus_db, "chat123")
        assert len(result) == 1
        assert result[0]["message_id"] == "msg-002"

    def test_custom_max_age(self, bus_db):
        """Messages within custom max_age should be returned."""
        two_hours_ago_ms = int((time.time() - 2 * 3600) * 1000)
        self._insert_record(bus_db, "messages", "message.queued", "chat123", {
            "message_id": "msg-001",
            "text": "hello",
            "source": "imessage",
        }, two_hours_ago_ms)

        # 1 hour max_age should exclude it
        result = query_undelivered_messages(bus_db, "chat123", max_age_hours=1)
        assert result == []

        # 3 hour max_age should include it
        result = query_undelivered_messages(bus_db, "chat123", max_age_hours=3)
        assert len(result) == 1


# ── inject() WAL tests ──────────────────────────────────────────────

@pytest.mark.asyncio
class TestInjectWriteAhead:
    """Test inject() write-ahead pattern."""

    async def test_inject_puts_tuple_in_queue(self, sdk_session):
        """inject() should put (message_id, text) tuple in queue."""
        await sdk_session.start()
        await sdk_session.inject("test message")
        # Give run_loop time to dequeue
        await asyncio.sleep(0.2)
        # The mock client should have received the message
        assert sdk_session._client._queries == ["test message"]

    async def test_inject_sentinel_has_none_id(self, sdk_session):
        """Sentinel messages should have None message_id."""
        # Don't start — just test the queue directly
        await sdk_session.inject("__SHUTDOWN__")
        msg_id, text = sdk_session._message_queue.get_nowait()
        assert msg_id is None
        assert text == "__SHUTDOWN__"

    async def test_inject_normal_has_uuid_id(self, sdk_session):
        """Normal messages should have a UUID message_id."""
        # Put directly without starting (avoid run_loop consuming it)
        await sdk_session.inject("hello")
        msg_id, text = sdk_session._message_queue.get_nowait()
        assert msg_id is not None
        assert len(msg_id) == 36  # UUID format
        assert text == "hello"

    async def test_inject_with_producer_emits_queued_event(self, sdk_session):
        """inject() should emit message.queued when producer is available."""
        mock_producer = MagicMock()
        sdk_session._producer = mock_producer

        with patch("assistant.sdk_session.produce_event") as mock_produce:
            await sdk_session.inject("hello")

            mock_produce.assert_called_once()
            args = mock_produce.call_args
            assert args[0][1] == "messages"  # topic
            assert args[0][2] == "message.queued"  # type
            payload = args[0][3]
            assert payload["text"] == "hello"
            assert "message_id" in payload
            assert payload["chat_id"] == sdk_session.chat_id

    async def test_inject_without_producer_still_queues(self, sdk_session):
        """inject() should work without a producer (graceful degradation)."""
        sdk_session._producer = None
        await sdk_session.inject("hello")
        msg_id, text = sdk_session._message_queue.get_nowait()
        assert text == "hello"

    async def test_inject_bus_failure_non_fatal(self, sdk_session):
        """Bus write failure should not prevent message from being queued."""
        mock_producer = MagicMock()
        sdk_session._producer = mock_producer

        with patch("assistant.sdk_session.produce_event", side_effect=Exception("bus down")):
            await sdk_session.inject("hello")

        # Message should still be in queue
        msg_id, text = sdk_session._message_queue.get_nowait()
        assert text == "hello"

    async def test_sentinel_skips_wal(self, sdk_session):
        """Sentinel messages should NOT emit bus events."""
        mock_producer = MagicMock()
        sdk_session._producer = mock_producer

        with patch("assistant.sdk_session.produce_event") as mock_produce:
            await sdk_session.inject("__SHUTDOWN__")
            mock_produce.assert_not_called()


# ── Delivery confirmation tests ──────────────────────────────────────

@pytest.mark.asyncio
class TestDeliveryConfirmation:
    """Test that _run_loop emits message.delivered after successful query."""

    async def test_delivery_event_after_query(self, sdk_session):
        """After successful query(), message.delivered should be emitted."""
        mock_producer = MagicMock()
        sdk_session._producer = mock_producer

        await sdk_session.start()

        produced_events = []

        def capture_event(*args, **kwargs):
            produced_events.append(args)

        with patch("assistant.sdk_session.produce_event", side_effect=capture_event):
            await sdk_session.inject("hello")
            await asyncio.sleep(0.5)

        # Should have both queued and delivered events
        queued = [e for e in produced_events if e[2] == "message.queued"]
        delivered = [e for e in produced_events if e[2] == "message.delivered"]
        assert len(queued) == 1
        assert len(delivered) == 1
        assert delivered[0][3]["message_id"] == queued[0][3]["message_id"]


# ── Replay tests ─────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestReplayUndelivered:
    """Test _replay_undelivered method on SDKBackend."""

    async def test_replay_injects_undelivered_messages(self, sdk_backend):
        """Replay should inject undelivered messages into session."""
        session = await sdk_backend.create_session(
            "Test User", "test:+15555551234", "admin", source="test"
        )

        undelivered = [
            {"message_id": "msg-001", "text": "missed message", "source": "imessage", "timestamp": 123},
        ]

        with patch("assistant.bus_helpers.query_undelivered_messages", return_value=undelivered):
            count = await sdk_backend._replay_undelivered("test:+15555551234", "existing-session-id")

        assert count == 1
        await asyncio.sleep(0.3)
        assert "missed message" in session._client._queries

    async def test_replay_dedup_skips_duplicate_texts(self, sdk_backend):
        """Replay should skip duplicate message texts within the batch."""
        session = await sdk_backend.create_session(
            "Test User", "test:+15555551234", "admin", source="test"
        )

        undelivered = [
            {"message_id": "msg-001", "text": "hello", "source": "imessage", "timestamp": 100},
            {"message_id": "msg-002", "text": "hello", "source": "imessage", "timestamp": 200},
        ]

        with patch("assistant.bus_helpers.query_undelivered_messages", return_value=undelivered):
            count = await sdk_backend._replay_undelivered("test:+15555551234", "existing-session-id")

        assert count == 1  # Only first "hello" replayed

    async def test_replay_returns_zero_when_no_undelivered(self, sdk_backend):
        """Replay should return 0 when nothing to replay."""
        await sdk_backend.create_session(
            "Test User", "test:+15555551234", "admin", source="test"
        )

        with patch("assistant.bus_helpers.query_undelivered_messages", return_value=[]):
            count = await sdk_backend._replay_undelivered("test:+15555551234", None)

        assert count == 0

    async def test_replay_handles_query_failure(self, sdk_backend):
        """Replay should return 0 on query failure."""
        await sdk_backend.create_session(
            "Test User", "test:+15555551234", "admin", source="test"
        )

        with patch("assistant.bus_helpers.query_undelivered_messages", side_effect=Exception("db locked")):
            count = await sdk_backend._replay_undelivered("test:+15555551234", None)

        assert count == 0

    async def test_replay_handles_inject_failure(self, sdk_backend):
        """Replay should continue after individual inject failures."""
        session = await sdk_backend.create_session(
            "Test User", "test:+15555551234", "admin", source="test"
        )

        undelivered = [
            {"message_id": "msg-001", "text": "will fail", "source": "imessage", "timestamp": 100},
            {"message_id": "msg-002", "text": "will succeed", "source": "imessage", "timestamp": 200},
        ]

        call_count = 0
        original_inject = session.inject

        async def failing_inject(text, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("inject failed")
            return await original_inject(text, **kwargs)

        session.inject = failing_inject

        with patch("assistant.bus_helpers.query_undelivered_messages", return_value=undelivered):
            count = await sdk_backend._replay_undelivered("test:+15555551234", "existing-session-id")

        assert count == 1  # Only second message succeeded

    async def test_replay_returns_zero_for_missing_session(self, sdk_backend):
        """Replay should return 0 if session doesn't exist."""
        with patch("assistant.bus_helpers.query_undelivered_messages", return_value=[
            {"message_id": "msg-001", "text": "hello", "source": "imessage", "timestamp": 100},
        ]):
            count = await sdk_backend._replay_undelivered("nonexistent", None)

        assert count == 0

    async def test_replay_emits_delivered_before_inject(self, sdk_backend):
        """At-most-once: delivered event should be emitted BEFORE inject."""
        session = await sdk_backend.create_session(
            "Test User", "test:+15555551234", "admin", source="test"
        )

        mock_producer = MagicMock()
        sdk_backend._producer = mock_producer

        undelivered = [
            {"message_id": "msg-001", "text": "hello", "source": "imessage", "timestamp": 100},
        ]

        event_order = []
        original_inject = session.inject

        async def tracking_inject(text, **kwargs):
            event_order.append(("inject", text))
            return await original_inject(text, **kwargs)

        session.inject = tracking_inject

        def tracking_produce(*args, **kwargs):
            if len(args) >= 3 and args[2] == "message.delivered":
                event_order.append(("delivered", args[3].get("message_id")))

        with patch("assistant.bus_helpers.query_undelivered_messages", return_value=undelivered), \
             patch("assistant.sdk_backend.produce_event", side_effect=tracking_produce):
            await sdk_backend._replay_undelivered("test:+15555551234", "existing-session-id")

        # Delivered should come before inject (at-most-once)
        assert len(event_order) == 2
        assert event_order[0][0] == "delivered"
        assert event_order[1][0] == "inject"

    async def test_replay_caps_at_max_replay(self, sdk_backend):
        """Replay should respect max_replay limit."""
        await sdk_backend.create_session(
            "Test User", "test:+15555551234", "admin", source="test"
        )

        undelivered = [
            {"message_id": f"msg-{i:03d}", "text": f"msg {i}", "source": "imessage", "timestamp": i}
            for i in range(100)
        ]

        with patch("assistant.bus_helpers.query_undelivered_messages", return_value=undelivered):
            count = await sdk_backend._replay_undelivered("test:+15555551234", "session-id", max_replay=5)

        assert count == 5


# ── _is_send_command adversarial tests ───────────────────────────────

class TestIsSendCommandAdversarial:
    """Test send command detection against false positives."""

    def test_echo_containing_send_path(self):
        """echo with send path should NOT match."""
        assert not _is_send_command('echo "/scripts/send-sms"')

    def test_cat_send_script(self):
        """cat of a send script should NOT match."""
        assert not _is_send_command("cat /scripts/send-sms")

    def test_grep_in_send_script(self):
        """grep inside a send script should NOT match."""
        assert not _is_send_command("grep pattern /scripts/send-signal")

    def test_send_sms_debug_variant(self):
        """A different script that contains send-sms should NOT match."""
        assert not _is_send_command("/scripts/send-sms-debug '+1234' 'msg'")

    def test_pipe_to_send(self):
        """Piped command where first token isn't a send script should NOT match."""
        assert not _is_send_command("echo hello | /scripts/send-sms '+1234'")

    def test_actual_send_with_full_path(self):
        """Full path to real send script should match."""
        assert _is_send_command('~/.claude/skills/sms-assistant/scripts/send-sms "+1234" "hello"')

    def test_actual_reply_with_full_path(self):
        assert _is_send_command('~/.claude/skills/sms-assistant/scripts/reply "hello"')

    # ── heredoc bodies must not be parsed as shell (2026-09-24) ──────
    # The detector splits on newlines, so every heredoc body line became its
    # own segment. Writing a file that merely NAMES a send CLI emitted a real
    # message.sent on the live bus. A phantom send advances last_send_at and
    # SUPPRESSES the unanswered-inbound watchdog, so it fails quiet.

    def test_heredoc_body_naming_send_cli(self):
        """Writing a doc that mentions the send CLI is NOT a send."""
        assert not _is_send_command(
            "cat > /tmp/notes.md <<'EOF'\n"
            "~/.claude/skills/sms-assistant/scripts/send-sms is the CLI\n"
            "EOF"
        )

    def test_heredoc_body_with_test_fixture(self):
        """The exact phantom: testing the detector tripped the detector."""
        assert not _is_send_command(
            "cd ~/dispatch && uv run python - <<'PY'\n"
            "real = 'cd ~/x && ~/.claude/skills/sms-assistant/scripts/reply \"hi\"'\n"
            "PY"
        )

    def test_heredoc_unquoted_and_dash_variant(self):
        """<<-EOF with an indented terminator is still stripped."""
        assert not _is_send_command(
            "cat <<-EOF > /tmp/a\n"
            "\t~/.claude/skills/sms-assistant/scripts/reply \"x\"\n"
            "EOF"
        )

    def test_unterminated_heredoc_strips_to_end(self):
        """An unterminated heredoc consumes to EOF, as the shell would."""
        assert not _is_send_command(
            "cat <<'EOF'\n~/.claude/skills/sms-assistant/scripts/reply \"x\""
        )

    def test_real_send_with_heredoc_argument_still_counts(self):
        """The documented group-send form must still register."""
        assert _is_send_command(
            "~/.claude/skills/sms-assistant/scripts/send-sms \"grp\" \"$(cat <<'ENDMSG'\n"
            "body line mentioning ~/.claude/skills/sms-assistant/scripts/reply\n"
            "ENDMSG\n)\""
        )


# ── message.sent emission test ───────────────────────────────────────

@pytest.mark.asyncio
class TestMessageSentEmission:
    """Test message.sent event emission from tool result handler."""

    async def test_send_command_emits_message_sent(self, sdk_session):
        """Bash tool result with send script should emit message.sent."""
        mock_producer = MagicMock()
        mock_producer.send_sdk_event = MagicMock()
        sdk_session._producer = mock_producer

        await sdk_session.start()

        # Simulate: AssistantMessage(ToolUseBlock) → UserMessage(ToolResultBlock) → ResultMessage
        from tests.conftest import (
            FakeToolUseBlock, FakeAssistantMessage, FakeToolResultBlock,
            FakeResultMessage, FakeUserMessage,
        )
        tool_id = "tool_send_1"
        send_cmd = '~/.claude/skills/sms-assistant/scripts/send-sms "+1234" "hello"'

        # UserMessage wraps tool results in the SDK protocol
        tool_result_msg = FakeUserMessage([
            FakeToolResultBlock(tool_use_id=tool_id, content="SENT|+1234", is_error=False),
        ])

        sdk_session._client._responses = [
            FakeAssistantMessage([FakeToolUseBlock("Bash", {"command": send_cmd}, tool_id)]),
            tool_result_msg,
            FakeResultMessage(),
        ]

        produced_events = []

        def capture_event(*args, **kwargs):
            produced_events.append(args)

        with patch("assistant.sdk_session.produce_event", side_effect=capture_event):
            await sdk_session.inject("send a message")
            await asyncio.sleep(1.0)

        sent_events = [e for e in produced_events if len(e) > 2 and e[2] == "message.sent"]
        assert len(sent_events) >= 1, f"Expected message.sent event, got events: {[e[2] for e in produced_events if len(e) > 2]}"
        assert sent_events[0][3]["chat_id"] == sdk_session.chat_id
        assert send_cmd in sent_events[0][3]["command"]


# ── Replay in restart_session test ───────────────────────────────────

@pytest.mark.asyncio
class TestReplayOnRestart:
    """Test that replay fires during restart_session (not just startup)."""

    async def test_restart_session_triggers_replay(self, sdk_backend):
        """restart_session should call _replay_undelivered after recreating."""
        session = await sdk_backend.create_session(
            "Test User", "test:+15555551234", "admin", source="test"
        )

        replay_calls = []
        original_replay = sdk_backend._replay_undelivered

        async def tracking_replay(chat_id, stored_session_id, **kwargs):
            replay_calls.append((chat_id, stored_session_id))
            return await original_replay(chat_id, stored_session_id, **kwargs)

        sdk_backend._replay_undelivered = tracking_replay

        # Restart the session
        new_session = await sdk_backend.restart_session("test:+15555551234")

        assert new_session is not None
        assert len(replay_calls) == 1
        assert replay_calls[0][0] == "test:+15555551234"


class TestSendCommandChained:
    """2026-09-23 regression: `cd <dir> && <send script>` was not detected.

    The documented send idiom is `cd ~/transcripts/<backend>/<chat> && .../reply`
    because the reply CLI infers chat_id from cwd. The old detector read only
    `cmd.split()[0]`, which is `cd`, so real sends never advanced `last_send_at`
    and the unanswered-inbound watchdog fired "you have not sent ANY reply" at a
    session that had replied twice a minute earlier. A false nudge invites
    re-sending — i.e. duplicate texts to the user.
    """

    def test_cd_then_reply_is_a_send(self):
        assert _is_send_command(
            'cd ~/transcripts/imessage/_15555550100 && '
            '~/.claude/skills/sms-assistant/scripts/reply "hello"')

    def test_semicolon_chain_is_a_send(self):
        assert _is_send_command(
            'cd /tmp; ~/.claude/skills/signal/scripts/send-signal "+1234" "hi"')

    def test_env_prefix_is_a_send(self):
        assert _is_send_command(
            'FOO=1 ~/.claude/skills/sms-assistant/scripts/reply "hi"')

    def test_send_sms_after_cd_is_a_send(self):
        assert _is_send_command(
            'cd ~/x && ~/.claude/skills/sms-assistant/scripts/send-sms "+1" "hi"')

    def test_chained_non_send_is_still_not_a_send(self):
        assert not _is_send_command("cd ~/dispatch && git status")
        assert not _is_send_command("cd /tmp && ls -la | grep foo")
