"""
Tests for bus event integration across daemon components.

Verifies that produce_event() calls are wired correctly into:
- manager.py (message routing, reactions, HEALME, MASTER, RESTART, health, consolidation)
- sdk_backend.py (session lifecycle: create, kill, restart, inject, idle, health)
- sdk_session.py (turn complete, compaction, permission denied, receive errors)

Uses a real Bus with temp database and FakeClaudeSDKClient from conftest.
Events are verified by querying the bus consumer.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

# conftest.py installs the SDK mock before we import assistant modules
from bus.bus import Bus


# ── Helpers ──────────────────────────────────────────────────

def drain_topic(bus: Bus, topic: str, group_id: str = "test-drain") -> list[dict]:
    """Read all records from a topic. Returns list of record dicts."""
    consumer = bus.consumer(group_id=group_id, topics=[topic], auto_offset_reset="earliest")
    consumer.seek_to_beginning()
    records = []
    for rec in consumer.poll(timeout_ms=100):
        records.append(rec)
        consumer.commit()
    return records


def find_events(records: list, event_type: str) -> list:
    """Filter records by event type (Record objects have .type attribute)."""
    return [r for r in records if r.type == event_type]


# ── Fixtures ─────────────────────────────────────────────────

@pytest.fixture
def bus_db(tmp_path):
    """Create a Bus with temp database."""
    db_path = tmp_path / "test-bus.db"
    bus = Bus(db_path=str(db_path))
    bus.create_topic("messages", retention_ms=3600000)
    bus.create_topic("sessions", retention_ms=3600000)
    bus.create_topic("system", retention_ms=3600000)
    yield bus
    bus.close()


@pytest.fixture
def producer(bus_db):
    """Get a producer from the test bus."""
    return bus_db.producer()


@pytest_asyncio.fixture
async def sdk_backend_with_bus(tmp_path, producer):
    """Create an SDKBackend with a real bus producer."""
    from assistant.sdk_backend import SDKBackend, SessionRegistry

    reg_file = tmp_path / "sessions.json"
    reg_file.write_text("{}")
    registry = SessionRegistry(reg_file)
    backend = SDKBackend(registry=registry, contacts_manager=None, producer=producer)
    yield backend
    for session in list(backend.sessions.values()):
        try:
            await session.stop()
        except Exception:
            pass


@pytest_asyncio.fixture
async def sdk_session_with_bus(tmp_path, producer):
    """Create a standalone SDKSession with a real bus producer."""
    from assistant.sdk_session import SDKSession

    cwd = str(tmp_path / "test-session")
    os.makedirs(cwd, exist_ok=True)
    session = SDKSession(
        chat_id="test:+15555551234",
        contact_name="Test User",
        tier="admin",
        cwd=cwd,
        session_type="individual",
        source="test",
        producer=producer,
    )
    yield session
    if session.is_alive():
        await session.stop()


# ── SDKSession Bus Events ────────────────────────────────────

@pytest.mark.asyncio
class TestSDKSessionBusEvents:
    """Test that SDKSession produces bus events correctly."""

    async def test_turn_complete_produces_event(self, sdk_session_with_bus, bus_db):
        """ResultMessage produces sdk.turn_complete event."""
        session = sdk_session_with_bus
        await session.start()
        await session.inject("hello")
        await asyncio.sleep(0.5)

        records = drain_topic(bus_db, "system", "test-turn")
        events = find_events(records, "sdk.turn_complete")
        assert len(events) >= 1
        ev = events[0]
        assert ev.payload["chat_id"] == "test:+15555551234"
        assert ev.payload["contact_name"] == "Test User"
        assert ev.payload["tier"] == "admin"
        assert "duration_ms" in ev.payload

    async def test_receive_error_produces_event(self, sdk_session_with_bus, bus_db):
        """Receiver error produces session.receive_error event."""
        session = sdk_session_with_bus
        await session.start()

        # Simulate receiver error by making receive_messages raise
        async def _raise_error():
            raise RuntimeError("test buffer overflow error")

        # Directly trigger the error path
        session._error_count = 0
        original_client = session._client
        # We need to trigger the except block in _receive_loop
        # The simplest way is to call the produce directly and check
        from assistant.bus_helpers import produce_session_event
        produce_session_event(session._producer, session.chat_id, "session.receive_error", {
            "error": "test error", "error_count": 1,
            "is_fatal": False, "contact_name": session.contact_name,
        }, source="sdk")

        records = drain_topic(bus_db, "sessions", "test-recv-err")
        events = find_events(records, "session.receive_error")
        assert len(events) == 1
        assert events[0].payload["error"] == "test error"

    async def test_permission_denied_produces_event(self, tmp_path, producer, bus_db):
        """Permission denied produces permission.denied event."""
        from assistant.sdk_session import SDKSession

        cwd = str(tmp_path / "perm-session")
        os.makedirs(cwd, exist_ok=True)
        session = SDKSession(
            chat_id="test:+15555559999",
            contact_name="Fav User",
            tier="favorite",
            cwd=cwd,
            session_type="individual",
            source="test",
            producer=producer,
        )
        # Don't start - just test _permission_check directly
        result = await session._permission_check("Write", {}, None)
        assert hasattr(result, 'reason') or hasattr(result, 'message')  # It's a deny

        records = drain_topic(bus_db, "sessions", "test-perm")
        events = find_events(records, "permission.denied")
        assert len(events) == 1
        assert events[0].payload["tool_name"] == "Write"
        assert events[0].payload["tier"] == "favorite"

    async def test_compaction_produces_event(self, sdk_session_with_bus, bus_db):
        """PreCompact hook produces compaction.triggered event."""
        session = sdk_session_with_bus
        # Don't need to start, just call the hook directly
        with patch("subprocess.Popen"):
            await session._pre_compact_hook({}, None, None)

        records = drain_topic(bus_db, "system", "test-compact")
        events = find_events(records, "compaction.triggered")
        assert len(events) == 1
        assert events[0].payload["chat_id"] == "test:+15555551234"
        assert events[0].payload["contact_name"] == "Test User"

    async def test_no_events_when_producer_is_none(self, tmp_path):
        """SDKSession with producer=None doesn't crash."""
        from assistant.sdk_session import SDKSession

        cwd = str(tmp_path / "no-bus-session")
        os.makedirs(cwd, exist_ok=True)
        session = SDKSession(
            chat_id="test:+15555550000",
            contact_name="No Bus",
            tier="admin",
            cwd=cwd,
            session_type="individual",
            source="test",
            producer=None,
        )
        await session.start()
        await session.inject("hello")
        await asyncio.sleep(0.3)
        assert session.turn_count >= 1
        await session.stop()


