"""Tests for reminder bus integration (write path).

Tests the dual-path pattern: produce reminder.due to bus + direct inject.
"""
import json
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch, call


class FakeContactsManager:
    """Minimal ContactsManager for testing."""
    def __init__(self, contacts=None):
        self._contacts = contacts or {}

    def lookup_phone_by_name(self, name):
        return self._contacts.get(name)


class FakeSession:
    """Minimal session mock."""
    def __init__(self, alive=True):
        self._alive = alive
        self.inject = AsyncMock()

    def is_alive(self):
        return self._alive


class FakeBackend:
    """Minimal SDKBackend mock."""
    def __init__(self):
        self.sessions = {}
        self._producer = MagicMock()
        self.create_session = AsyncMock()


def make_poller(contacts=None, sessions=None):
    """Create a ReminderPoller with mocked dependencies.

    Sessions dict keys should be the normalized chat_id (what normalize_chat_id returns).
    For phone "+15551234567", use the result of normalize_chat_id("+15551234567").
    """
    from assistant.manager import ReminderPoller
    from assistant.common import normalize_chat_id

    backend = FakeBackend()
    if sessions:
        backend.sessions = sessions

    contacts_mgr = FakeContactsManager(contacts or {})
    poller = ReminderPoller(backend, contacts_mgr)
    poller._load_reminders()  # Initialize empty
    return poller, backend


def _normalized(chat_id: str) -> str:
    """Get normalized session key for a chat_id."""
    from assistant.common import normalize_chat_id
    return normalize_chat_id(chat_id)


def make_reminder(id="test123", title="Check the chess game", contact="+15551234567",
                  target="fg", schedule_type="once", schedule_value="2026-03-14T19:00:00Z",
                  next_fire="2026-03-14T19:00:00Z", **kwargs):
    """Create a test reminder dict."""
    r = {
        "id": id,
        "title": title,
        "contact": contact,
        "target": target,
        "schedule": {
            "type": schedule_type,
            "value": schedule_value,
        },
        "next_fire": next_fire,
        "created_at": "2026-03-14T17:00:00Z",
        "last_fired": None,
        "fired_count": 0,
        "retry_count": 0,
        "last_error": None,
    }
    r.update(kwargs)
    return r


# ─── _resolve_reminder_contact() ─────────────────────────────────


class TestResolveReminderContact:
    """Tests for _resolve_reminder_contact()."""

    def test_phone_number_returns_admin(self):
        poller, _ = make_poller()
        r = make_reminder(contact="+15551234567")
        chat_id, tier = poller._resolve_reminder_contact(r)
        assert chat_id == "+15551234567"
        assert tier == "admin"

    def test_group_hex_returns_admin(self):
        poller, _ = make_poller()
        r = make_reminder(contact="b3d258b9a4de447ca412eb335c82a077")
        chat_id, tier = poller._resolve_reminder_contact(r)
        assert chat_id == "b3d258b9a4de447ca412eb335c82a077"
        assert tier == "admin"

    def test_contact_name_resolved(self):
        poller, _ = make_poller(contacts={
            "Alice": {"phone": "+15559876543", "tier": "favorite"}
        })
        r = make_reminder(contact="Alice")
        chat_id, tier = poller._resolve_reminder_contact(r)
        assert chat_id == "+15559876543"
        assert tier == "favorite"

    def test_missing_contact_raises(self):
        poller, _ = make_poller()
        r = make_reminder(contact=None)
        with pytest.raises(ValueError, match="no contact"):
            poller._resolve_reminder_contact(r)

    def test_unknown_contact_name_raises(self):
        poller, _ = make_poller()
        r = make_reminder(contact="NonexistentPerson")
        with pytest.raises(ValueError, match="Contact not found"):
            poller._resolve_reminder_contact(r)

    def test_contact_no_phone_raises(self):
        poller, _ = make_poller(contacts={
            "NoPhone": {"tier": "favorite"}
        })
        r = make_reminder(contact="NoPhone")
        with pytest.raises(ValueError, match="No phone"):
            poller._resolve_reminder_contact(r)


# ─── _fire_reminder() bus produce ────────────────────────────────


