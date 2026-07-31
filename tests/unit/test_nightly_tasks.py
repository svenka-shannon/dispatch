"""Tests for nightly task migration from hardcoded 2am to bus-scheduled ephemeral tasks.

Validates:
1. Reminder event templates produce valid task.requested payloads
2. Consolidation script exists and is well-formed
3. Script-task dedup works correctly (behavioral)
4. Startup check detects missing reminders
5. Event template validation rejects bad inputs
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from assistant.reminders import create_reminder, validate_event_template


ADMIN_PHONE = "+15555550001"
DISPATCH_ROOT = Path(__file__).parent.parent.parent


# ── Reminder creation tests ──────────────────────────────────────────────


class TestConsolidationReminder:
    """Test that the consolidation reminder produces a valid task.requested event."""

    def _make_consolidation_reminder(self):
        return create_reminder(
            title="Nightly memory consolidation",
            schedule_type="cron",
            schedule_value="0 2 * * *",
            tz_name="America/New_York",
            event={
                "topic": "tasks",
                "type": "task.requested",
                "key": ADMIN_PHONE,
                "payload": {
                    "task_id": "nightly-consolidation",
                    "title": "Nightly memory consolidation",
                    "requested_by": ADMIN_PHONE,
                    "instructions": "Run the nightly consolidation",
                    "notify": True,
                    "timeout_minutes": 30,
                    "execution": {
                        "mode": "script",
                        "command": ["bash", "-c", "$HOME/dispatch/scripts/nightly-consolidation.sh"],
                    },
                },
            },
        )

    def test_creates_valid_reminder(self):
        r = self._make_consolidation_reminder()
        assert r["title"] == "Nightly memory consolidation"
        assert r["schedule"]["type"] == "cron"
        assert r["schedule"]["value"] == "0 2 * * *"
        assert "event" in r

    def test_event_template_valid(self):
        r = self._make_consolidation_reminder()
        validate_event_template(r["event"])

    def test_event_topic_is_tasks(self):
        r = self._make_consolidation_reminder()
        assert r["event"]["topic"] == "tasks"

    def test_event_type_is_task_requested(self):
        r = self._make_consolidation_reminder()
        assert r["event"]["type"] == "task.requested"

    def test_payload_has_script_mode(self):
        r = self._make_consolidation_reminder()
        payload = r["event"]["payload"]
        assert payload["execution"]["mode"] == "script"
        assert isinstance(payload["execution"]["command"], list)

    def test_payload_has_required_fields(self):
        r = self._make_consolidation_reminder()
        payload = r["event"]["payload"]
        assert "task_id" in payload
        assert "requested_by" in payload
        assert "instructions" in payload
        assert "timeout_minutes" in payload

    def test_cron_schedule_computes_next_fire(self):
        r = self._make_consolidation_reminder()
        assert r["next_fire"] is not None

    def test_command_uses_home_variable(self):
        """Command should use $HOME, not an expanded absolute path."""
        r = self._make_consolidation_reminder()
        command = r["event"]["payload"]["execution"]["command"]
        cmd_str = " ".join(command)
        assert "$HOME" in cmd_str
        assert "/Users/" not in cmd_str


class TestSkillifyReminder:
    """Test that the skillify reminder produces a valid task.requested event."""

    def _make_skillify_reminder(self):
        return create_reminder(
            title="Nightly skillify analysis",
            schedule_type="cron",
            schedule_value="30 2 * * *",
            tz_name="America/New_York",
            event={
                "topic": "tasks",
                "type": "task.requested",
                "key": ADMIN_PHONE,
                "payload": {
                    "task_id": "nightly-skillify",
                    "title": "Nightly skillify analysis",
                    "requested_by": ADMIN_PHONE,
                    "instructions": "Run /skillify --nightly",
                    "notify": True,
                    "timeout_minutes": 45,
                    "execution": {
                        "mode": "agent",
                        "prompt": "Run /skillify --nightly",
                    },
                },
            },
        )

    def test_creates_valid_reminder(self):
        r = self._make_skillify_reminder()
        assert r["title"] == "Nightly skillify analysis"
        assert r["schedule"]["value"] == "30 2 * * *"

    def test_event_template_valid(self):
        r = self._make_skillify_reminder()
        validate_event_template(r["event"])

    def test_payload_has_agent_mode(self):
        r = self._make_skillify_reminder()
        payload = r["event"]["payload"]
        assert payload["execution"]["mode"] == "agent"
        assert "prompt" in payload["execution"]

    def test_skillify_starts_after_consolidation_timeout(self):
        """Skillify at 2:30 gives consolidation its full 30min timeout window."""
        consol = create_reminder(
            title="consolidation",
            schedule_type="cron",
            schedule_value="0 2 * * *",
            tz_name="America/New_York",
            event={
                "topic": "tasks",
                "type": "task.requested",
                "key": ADMIN_PHONE,
                "payload": {
                    "task_id": "c",
                    "requested_by": ADMIN_PHONE,
                    "instructions": "x",
                    "timeout_minutes": 30,
                    "execution": {"mode": "script", "command": ["echo"]},
                },
            },
        )
        skillify = self._make_skillify_reminder()
        # Consolidation at minute 0, skillify at minute 30 (after timeout window)
        assert consol["schedule"]["value"] == "0 2 * * *"
        assert skillify["schedule"]["value"] == "30 2 * * *"
        # Verify gap >= timeout
        timeout = consol["event"]["payload"]["timeout_minutes"]
        consol_minute = int(consol["schedule"]["value"].split()[0])
        skillify_minute = int(skillify["schedule"]["value"].split()[0])
        assert (skillify_minute - consol_minute) >= timeout

    def test_timeout_longer_than_consolidation(self):
        r = self._make_skillify_reminder()
        assert r["event"]["payload"]["timeout_minutes"] >= 45


# ── Validation edge cases ────────────────────────────────────────────────


class TestEventTemplateValidation:
    """Negative tests for event template validation."""

    def test_missing_topic_raises(self):
        with pytest.raises(ValueError, match="topic"):
            validate_event_template({
                "type": "task.requested",
                "key": ADMIN_PHONE,
                "payload": {"task_id": "x", "requested_by": ADMIN_PHONE,
                             "instructions": "x",
                             "execution": {"mode": "script", "command": ["echo"]}},
            })

    def test_missing_type_raises(self):
        with pytest.raises(ValueError, match="type"):
            validate_event_template({
                "topic": "tasks",
                "key": ADMIN_PHONE,
                "payload": {"task_id": "x", "requested_by": ADMIN_PHONE,
                             "instructions": "x",
                             "execution": {"mode": "script", "command": ["echo"]}},
            })

    def test_agent_without_prompt_raises(self):
        with pytest.raises(ValueError, match="prompt"):
            validate_event_template({
                "topic": "tasks",
                "type": "task.requested",
                "key": ADMIN_PHONE,
                "payload": {"task_id": "x", "requested_by": ADMIN_PHONE,
                             "instructions": "x",
                             "execution": {"mode": "agent"}},
            })

    def test_valid_script_without_prompt_ok(self):
        """Script mode doesn't require execution.prompt."""
        validate_event_template({
            "topic": "tasks",
            "type": "task.requested",
            "key": ADMIN_PHONE,
            "payload": {"task_id": "x", "requested_by": ADMIN_PHONE,
                         "instructions": "x",
                         "execution": {"mode": "script", "command": ["echo"]}},
        })


