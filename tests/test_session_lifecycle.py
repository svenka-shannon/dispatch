"""
Tests for session lifecycle management.

Covers:
- Session creation, start, stop
- Session health states (alive, healthy, busy)
- Error counting and max-error termination
- Queue processing (FIFO order)
- Idle timeout behavior
- Session restart preserving registry data
- Duplicate session prevention under lock
- Turn counting and metrics
- Stop hook behavior (dynamic send marker)
"""
import asyncio
import time
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
import pytest_asyncio


@pytest.mark.asyncio
class TestSDKSessionLifecycle:
    """Test SDKSession start/stop/inject."""

    async def test_session_starts(self, sdk_session):
        await sdk_session.start()
        assert sdk_session.is_alive()
        assert sdk_session.running is True
        assert sdk_session._client is not None
        assert sdk_session._client.connected is True

    async def test_session_stops(self, sdk_session):
        await sdk_session.start()
        assert sdk_session.is_alive()
        await sdk_session.stop()
        assert not sdk_session.is_alive()
        assert sdk_session.running is False

    async def test_session_inject_queues_message(self, sdk_session):
        await sdk_session.start()
        await sdk_session.inject("test message")
        # Message should be in the queue or already being processed
        # Give the run loop a chance to pick it up
        await asyncio.sleep(0.2)
        assert sdk_session._client._queries == ["test message"]

    async def test_session_processes_multiple_messages_fifo(self, sdk_session):
        await sdk_session.start()
        await sdk_session.inject("msg1")
        await sdk_session.inject("msg2")
        await sdk_session.inject("msg3")
        # Wait for all to process
        await asyncio.sleep(0.5)
        assert sdk_session._client._queries == ["msg1", "msg2", "msg3"]

    async def test_session_turn_count_increments(self, sdk_session):
        await sdk_session.start()
        assert sdk_session.turn_count == 0
        await sdk_session.inject("msg1")
        await asyncio.sleep(0.3)
        assert sdk_session.turn_count >= 1

    async def test_session_last_activity_updates(self, sdk_session):
        await sdk_session.start()
        before = sdk_session.last_activity
        await asyncio.sleep(0.05)
        await sdk_session.inject("msg")
        await asyncio.sleep(0.3)
        assert sdk_session.last_activity > before

    async def test_double_stop_is_safe(self, sdk_session):
        await sdk_session.start()
        await sdk_session.stop()
        await sdk_session.stop()  # Should not raise


@pytest.mark.asyncio
class TestSessionHealthStates:
    """Test session health checking logic."""

    async def test_alive_after_start(self, sdk_session):
        await sdk_session.start()
        assert sdk_session.is_alive()

    async def test_not_alive_before_start(self, sdk_session):
        assert not sdk_session.is_alive()

    async def test_healthy_when_alive_no_errors(self, sdk_session):
        await sdk_session.start()
        healthy, reason = sdk_session.is_healthy()
        assert healthy
        assert reason == "ok"

    async def test_unhealthy_when_dead(self, sdk_session):
        healthy, reason = sdk_session.is_healthy()
        assert not healthy
        assert reason == "dead"

    async def test_unhealthy_after_3_errors(self, sdk_session):
        await sdk_session.start()
        sdk_session._error_count = 3
        healthy, reason = sdk_session.is_healthy()
        assert not healthy
        assert "error_count" in reason

    async def test_unhealthy_with_stale_queue(self, sdk_session):
        """Messages pending but no activity for 10+ minutes = unhealthy."""
        await sdk_session.start()
        # Manually put something in queue without processing
        from assistant.sdk_session import QueueItem
        await sdk_session._message_queue.put(QueueItem(None, "stale msg"))
        # Backdate last_activity
        sdk_session.last_activity = datetime.now() - timedelta(minutes=15)
        healthy, reason = sdk_session.is_healthy()
        assert not healthy
        assert "stale_queue" in reason

    async def test_healthy_with_recent_queue(self, sdk_session):
        """Messages pending with recent activity = still healthy."""
        await sdk_session.start()
        from assistant.sdk_session import QueueItem
        await sdk_session._message_queue.put(QueueItem(None, "fresh msg"))
        sdk_session.last_activity = datetime.now()
        healthy, reason = sdk_session.is_healthy()
        assert healthy

    async def test_busy_during_processing(self, sdk_session):
        """Session should be busy while processing a query."""
        await sdk_session.start()
        # Make the client slow (query_delay affects both send and receive_messages)
        sdk_session._client._query_delay = 0.5
        await sdk_session.inject("slow msg")
        await asyncio.sleep(0.1)
        assert sdk_session.is_busy
        # Wait for query + receive_messages processing + ResultMessage reset
        await asyncio.sleep(1.0)
        assert not sdk_session.is_busy