class TestFireReminderBusProduce:
    """Tests for _fire_reminder() bus event production."""

    @pytest.mark.asyncio
    async def test_produces_reminder_due_event(self):
        """Verify bus event is produced with correct topic/type/payload."""
        session = FakeSession()
        poller, backend = make_poller(sessions={_normalized("+15551234567"): session})
        r = make_reminder()
        poller.reminders = [r]

        await poller._fire_reminder(r)

        # Check that producer.send was called with reminder.due
        send_calls = backend._producer.send.call_args_list
        # Find the reminder.due call (there may also be session.injected calls)
        reminder_calls = [c for c in send_calls if c.kwargs.get("type") == "reminder.due"
                          or (len(c.args) > 0 and c.kwargs.get("type") == "reminder.due")]
        # produce_event calls producer.send(topic, payload=..., key=..., type=..., source=...)
        found = False
        for c in send_calls:
            if c.kwargs.get("type") == "reminder.due":
                found = True
                assert c.args[0] == "reminders"  # topic
                assert c.kwargs["key"] == "+15551234567"  # chat_id as key
                assert c.kwargs["source"] == "reminder-poller"
                payload = c.kwargs["payload"]
                assert payload["reminder_id"] == "test123"
                assert payload["title"] == "Check the chess game"
                assert payload["chat_id"] == "+15551234567"
                assert payload["target"] == "fg"
                assert payload["schedule_type"] == "once"
                assert payload["is_late"] is False
                assert payload["fired_count"] == 1
                break
        assert found, f"No reminder.due event found in send calls: {send_calls}"

    @pytest.mark.asyncio
    async def test_payload_is_json_serializable(self):
        """Verify the payload round-trips through JSON."""
        session = FakeSession()
        poller, backend = make_poller(sessions={_normalized("+15551234567"): session})
        r = make_reminder()
        poller.reminders = [r]

        await poller._fire_reminder(r)

        for c in backend._producer.send.call_args_list:
            if c.kwargs.get("type") == "reminder.due":
                payload = c.kwargs["payload"]
                # Must round-trip through JSON
                serialized = json.dumps(payload)
                deserialized = json.loads(serialized)
                assert deserialized == payload
                break

    @pytest.mark.asyncio
    async def test_still_injects_directly(self):
        """Verify direct inject still fires (dual path)."""
        session = FakeSession()
        poller, backend = make_poller(sessions={_normalized("+15551234567"): session})
        r = make_reminder()
        poller.reminders = [r]

        await poller._fire_reminder(r)

        # Session should have been injected into
        session.inject.assert_called_once()
        inject_msg = session.inject.call_args[0][0]
        assert "Check the chess game" in inject_msg
        assert "REMINDER" in inject_msg

    @pytest.mark.asyncio
    async def test_once_reminder_deleted_on_success(self):
        """Once reminders should be removed after firing."""
        session = FakeSession()
        poller, backend = make_poller(sessions={_normalized("+15551234567"): session})
        r = make_reminder(schedule_type="once")
        poller.reminders = [r]

        await poller._fire_reminder(r)

        assert r not in poller.reminders

    @pytest.mark.asyncio
    async def test_cron_reminder_advanced_on_success(self):
        """Cron reminders should advance next_fire."""
        session = FakeSession()
        poller, backend = make_poller(sessions={_normalized("+15551234567"): session})
        r = make_reminder(schedule_type="cron", schedule_value="0 9 * * *",
                          next_fire="2026-03-14T13:00:00Z")
        poller.reminders = [r]

        await poller._fire_reminder(r)

        # next_fire should be advanced (not the old value)
        assert r["next_fire"] != "2026-03-14T13:00:00Z"
        assert r in poller.reminders  # Not deleted

    @pytest.mark.asyncio
    async def test_retry_on_inject_failure(self):
        """If inject raises, retry_count should increment."""
        session = FakeSession()
        session.inject = AsyncMock(side_effect=RuntimeError("inject failed"))
        poller, backend = make_poller(sessions={_normalized("+15551234567"): session})
        r = make_reminder()
        poller.reminders = [r]

        await poller._fire_reminder(r)

        assert r["retry_count"] == 1
        assert r["last_error"] == "inject failed"

    @pytest.mark.asyncio
    async def test_dead_reminder_alerts_admin(self):
        """After max retries, admin should be alerted."""
        session = FakeSession()
        session.inject = AsyncMock(side_effect=RuntimeError("inject failed"))
        poller, backend = make_poller(sessions={_normalized("+15551234567"): session})
        r = make_reminder(retry_count=2)  # One more failure = 3 = dead
        poller.reminders = [r]
        poller.config = {"max_retries": 3}

        with patch.object(poller, '_alert_admin', new_callable=AsyncMock) as mock_alert:
            await poller._fire_reminder(r)
            mock_alert.assert_called_once_with(r)

    @pytest.mark.asyncio
    async def test_late_reminder_payload(self):
        """Late reminders should have is_late=True and minutes_late > 0."""
        session = FakeSession()
        poller, backend = make_poller(sessions={_normalized("+15551234567"): session})
        r = make_reminder(next_fire="2026-03-14T16:00:00Z")
        poller.reminders = [r]

        # Fire as late (catch-up)
        await poller._fire_reminder(r, late=True)

        for c in backend._producer.send.call_args_list:
            if c.kwargs.get("type") == "reminder.due":
                payload = c.kwargs["payload"]
                assert payload["is_late"] is True
                assert payload["minutes_late"] > 0
                break


