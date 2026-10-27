"""Tests for `claude-assistant remind` list/cancel/add CLI behaviour.

Regression coverage for the silent-cancel bug: `remind list | grep | awk |
xargs remind cancel` silently did nothing because `list` truncated titles with
'..' and `cancel` with no/unknown id did not fail loudly.
"""
import argparse
import json
from unittest.mock import patch

import pytest

LONG_TITLE = "Check Walmart verification code and reply to the support thread"


@pytest.fixture
def reminders_store(tmp_path):
    reminders_file = tmp_path / "reminders.json"
    lock_file = tmp_path / "reminders.lock"
    with patch("assistant.reminders.REMINDERS_FILE", reminders_file), \
            patch("assistant.reminders.LOCK_FILE", lock_file):
        yield reminders_file


def _add(title, contact="+15555550100", in_duration="2h"):
    from assistant.reminders import add_reminder_cli
    return add_reminder_cli(title=title, contact=contact, in_duration=in_duration)


def _ids(reminders_file):
    data = json.loads(reminders_file.read_text())
    return {r["id"]: r["title"] for r in data["reminders"]}


def _list_args(**kw):
    base = dict(remind_command="list", contact=None, failed=False, json=False)
    base.update(kw)
    return argparse.Namespace(**base)


def _cancel_args(**kw):
    base = dict(remind_command="cancel", id=None, title=None, match=None, force=False)
    base.update(kw)
    return argparse.Namespace(**base)


# ─── list ──────────────────────────────────────────────────────


class TestRemindList:
    def test_non_tty_prints_full_title(self, reminders_store, capsys):
        from assistant.cli import cmd_remind
        _add(LONG_TITLE)
        with patch("sys.stdout.isatty", return_value=False):
            assert cmd_remind(_list_args()) == 0
        out = capsys.readouterr().out
        assert LONG_TITLE in out
        assert ".." not in out.replace("...", "")

    def test_tty_truncates_title(self, reminders_store, capsys):
        from assistant.cli import cmd_remind
        _add(LONG_TITLE)
        with patch("sys.stdout.isatty", return_value=True):
            assert cmd_remind(_list_args()) == 0
        out = capsys.readouterr().out
        assert LONG_TITLE not in out
        assert LONG_TITLE[:28] + ".." in out

    def test_json_outputs_full_records(self, reminders_store, capsys):
        from assistant.cli import cmd_remind
        r = _add(LONG_TITLE)
        assert cmd_remind(_list_args(json=True)) == 0
        records = json.loads(capsys.readouterr().out)
        assert len(records) == 1
        rec = records[0]
        assert rec["id"] == r["id"]
        assert rec["title"] == LONG_TITLE
        assert rec["next_fire"] == r["next_fire"]
        assert rec["schedule"]["type"] == "once"
        assert rec["contact"] == "+15555550100"
        assert rec["next_fire_display"]
        assert not any(k.startswith("_") for k in rec)

    def test_json_empty_is_empty_list(self, reminders_store, capsys):
        from assistant.cli import cmd_remind
        assert cmd_remind(_list_args(json=True)) == 0
        assert json.loads(capsys.readouterr().out) == []


# ─── cancel ────────────────────────────────────────────────────