# ── Script file tests (relative to repo root) ───────────────────────────


class TestConsolidationScript:
    """Test the nightly consolidation wrapper script exists and is valid."""

    @property
    def script_path(self):
        return DISPATCH_ROOT / "scripts" / "nightly-consolidation.sh"

    def test_script_exists(self):
        assert self.script_path.exists(), f"Missing: {self.script_path}"

    def test_script_is_executable(self):
        assert os.access(self.script_path, os.X_OK), f"Not executable: {self.script_path}"

    def test_script_references_consolidation_scripts(self):
        content = self.script_path.read_text()
        assert "consolidate_3pass.py" in content
        assert "consolidate_chat.py" in content
        assert "--all" in content

    def test_script_uses_home_variable(self):
        """Script should use $HOME, not hardcoded absolute paths."""
        content = self.script_path.read_text()
        assert "$HOME" in content

    def test_no_set_e_conflict(self):
        """Script should NOT use set -e (conflicts with manual exit code capture)."""
        content = self.script_path.read_text()
        import re
        for line in content.split('\n'):
            stripped = line.strip()
            if stripped.startswith('#'):
                continue
            # Match set -e as a standalone flag (not inside set -o pipefail)
            if re.search(r'\bset\b.*\b-e\b', stripped) and 'pipefail' not in stripped:
                pytest.fail(f"Found 'set -e' on non-comment line: {stripped}")
            if re.search(r'\bset -eo\b', stripped):
                pytest.fail(f"Found 'set -eo' on non-comment line: {stripped}")

    def test_has_pipefail(self):
        content = self.script_path.read_text()
        assert "pipefail" in content