# ─── Dual path divergence ────────────────────────────────────────


class TestDualPathDivergence:
    """Test that bus failure doesn't block direct inject."""

    @pytest.mark.asyncio
    async def test_bus_fails_inject_succeeds(self):
        """If produce_event silently fails, inject should still work."""
        session = FakeSession()
        poller, backend = make_poller(sessions={_normalized("+15551234567"): session})
        r = make_reminder()
        poller.reminders = [r]

        # Make producer.send raise (simulating bus failure)
        backend._producer.send.side_effect = Exception("bus.db locked")

        await poller._fire_reminder(r)

        # Direct inject should still have fired
        session.inject.assert_called_once()
        # Reminder state should still advance (once → deleted)
        assert r not in poller.reminders

    @pytest.mark.asyncio
    async def test_bus_fails_state_still_advances(self):
        """Cron reminder state advances even if bus fails."""
        session = FakeSession()
        poller, backend = make_poller(sessions={_normalized("+15551234567"): session})
        r = make_reminder(schedule_type="cron", schedule_value="0 9 * * *",
                          next_fire="2026-03-14T13:00:00Z")
        poller.reminders = [r]

        # Make producer.send raise
        backend._producer.send.side_effect = Exception("bus.db locked")

        await poller._fire_reminder(r)

        # State should still advance
        assert r["fired_count"] == 1
        assert r["last_error"] is None
        assert r["next_fire"] != "2026-03-14T13:00:00Z"


# ─── Injected handling guidance ──────────────────────────────────


class TestInjectedHandlingGuidance:
    """The legacy inject must carry judgment guidance, not a fixed 'Working on it' script."""

    async def _inject_msg(self, r, late=False):
        session = FakeSession()
        poller, backend = make_poller(sessions={_normalized("+15551234567"): session})
        poller.reminders = [r]
        await poller._fire_reminder(r, late=late)
        session.inject.assert_called_once()
        return session.inject.call_args[0][0]

    @pytest.mark.asyncio
    async def test_no_fixed_action_required_script(self):
        msg = await self._inject_msg(make_reminder(title="text Eric: time to go to the bank"))
        assert "ACTION REQUIRED" not in msg
        assert "Working on it now" not in msg

    @pytest.mark.asyncio
    async def test_guidance_appended_verbatim(self):
        from assistant.manager import REMINDER_HANDLING_GUIDANCE
        msg = await self._inject_msg(make_reminder())
        assert msg.endswith(REMINDER_HANDLING_GUIDANCE)

    @pytest.mark.asyncio
    async def test_guidance_covers_nudge_task_recheck_and_override(self):
        from assistant.manager import REMINDER_HANDLING_GUIDANCE as g
        assert "send ONLY that message" in g
        assert "on it" in g
        assert "send nothing" in g
        assert "override these defaults" in g

    @pytest.mark.asyncio
    async def test_title_block_precedes_guidance(self):
        msg = await self._inject_msg(make_reminder(title="re-check X; tell Eric only if Y"))
        assert msg.index("re-check X; tell Eric only if Y") < msg.index("---END REMINDER---")
        assert msg.index("---END REMINDER---") < msg.index("HOW TO HANDLE THIS REMINDER")

    @pytest.mark.asyncio
    async def test_cron_and_late_reminders_also_get_guidance(self):
        r = make_reminder(schedule_type="cron", schedule_value="0 9 * * *",
                          next_fire="2026-03-14T13:00:00Z")
        msg = await self._inject_msg(r, late=True)
        assert "[LATE:" in msg
        assert "[Schedule: 0 9 * * *" in msg
        assert "HOW TO HANDLE THIS REMINDER" in msg
        assert "ACTION REQUIRED" not in msg

    @pytest.mark.asyncio
    async def test_spawn_target_gets_guidance(self):
        session = FakeSession()
        poller, backend = make_poller()
        r = make_reminder(id="sp1", target="spawn")
        spawn_id = f"{_normalized('+15551234567')}-spawn-sp1"

        async def _create(contact, name, tier):
            backend.sessions[name] = session
        backend.create_session.side_effect = _create
        poller.reminders = [r]

        await poller._fire_reminder(r)

        assert spawn_id in backend.sessions
        msg = session.inject.call_args[0][0]
        assert "HOW TO HANDLE THIS REMINDER" in msg
        assert "Working on it now" not in msg
