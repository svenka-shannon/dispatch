#!/usr/bin/env -S uv run --script
"""
Set up nightly ephemeral tasks via the reminder/scheduler system.

Creates five cron reminders that fire task.requested events:
1. Memory consolidation (script mode, 2:00am) - runs consolidate_3pass + consolidate_chat
2. Skillify analysis (agent mode, 2:10am) - runs /skillify --nightly
3. Quota scheduler (agent mode, 2:00am) - dynamically triggers bugfinder + latency finder
4. Sven Times gazette (agent mode, 2:40am) - generates daily gazette from system activity

Note: Vacation house scraper is registered separately via claude-assistant remind.

Usage:
    setup-nightly-tasks.py           # Add all reminders
    setup-nightly-tasks.py --list    # Show existing nightly task reminders
    setup-nightly-tasks.py --remove  # Remove existing nightly task reminders
"""
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml", "croniter"]
# ///

import sys
from pathlib import Path

# Add dispatch to path
sys.path.insert(0, str(Path.home() / "dispatch"))

from assistant import config
from assistant.reminders import (
    create_reminder, load_reminders, save_reminders, reminders_lock,
)


def _get_admin_phone() -> str:
    """Look up admin phone from config (never hardcode)."""
    return config.require("owner.phone")


# Task IDs are stable so we can detect duplicates
CONSOLIDATION_TASK_ID = "nightly-consolidation"
SKILLIFY_TASK_ID = "nightly-skillify"
BUGFINDER_TASK_ID = "nightly-bugfinder"
LATENCYFINDER_TASK_ID = "nightly-latencyfinder"
SVENTIMES_TASK_ID = "nightly-sventimes"

NIGHTLY_TASK_IDS = {CONSOLIDATION_TASK_ID, SKILLIFY_TASK_ID, BUGFINDER_TASK_ID, LATENCYFINDER_TASK_ID, SVENTIMES_TASK_ID, "quota-scheduler"}

# Sven Times daily gazette prompt
SVENTIMES_PROMPT = (
    "Generate today's edition of The Svenka Times — a daily gazette of system activity.\n"
    "Project dir: ~/code/svenka-times (this box's own version; upstream's svenflow.ai repo is not available here).\n\n"
    "TONE & STYLE: Professional newspaper voice with understated wit. Third-person perspective. "
    "Write as if reporting on a complex technical system for an audience of engineers. "
    "Example lead: 'The messaging subsystem processed 400 inbound messages across eight active sessions "
    "yesterday, while a nightly consolidation pass merged conversation context for all contacts.'\n\n"
    "Steps:\n"
    "1. GATHER DATA: Run: ~/code/svenka-times/scripts/gather-daily-data (last 24h). "
    "It outputs aggregate counts only and already drops anything failing OPSEC.\n\n"
    "2. ARCHIVE FIRST: Run: ~/code/svenka-times/scripts/archive-edition\n"
    "   This archives the CURRENT front page (yesterday's edition) before you overwrite it. Do not do this manually.\n\n"
    "3. READ TEMPLATE: Read ~/code/svenka-times/template.html — replicate its exact HTML structure, CSS, and class names. "
    "Do NOT invent new structure. Fill BOTH meta tags (edition-date = YYYY-MM-DD, edition-headline = lead headline); "
    "archive-edition depends on them.\n\n"
    "4. READ BLOCKLIST: Read ~/code/svenka-times/scripts/opsec-blocklist.txt. Contact names from Contacts.app are also "
    "blocked automatically by the verifier. These CATEGORIES are always denied:\n"
    "   - Infrastructure identifiers: IP addresses, ports, hostnames, API keys, session IDs, chat IDs, commit SHAs\n"
    "   - Media services: streaming, downloading, media automation, media management\n"
    "   - Real estate: property searches, listings, addresses, house hunting\n"
    "   - PII: phone numbers, email addresses, real names of ANY person (the assistant's own name, Svenka, is fine)\n"
    "   - What people asked for or talked about: never describe message contents, only volumes and system behavior\n"
    "   When in doubt, OMIT. Never mention what was filtered — just skip it entirely.\n\n"
    "5. PLAN CONTENT: Before writing HTML, plan the gazette as structured notes:\n"
    "   Wire ticker: 2-4 short updates\n"
    "   Lead article: kicker, headline, 1-sentence summary, 150-250 word body with a pull-quote\n"
    "   By-the-numbers sidebar: 6-8 stats taken straight from the data\n"
    "   2 dispatch articles: kicker, headline, 60-100 word summary\n"
    "   3-4 briefing items: headline + 1-2 sentence description\n"
    "   Editor's desk: 2-3 sentences, 50-80 words\n"
    "   Valid kickers: INFRASTRUCTURE, MESSAGING, SESSION MANAGEMENT, HEALTH, DEVELOPMENT, AUTOMATION, OPERATIONS\n"
    "   Every number must come from the gathered data. Do not invent figures.\n"
    "   SPARSE DATA: If messages.received < 10 AND tasks.completed_titles is empty AND git_commits is empty, "
    "generate a 'quiet day' edition — focus on uptime/stability themes, system health metrics, shorter content.\n"
    "   ERROR: If gather-daily-data exits non-zero or returns {\"error\": ...}, generate a minimal edition "
    "noting data was unavailable with a brief system status.\n\n"
    "6. WRITE HTML to ~/code/svenka-times/pages/times/index.html.\n"
    "   Dateline format: 'Vol. 1 · [Full weekday], [Month] [Day], [Year]' (e.g., 'Vol. 1 · Friday, March 21, 2026')\n"
    "   Use the drop-cap class on the lead article's first paragraph and include the pull-quote.\n"
    "   No external fonts, scripts or images.\n\n"
    "7. OPSEC VERIFICATION (MANDATORY): Run the deterministic verifier:\n"
    "   ~/code/svenka-times/scripts/verify-opsec ~/code/svenka-times/pages/times/index.html\n"
    "   If it reports violations, fix them in the HTML and re-run until it prints CLEAN.\n"
    "   Do NOT proceed to deploy until verify-opsec exits with code 0.\n\n"
    "8. DEPLOY: bash ~/code/svenka-times/deploy.sh (re-verifies, then publishes the public 'times' folder to svenka-pages).\n"
    "   If deploy fails, do NOT retry. Log the error and exit.\n\n"
    "9. Do NOT send any SMS notification — this runs silently."
)