@pytest.mark.asyncio
class TestSessionErrorHandling:
    """Test error counting and max-error termination."""

    async def test_error_count_increments_on_failure(self, sdk_session):
        await sdk_session.start()
        sdk_session._client._should_error = True
        await sdk_session.inject("will fail")
        await asyncio.sleep(0.3)
        assert sdk_session._error_count >= 1

    async def test_error_count_resets_on_success(self, sdk_session):
        await sdk_session.start()
        # First, cause an error
        sdk_session._client._should_error = True
        await sdk_session.inject("will fail")
        # Backoff is 5*error_count seconds, so wait for error + 5s backoff
        await asyncio.sleep(6)
        assert sdk_session._error_count >= 1

        # Then succeed
        sdk_session._client._should_error = False
        await sdk_session.inject("will succeed")
        await asyncio.sleep(0.5)
        assert sdk_session._error_count == 0

    async def test_session_dies_after_3_errors(self, sdk_session):
        await sdk_session.start()
        sdk_session._client._should_error = True
        # Inject 3 messages that will all fail
        for i in range(3):
            await sdk_session.inject(f"fail {i}")
        # Backoff: 5s after error 1, 10s after error 2, then dies at 3
        # Total wait: ~5 + 10 + margin
        await asyncio.sleep(18)
        assert not sdk_session.running or sdk_session._error_count >= 3


@pytest.mark.asyncio
class TestStopHook:
    """Test the stop hook sends correct backend-specific reminder."""

    async def test_stop_hook_uses_backend_marker(self, sdk_session):
        """Stop hook should use the test backend's send marker, not hardcoded send-sms."""
        await sdk_session.start()

        class FakeContext:
            response = {"messages": []}

        result = await sdk_session._stop_hook({}, None, FakeContext())
        assert "test-send" in result.get("systemMessage", ""), \
            f"Stop hook should use test-send for test backend, got: {result}"

    async def test_stop_hook_skips_if_already_sent(self, sdk_session):
        await sdk_session.start()

        class FakeContext:
            response = {
                "messages": [
                    {"type": "tool_use", "content": "test-send +15555551234 hello"}
                ]
            }

        result = await sdk_session._stop_hook({}, None, FakeContext())
        assert result == {}  # No reminder needed


@pytest.mark.asyncio
class TestBackendSessionCreation:
    """Test SDKBackend session creation with different backends."""

    async def test_create_individual_session(self, sdk_backend):
        session = await sdk_backend.create_session(
            "Test User", "test:+15555551234", "admin", source="test"
        )
        assert session is not None
        assert session.is_alive()
        assert "test:+15555551234" in sdk_backend.sessions

    async def test_create_session_registers_in_registry(self, sdk_backend, registry):
        await sdk_backend.create_session(
            "Test User", "test:+15555551234", "admin", source="test"
        )
        reg = registry.get("test:+15555551234")
        assert reg is not None
        assert reg["contact_name"] == "Test User"
        assert reg["tier"] == "admin"
        assert reg["source"] == "test"

    async def test_duplicate_session_returns_existing(self, sdk_backend):
        s1 = await sdk_backend.create_session(
            "Test User", "test:+15555551234", "admin", source="test"
        )
        s2 = await sdk_backend.create_session(
            "Test User", "test:+15555551234", "admin", source="test"
        )
        assert s1 is s2  # Same object

    async def test_session_isolation_by_backend(self, sdk_backend):
        """test: and imessage sessions for same phone should be separate."""
        s_test = await sdk_backend.create_session(
            "Test User", "test:+15555551234", "admin", source="test"
        )
        s_imessage = await sdk_backend.create_session(
            "Test User", "+15555551234", "admin", source="imessage"
        )
        assert s_test is not s_imessage
        assert "test:+15555551234" in sdk_backend.sessions
        assert "+15555551234" in sdk_backend.sessions

    async def test_kill_session(self, sdk_backend):
        await sdk_backend.create_session(
            "Test User", "test:+15555551234", "admin", source="test"
        )
        result = await sdk_backend.kill_session("test:+15555551234")
        assert result is True
        assert "test:+15555551234" not in sdk_backend.sessions

    async def test_kill_nonexistent_session(self, sdk_backend):
        result = await sdk_backend.kill_session("nonexistent")
        assert result is False

    async def test_kill_all_sessions(self, sdk_backend):
        await sdk_backend.create_session("U1", "test:+15555550006", "admin", source="test")
        await sdk_backend.create_session("U2", "test:+15555550008", "admin", source="test")
        count = await sdk_backend.kill_all_sessions()
        assert count == 2
        assert len(sdk_backend.sessions) == 0

    async def test_restart_session_preserves_registry(self, sdk_backend, registry):
        await sdk_backend.create_session(
            "Test User", "test:+15555551234", "admin", source="test"
        )
        # Restart
        new_session = await sdk_backend.restart_session("test:+15555551234")
        assert new_session is not None
        assert new_session.is_alive()
        # Registry should still have the entry
        reg = registry.get("test:+15555551234")
        assert reg is not None
        assert reg["contact_name"] == "Test User"


