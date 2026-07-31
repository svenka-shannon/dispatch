"""Tests for Phases 2-3: Ephemeral Tasks + Task Consumer.

Tests the task consumer, ephemeral session creation, timeout supervision,
script task execution, and task lifecycle events.
"""
import asyncio
import json
import time
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from pathlib import Path


# ─── Helpers ────────────────────────────────────────────────────


class FakeSession:
    """Minimal session mock."""
    def __init__(self, alive=True):
        self._alive = alive
        self.inject = AsyncMock()
        self.stop = AsyncMock()
        self.contact_name = "Test"
        self.tier = "admin"
        self.turn_count = 0
        self._error_count = 0
        self.session_id = None
        self.created_at = datetime.now()
        self.chat_id = "test"

    def is_alive(self):
        return self._alive

    async def _kill_subprocess(self):
        pass


class FakeBackend:
    """Minimal SDKBackend mock."""
    def __init__(self):
        self.sessions = {}
        self._producer = MagicMock()
        self._lock = asyncio.Lock()
        self.registry = MagicMock()
        self.create_session = AsyncMock()
        self.create_ephemeral_session = AsyncMock(return_value=FakeSession())
        self.kill_ephemeral_session = AsyncMock(return_value=True)


class FakeBusRecord:
    """Minimal bus record mock."""
    def __init__(self, type, payload, key=None, headers=None, offset=0):
        self.type = type
        self.payload = payload
        self.key = key
        self.headers = headers or {}
        self.offset = offset
        self.source = "test"


class FakeContactsManager:
    """Minimal ContactsManager for testing."""
    def __init__(self):
        pass

    def lookup_phone_by_name(self, name):
        return None


# ─── bus_helpers payload builders ───────────────────────────────


class TestTaskPayloadBuilders:
    """Test task event payload builder functions."""

    def test_task_started_payload(self):
        from assistant.bus_helpers import task_started_payload
        p = task_started_payload(
            task_id="task-123", title="Test Task",
            requested_by="+15551234567",
            session_name="ephemeral-task-123",
            timeout_minutes=30,
        )
        assert p["task_id"] == "task-123"
        assert p["title"] == "Test Task"
        assert p["requested_by"] == "+15551234567"
        assert p["session_name"] == "ephemeral-task-123"
        assert p["timeout_minutes"] == 30
        assert p["execution_mode"] == "agent"

    def test_task_started_payload_with_extras(self):
        from assistant.bus_helpers import task_started_payload
        p = task_started_payload(
            task_id="task-123", title="Test",
            requested_by="+1", session_name="s",
            timeout_minutes=10, execution_mode="script",
            custom_field="hello",
        )
        assert p["execution_mode"] == "script"
        assert p["custom_field"] == "hello"

    def test_task_completed_payload(self):
        from assistant.bus_helpers import task_completed_payload
        p = task_completed_payload(
            task_id="task-123", title="Test Task",
            requested_by="+15551234567",
            duration_seconds=123.456,
        )
        assert p["task_id"] == "task-123"
        assert p["duration_seconds"] == 123.5

    def test_task_failed_payload(self):
        from assistant.bus_helpers import task_failed_payload
        p = task_failed_payload(
            task_id="task-123", title="Test",
            requested_by="+1", error="boom",
        )
        assert p["error"] == "boom"

    def test_task_timeout_payload(self):
        from assistant.bus_helpers import task_timeout_payload
        p = task_timeout_payload(
            task_id="task-123", title="Test",
            requested_by="+1", timeout_minutes=30,
        )
        assert p["timeout_minutes"] == 30


# ─── _handle_task_requested ─────────────────────────────────────


