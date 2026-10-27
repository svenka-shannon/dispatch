# Staged patches — reviewed, NOT applied

Apply with:

    cd ~/dispatch
    git apply --check staged-patches/<file>.patch   # verify first
    git apply staged-patches/<file>.patch
    claude-assistant restart-sessions                # required: changes SDK session code

---

## 2026-08-31-outage-briefing.patch

**Problem.** The assistant cannot perceive its own downtime. On 2026-08-31 OAuth
died 06:28:53 PDT; the daemon detected it, escalated at 06:29:33, and kept firing
reminders into a dead session. A reminder ("Pack Lactaid in your bag", due 07:27)
was injected, the turn errored in 54ms, and it was marked `message.delivered` and
deleted. Auth recovered ~08:57.

When the session resumed, **nothing in its context said it had been down for 2.5
hours.** Eric texted "Login fixed" — meaning the machine's OAuth — and the
assistant read it as an unrelated Reddit/X login thread and went to check those.
Eric: *"You not being able to see your own outages is a problem."*

**Fix.** New `assistant/outage.py` computes the outage window from `bus.db` and
renders one `[SYSTEM]` line, which the session **prepends to the next turn**.

Hook points:
- `sdk_session.py` → `SDKSession._prepend_outage_briefing()`, called in
  `_run_loop` immediately **before** `self._client.query(msg)`. Prefixing the
  *same* turn is the crux — a follow-up injection lands one turn too late, which
  is exactly how "Login fixed" got misread.
- `sdk_backend.py` → `_recreate_active_sessions`. **Composes** with the existing
  "Session resumed after daemon restart" message rather than replacing it.

Window is derived from `bus.db`, **not** the flag file mtime: `auth_failure.json`
is rewritten on every failing turn, so its `ts` is the *last* failure, not the
first. Uses `MIN(session.auth_failure)` after the session's last clean
`sdk.turn_complete`. Window closes on the first clean `ResultMessage` (new
`session.auth_recovered` bus event).

**Validated against real data**, emits:
`[SYSTEM] You were unreachable 06:28 PDT–08:57 PDT (2h28m, auth failure)... 1 reminder(s) fired... "Pack Lactaid in your bag" (due 07:27 PDT).`

**False-positive guard.** The window opens only on an explicit auth-failure or
restart signal — never inferred from idle time — plus a 120s floor. Verified: the
01:42:39 restart produces no briefing.

**Known cost.** `json_extract` scans on `records` add per-failure query cost, but
only on the failure path.

**Not fixed by this patch** (separate, also real): reminders are deleted as
delivered even when the turn errored. `sdk_session.py:646-654` emits
`message.delivered` because `query()` didn't raise — the error surfaces later as
`ResultMessage.is_error` and is never fed back; `manager.py:1320-1328` then
deletes the one-shot. Proposed separately: treat `is_error` as non-delivery and
gate `process_due_reminders` on `AUTH_FAILURE_FLAG` the way it already gates on
quota.

Verified `git apply --check` clean on branch `feat/sms-length-guard`, 2026-08-31.