class TestSetupScript:
    """Test the setup script uses config lookup, not hardcoded values."""

    @property
    def script_path(self):
        return DISPATCH_ROOT / "scripts" / "setup-nightly-tasks.py"

    def test_script_exists(self):
        assert self.script_path.exists()

    def test_no_hardcoded_phone(self):
        """Setup script must use config.require, not hardcoded phone numbers."""
        content = self.script_path.read_text()
        assert "ADMIN_PHONE = \"+1" not in content
        assert "config.require" in content

    def test_defines_both_task_ids(self):
        content = self.script_path.read_text()
        assert "nightly-consolidation" in content
        assert "nightly-skillify" in content

    def test_skillify_prompt_single_source(self):
        """Skillify prompt should have a single source of truth constant."""
        content = self.script_path.read_text()
        assert "SKILLIFY_PROMPT" in content

    def test_command_uses_home_variable(self):
        """Setup script should not bake absolute paths into reminders."""
        content = self.script_path.read_text()
        assert "$HOME/dispatch" in content


# ── Behavioral: script-task dedup ────────────────────────────────────────


class TestScriptTaskDedupBehavioral:
    """Behavioral test: verify script tasks are tracked and deduped."""

    def _make_manager(self):
        """Create a minimal Manager mock for testing _handle_task_requested."""
        from assistant.manager import Manager

        class FakeBackend:
            sessions = {}
            async def create_ephemeral_session(self, **kw):
                return MagicMock()

        m = MagicMock(spec=Manager)
        m.sessions = FakeBackend()
        m._producer = MagicMock()
        m._ephemeral_tasks = {}
        m._running_script_tasks = {}
        m._completed_task_traces = {}
        m._shutdown_flag = False
        m._handle_task_requested = Manager._handle_task_requested.__get__(m, Manager)
        m._run_script_task = AsyncMock()
        m._notify_task_event = AsyncMock()
        return m

    @pytest.mark.asyncio
    async def test_script_task_tracked_for_dedup(self):
        """First script task should be tracked in _running_script_tasks."""
        m = self._make_manager()
        payload = {
            "task_id": "test-dedup",
            "title": "Dedup Test",
            "requested_by": "+1",
            "instructions": "test",
            "execution": {"mode": "script", "command": ["echo"]},
        }

        await m._handle_task_requested(payload, {})
        assert "test-dedup" in m._running_script_tasks
        # Let task complete
        await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_duplicate_script_task_skipped(self):
        """Second identical script task should be skipped."""
        m = self._make_manager()
        # Simulate a running script task
        m._running_script_tasks["test-dedup"] = asyncio.create_task(
            asyncio.sleep(10))

        payload = {
            "task_id": "test-dedup",
            "title": "Dedup Test",
            "requested_by": "+1",
            "instructions": "test",
            "execution": {"mode": "script", "command": ["echo"]},
        }

        await m._handle_task_requested(payload, {})
        # Should NOT have called _run_script_task (skipped as duplicate)
        m._run_script_task.assert_not_awaited()
        # Clean up
        m._running_script_tasks["test-dedup"].cancel()
        try:
            await m._running_script_tasks["test-dedup"]
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_completed_script_task_allows_rerun(self):
        """After a script task completes, the same task_id can run again."""
        m = self._make_manager()
        # Simulate a completed task with a done future
        done_future = asyncio.get_running_loop().create_future()
        done_future.set_result(None)
        m._running_script_tasks["test-rerun"] = done_future

        payload = {
            "task_id": "test-rerun",
            "title": "Rerun Test",
            "requested_by": "+1",
            "instructions": "test",
            "execution": {"mode": "script", "command": ["echo"]},
        }

        await m._handle_task_requested(payload, {})
        # Should proceed (done future is done, so not considered running)
        assert "test-rerun" in m._running_script_tasks
        await asyncio.sleep(0)


