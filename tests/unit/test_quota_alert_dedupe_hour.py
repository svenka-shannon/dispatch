"""2026-11-09: the same 90% alert went out twice because resets_at jittered
across a minute boundary (19:59:59.9 vs 20:00:00.1)."""
from assistant import health


def test_reset_jitter_across_minute_boundary_dedupes(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "_quota_alerts_sent", {})
    monkeypatch.setattr(health, "_load_quota_alerts", lambda: None)
    monkeypatch.setattr(health, "_save_quota_alerts", lambda *a, **k: None, raising=False)
    from datetime import datetime, timedelta, timezone
    base = (datetime.now(timezone.utc) + timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
    a = (base - timedelta(milliseconds=100)).isoformat()
    b = (base + timedelta(milliseconds=100)).isoformat()
    first = health.check_quota_thresholds({"seven_day": {"utilization": 90, "resets_at": a}})
    second = health.check_quota_thresholds({"seven_day": {"utilization": 90, "resets_at": b}})
    assert first, "first crossing should alert"
    assert second == [], "jittered reset time must not re-alert"