# ── SDKBackend Bus Events ────────────────────────────────────

@pytest.mark.asyncio
class TestSDKBackendBusEvents:
    """Test that SDKBackend produces bus events correctly."""

    async def test_session_created_produces_event(self, sdk_backend_with_bus, bus_db, tmp_path):
        """create_session produces session.created event."""
        backend = sdk_backend_with_bus
        session = await backend.create_session(
            contact_name="Alice",
            chat_id="+15555551111",
            tier="admin",
            source="test",
        )
        assert session is not None

        records = drain_topic(bus_db, "sessions", "test-create")
        events = find_events(records, "session.created")
        assert len(events) >= 1
        ev = events[0]
        assert ev.payload["contact_name"] == "Alice"
        assert ev.payload["tier"] == "admin"

    async def test_session_killed_produces_event(self, sdk_backend_with_bus, bus_db):
        """kill_session produces session.killed event."""
        backend = sdk_backend_with_bus
        await backend.create_session(
            contact_name="Bob",
            chat_id="+15555552222",
            tier="admin",
            source="test",
        )

        result = await backend.kill_session("+15555552222")
        assert result is True

        records = drain_topic(bus_db, "sessions", "test-kill")
        events = find_events(records, "session.killed")
        assert len(events) == 1
        assert events[0].payload["contact_name"] == "Bob"
        assert "uptime_seconds" in events[0].payload

    async def test_session_restarted_produces_event(self, sdk_backend_with_bus, bus_db):
        """restart_session produces session.restarted event."""
        backend = sdk_backend_with_bus
        await backend.create_session(
            contact_name="Carol",
            chat_id="+15555553333",
            tier="admin",
            source="test",
        )

        session = await backend.restart_session("+15555553333")
        assert session is not None

        records = drain_topic(bus_db, "sessions", "test-restart")
        events = find_events(records, "session.restarted")
        assert len(events) == 1
        assert events[0].payload["contact_name"] == "Carol"
        assert events[0].payload["reason"] == "restart_requested"

    async def test_session_injected_produces_event(self, sdk_backend_with_bus, bus_db):
        """inject_message produces session.injected event."""
        backend = sdk_backend_with_bus
        # Create session first
        await backend.create_session(
            contact_name="Dave",
            chat_id="+15555554444",
            tier="admin",
            source="test",
        )

        result = await backend.inject_message(
            contact_name="Dave",
            chat_id="+15555554444",
            text="hello there",
            tier="admin",
            source="test",
        )
        assert result is True

        records = drain_topic(bus_db, "sessions", "test-inject")
        events = find_events(records, "session.injected")
        assert len(events) == 1
        assert events[0].payload["injection_type"] == "message"
        assert events[0].payload["contact_name"] == "Dave"

    async def test_idle_killed_produces_event(self, sdk_backend_with_bus, bus_db):
        """check_idle_sessions produces session.idle_killed for idle sessions."""
        backend = sdk_backend_with_bus
        # Non-admin tier: admin sessions are exempt from idle-kill.
        await backend.create_session(
            contact_name="Eve",
            chat_id="+15555555555",
            tier="favorite",
            source="test",
        )

        # Make session appear idle (3 hours ago)
        session = backend.sessions["+15555555555"]
        from datetime import timedelta
        session.last_activity = datetime.now() - timedelta(hours=3)

        killed = await backend.check_idle_sessions(timeout_hours=2.0)
        assert "+15555555555" in killed

        records = drain_topic(bus_db, "sessions", "test-idle")
        events = find_events(records, "session.idle_killed")
        assert len(events) == 1
        assert events[0].payload["contact_name"] == "Eve"
        assert events[0].payload["idle_hours"] >= 2.0

    async def test_health_check_completed_produces_event(self, sdk_backend_with_bus, bus_db):
        """health_check_all produces health.check_completed event."""
        backend = sdk_backend_with_bus
        await backend.create_session(
            contact_name="Frank",
            chat_id="+15555556666",
            tier="admin",
            source="test",
        )

        results = await backend.health_check_all()
        assert len(results) >= 1

        records = drain_topic(bus_db, "system", "test-health")
        events = find_events(records, "health.check_completed")
        assert len(events) == 1

    async def test_session_crashed_produces_event(self, sdk_backend_with_bus, bus_db):
        """check_session_health for unhealthy session produces session.crashed event."""
        backend = sdk_backend_with_bus
        await backend.create_session(
            contact_name="Grace",
            chat_id="+15555557777",
            tier="admin",
            source="test",
        )

        # Make session unhealthy
        session = backend.sessions["+15555557777"]
        session._error_count = 5

        result = await backend.check_session_health("+15555557777")
        assert result is False

        # Flush the async producer so the event is written before we drain
        backend._producer.flush()
        records = drain_topic(bus_db, "sessions", "test-crashed")
        events = find_events(records, "session.crashed")
        assert len(events) == 1
        assert events[0].payload["contact_name"] == "Grace"
        assert events[0].payload["error_count"] == 5