class TestRemindCancel:
    def test_cancel_by_id_still_works(self, reminders_store, capsys):
        from assistant.cli import cmd_remind
        r = _add("one")
        keep = _add("two")
        assert cmd_remind(_cancel_args(id=r["id"])) == 0
        out = capsys.readouterr().out
        assert f"Cancelled: {r['id']}" in out
        assert _ids(reminders_store) == {keep["id"]: "two"}

    def test_cancel_unknown_id_exits_nonzero(self, reminders_store, capsys):
        from assistant.cli import cmd_remind
        _add("one")
        assert cmd_remind(_cancel_args(id="deadbeef")) == 1
        assert "deadbeef" in capsys.readouterr().err
        assert len(_ids(reminders_store)) == 1

    def test_cancel_no_args_exits_nonzero(self, reminders_store, capsys):
        """Empty xargs input → `remind cancel` with no id must fail loudly."""
        from assistant.cli import cmd_remind
        _add("one")
        assert cmd_remind(_cancel_args()) == 1
        assert len(_ids(reminders_store)) == 1

    def test_cancel_match_case_insensitive_full_title(self, reminders_store, capsys):
        from assistant.cli import cmd_remind
        target = _add(LONG_TITLE)
        keep = _add("Daily standup")
        # Substring lies past the old 28-char truncation point
        assert cmd_remind(_cancel_args(match="SUPPORT THREAD")) == 0
        out = capsys.readouterr().out
        assert f"Cancelled: {target['id']} → {LONG_TITLE}" in out
        assert _ids(reminders_store) == {keep["id"]: "Daily standup"}

    def test_cancel_match_cancels_all_matches(self, reminders_store, capsys):
        from assistant.cli import cmd_remind
        a = _add("walmart verification #1")
        b = _add("Re-check Walmart Verification")
        keep = _add("costco run")
        assert cmd_remind(_cancel_args(match="walmart verification")) == 0
        out = capsys.readouterr().out
        assert a["id"] in out and b["id"] in out
        assert "Cancelled 2 reminder(s)" in out
        assert _ids(reminders_store) == {keep["id"]: "costco run"}

    def test_cancel_match_zero_exits_nonzero(self, reminders_store, capsys):
        from assistant.cli import cmd_remind
        _add("costco run")
        assert cmd_remind(_cancel_args(match="walmart")) == 1
        assert "walmart" in capsys.readouterr().err
        assert len(_ids(reminders_store)) == 1

    def test_cancel_match_empty_string_rejected(self, reminders_store, capsys):
        from assistant.cli import cmd_remind
        _add("costco run")
        assert cmd_remind(_cancel_args(match="  ")) == 1
        assert len(_ids(reminders_store)) == 1

    def test_cancel_by_exact_title_still_works(self, reminders_store, capsys):
        from assistant.cli import cmd_remind
        _add("Daily standup")
        assert cmd_remind(_cancel_args(title="Daily standup")) == 0
        assert _ids(reminders_store) == {}

    def test_legacy_cancel_reminder_cli_returns_count(self, reminders_store):
        from assistant.reminders import cancel_reminder_cli
        r = _add("one")
        assert cancel_reminder_cli(reminder_id=r["id"]) == 1
        assert cancel_reminder_cli(reminder_id=r["id"]) == 0


# ─── add ───────────────────────────────────────────────────────


class TestRemindAdd:
    def test_add_prints_capturable_id_line(self, reminders_store, capsys):
        from assistant.cli import cmd_remind
        args = argparse.Namespace(
            remind_command="add", title="ping me", contact="+15555550100",
            in_duration="10m", at_time=None, cron=None, tz=None, target="fg",
            event_json=None,
        )
        assert cmd_remind(args) == 0
        out = capsys.readouterr().out
        id_lines = [l for l in out.splitlines() if l.startswith("ID: ")]
        assert len(id_lines) == 1
        rid = id_lines[0].split()[1]
        assert rid in _ids(reminders_store)


# ─── argparse wiring ───────────────────────────────────────────


def test_parser_accepts_new_flags():
    """`remind list --json` and `remind cancel --match` are wired into argparse."""
    import assistant.cli as cli
    captured = {}

    def fake_remind(args):
        captured["args"] = args
        return 0

    with patch.object(cli, "cmd_remind", fake_remind), \
            patch("sys.argv", ["claude-assistant", "remind", "cancel", "--match", "foo"]):
        assert cli.main() == 0
    assert captured["args"].match == "foo"

    with patch.object(cli, "cmd_remind", fake_remind), \
            patch("sys.argv", ["claude-assistant", "remind", "list", "--json"]):
        assert cli.main() == 0
    assert captured["args"].json is True