class TestHandleTaskRequested:
    """Test the task.requested event handler."""

    def _make_manager(self):
        """Create a minimal Manager mock for testing."""
        from assistant.manager import Manager
        m = MagicMock(spec=Manager)
        m.sessions = FakeBackend()
        m._producer = MagicMock()
        m._ephemeral_tasks = {}
        m._running_script_tasks = {}
        m._completed_task_traces = {}
        m._shutdown_flag = False
        # Bind the real method
        m._handle_task_requested = Manager._handle_task_requested.__get__(m, Manager)
        m._run_script_task = AsyncMock()
        m._notify_task_event = AsyncMock()
        return m

    @pytest.mark.asyncio
    async def test_valid_agent_task(self):
        m = self._make_manager()
        payload = {
            "task_id": "task-abc",
            "title": "Test Agent Task",
            "requested_by": "+15551234567",
            "instructions": "Do something useful",
            "timeout_minutes": 15,
            "notify": True,
        }

        await m._handle_task_requested(payload, {})

        # Should create ephemeral session
        m.sessions.create_ephemeral_session.assert_awaited_once()
        call_kwargs = m.sessions.create_ephemeral_session.call_args.kwargs
        assert call_kwargs["task_id"] == "task-abc"
        assert call_kwargs["title"] == "Test Agent Task"
        assert call_kwargs["instructions"] == "Do something useful"
        assert call_kwargs["timeout_minutes"] == 15

        # Should track in ephemeral_tasks
        assert "task-abc" in m._ephemeral_tasks
        info = m._ephemeral_tasks["task-abc"]
        assert info["session_key"] == "ephemeral-task-abc"
        assert info["requested_by"] == "+15551234567"
        assert info["notify"] is True

        # Should produce task.started
        m._producer.send.assert_called()

    @pytest.mark.asyncio
    async def test_missing_task_id(self):
        m = self._make_manager()
        payload = {
            "title": "No ID",
            "requested_by": "+1",
            "instructions": "test",
        }

        await m._handle_task_requested(payload, {})

        # Should NOT create session
        m.sessions.create_ephemeral_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_requested_by(self):
        m = self._make_manager()
        payload = {
            "task_id": "task-123",
            "title": "No Requester",
            "instructions": "test",
        }

        await m._handle_task_requested(payload, {})
        m.sessions.create_ephemeral_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_instructions(self):
        m = self._make_manager()
        payload = {
            "task_id": "task-123",
            "requested_by": "+1",
        }

        await m._handle_task_requested(payload, {})
        m.sessions.create_ephemeral_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dedup_already_running(self):
        m = self._make_manager()
        # Pre-populate a running session
        fake_session = FakeSession(alive=True)
        m.sessions.sessions["ephemeral-task-dup"] = fake_session

        payload = {
            "task_id": "task-dup",
            "title": "Duplicate",
            "requested_by": "+1",
            "instructions": "test",
        }

        await m._handle_task_requested(payload, {})

        # Should skip, not create a new session
        m.sessions.create_ephemeral_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_script_mode_delegates(self):
        m = self._make_manager()
        payload = {
            "task_id": "task-script",
            "title": "Script Task",
            "requested_by": "+1",
            "instructions": "ignored for scripts",
            "execution": {
                "mode": "script",
                "command": ["echo", "hello"],
            },
        }

        await m._handle_task_requested(payload, {})

        # Should track script task for dedup and NOT create ephemeral session
        assert "task-script" in m._running_script_tasks
        m.sessions.create_ephemeral_session.assert_not_awaited()
        # Let the task run
        await asyncio.sleep(0)  # Yield to let create_task execute

    @pytest.mark.asyncio
    async def test_execution_prompt_fallback(self):
        """When instructions is empty but execution.prompt is set, use that."""
        m = self._make_manager()
        payload = {
            "task_id": "task-exec",
            "title": "Exec Prompt",
            "requested_by": "+1",
            "execution": {
                "mode": "agent",
                "prompt": "Do this via execution.prompt",
            },
        }

        await m._handle_task_requested(payload, {})

        call_kwargs = m.sessions.create_ephemeral_session.call_args.kwargs
        assert call_kwargs["instructions"] == "Do this via execution.prompt"

    @pytest.mark.asyncio
    async def test_session_creation_failure(self):
        m = self._make_manager()
        m.sessions.create_ephemeral_session = AsyncMock(
            side_effect=RuntimeError("SDK crashed")
        )

        payload = {
            "task_id": "task-fail",
            "title": "Failing Task",
            "requested_by": "+1",
            "instructions": "test",
        }

        # Should NOT raise — error handled internally
        await m._handle_task_requested(payload, {})

        # Should produce task.failed event
        assert any(
            call.args[1] == "task.failed"
            for call in m._producer.send.call_args_list
            if len(call.args) >= 2
        ) or any(
            call.kwargs.get("type") == "task.failed"
            for call in m._producer.send.call_args_list
        )