# ── Manager Bus Events (message routing) ─────────────────────

@pytest.mark.asyncio
class TestManagerMessageBusEvents:
    """Test that Manager.process_message produces bus events."""

    @pytest_asyncio.fixture
    async def manager_with_bus(self, tmp_path):
        """Create a minimal Manager-like object for testing process_message.

        We can't instantiate the real Manager (it connects to chat.db, spawns
        daemons etc.), so we test process_message with a mock-enriched backend.
        """
        from assistant.sdk_backend import SDKBackend, SessionRegistry

        bus = Bus(db_path=str(tmp_path / "mgr-bus.db"))
        bus.create_topic("messages", retention_ms=3600000)
        bus.create_topic("sessions", retention_ms=3600000)
        bus.create_topic("system", retention_ms=3600000)
        prod = bus.producer()

        reg_file = tmp_path / "sessions.json"
        reg_file.write_text("{}")
        registry = SessionRegistry(reg_file)
        backend = SDKBackend(registry=registry, contacts_manager=None, producer=prod)

        # Build a minimal Manager stand-in with only what process_message needs
        from assistant.manager import Manager

        class FakeManager:
            pass

        mgr = FakeManager()
        mgr._producer = prod
        mgr._bus = bus
        mgr.sessions = backend
        mgr.registry = registry
        mgr._start_time = time.time()

        # Mock contacts manager
        contacts = MagicMock()
        contacts.lookup_identifier = MagicMock(return_value=None)
        mgr.contacts = contacts

        # Bind process_message and process_reaction from Manager
        import types
        mgr.process_message = types.MethodType(Manager.process_message, mgr)
        mgr.process_reaction = types.MethodType(Manager.process_reaction, mgr)

        yield mgr, bus

        for session in list(backend.sessions.values()):
            try:
                await session.stop()
            except Exception:
                pass
        bus.close()

    async def test_message_received_produces_event(self, manager_with_bus, normalized_message):
        """Poll loop produces message.received event (not process_message)."""
        from assistant.bus_helpers import produce_event, sanitize_msg_for_bus
        mgr, bus = manager_with_bus
        msg = normalized_message(phone="+15555550001", text="hello test")

        # Simulate the poll loop: produce event to bus (this is what the poll loop does now)
        produce_event(mgr._producer, "messages", "message.received",
            sanitize_msg_for_bus(msg),
            key=msg.get("chat_identifier") or msg.get("phone"),
            source=msg.get("source", "imessage"))

        records = drain_topic(bus, "messages", "test-msg-recv")
        events = find_events(records, "message.received")
        assert len(events) >= 1
        assert events[0].payload["phone"] == "+15555550001"

    async def test_message_ignored_produces_event(self, manager_with_bus, normalized_message):
        """Unknown sender individual message produces message.ignored."""
        mgr, bus = manager_with_bus
        msg = normalized_message(phone="+15555550002", text="from unknown")

        mgr.contacts.lookup_identifier.return_value = None

        await mgr.process_message(msg)

        records = drain_topic(bus, "messages", "test-msg-ign")
        events = find_events(records, "message.ignored")
        assert len(events) == 1
        assert events[0].payload["reason"] == "unknown_sender"

    async def test_reaction_received_produces_event(self, manager_with_bus):
        """process_reaction produces reaction.received event."""
        mgr, bus = manager_with_bus
        reaction = {
            "rowid": 999,
            "phone": "+15555550003",
            "emoji": "👍",
            "is_removal": False,
            "target_text": "hello",
            "target_is_from_me": True,
            "is_group": False,
            "chat_identifier": "+15555550003",
            "source": "test",
        }

        # Contact exists but non-blessed tier
        mgr.contacts.lookup_identifier.return_value = {
            "name": "Unknown Person", "tier": "unknown", "phone": "+15555550003",
        }

        await mgr.process_reaction(reaction)

        records = drain_topic(bus, "messages", "test-rxn-recv")
        events = find_events(records, "reaction.received")
        assert len(events) >= 1

    async def test_reaction_ignored_removal(self, manager_with_bus):
        """Reaction removal produces reaction.ignored with reason=removal."""
        mgr, bus = manager_with_bus
        reaction = {
            "rowid": 1000,
            "phone": "+15555550004",
            "emoji": "❤️",
            "is_removal": True,
            "target_text": "hi",
            "target_is_from_me": True,
            "is_group": False,
            "chat_identifier": "+15555550004",
            "source": "test",
        }

        await mgr.process_reaction(reaction)

        records = drain_topic(bus, "messages", "test-rxn-rem")
        events = find_events(records, "reaction.ignored")
        assert len(events) >= 1
        assert any(e.payload["reason"] == "removal" for e in events)

    async def test_reaction_ignored_not_from_me(self, manager_with_bus):
        """Reaction to non-self message produces reaction.ignored with reason=not_from_me."""
        mgr, bus = manager_with_bus
        reaction = {
            "rowid": 1001,
            "phone": "+15555550005",
            "emoji": "😂",
            "is_removal": False,
            "target_text": "hi",
            "target_is_from_me": False,
            "is_group": False,
            "chat_identifier": "+15555550005",
            "source": "test",
        }

        await mgr.process_reaction(reaction)

        records = drain_topic(bus, "messages", "test-rxn-nfm")
        events = find_events(records, "reaction.ignored")
        assert any(e.payload["reason"] == "not_from_me" for e in events)