@pytest.mark.asyncio
class TestInjectMessage:
    """Test inject_message with test backend."""

    async def test_inject_creates_session_lazily(self, sdk_backend):
        """inject_message should create session on-demand if missing."""
        result = await sdk_backend.inject_message(
            "Test User", "+15555551234", "hello",
            tier="admin", source="test",
        )
        assert result is True
        # Session should exist now with test: prefix
        assert "test:+15555551234" in sdk_backend.sessions

    async def test_inject_wraps_message_correctly(self, sdk_backend):
        await sdk_backend.inject_message(
            "Test User", "+15555551234", "hello",
            tier="admin", source="test",
        )
        session = sdk_backend.sessions["test:+15555551234"]
        await asyncio.sleep(0.3)
        # The injected system prompt + our message should have been queried
        assert len(session._client._queries) >= 1

    async def test_inject_updates_last_message_time(self, sdk_backend, registry):
        await sdk_backend.inject_message(
            "Test User", "+15555551234", "hello",
            tier="admin", source="test",
        )
        reg = registry.get("test:+15555551234")
        assert reg is not None
        assert "last_message_time" in reg


@pytest.mark.asyncio
class TestIdleSessionReaping:
    """Test idle session killing."""

    async def test_idle_session_killed(self, sdk_backend):
        # Non-admin tier: admin sessions are exempt from idle-kill.
        await sdk_backend.create_session("User", "test:+15555550006", "favorite", source="test")
        session = sdk_backend.sessions["test:+15555550006"]
        # Backdate last_activity
        session.last_activity = datetime.now() - timedelta(hours=3)
        killed = await sdk_backend.check_idle_sessions(2.0)
        assert "test:+15555550006" in killed

    async def test_active_session_not_killed(self, sdk_backend):
        await sdk_backend.create_session("User", "test:+15555550006", "favorite", source="test")
        session = sdk_backend.sessions["test:+15555550006"]
        session.last_activity = datetime.now()  # Recent
        killed = await sdk_backend.check_idle_sessions(2.0)
        assert len(killed) == 0