# ─── Timeout supervision ────────────────────────────────────────


class TestTaskSupervision:
    """Test the ephemeral task timeout supervisor."""

    @pytest.mark.asyncio
    async def test_timeout_kills_task(self):
        """Task that exceeds timeout gets killed via the real supervisor method."""
        from assistant.manager import Manager

        m = MagicMock(spec=Manager)
        m.sessions = FakeBackend()
        m._producer = MagicMock()
        m._shutdown_flag = False
        m._completed_task_traces = {}
        m._ephemeral_tasks = {
            "task-old": {
                "session_key": "ephemeral-task-old",
                "started_at": time.time() - 3600,  # 1 hour ago
                "timeout_minutes": 10,  # 10 min timeout (exceeded)
                "requested_by": "+1",
                "title": "Old Task",
                "notify": True,
            }
        }
        m._notify_task_event = AsyncMock()

        # Bind the real supervisor method
        real_supervise = Manager._supervise_ephemeral_tasks.__get__(m, Manager)

        # Run it, but stop after one iteration by setting shutdown flag
        async def run_one_cycle():
            task = asyncio.create_task(real_supervise())
            await asyncio.sleep(0.1)  # Let the sleep(30) start
            # The supervisor sleeps 30s before first check — cancel and check manually
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Since supervisor sleeps 30s first, directly test the inner logic
        # by calling it with a shorter approach: patch asyncio.sleep to return immediately
        with patch('asyncio.sleep', new_callable=AsyncMock) as mock_sleep:
            async def stop_after_one_cycle(*args):
                m._shutdown_flag = True

            mock_sleep.side_effect = stop_after_one_cycle

            await real_supervise()

        # Should have killed the session
        m.sessions.kill_ephemeral_session.assert_awaited_once_with("task-old")
        assert "task-old" not in m._ephemeral_tasks

    @pytest.mark.asyncio
    async def test_completed_session_detected(self):
        """Session that died naturally gets detected as completed by real supervisor."""
        from assistant.manager import Manager

        m = MagicMock(spec=Manager)
        m.sessions = FakeBackend()
        m._producer = MagicMock()
        m._shutdown_flag = False
        m._completed_task_traces = {}
        m._ephemeral_tasks = {
            "task-done": {
                "session_key": "ephemeral-task-done",
                "started_at": time.time() - 60,  # 1 min ago
                "timeout_minutes": 30,  # not timed out
                "requested_by": "+1",
                "title": "Done Task",
                "notify": False,
            }
        }
        m._notify_task_event = AsyncMock()
        # Session is NOT in sessions dict (died)
        # sessions.sessions is empty by default in FakeBackend

        # Bind the real supervisor
        real_supervise = Manager._supervise_ephemeral_tasks.__get__(m, Manager)

        with patch('asyncio.sleep', new_callable=AsyncMock) as mock_sleep:
            async def stop_after_one_cycle(*args):
                m._shutdown_flag = True

            mock_sleep.side_effect = stop_after_one_cycle
            await real_supervise()

        # Should have detected completion and cleaned up
        m.sessions.kill_ephemeral_session.assert_awaited_once_with("task-done")
        assert "task-done" not in m._ephemeral_tasks


# ─── SDKBackend.create_ephemeral_session ─────────────────────────