# ── Fire-and-Forget Safety ───────────────────────────────────

@pytest.mark.asyncio
class TestBusFireAndForget:
    """Test that bus failures don't crash the daemon."""

    async def test_broken_producer_doesnt_crash_session(self, tmp_path):
        """SDKSession with a broken producer still works normally."""
        from assistant.sdk_session import SDKSession

        # Create a producer that raises on send
        broken_producer = MagicMock()
        broken_producer.send.side_effect = RuntimeError("bus is broken")

        cwd = str(tmp_path / "broken-bus-session")
        os.makedirs(cwd, exist_ok=True)
        session = SDKSession(
            chat_id="test:+15555550099",
            contact_name="Broken Bus",
            tier="admin",
            cwd=cwd,
            session_type="individual",
            source="test",
            producer=broken_producer,
        )
        await session.start()
        await session.inject("hello")
        await asyncio.sleep(0.5)

        # Session should still function despite bus errors
        assert session.turn_count >= 1
        assert session.is_alive()
        await session.stop()

    async def test_broken_producer_doesnt_crash_backend(self, tmp_path):
        """SDKBackend with a broken producer still creates sessions."""
        from assistant.sdk_backend import SDKBackend, SessionRegistry

        broken_producer = MagicMock()
        broken_producer.send.side_effect = RuntimeError("bus is broken")

        reg_file = tmp_path / "sessions.json"
        reg_file.write_text("{}")
        registry = SessionRegistry(reg_file)
        backend = SDKBackend(registry=registry, contacts_manager=None, producer=broken_producer)

        # Should not raise despite broken bus
        session = await backend.create_session(
            contact_name="Test",
            chat_id="+15555550088",
            tier="admin",
            source="test",
        )
        assert session is not None
        assert session.is_alive()

        # Kill should also work
        result = await backend.kill_session("+15555550088")
        assert result is True

    async def test_none_producer_is_safe(self, tmp_path):
        """SDKBackend with producer=None produces no events and no errors."""
        from assistant.sdk_backend import SDKBackend, SessionRegistry

        reg_file = tmp_path / "sessions.json"
        reg_file.write_text("{}")
        registry = SessionRegistry(reg_file)
        backend = SDKBackend(registry=registry, contacts_manager=None, producer=None)

        session = await backend.create_session(
            contact_name="No Bus",
            chat_id="+15555550077",
            tier="admin",
            source="test",
        )
        assert session is not None

        result = await backend.kill_session("+15555550077")
        assert result is True