@pytest.mark.asyncio
class TestZombieSessionCleanup:
    """Test zombie session cleanup on session creation.

    Regression tests for bug: buffer overflow crash leaves orphan subprocess
    (receiver dies but subprocess keeps running). When creating a new session,
    we must kill the old subprocess to prevent duplicates.
    """

    async def test_zombie_cleanup_on_create_session(self, sdk_backend):
        """Creating a session when dead session exists should kill the zombie subprocess."""
        # Create first session
        s1 = await sdk_backend.create_session(
            "Test User", "test:+15555551234", "admin", source="test"
        )
        assert s1.is_alive()

        # Simulate receiver crash: session is in dict, but running=False
        # (real scenario: buffer overflow kills receiver, subprocess orphaned)
        s1.running = False
        # Keep session in dict to simulate the zombie state
        assert "test:+15555551234" in sdk_backend.sessions

        # Track if _kill_subprocess was called
        kill_called = False
        original_kill = s1._kill_subprocess

        async def track_kill():
            nonlocal kill_called
            kill_called = True
            await original_kill()

        s1._kill_subprocess = track_kill

        # Create new session for same chat_id - should clean up zombie
        s2 = await sdk_backend.create_session(
            "Test User", "test:+15555551234", "admin", source="test"
        )

        # Verify zombie was killed and new session created
        assert kill_called, "Zombie subprocess should have been killed"
        assert s2.is_alive()
        assert s2 is not s1, "Should create new session, not return zombie"
        assert sdk_backend.sessions["test:+15555551234"] is s2

    async def test_zombie_cleanup_on_inject_message(self, sdk_backend):
        """inject_message creating session should clean up zombie first."""
        # Create and zombify a session
        s1 = await sdk_backend.create_session(
            "Test User", "test:+15555551234", "admin", source="test"
        )
        s1.running = False

        # Track kill
        kill_called = False
        original_kill = s1._kill_subprocess

        async def track_kill():
            nonlocal kill_called
            kill_called = True
            await original_kill()

        s1._kill_subprocess = track_kill

        # inject_message should trigger zombie cleanup via _create_session_unlocked
        result = await sdk_backend.inject_message(
            "Test User", "+15555551234", "hello",
            tier="admin", source="test",
        )

        assert result is True
        assert kill_called, "Zombie subprocess should have been killed during inject"

    async def test_zombie_cleanup_on_create_group_session(self, sdk_backend):
        """Group session creation should clean up zombie first."""
        chat_id = "abc123def456"

        # Create and zombify a group session
        s1 = await sdk_backend.create_group_session(chat_id, "Test Group", source="test")
        s1.running = False

        kill_called = False
        original_kill = s1._kill_subprocess

        async def track_kill():
            nonlocal kill_called
            kill_called = True
            await original_kill()

        s1._kill_subprocess = track_kill

        # Create new group session - should clean up zombie
        s2 = await sdk_backend.create_group_session(chat_id, "Test Group", source="test")

        assert kill_called, "Zombie subprocess should have been killed"
        assert s2.is_alive()
        assert s2 is not s1

    async def test_zombie_cleanup_on_create_master_session(self, sdk_backend):
        """Master session creation should clean up zombie first."""
        from assistant.common import MASTER_SESSION

        # Create and zombify master session
        s1 = await sdk_backend.create_master_session()
        s1.running = False

        kill_called = False
        original_kill = s1._kill_subprocess

        async def track_kill():
            nonlocal kill_called
            kill_called = True
            await original_kill()

        s1._kill_subprocess = track_kill

        # Create new master session - should clean up zombie
        s2 = await sdk_backend.create_master_session()

        assert kill_called, "Zombie subprocess should have been killed"
        assert s2.is_alive()
        assert s2 is not s1

    async def test_no_duplicate_sessions_after_zombie_cleanup(self, sdk_backend):
        """After zombie cleanup, should have exactly 1 session for chat_id."""
        chat_id = "test:+15555551234"

        # Create and zombify
        s1 = await sdk_backend.create_session("Test User", chat_id, "admin", source="test")
        s1.running = False

        # Create new session (triggers cleanup)
        s2 = await sdk_backend.create_session("Test User", chat_id, "admin", source="test")

        # Should have exactly 1 session
        assert len([k for k in sdk_backend.sessions.keys() if k == chat_id]) == 1
        assert sdk_backend.sessions[chat_id] is s2

    async def test_alive_session_not_killed_during_create(self, sdk_backend):
        """Creating session when alive session exists should return existing, not kill it."""
        s1 = await sdk_backend.create_session(
            "Test User", "test:+15555551234", "admin", source="test"
        )
        assert s1.is_alive()

        # Track if _kill_subprocess was called
        kill_called = False
        original_kill = s1._kill_subprocess

        async def track_kill():
            nonlocal kill_called
            kill_called = True
            await original_kill()

        s1._kill_subprocess = track_kill

        # Try to create again - should return existing, not kill
        s2 = await sdk_backend.create_session(
            "Test User", "test:+15555551234", "admin", source="test"
        )

        assert not kill_called, "Alive session should not be killed"
        assert s2 is s1, "Should return existing alive session"
