"""Tests for bin/package-watch.

Written 2026-09-23 after the watcher failed SILENTLY twice in 24 hours, both
times while appearing healthy:

  1. launchd runs with a minimal PATH and could not find `uv` for the script's
     shebang → exit 127 on every scheduled run, no log, no alert.
  2. The ETA parser only accepted "%Y-%m-%d"; FedEx actually sends "9/25/26".
     Every parse raised into a bare `except: continue`, so the 1-day-out alert
     could never fire. The first-ETA alert still worked, which is precisely why
     nothing looked wrong.

Both were found by hand, late, by luck. Hence these.
"""
import importlib.machinery
import importlib.util
import json
import os
from datetime import date, timedelta
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "bin" / "package-watch"


def _load():
    loader = importlib.machinery.SourceFileLoader("package_watch", str(SCRIPT))
    spec = importlib.util.spec_from_loader("package_watch", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


pw = _load()


class TestParseEta:
    """The bug that could never fire an alert."""

    @pytest.mark.parametrize("raw,expected", [
        ("9/25/26", date(2026, 9, 25)),        # FedEx — the real-world format
        ("09/25/26", date(2026, 9, 25)),
        ("9/25/2026", date(2026, 9, 25)),
        ("2026-09-25", date(2026, 9, 25)),     # ISO, the only one I handled
        ("25 September 2026", date(2026, 9, 25)),  # USPS prose
        ("Sep 25, 2026", date(2026, 9, 25)),
    ])
    def test_carrier_formats_parse(self, raw, expected):
        assert pw._parse_eta(raw) == expected

    def test_fedex_format_is_not_silently_dropped(self):
        # The exact regression: this returned None, hit `except: continue`,
        # and the 1-day-out alert never fired.
        assert pw._parse_eta("9/25/26") is not None

    @pytest.mark.parametrize("raw", ["", None, "garbage", "tomorrow"])
    def test_unparseable_returns_none_not_raises(self, raw):
        assert pw._parse_eta(raw) is None


class TestNotifyVerifiesDelivery:
    """send-sms exits non-zero when chat.db reports the send failed. The old
    usps-watch ignored that and logged 'notice sent' for a message that does not
    exist in chat.db. State must NOT advance on a failed send, or the retry is
    lost forever."""

    def test_failed_send_returns_false(self, monkeypatch):
        class P:
            returncode = 1
            stderr = b"ERROR: NOT DELIVERED (chat.db error=22)"
        monkeypatch.setattr(pw.subprocess, "run", lambda *a, **k: P())
        assert pw.notify("+15550000000", "x") is False

    def test_successful_send_returns_true(self, monkeypatch):
        class P:
            returncode = 0
            stderr = b""
        monkeypatch.setattr(pw.subprocess, "run", lambda *a, **k: P())
        assert pw.notify("+15550000000", "x") is True

    def test_send_exception_is_not_reported_as_success(self, monkeypatch):
        def boom(*a, **k):
            raise OSError("send-sms missing")
        monkeypatch.setattr(pw.subprocess, "run", boom)
        assert pw.notify("+15550000000", "x") is False


class TestShebangIsLaunchdSafe:
    """Regression for the exit-127 failure: the script is invoked directly by a
    LaunchAgent, so anything on its shebang must be resolvable from launchd's
    minimal PATH — or the plist must supply a PATH that contains it."""

    def test_launchagent_supplies_path_for_the_shebang_interpreter(self):
        plist = Path.home() / "Library/LaunchAgents/com.dispatch.package-watch.plist"
        if not plist.exists():
            pytest.skip("LaunchAgent not installed on this machine")
        text = plist.read_text()
        shebang = SCRIPT.read_text().splitlines()[0]
        if "uv" in shebang:
            assert "EnvironmentVariables" in text, (
                "shebang needs uv but the plist sets no PATH — this is the "
                "exit-127 silent failure"
            )
            assert "/opt/homebrew/bin" in text, "uv lives in /opt/homebrew/bin"


class TestConversationDeferral:
    """Don't talk over a live conversation.

    2026-09-23 and AGAIN 2026-09-24: a package question was answered by hand and
    the watcher repeated the same fact ~60s later. The first fix was "remember to
    seed state after reporting manually" — a mitigation that relies on memory, and
    it failed within 24h. This is the structural version.
    """

    def _fake_db(self, tmp_path, age_seconds):
        import sqlite3
        import time as _t
        db = tmp_path / "chat.db"
        con = sqlite3.connect(db)
        con.executescript(
            "CREATE TABLE handle(ROWID INTEGER PRIMARY KEY, id TEXT);"
            "CREATE TABLE message(ROWID INTEGER PRIMARY KEY, handle_id INT,"
            " is_from_me INT, date INT);"
            "INSERT INTO handle VALUES (1,'+15550001111');"
        )
        stamp = int((_t.time() - age_seconds - 978307200) * 1_000_000_000)
        con.execute("INSERT INTO message VALUES (1,1,1,?)", (stamp,))
        con.commit()
        con.close()
        return db

    def test_recent_outbound_defers(self, tmp_path, monkeypatch):
        db = self._fake_db(tmp_path, age_seconds=60)
        monkeypatch.setattr(pw.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(pw, "HOME", tmp_path, raising=False)
        # recently_spoke builds its path as Path.home()/"Library/Messages/chat.db"
        target = tmp_path / "Library" / "Messages"
        target.mkdir(parents=True, exist_ok=True)
        db.replace(target / "chat.db")
        assert pw.recently_spoke("+15550001111", 20) is True

    def test_old_outbound_does_not_defer(self, tmp_path, monkeypatch):
        db = self._fake_db(tmp_path, age_seconds=3 * 3600)
        monkeypatch.setattr(pw.Path, "home", staticmethod(lambda: tmp_path))
        target = tmp_path / "Library" / "Messages"
        target.mkdir(parents=True, exist_ok=True)
        db.replace(target / "chat.db")
        assert pw.recently_spoke("+15550001111", 20) is False

    def test_missing_db_fails_open(self, tmp_path, monkeypatch):
        """A broken check must never MUTE a real alert."""
        monkeypatch.setattr(pw.Path, "home", staticmethod(lambda: tmp_path))
        assert pw.recently_spoke("+15550001111", 20) is False

    def test_time_module_is_imported(self):
        """Regression: `time` was used by recently_spoke but never imported, so
        every call raised NameError into a bare `except: return False` — the
        guard silently never fired. The unit test passed; only checking the LIVE
        value caught it."""
        assert hasattr(pw, "time"), "package-watch must import time"
        assert callable(pw.time.time)


class TestDailyDigest:
    """2026-09-24, Eric: "I don't need a text every milestone, just a daily or
    sub-daily tracker ... nice to know the day before it arrives and the day it
    arrives, as well as periodic reminders of the eta."

    The event-driven model is gone. These pin the replacement: exactly one
    message per day, covering everything, whether or not anything changed.
    """

    def _harness(self, tmp_path, monkeypatch, results, cfg, hour=10,
                 today=(2026, 9, 24)):
        import datetime as _dt
        import json as _j
        pw.CONFIG = tmp_path / "c.json"
        pw.STATE = tmp_path / "s.json"
        pw.LOG = tmp_path / "l.txt"
        pw.CONFIG.write_text(_j.dumps(cfg))
        sent = []
        monkeypatch.setattr(pw, "notify", lambda c, t: (sent.append(t), True)[1])
        monkeypatch.setattr(pw, "recently_spoke", lambda c, minutes=20: False)

        class D(_dt.date):
            @classmethod
            def today(cls):
                return _dt.date(*today)

        class DT(_dt.datetime):
            @classmethod
            def now(cls):
                return _dt.datetime(*today, hour, 0)

        monkeypatch.setattr(pw, "date", D)
        monkeypatch.setattr(pw, "datetime", DT)

        class R:
            stdout = _j.dumps(results).encode()
            stderr = b""

        monkeypatch.setattr(pw.subprocess, "run", lambda *a, **k: R())
        return sent

    def test_one_message_per_day_not_per_milestone(self, tmp_path, monkeypatch):
        sent = self._harness(
            tmp_path, monkeypatch,
            [{"tracking": "1" * 12, "status": "On the way", "delivered_at": None,
              "eta": "9/25/26"},
             {"tracking": "2" * 12, "status": "On the way", "delivered_at": None,
              "eta": "9/25/26"}],
            [{"tracking": "1" * 12, "label": "box A", "contact": "+1555"},
             {"tracking": "2" * 12, "label": "box B", "contact": "+1555"}])
        pw.main()
        assert len(sent) == 1, "two parcels must not mean two texts"
        assert "box A" in sent[0] and "box B" in sent[0]
        pw.main(); pw.main()
        assert len(sent) == 1, "re-running the same day must not re-send"

    def test_digest_holds_until_digest_hour(self, tmp_path, monkeypatch):
        sent = self._harness(
            tmp_path, monkeypatch,
            [{"tracking": "1" * 12, "status": "On the way", "delivered_at": None,
              "eta": "9/25/26"}],
            [{"tracking": "1" * 12, "label": "box A", "contact": "+1555"}],
            hour=6)
        pw.main()
        assert sent == [], "must not fire before DIGEST_HOUR"

    def test_day_before_and_day_of_are_called_out(self, tmp_path, monkeypatch):
        sent = self._harness(
            tmp_path, monkeypatch,
            [{"tracking": "1" * 12, "status": "On the way", "delivered_at": None,
              "eta": "9/25/26"},
             {"tracking": "2" * 12, "status": "On the way", "delivered_at": None,
              "eta": "9/24/26"}],
            [{"tracking": "1" * 12, "label": "tomorrow one", "contact": "+1555"},
             {"tracking": "2" * 12, "label": "today one", "contact": "+1555"}])
        pw.main()
        assert "arrives tomorrow" in sent[0]
        assert "ARRIVING TODAY" in sent[0]

    def test_unshipped_orders_appear_with_age(self, tmp_path, monkeypatch):
        sent = self._harness(
            tmp_path, monkeypatch, [],
            [{"order": "TR103896", "label": "monitor stand",
              "ordered": "2026-09-20", "alert_after_days": 7, "contact": "+1555"}])
        pw.main()
        # Must NOT assert "not shipped" — we only know we lack a tracking
        # number. On 2026-09-24 that overclaim told the user his monitor
        # stand was stuck when it had in fact already been delivered.
        assert "no tracking number known" in sent[0]
        assert "not shipped" not in sent[0]
        assert "4d since order" in sent[0]

    def test_passed_eta_is_not_reported_as_an_eta(self, tmp_path, monkeypatch):
        """A carrier keeps serving a stale ETA after a parcel slips; saying
        'eta 9/22' on 9/24 reads as though it already arrived."""
        sent = self._harness(
            tmp_path, monkeypatch,
            [{"tracking": "1" * 12, "status": "On the way", "delivered_at": None,
              "eta": "9/22/26"}],
            [{"tracking": "1" * 12, "label": "late one", "contact": "+1555"}])
        pw.main()
        assert "has passed" in sent[0] and "late, not lost" in sent[0]


class TestItActuallyRuns:
    """Every test above calls pw.main() directly — which means NONE of them can
    catch the script failing to invoke main() at all.

    2026-09-24: an edit removed the `if __name__ == "__main__": sys.exit(main())`
    block. The file imported fine, all 24 tests passed, and running it produced
    exit 0 with no output — it would have silently done nothing under launchd
    forever. Same shape as the missing `import time` and the launchd PATH bug:
    the unit tests exercised the function, never the invocation.
    """

    def test_script_has_an_entrypoint(self):
        src = REPLY_SRC = (Path.home() / "dispatch/bin/package-watch").read_text()
        assert '__name__ == "__main__"' in src, "script must invoke main()"
        assert "sys.exit(main())" in src

    def test_runs_end_to_end_as_a_subprocess(self, tmp_path):
        """Execute it the way launchd does and require real output."""
        import subprocess as sp
        env = dict(os.environ)
        # Isolated: never touch the live config/state or text anyone from pytest.
        (tmp_path / "package-watch.json").write_text("[]")
        env["PACKAGE_WATCH_STATE_DIR"] = str(tmp_path)
        env["PACKAGE_WATCH_LOG"] = str(tmp_path / "pw.log")
        r = sp.run([str(Path.home() / "dispatch/bin/package-watch")],
                   capture_output=True, timeout=300, env=env)
        assert r.returncode == 0, r.stderr.decode()[:400]
        out = r.stdout.decode()
        assert out.strip(), "a run that logs nothing is a run that did nothing"


def test_digest_hour_not_after_first_launchd_slot():
    """2026-09-25: DIGEST_HOUR=9 with a first slot at 08:15 held the morning
    digest until 12:15 — after FedEx had already delivered at 10:05."""
    import plistlib, importlib.machinery, importlib.util
    from pathlib import Path
    plist = Path.home() / "Library/LaunchAgents/com.dispatch.package-watch.plist"
    if not plist.exists():
        import pytest
        pytest.skip("launchd plist not installed on this machine")
    slots = plistlib.loads(plist.read_bytes())["StartCalendarInterval"]
    first = min((s["Hour"], s.get("Minute", 0)) for s in slots)
    src = Path(__file__).resolve().parents[2] / "bin/package-watch"
    loader = importlib.machinery.SourceFileLoader("pw_hourcheck", str(src))
    spec = importlib.util.spec_from_loader("pw_hourcheck", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    assert mod.DIGEST_HOUR <= first[0], (
        f"DIGEST_HOUR={mod.DIGEST_HOUR} is after the first run at "
        f"{first[0]:02d}:{first[1]:02d}; the morning digest would slip to the next slot")


class TestDeliveredIsNewsOnce:
    """2026-09-26, Eric: "Why are you pinging me about shit delivered already".
    The 08:15 digest listed three parcels delivered 1-2 days earlier."""

    def _h(self, tmp_path, monkeypatch, results, cfg, today=(2026, 9, 26)):
        return TestDailyDigest()._harness(tmp_path, monkeypatch, results, cfg,
                                          today=today)

    def test_delivered_on_earlier_day_is_silent_and_retired(self, tmp_path, monkeypatch):
        import json as _j
        sent = self._h(tmp_path, monkeypatch,
            [{"tracking": "1" * 12, "status": "Delivered", "eta": "9/25/26 10:05 AM"}],
            [{"tracking": "1" * 12, "label": "old box", "contact": "+1555"}])
        pw.main()
        assert sent == [], "a parcel delivered yesterday is not news today"
        assert _j.loads(pw.CONFIG.read_text()) == [], "it must be retired from config"

    def test_delivered_today_reported_once_then_retired(self, tmp_path, monkeypatch):
        import json as _j
        cfg = [{"tracking": "1" * 12, "label": "new box", "contact": "+1555"}]
        res = [{"tracking": "1" * 12, "status": "Delivered", "eta": "9/26/26 10:05 AM"}]
        sent = self._h(tmp_path, monkeypatch, res, cfg)
        pw.main()
        assert len(sent) == 1 and "new box" in sent[0] and "DELIVERED" in sent[0]
        # next day: must not repeat, and gets retired
        sent2 = self._h(tmp_path, monkeypatch, res, cfg, today=(2026, 9, 27))
        pw.STATE.write_text(_j.dumps({f"{'1'*12}:delivered_reported": "2026-09-26"}))
        pw.main()
        assert sent2 == []
        assert _j.loads(pw.CONFIG.read_text()) == []

    def test_in_transit_parcel_survives_retirement_of_delivered_one(self, tmp_path, monkeypatch):
        import json as _j
        sent = self._h(tmp_path, monkeypatch,
            [{"tracking": "1" * 12, "status": "Delivered", "eta": "9/24/26"},
             {"tracking": "2" * 12, "status": "On the way", "eta": "9/28/26"}],
            [{"tracking": "1" * 12, "label": "old", "contact": "+1555"},
             {"tracking": "2" * 12, "label": "moving", "contact": "+1555"}])
        pw.main()
        assert len(sent) == 1 and "moving" in sent[0] and "old" not in sent[0]
        assert [w["label"] for w in _j.loads(pw.CONFIG.read_text())] == ["moving"]


import pytest as _pytest


@_pytest.fixture(autouse=True)
def _isolate_log(tmp_path, monkeypatch):
    """Tests must not write fake 'SEND FAILED +15550000000' lines into the live log."""
    monkeypatch.setattr(pw, "LOG", tmp_path / "pw-test.log")


def test_delivered_yesterday_with_only_delivered_at_is_retired(tmp_path, monkeypatch):
    """2026-10-02: single-package FedEx results carry delivered_at, eta=None → parcel was
    re-announced the next morning ("we already discussed its arrival")."""
    import json as _j
    sent = TestDailyDigest()._harness(
        tmp_path, monkeypatch,
        [{"tracking": "1" * 12, "status": "Delivered", "eta": None, "delivered_at": "10/01/2026 10:10 am"}],
        [{"tracking": "1" * 12, "label": "plate", "contact": "+1555"}], today=(2026, 10, 2))
    pw.main()
    assert sent == []
    assert _j.loads(pw.CONFIG.read_text()) == []