# ── Behavioral: startup check ───────────────────────────────────────────


class TestStartupCheck:
    """Test _check_nightly_tasks_configured behavioral correctness."""

    def test_warns_when_reminders_missing(self):
        """Should log a warning when nightly reminders are missing."""
        from assistant.manager import Manager

        m = MagicMock(spec=Manager)
        m._check_nightly_tasks_configured = Manager._check_nightly_tasks_configured.__get__(m, Manager)

        # Mock empty reminders
        with patch("assistant.reminders.load_reminders", return_value={"reminders": []}), \
             patch("assistant.reminders.reminders_lock"), \
             patch("assistant.manager.log") as mock_log:
            m._check_nightly_tasks_configured()
            # Should have warned about missing reminders
            mock_log.warning.assert_called()
            warning_msg = mock_log.warning.call_args[0][0]
            assert "nightly" in warning_msg.lower()

    def test_no_warning_when_reminders_present(self):
        """Should log info (not warning) when reminders are configured."""
        from assistant.manager import Manager

        m = MagicMock(spec=Manager)
        m._check_nightly_tasks_configured = Manager._check_nightly_tasks_configured.__get__(m, Manager)

        reminders_data = {"reminders": [
            {"event": {"payload": {"task_id": "nightly-consolidation"}}},
            {"event": {"payload": {"task_id": "nightly-skillify"}}},
            {"event": {"payload": {"task_id": "nightly-bugfinder"}}},
            {"event": {"payload": {"task_id": "nightly-latencyfinder"}}},
            {"event": {"payload": {"task_id": "nightly-vacation-scraper"}}},
        ]}

        with patch("assistant.reminders.load_reminders", return_value=reminders_data), \
             patch("assistant.reminders.reminders_lock"), \
             patch("assistant.manager.log") as mock_log:
            m._check_nightly_tasks_configured()
            mock_log.warning.assert_not_called()
            mock_log.info.assert_called()

    def test_handles_missing_reminders_file_gracefully(self):
        """Should not crash if reminders file doesn't exist."""
        from assistant.manager import Manager

        m = MagicMock(spec=Manager)
        m._check_nightly_tasks_configured = Manager._check_nightly_tasks_configured.__get__(m, Manager)

        with patch("assistant.reminders.load_reminders", side_effect=FileNotFoundError), \
             patch("assistant.reminders.reminders_lock"), \
             patch("assistant.manager.log") as mock_log:
            # Should not raise
            m._check_nightly_tasks_configured()
            mock_log.warning.assert_called()


# ── Output truncation behavioral test ────────────────────────────────────


