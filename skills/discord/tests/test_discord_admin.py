"""Tests for discord-admin. Run with:

    uv run --with pytest --with httpx pytest ~/.claude/skills/discord/tests/ -q
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import subprocess
from pathlib import Path

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "discord-admin"

PROJECT = "1000000000000000001"
OTHER = "2000000000000000002"
BOT_ID = "9000000000000000009"
KEYCHAIN_TOKEN = "keychain-token-abc"


def load_module():
    loader = importlib.machinery.SourceFileLoader("discord_admin", str(SCRIPT))
    spec = importlib.util.spec_from_loader("discord_admin", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def da(monkeypatch):
    mod = load_module()
    monkeypatch.setattr(mod, "_sleep", lambda s: None)
    return mod


class FakeDiscord:
    """In-memory Discord REST API, served through httpx.MockTransport."""

    def __init__(self):
        self.guilds = {
            PROJECT: {"id": PROJECT, "name": "Project Server", "owner": False},
        }
        self.channels = {
            # project guild
            "11": {"id": "11", "guild_id": PROJECT, "type": 4, "name": "work", "position": 0},
            "12": {"id": "12", "guild_id": PROJECT, "type": 0, "name": "general", "parent_id": "11", "position": 0},
            "13": {"id": "13", "guild_id": PROJECT, "type": 2, "name": "voice", "parent_id": "11", "position": 1},
            "14": {"id": "14", "guild_id": PROJECT, "type": 4, "name": "empty-cat", "position": 1},
            "15": {"id": "15", "guild_id": PROJECT, "type": 0, "name": "loose", "position": 2},
            # another guild (bot can see the channel, e.g. via a shared server)
            "21": {"id": "21", "guild_id": OTHER, "type": 0, "name": "general", "position": 0},
        }
        self.requests: list[httpx.Request] = []
        self.rate_limit_next = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/api/v10")
        m = request.method
        if self.rate_limit_next:
            self.rate_limit_next -= 1
            return httpx.Response(429, headers={"Retry-After": "0.01"}, json={"retry_after": 0.01})
        if m == "GET" and path == "/users/@me/guilds":
            return httpx.Response(200, json=list(self.guilds.values()))
        if m == "GET" and path == "/users/@me":
            return httpx.Response(200, json={"id": BOT_ID, "username": "sven"})
        if path.startswith("/guilds/"):
            parts = path.split("/")
            gid = parts[2]
            if len(parts) == 4 and parts[3] == "channels":
                if m == "GET":
                    return httpx.Response(200, json=[c for c in self.channels.values() if c["guild_id"] == gid])
                if m == "POST":
                    body = json.loads(request.content)
                    new_id = str(100 + len(self.channels))
                    ch = {"id": new_id, "guild_id": gid, **body}
                    self.channels[new_id] = ch
                    return httpx.Response(201, json=ch)
        if path.startswith("/channels/"):
            cid = path.split("/")[2]
            if cid not in self.channels:
                return httpx.Response(404, json={"message": "Unknown Channel", "code": 10003})
            if m == "GET":
                return httpx.Response(200, json=self.channels[cid])
            if m == "DELETE":
                return httpx.Response(200, json=self.channels.pop(cid))
        return httpx.Response(404, json={"message": f"unhandled {m} {path}"})

    @property
    def transport(self):
        return httpx.MockTransport(self.handler)

    def deletes(self):
        return [r.url.path.split("/")[-1] for r in self.requests if r.method == "DELETE"]


@pytest.fixture
def fake():
    return FakeDiscord()


@pytest.fixture
def keychain(monkeypatch, da):
    """Mock `security` (Keychain) and `identity` subprocess calls."""
    calls = []

    def fake_run(cmd, *a, **kw):
        calls.append(cmd)
        if cmd[0] == "security":
            return subprocess.CompletedProcess(cmd, 0, stdout=KEYCHAIN_TOKEN + "\n", stderr="")
        if cmd[0].endswith("identity") and cmd[1] == "discord.guild_ids":
            return subprocess.CompletedProcess(cmd, 0, stdout=f"['{PROJECT}']\n", stderr="")
        raise AssertionError(f"unexpected subprocess: {cmd}")

    monkeypatch.setattr(da.subprocess, "run", fake_run)
    monkeypatch.setattr(da, "_identity_bin", lambda: "/fake/identity")
    return calls


def run(da, fake, *argv):
    return da.main(list(argv), transport=fake.transport)


# ---------------------------------------------------------------- guards


def test_missing_guild_exits_2(da, fake, keychain):
    for argv in (["channels"], ["delete", "12"], ["perms"], ["create-category", "x"],
                 ["create-channel", "x"]):
        with pytest.raises(SystemExit) as exc:
            run(da, fake, *argv)
        assert exc.value.code == 2, argv
    assert fake.requests == []


def test_guild_mismatch_refuses_delete(da, fake, keychain, capsys):
    rc = run(da, fake, "delete", "21", "--guild", PROJECT)
    assert rc == da.EXIT_REFUSED
    assert fake.deletes() == []
    assert "21" in fake.channels
    err = capsys.readouterr().err
    assert "REFUSED" in err and OTHER in err


def test_mixed_ids_refuse_everything(da, fake, keychain):
    """One foreign id in the list means nothing is deleted at all."""
    rc = run(da, fake, "delete", "15", "21", "--guild", PROJECT)
    assert rc == da.EXIT_REFUSED
    assert fake.deletes() == []
    assert "15" in fake.channels


def test_delete_by_name_refused(da, fake, keychain):
    rc = run(da, fake, "delete", "general", "--guild", PROJECT)
    assert rc == da.EXIT_REFUSED
    assert fake.deletes() == []


def test_env_token_ignored(da, fake, keychain, monkeypatch):
    for var in ("DISCORD_TOKEN", "DISCORD_BOT_TOKEN", "BOT_TOKEN"):
        monkeypatch.setenv(var, "env-token-WRONG")
    rc = run(da, fake, "guilds")
    assert rc == 0
    assert fake.requests
    for r in fake.requests:
        assert r.headers["Authorization"] == f"Bot {KEYCHAIN_TOKEN}"


def test_no_keychain_token_fails_even_with_env(da, fake, monkeypatch, capsys):
    monkeypatch.setenv("DISCORD_TOKEN", "env-token-WRONG")
    monkeypatch.setattr(da.subprocess, "run",
                        lambda cmd, *a, **kw: subprocess.CompletedProcess(cmd, 44, stdout="", stderr="not found"))
    rc = run(da, fake, "guilds")
    assert rc == da.EXIT_ERROR
    assert fake.requests == []
    assert "env-token-WRONG" not in capsys.readouterr().err


def test_token_never_printed(da, fake, keychain, capsys):
    run(da, fake, "channels", "--guild", PROJECT)
    run(da, fake, "delete", "21", "--guild", PROJECT)
    out = capsys.readouterr()
    assert KEYCHAIN_TOKEN not in out.out and KEYCHAIN_TOKEN not in out.err


def test_category_without_recursive_refused(da, fake, keychain, capsys):
    rc = run(da, fake, "delete", "11", "--guild", PROJECT)
    assert rc == da.EXIT_REFUSED
    assert fake.deletes() == []
    assert {"11", "12", "13"} <= fake.channels.keys()
    assert "--recursive" in capsys.readouterr().err


def test_bot_not_in_guild(da, fake, keychain, capsys):
    rc = run(da, fake, "channels", "--guild", OTHER)
    assert rc == da.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "not a member" in err
    for word in ("keychain", ".env", "token"):
        assert word not in err.lower()


def test_bad_guild_alias_is_usage_error(da, fake, keychain):
    assert run(da, fake, "channels", "--guild", "riftbound") == da.EXIT_USAGE
    assert fake.requests == []


# ---------------------------------------------------------------- behaviour


def test_project_alias_resolves_via_identity(da, fake, keychain, capsys):
    rc = run(da, fake, "channels", "--guild", "project")
    assert rc == 0
    assert ["/fake/identity", "discord.guild_ids"] in keychain
    out = capsys.readouterr().out
    assert "general" in out and "work" in out


def test_empty_category_deletes_without_recursive(da, fake, keychain):
    assert run(da, fake, "delete", "14", "--guild", PROJECT) == 0
    assert "14" not in fake.channels


def test_recursive_deletes_children_then_category(da, fake, keychain, capsys):
    rc = run(da, fake, "delete", "11", "--guild", PROJECT, "--recursive", "--reason", "cleanup test")
    assert rc == 0
    dels = fake.deletes()
    assert set(dels) == {"11", "12", "13"}
    assert dels[-1] == "11"
    for r in fake.requests:
        if r.method == "DELETE":
            assert r.headers["X-Audit-Log-Reason"] == "cleanup test"
    out = capsys.readouterr().out
    assert "GONE (3)" in out and "STILL PRESENT (0)" in out


def test_report_uses_relist_not_http_codes(da, fake, keychain, capsys):
    """DELETE returns 200 but the channel is still listed -> reported as remaining."""
    orig = fake.handler

    def lying(request):
        if request.method == "DELETE":
            fake.requests.append(request)
            return httpx.Response(200, json={})
        return orig(request)

    rc = da.main(["delete", "15", "--guild", PROJECT], transport=httpx.MockTransport(lying))
    assert rc == da.EXIT_INCOMPLETE
    out = capsys.readouterr().out
    assert "GONE (0)" in out and "STILL PRESENT (1)" in out


def test_dry_run_deletes_nothing(da, fake, keychain):
    assert run(da, fake, "delete", "11", "--guild", PROJECT, "--recursive", "--dry-run") == 0
    assert fake.deletes() == []


def test_honors_429_retry_after(da, fake, keychain, monkeypatch):
    slept = []
    monkeypatch.setattr(da, "_sleep", slept.append)
    fake.rate_limit_next = 2
    assert run(da, fake, "guilds") == 0
    assert len(slept) == 2 and all(s >= 0.01 for s in slept)


def test_create_channel_rejects_foreign_category(da, fake, keychain):
    rc = run(da, fake, "create-channel", "x", "--guild", PROJECT, "--category", "21")
    assert rc == da.EXIT_REFUSED
    assert not any(r.method == "POST" for r in fake.requests)


def test_create_channel_under_category(da, fake, keychain):
    rc = run(da, fake, "create-channel", "new-voice", "--guild", PROJECT, "--category", "11", "--type", "voice")
    assert rc == 0
    post = [r for r in fake.requests if r.method == "POST"][0]
    assert json.loads(post.content) == {"name": "new-voice", "type": 2, "parent_id": "11"}
    assert "X-Audit-Log-Reason" in post.headers


def test_no_dangerous_commands():
    mod = load_module()
    parser = mod.build_parser()
    sub = next(a for a in parser._actions if a.dest == "command")
    for bad in ("transfer", "transfer-ownership", "delete-guild", "kick", "ban"):
        assert bad not in sub.choices
    assert "ownership" in parser.epilog