# Skillify prompt (single source of truth, used in both instructions and execution.prompt)
SKILLIFY_PROMPT = (
    "Run /skillify --nightly to analyze today's conversations for "
    "new skill opportunities and improvements to existing skills. "
    "This runs the full discovery→refinement pipeline. "
    "When done, send a concise summary of findings to the admin via SMS."
)


def _build_consolidation_reminder(admin_phone: str) -> dict:
    """Build the consolidation reminder config."""
    return {
        "title": "Nightly memory consolidation",
        "schedule_type": "cron",
        "schedule_value": "0 2 * * *",  # 2am daily
        "tz_name": "America/New_York",
        "event": {
            "topic": "tasks",
            "type": "task.requested",
            "key": admin_phone,
            "payload": {
                "task_id": CONSOLIDATION_TASK_ID,
                "title": "Nightly memory consolidation",
                "requested_by": admin_phone,
                "instructions": "Run the nightly memory consolidation scripts",
                "notify": True,
                "timeout_minutes": 90,
                "execution": {
                    "mode": "script",
                    # Store $HOME-relative path; bash expands $HOME at runtime
                    "command": [
                        "bash", "-c",
                        "$HOME/dispatch/scripts/nightly-consolidation.sh",
                    ],
                },
            },
        },
    }


def _build_skillify_reminder(admin_phone: str) -> dict:
    """Build the skillify reminder config."""
    return {
        "title": "Nightly skillify analysis",
        "schedule_type": "cron",
        "schedule_value": "10 2 * * *",  # 2:10am daily (staggered)
        "tz_name": "America/New_York",
        "event": {
            "topic": "tasks",
            "type": "task.requested",
            "key": admin_phone,
            "payload": {
                "task_id": SKILLIFY_TASK_ID,
                "title": "Nightly skillify analysis",
                "requested_by": admin_phone,
                "instructions": SKILLIFY_PROMPT,
                "notify": True,
                "timeout_minutes": 90,
                "execution": {
                    "mode": "agent",
                    "prompt": SKILLIFY_PROMPT,
                },
            },
        },
    }


QUOTA_SCHEDULER_TASK_ID = "quota-scheduler"

QUOTA_SCHEDULER_PROMPT = (
    "You are the quota-aware task scheduler. Your job is to check the weekly quota reset time "
    "and schedule bugfinder + latency finder to run 5 hours before reset.\n\n"
    "1. Run: ~/.claude/skills/ccusage/scripts/server-usage --hours-until-reset\n"
    "2. If the result is between 5.0 and 29.0 hours (reset is today or tomorrow), "
    "create two one-shot reminders via the reminders system:\n"
    "   - Bug finder: fires at (reset_time - 5 hours)\n"
    "   - Latency finder: fires at (reset_time - 5 hours + 10 minutes)\n"
    "3. If > 29 hours until reset, log 'Reset not within scheduling window' and exit.\n"
    "4. If <= 5 hours, the window has already started — fire both tasks immediately by running:\n"
    "   - /bug-finder --nightly (scan ~/dispatch/, full pipeline, opus subagents, SMS if ACCEPT/REFINE)\n"
    "   - /latency-finder --nightly (same pipeline, SMS if ACCEPT/REFINE)\n"
    "5. If < 0 or error, log and exit.\n\n"
    "IMPORTANT: Check if bugfinder/latency-finder already ran this week (check reminders fired_count or "
    "last_fired timestamp). If they already ran within the last 5 days, skip to avoid double-running."
)