class TestNoOutputTruncation:
    """Verify stdout/stderr are not truncated in _run_script_task."""

    @pytest.mark.asyncio
    async def test_full_stdout_in_completed_event(self):
        """task.completed event should contain full stdout, not truncated."""
        from assistant.manager import Manager

        # Generate a large stdout (>2000 chars to catch old [-2000:] truncation)
        large_output = "x" * 5000

        m = MagicMock(spec=Manager)
        m._producer = MagicMock()
        m._running_script_tasks = {}
        m._completed_task_traces = {}
        m._shutdown_flag = False
        m._run_script_task = Manager._run_script_task.__get__(m, Manager)
        m._notify_task_event = AsyncMock()

        payload = {
            "task_id": "test-no-truncate",
            "title": "Truncation Test",
            "requested_by": "+1",
            "instructions": "test",
            "notify": False,
            "timeout_minutes": 1,
            "execution": {"mode": "script", "command": ["echo", large_output]},
        }

        # Patch produce_event to capture the payload
        captured_payloads = []
        def capture_produce(producer, topic, event_type, payload_dict, **kwargs):
            captured_payloads.append((event_type, payload_dict))

        with patch("assistant.manager.produce_event", side_effect=capture_produce):
            await m._run_script_task(payload, {})

        # Find the task.completed event
        completed = [p for et, p in captured_payloads if et == "task.completed"]
        assert len(completed) == 1, f"Expected 1 task.completed, got {len(completed)}"
        stdout_val = completed[0].get("stdout", "")
        # Full output should be preserved (echo adds a newline)
        assert len(stdout_val) >= 5000, \
            f"stdout was truncated: got {len(stdout_val)} chars, expected >= 5000"

    @pytest.mark.asyncio
    async def test_stderr_included_on_success(self):
        """task.completed should include stderr even on success (warnings etc)."""
        from assistant.manager import Manager

        m = MagicMock(spec=Manager)
        m._producer = MagicMock()
        m._running_script_tasks = {}
        m._completed_task_traces = {}
        m._shutdown_flag = False
        m._run_script_task = Manager._run_script_task.__get__(m, Manager)
        m._notify_task_event = AsyncMock()

        # Script that writes to both stdout and stderr but exits 0
        payload = {
            "task_id": "test-stderr",
            "title": "Stderr Test",
            "requested_by": "+1",
            "instructions": "test",
            "notify": False,
            "timeout_minutes": 1,
            "execution": {
                "mode": "script",
                "command": ["bash", "-c", "echo stdout_data; echo stderr_data >&2"],
            },
        }

        captured_payloads = []
        def capture_produce(producer, topic, event_type, payload_dict, **kwargs):
            captured_payloads.append((event_type, payload_dict))

        with patch("assistant.manager.produce_event", side_effect=capture_produce):
            await m._run_script_task(payload, {})

        completed = [p for et, p in captured_payloads if et == "task.completed"]
        assert len(completed) == 1
        assert "stderr_data" in completed[0].get("stderr", ""), \
            f"stderr missing from task.completed payload: {completed[0]}"
        assert "stdout_data" in completed[0].get("stdout", "")


# ── Integration test: full fire→consume→execute loop ─────────────────────


