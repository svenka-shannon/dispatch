"""Tests for the `chrome` CLI's errored-extension detection helpers.

Covers `extension_prefs_state()` / `_describe_disable_reasons()` — the logic
`chrome wake` uses to turn a "service worker didn't come back" timeout into a
*specific* escalation (confirmed-disabled vs. enabled-but-wedged vs. unknown).

The `chrome` CLI has no `.py` extension, so it's loaded via importlib. The
Chrome profile dir is monkeypatched to a tmp dir so no real Chrome state is
touched.
"""
import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

import pytest

CHROME_CLI_PATH = (
    Path(__file__).resolve().parent.parent
    / "skills" / "chrome-control" / "scripts" / "chrome"
)


@pytest.fixture(scope="module")
def chrome_mod():
    spec = importlib.util.spec_from_loader(
        "chrome_cli_under_test",
        importlib.machinery.SourceFileLoader("chrome_cli_under_test", str(CHROME_CLI_PATH)),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["chrome_cli_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def _write_prefs(profile_dir: Path, fname: str, ext_id: str, entry):
    profile_dir.mkdir(parents=True, exist_ok=True)
    prefs = {"extensions": {"settings": {ext_id: entry}}}
    (profile_dir / fname).write_text(json.dumps(prefs))


# --- _describe_disable_reasons ------------------------------------------------

def test_describe_disable_reasons_empty(chrome_mod):
    assert chrome_mod._describe_disable_reasons(0) == []
    assert chrome_mod._describe_disable_reasons(None) == []


def test_describe_disable_reasons_single(chrome_mod):
    assert chrome_mod._describe_disable_reasons(1) == ["disabled by user"]


def test_describe_disable_reasons_combined(chrome_mod):
    # 1 (user) | 4 (needs reload)
    desc = chrome_mod._describe_disable_reasons(1 | 4)
    assert "disabled by user" in desc
    assert "needs a reload" in desc


def test_describe_disable_reasons_unknown_bit(chrome_mod):
    # A bit we don't have a name for falls back to a code string.
    desc = chrome_mod._describe_disable_reasons(1 << 20)
    assert desc and "reason code" in desc[0]


# --- extension_prefs_state ----------------------------------------------------

EXT_ID = "cpmffhepnhgdhdamobkamndnfndilngo"


def test_prefs_state_not_found(chrome_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(chrome_mod, "CHROME_DEFAULT_PROFILE_DIR", tmp_path / "Default")
    # No Preferences files at all.
    assert chrome_mod.extension_prefs_state(EXT_ID) == {"found": False}


def test_prefs_state_extension_absent_from_prefs(chrome_mod, tmp_path, monkeypatch):
    profile_dir = tmp_path / "Default"
    profile_dir.mkdir(parents=True)
    (profile_dir / "Preferences").write_text(json.dumps({"extensions": {"settings": {}}}))
    monkeypatch.setattr(chrome_mod, "CHROME_DEFAULT_PROFILE_DIR", profile_dir)
    assert chrome_mod.extension_prefs_state(EXT_ID) == {"found": False}


def test_prefs_state_enabled(chrome_mod, tmp_path, monkeypatch):
    profile_dir = tmp_path / "Default"
    _write_prefs(profile_dir, "Preferences", EXT_ID, {"state": 1})
    monkeypatch.setattr(chrome_mod, "CHROME_DEFAULT_PROFILE_DIR", profile_dir)
    res = chrome_mod.extension_prefs_state(EXT_ID)
    assert res["found"] is True
    assert res["enabled"] is True
    assert res["reasons"] == []


def test_prefs_state_disabled_by_state(chrome_mod, tmp_path, monkeypatch):
    profile_dir = tmp_path / "Default"
    _write_prefs(profile_dir, "Preferences", EXT_ID, {"state": 0})
    monkeypatch.setattr(chrome_mod, "CHROME_DEFAULT_PROFILE_DIR", profile_dir)
    res = chrome_mod.extension_prefs_state(EXT_ID)
    assert res["found"] is True
    assert res["enabled"] is False


def test_prefs_state_disabled_with_reasons(chrome_mod, tmp_path, monkeypatch):
    profile_dir = tmp_path / "Default"
    _write_prefs(profile_dir, "Preferences", EXT_ID, {"state": 1, "disable_reasons": 1})
    monkeypatch.setattr(chrome_mod, "CHROME_DEFAULT_PROFILE_DIR", profile_dir)
    res = chrome_mod.extension_prefs_state(EXT_ID)
    assert res["found"] is True
    assert res["enabled"] is False
    assert "disabled by user" in res["reasons"]


def test_prefs_state_disable_reasons_as_list(chrome_mod, tmp_path, monkeypatch):
    profile_dir = tmp_path / "Default"
    _write_prefs(profile_dir, "Preferences", EXT_ID, {"state": 1, "disable_reasons": [1, 4]})
    monkeypatch.setattr(chrome_mod, "CHROME_DEFAULT_PROFILE_DIR", profile_dir)
    res = chrome_mod.extension_prefs_state(EXT_ID)
    assert res["enabled"] is False
    assert "disabled by user" in res["reasons"]
    assert "needs a reload" in res["reasons"]


def test_prefs_state_falls_back_to_secure_preferences(chrome_mod, tmp_path, monkeypatch):
    profile_dir = tmp_path / "Default"
    profile_dir.mkdir(parents=True)
    # Preferences has no entry; Secure Preferences does.
    (profile_dir / "Preferences").write_text(json.dumps({"extensions": {"settings": {}}}))
    _write_prefs(profile_dir, "Secure Preferences", EXT_ID, {"state": 0})
    monkeypatch.setattr(chrome_mod, "CHROME_DEFAULT_PROFILE_DIR", profile_dir)
    res = chrome_mod.extension_prefs_state(EXT_ID)
    assert res["found"] is True
    assert res["enabled"] is False


def test_prefs_state_corrupt_json_is_graceful(chrome_mod, tmp_path, monkeypatch):
    profile_dir = tmp_path / "Default"
    profile_dir.mkdir(parents=True)
    (profile_dir / "Preferences").write_text("{ not valid json")
    monkeypatch.setattr(chrome_mod, "CHROME_DEFAULT_PROFILE_DIR", profile_dir)
    assert chrome_mod.extension_prefs_state(EXT_ID) == {"found": False}