def _build_quota_scheduler_reminder(admin_phone: str) -> dict:
    """Build the daily quota scheduler that dynamically triggers heavy tasks."""
    return {
        "title": "Daily quota scheduler check",
        "schedule_type": "cron",
        "schedule_value": "0 2 * * *",  # 2:00am daily — lightweight check
        "tz_name": "America/New_York",
        "event": {
            "topic": "tasks",
            "type": "task.requested",
            "key": admin_phone,
            "payload": {
                "task_id": QUOTA_SCHEDULER_TASK_ID,
                "title": "Daily quota scheduler check",
                "requested_by": admin_phone,
                "instructions": QUOTA_SCHEDULER_PROMPT,
                "notify": False,
                "timeout_minutes": 30,
                "execution": {
                    "mode": "agent",
                    "prompt": QUOTA_SCHEDULER_PROMPT,
                },
            },
        },
    }


def _build_sventimes_reminder(admin_phone: str) -> dict:
    """Build the Sven Times daily gazette reminder config."""
    return {
        "title": "Nightly Sven Times gazette",
        "schedule_type": "cron",
        "schedule_value": "40 2 * * *",  # 2:40am daily (after other agents)
        "tz_name": "America/New_York",
        "event": {
            "topic": "tasks",
            "type": "task.requested",
            "key": admin_phone,
            "payload": {
                "task_id": SVENTIMES_TASK_ID,
                "title": "Nightly Sven Times gazette",
                "requested_by": admin_phone,
                "instructions": SVENTIMES_PROMPT,
                "notify": False,
                "timeout_minutes": 60,
                "execution": {
                    "mode": "agent",
                    "prompt": SVENTIMES_PROMPT,
                },
            },
        },
    }


def find_existing(reminders: list) -> list:
    """Find existing nightly task reminders by task_id in event payload."""
    found = []
    for r in reminders:
        event = r.get("event", {})
        payload = event.get("payload", {})
        if payload.get("task_id") in NIGHTLY_TASK_IDS:
            found.append(r)
    return found


def cmd_list():
    with reminders_lock():
        data = load_reminders()
    existing = find_existing(data["reminders"])
    if not existing:
        print("No nightly task reminders found.")
        return
    for r in existing:
        task_id = r.get("event", {}).get("payload", {}).get("task_id", "?")
        cron = r.get("schedule", {}).get("value", "?")
        print(f"  [{r['id']}] {r['title']} (cron: {cron}, task: {task_id})")


def cmd_remove():
    with reminders_lock():
        data = load_reminders()
        existing = find_existing(data["reminders"])
        if not existing:
            print("No nightly task reminders to remove.")
            return
        ids_to_remove = {r["id"] for r in existing}
        data["reminders"] = [r for r in data["reminders"] if r["id"] not in ids_to_remove]
        save_reminders(data)
    for r in existing:
        print(f"  Removed: {r['title']} ({r['id']})")


def cmd_add():
    admin_phone = _get_admin_phone()

    with reminders_lock():
        data = load_reminders()

        # Check for existing
        existing = find_existing(data["reminders"])
        if existing:
            print("Nightly task reminders already exist:")
            for r in existing:
                print(f"  [{r['id']}] {r['title']}")
            print("\nUse --remove first to replace them.")
            return

        # Create consolidation reminder
        r1 = create_reminder(**_build_consolidation_reminder(admin_phone))
        data["reminders"].append(r1)
        print(f"  Added: {r1['title']} (id={r1['id']}, cron={r1['schedule']['value']}, mode=script)")

        # Create skillify reminder
        r2 = create_reminder(**_build_skillify_reminder(admin_phone))
        data["reminders"].append(r2)
        print(f"  Added: {r2['title']} (id={r2['id']}, cron={r2['schedule']['value']}, mode=agent)")

        # Create quota-aware scheduler (replaces fixed bugfinder + latency finder crons)
        r3 = create_reminder(**_build_quota_scheduler_reminder(admin_phone))
        data["reminders"].append(r3)
        print(f"  Added: {r3['title']} (id={r3['id']}, cron={r3['schedule']['value']}, mode=agent)")

        # Create Sven Times gazette reminder
        r5 = create_reminder(**_build_sventimes_reminder(admin_phone))
        data["reminders"].append(r5)
        print(f"  Added: {r5['title']} (id={r5['id']}, cron={r5['schedule']['value']}, mode=agent)")

        save_reminders(data)

    print("\n✅ All nightly tasks scheduled (staggered, ET):")
    print("  2:00am - Memory consolidation (90min timeout, script mode)")
    print("  2:00am - Quota scheduler check (30min timeout, agent mode)")
    print("  2:10am - Skillify analysis (90min timeout, agent mode)")
    print("  2:40am - Sven Times gazette (60min timeout, agent mode)")
    print("\nNote: Vacation house scraper is registered separately via claude-assistant remind.")


def main():
    if "--list" in sys.argv:
        cmd_list()
    elif "--remove" in sys.argv:
        cmd_remove()
    else:
        cmd_add()


if __name__ == "__main__":
    main()