class TestIntegrationFireConsumeExecute:
    """Integration test: create a reminder event, produce it to bus,
    consume it via _handle_task_requested, and verify the task executes."""

    @pytest.mark.asyncio
    async def test_reminder_event_fires_script_task(self):
        """End-to-end: reminder event template → bus → task consumer → script runs."""
        import tempfile
        from assistant.manager import Manager

        # 1. Create a reminder with an event template (same as setup script does)
        marker_file = tempfile.mktemp(suffix=".marker")
        event_template = {
            "topic": "tasks",
            "type": "task.requested",
            "key": ADMIN_PHONE,
            "payload": {
                "task_id": "integration-test-1",
                "title": "Integration Test",
                "requested_by": ADMIN_PHONE,
                "instructions": "Create a marker file",
                "notify": False,
                "timeout_minutes": 1,
                "execution": {
                    "mode": "script",
                    "command": ["bash", "-c", f"echo integration_ok > {marker_file}"],
                },
            },
        }

        # Validate the event template (same validation that create_reminder does)
        validate_event_template(event_template)

        # 2. Extract the payload (simulating what the reminder poller does on fire)
        payload = event_template["payload"]

        # 3. Feed it to _handle_task_requested (simulating what the task consumer does)
        class FakeBackend:
            sessions = {}
            async def create_ephemeral_session(self, **kw):
                return MagicMock()

        m = MagicMock(spec=Manager)
        m.sessions = FakeBackend()
        m._producer = MagicMock()
        m._ephemeral_tasks = {}
        m._running_script_tasks = {}
        m._completed_task_traces = {}
        m._shutdown_flag = False
        m._handle_task_requested = Manager._handle_task_requested.__get__(m, Manager)
        m._run_script_task = Manager._run_script_task.__get__(m, Manager)
        m._notify_task_event = AsyncMock()

        captured_events = []
        def capture_produce(producer, topic, event_type, payload_dict, **kwargs):
            captured_events.append((topic, event_type, payload_dict))

        with patch("assistant.manager.produce_event", side_effect=capture_produce):
            await m._handle_task_requested(payload, {})
            # Wait for the script task to complete (it's fire-and-forget via create_task)
            task = m._running_script_tasks.get("integration-test-1")
            if task:
                await task

        # 4. Verify the script actually ran
        marker = Path(marker_file)
        try:
            assert marker.exists(), f"Marker file not created: {marker_file}"
            content = marker.read_text().strip()
            assert content == "integration_ok"
        finally:
            marker.unlink(missing_ok=True)

        # 5. Verify task.completed was produced
        completed = [(t, et, p) for t, et, p in captured_events if et == "task.completed"]
        assert len(completed) == 1, f"Expected 1 task.completed, got {len(completed)}"
        assert completed[0][0] == "tasks"  # topic
        assert completed[0][2]["task_id"] == "integration-test-1"

    @pytest.mark.asyncio
    async def test_reminder_event_fires_agent_task(self):
        """End-to-end: agent-mode task.requested → ephemeral session created."""
        from assistant.manager import Manager

        event_template = {
            "topic": "tasks",
            "type": "task.requested",
            "key": ADMIN_PHONE,
            "payload": {
                "task_id": "integration-agent-1",
                "title": "Agent Integration Test",
                "requested_by": ADMIN_PHONE,
                "instructions": "Run /skillify --nightly",
                "notify": False,
                "timeout_minutes": 5,
                "execution": {
                    "mode": "agent",
                    "prompt": "Run /skillify --nightly",
                },
            },
        }

        validate_event_template(event_template)
        payload = event_template["payload"]

        class FakeBackend:
            sessions = {}
            create_ephemeral_session = AsyncMock(return_value=MagicMock())

        m = MagicMock(spec=Manager)
        m.sessions = FakeBackend()
        m._producer = MagicMock()
        m._ephemeral_tasks = {}
        m._running_script_tasks = {}
        m._completed_task_traces = {}
        m._shutdown_flag = False
        m._handle_task_requested = Manager._handle_task_requested.__get__(m, Manager)
        m._notify_task_event = AsyncMock()

        with patch("assistant.manager.produce_event"):
            await m._handle_task_requested(payload, {})

        # Verify ephemeral session was created
        m.sessions.create_ephemeral_session.assert_awaited_once()
        call_kwargs = m.sessions.create_ephemeral_session.call_args[1]
        assert call_kwargs["task_id"] == "integration-agent-1"
        assert call_kwargs["instructions"] == "Run /skillify --nightly"
        # Verify tracked for timeout supervision
        assert "integration-agent-1" in m._ephemeral_tasks