class TestCreateEphemeralSession:
    """Test SDKBackend.create_ephemeral_session()."""

    @pytest.mark.asyncio
    async def test_creates_session_and_injects(self):
        """Ephemeral session creation sets up cwd, .claude symlink, and injects."""
        from assistant.sdk_backend import SDKBackend

        with patch.object(SDKBackend, '__init__', lambda self: None):
            backend = SDKBackend()
            backend._lock = asyncio.Lock()
            backend.sessions = {}
            backend._producer = MagicMock()
            backend.registry = MagicMock()

            fake_session = FakeSession()
            with patch('assistant.sdk_backend.SDKSession', return_value=fake_session):
                fake_session.start = AsyncMock()
                with patch('pathlib.Path.mkdir'):
                    with patch('pathlib.Path.exists', return_value=True):
                        with patch('pathlib.Path.symlink_to'):
                            session = await backend.create_ephemeral_session(
                                task_id="test-task",
                                title="Test Task",
                                instructions="Do something",
                                requested_by="+15551234567",
                                timeout_minutes=15,
                            )

            assert session is fake_session
            assert "ephemeral-test-task" in backend.sessions
            # Should have injected task prompt
            fake_session.inject.assert_awaited_once()
            injected_text = fake_session.inject.call_args[0][0]
            assert "Do something" in injected_text
            assert "test-task" in injected_text

    @pytest.mark.asyncio
    async def test_kill_ephemeral_session_cleanup(self):
        """kill_ephemeral_session stops session and cleans up cwd."""
        from assistant.sdk_backend import SDKBackend

        with patch.object(SDKBackend, '__init__', lambda self: None):
            backend = SDKBackend()
            backend._lock = asyncio.Lock()
            fake_session = FakeSession()
            backend.sessions = {"ephemeral-task-clean": fake_session}
            backend._producer = MagicMock()

            with patch('shutil.rmtree') as mock_rmtree:
                with patch('pathlib.Path.exists', return_value=True):
                    result = await backend.kill_ephemeral_session("task-clean")

            assert result is True
            fake_session.stop.assert_awaited_once()
            assert "ephemeral-task-clean" not in backend.sessions


# ─── Script task execution ───────────────────────────────────────


class TestScriptTask:
    """Test _run_script_task for subprocess execution."""

    @pytest.mark.asyncio
    async def test_successful_script(self):
        from assistant.manager import Manager

        m = MagicMock(spec=Manager)
        m._producer = MagicMock()
        m._completed_task_traces = {}
        m._notify_task_event = AsyncMock()
        m._run_script_task = Manager._run_script_task.__get__(m, Manager)

        payload = {
            "task_id": "script-ok",
            "title": "Echo Test",
            "requested_by": "+1",
            "notify": True,
            "timeout_minutes": 5,
            "execution": {
                "mode": "script",
                "command": ["echo", "hello world"],
            },
        }

        with patch('assistant.manager.produce_event') as mock_produce:
            await m._run_script_task(payload, {})

            # Should produce task.completed event
            completed_calls = [
                call for call in mock_produce.call_args_list
                if call.args[2] == "task.completed"
            ]
            assert len(completed_calls) == 1
            completed_payload = completed_calls[0].args[3]
            assert completed_payload["task_id"] == "script-ok"
            assert "duration_seconds" in completed_payload
            assert "hello world" in completed_payload.get("stdout", "")

    @pytest.mark.asyncio
    async def test_missing_command(self):
        from assistant.manager import Manager

        m = MagicMock(spec=Manager)
        m._producer = MagicMock()
        m._completed_task_traces = {}
        m._run_script_task = Manager._run_script_task.__get__(m, Manager)

        payload = {
            "task_id": "script-nocommand",
            "title": "No Command",
            "requested_by": "+1",
            "execution": {"mode": "script"},
        }

        with patch('assistant.manager.produce_event') as mock_produce:
            await m._run_script_task(payload, {})

            # Should produce task.failed event
            failed_calls = [
                call for call in mock_produce.call_args_list
                if call.args[2] == "task.failed"
            ]
            assert len(failed_calls) == 1
            assert "missing execution.command" in failed_calls[0].args[3]["error"]


    @pytest.mark.asyncio
    async def test_script_nonzero_exit_code(self):
        """Script that exits with non-zero code produces task.failed."""
        from assistant.manager import Manager

        m = MagicMock(spec=Manager)
        m._producer = MagicMock()
        m._completed_task_traces = {}
        m._notify_task_event = AsyncMock()
        m._run_script_task = Manager._run_script_task.__get__(m, Manager)

        payload = {
            "task_id": "script-fail",
            "title": "Failing Script",
            "requested_by": "+1",
            "notify": True,
            "timeout_minutes": 5,
            "execution": {
                "mode": "script",
                "command": ["sh", "-c", "echo error >&2; exit 1"],
            },
        }

        with patch('assistant.manager.produce_event') as mock_produce:
            await m._run_script_task(payload, {})

            # Should produce task.failed event
            failed_calls = [
                call for call in mock_produce.call_args_list
                if call.args[2] == "task.failed"
            ]
            assert len(failed_calls) == 1
            assert failed_calls[0].args[3]["task_id"] == "script-fail"
            assert "error" in failed_calls[0].args[3]["error"]