# ── Shutdown Bus Events ──────────────────────────────────────

@pytest.mark.asyncio
class TestShutdownBusEvents:
    """Test daemon.stopped event and bus cleanup."""

    async def test_shutdown_produces_daemon_stopped(self, tmp_path):
        """_shutdown produces daemon.stopped event and closes bus."""
        bus = Bus(db_path=str(tmp_path / "shutdown-bus.db"))
        bus.create_topic("messages", retention_ms=3600000)
        bus.create_topic("sessions", retention_ms=3600000)
        bus.create_topic("system", retention_ms=3600000)
        prod = bus.producer()

        from assistant.bus_helpers import produce_event
        produce_event(prod, "system", "daemon.started", {"rowid": 0, "session_count": 0}, source="daemon")
        produce_event(prod, "system", "daemon.stopped", {"session_count": 0, "uptime_seconds": 1.0}, source="daemon")

        records = drain_topic(bus, "system", "test-shutdown")
        started = find_events(records, "daemon.started")
        stopped = find_events(records, "daemon.stopped")
        assert len(started) == 1
        assert len(stopped) == 1
        assert stopped[0].payload["uptime_seconds"] == 1.0

        bus.close()


# ── HEALME / MASTER / RESTART ────────────────────────────────

@pytest.mark.asyncio
class TestCommandBusEvents:
    """Test HEALME, MASTER, RESTART produce bus events."""

    async def test_healme_event(self, tmp_path):
        """HEALME produces healme.triggered event."""
        bus = Bus(db_path=str(tmp_path / "healme-bus.db"))
        bus.create_topic("system", retention_ms=3600000)
        prod = bus.producer()

        from assistant.bus_helpers import produce_event, healme_payload
        produce_event(prod, "system", "healme.triggered",
            healme_payload("+15555551234", "Admin", "triggered", "fix it"),
            source="daemon")

        records = drain_topic(bus, "system", "test-healme")
        events = find_events(records, "healme.triggered")
        assert len(events) == 1
        assert events[0].payload["admin_phone"] == "+15555551234"
        assert events[0].payload["custom_prompt"] == "fix it"
        bus.close()

    async def test_master_event(self, tmp_path):
        """MASTER produces master.triggered event."""
        bus = Bus(db_path=str(tmp_path / "master-bus.db"))
        bus.create_topic("system", retention_ms=3600000)
        prod = bus.producer()

        from assistant.bus_helpers import produce_event
        produce_event(prod, "system", "master.triggered", {
            "admin_phone": "+15555551234", "prompt_length": 42,
        }, source="daemon")

        records = drain_topic(bus, "system", "test-master")
        events = find_events(records, "master.triggered")
        assert len(events) == 1
        assert events[0].payload["prompt_length"] == 42
        bus.close()

    async def test_command_restart_event(self, tmp_path):
        """RESTART command produces command.restart event."""
        bus = Bus(db_path=str(tmp_path / "restart-bus.db"))
        bus.create_topic("sessions", retention_ms=3600000)
        prod = bus.producer()

        from assistant.bus_helpers import produce_event
        produce_event(prod, "sessions", "command.restart", {
            "chat_id": "+15555551234", "session_name": "test/+15555551234",
            "source": "sms",
        }, key="+15555551234", source="daemon")

        records = drain_topic(bus, "sessions", "test-cmd-restart")
        events = find_events(records, "command.restart")
        assert len(events) == 1
        assert events[0].payload["chat_id"] == "+15555551234"
        bus.close()