# ─── Consumer auto-restart ───────────────────────────────────────


class TestConsumerAutoRestart:
    """Test _on_task_consumer_done restart behavior."""

    def test_restarts_on_exception(self):
        from assistant.manager import Manager

        m = MagicMock(spec=Manager)
        m._shutdown_flag = False
        m._task_consumer_restarts = 0
        m._on_task_consumer_done = Manager._on_task_consumer_done.__get__(m, Manager)

        # Create a failed task
        failed_task = MagicMock()
        failed_task.exception.return_value = RuntimeError("boom")

        with patch('assistant.manager._fire_and_forget') as mock_faf:
            m._on_task_consumer_done(failed_task)

            # Should fire restart
            mock_faf.assert_called_once()
            assert m._task_consumer_restarts == 1

    def test_stops_after_max_restarts(self):
        from assistant.manager import Manager

        m = MagicMock(spec=Manager)
        m._shutdown_flag = False
        m._task_consumer_restarts = 5  # Already at max
        m._on_task_consumer_done = Manager._on_task_consumer_done.__get__(m, Manager)

        failed_task = MagicMock()
        failed_task.exception.return_value = RuntimeError("boom")

        with patch('assistant.manager._fire_and_forget') as mock_faf:
            m._on_task_consumer_done(failed_task)

            # Should NOT restart — exceeded max
            mock_faf.assert_not_called()

    def test_no_restart_on_shutdown(self):
        from assistant.manager import Manager

        m = MagicMock(spec=Manager)
        m._shutdown_flag = True
        m._on_task_consumer_done = Manager._on_task_consumer_done.__get__(m, Manager)

        failed_task = MagicMock()
        failed_task.exception.return_value = RuntimeError("boom")

        with patch('assistant.manager._fire_and_forget') as mock_faf:
            m._on_task_consumer_done(failed_task)

            mock_faf.assert_not_called()

    def test_no_restart_on_cancel(self):
        from assistant.manager import Manager

        m = MagicMock(spec=Manager)
        m._shutdown_flag = False
        m._on_task_consumer_done = Manager._on_task_consumer_done.__get__(m, Manager)

        cancelled_task = MagicMock()
        cancelled_task.exception.side_effect = asyncio.CancelledError()

        with patch('assistant.manager._fire_and_forget') as mock_faf:
            m._on_task_consumer_done(cancelled_task)

            mock_faf.assert_not_called()


# ─── task_skipped_payload ────────────────────────────────────────


class TestTaskSkippedPayload:
    """Test the task_skipped_payload builder."""

    def test_basic(self):
        from assistant.bus_helpers import task_skipped_payload
        p = task_skipped_payload("task-123", "already_running")
        assert p["task_id"] == "task-123"
        assert p["reason"] == "already_running"

    def test_with_extras(self):
        from assistant.bus_helpers import task_skipped_payload
        p = task_skipped_payload("task-123", "already_running", detail="foo")
        assert p["detail"] == "foo"


# ─── Notify task event ───────────────────────────────────────────


class TestNotifyTaskEvent:
    """Test _notify_task_event delivery."""

    @pytest.mark.asyncio
    async def test_injects_to_existing_session(self):
        from assistant.manager import Manager

        m = MagicMock(spec=Manager)
        fake_session = FakeSession(alive=True)
        m.sessions = MagicMock()
        m.sessions.sessions = {"+1": fake_session}
        m._notify_task_event = Manager._notify_task_event.__get__(m, Manager)

        await m._notify_task_event("+1", "Task done!")

        fake_session.inject.assert_awaited_once()
        assert "Task done!" in fake_session.inject.call_args[0][0]

    @pytest.mark.asyncio
    async def test_falls_back_to_sms_if_no_session(self):
        from assistant.manager import Manager

        m = MagicMock(spec=Manager)
        m.sessions = MagicMock()
        m.sessions.sessions = {}  # No active sessions
        m.sessions.registry.get.return_value = {"source": "imessage"}
        m._notify_task_event = Manager._notify_task_event.__get__(m, Manager)

        with patch('asyncio.create_subprocess_exec', new_callable=AsyncMock) as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_exec.return_value = mock_proc

            await m._notify_task_event("+1", "Task done!")

            mock_exec.assert_awaited_once()
            # Should use send-sms for imessage backend
            cmd = mock_exec.call_args[0][0]
            assert "send-sms" in cmd

    @pytest.mark.asyncio
    async def test_falls_back_to_signal_for_signal_contact(self):
        from assistant.manager import Manager

        m = MagicMock(spec=Manager)
        m.sessions = MagicMock()
        m.sessions.sessions = {}  # No active sessions
        m.sessions.registry.get.return_value = {"source": "signal"}
        m._notify_task_event = Manager._notify_task_event.__get__(m, Manager)

        with patch('asyncio.create_subprocess_exec', new_callable=AsyncMock) as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_exec.return_value = mock_proc

            await m._notify_task_event("+1", "Task done!")

            mock_exec.assert_awaited_once()
            # Should use send-signal for signal backend
            cmd = mock_exec.call_args[0][0]
            assert "send-signal" in cmd


# ─── Cleanup orphaned ephemeral sessions ─────────────────────────


class TestCleanupOrphanedSessions:
    """Test _cleanup_orphaned_ephemeral_sessions."""

    @pytest.mark.asyncio
    async def test_cleans_directories(self):
        from assistant.manager import Manager

        m = MagicMock(spec=Manager)
        m.sessions = FakeBackend()
        m._cleanup_orphaned_ephemeral_sessions = (
            Manager._cleanup_orphaned_ephemeral_sessions.__get__(m, Manager)
        )

        # Mock the ephemeral directory
        with patch('pathlib.Path.exists', return_value=True):
            mock_dir = MagicMock()
            mock_dir.is_dir.return_value = True
            mock_dir.name = "old-task"

            with patch('pathlib.Path.iterdir', return_value=[mock_dir]):
                with patch('shutil.rmtree') as mock_rmtree:
                    await m._cleanup_orphaned_ephemeral_sessions()

                    mock_rmtree.assert_called_once()

    @pytest.mark.asyncio
    async def test_kills_orphaned_sessions(self):
        """Orphaned ephemeral sessions get stopped and removed."""
        from assistant.manager import Manager

        m = MagicMock(spec=Manager)
        fake_session = FakeSession(alive=True)
        regular_session = FakeSession()
        # Use a real dict for sessions so mutations work
        sessions_dict = {"ephemeral-orphan": fake_session, "regular-session": regular_session}
        m.sessions = MagicMock()
        m.sessions.sessions = sessions_dict
        m._cleanup_orphaned_ephemeral_sessions = (
            Manager._cleanup_orphaned_ephemeral_sessions.__get__(m, Manager)
        )

        # Patch Path.exists to return True for ephemeral_base but no subdirs
        original_exists = Path.exists

        def mock_exists(self):
            if "ephemeral" in str(self):
                return True  # ephemeral base dir exists
            return original_exists(self)

        with patch.object(Path, 'exists', mock_exists):
            with patch.object(Path, 'iterdir', return_value=[]):  # no orphaned dirs
                await m._cleanup_orphaned_ephemeral_sessions()

        # Ephemeral session removed, regular session preserved
        assert "ephemeral-orphan" not in sessions_dict
        assert "regular-session" in sessions_dict
        fake_session.stop.assert_awaited_once()


# ─── Integration: topic creation ─────────────────────────────────


class TestTopicCreation:
    """Test that the 'tasks' topic is created on Manager init."""

    def test_tasks_topic_created(self):
        """Manager.__init__ creates 'tasks' topic."""
        from assistant.manager import Manager

        with patch('assistant.manager.ContactsManager'), \
             patch('assistant.manager.MessagesReader'), \
             patch('assistant.manager.SessionRegistry'), \
             patch('bus.bus.Bus') as MockBus, \
             patch('assistant.manager.SDKBackend'), \
             patch('assistant.manager.ReminderPoller'), \
             patch('assistant.manager.IPCServer'), \
             patch('assistant.manager.Manager._load_state', return_value=0), \
             patch('assistant.manager.Manager._create_dispatch_api_process', return_value=None):

            mock_bus = MagicMock()
            MockBus.return_value = mock_bus
            mock_bus.producer.return_value = MagicMock()

            mgr = Manager()

            # Check create_topic was called with "tasks"
            topic_calls = [
                call.args[0]
                for call in mock_bus.create_topic.call_args_list
            ]
            assert "tasks" in topic_calls
