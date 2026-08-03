"""
Shared bus event production helpers.

All event production is non-blocking and fire-and-forget.
Failures are logged but never block the caller.

Topic/type taxonomy (v6):
    messages  — message.received, message.sent, message.failed,
                message.produce_failed, message.processing_failed,
                message.ignored,
                message.queued, message.delivered, message.replay_failed,
                reaction.received, reaction.ignored (keyed by chat_id)
    messages.dlq — dead_letter (keyed by chat_id, includes original payload + error)
    sessions  — session.created/restarted/killed/compacted/crashed/injected,
                session.idle_killed, session.prewarmed, session.tier_mismatch,
                session.prompt_built, session.receive_error,
                session.stop_failed, session.model_changed, session.stuck_nudge,
                session.auth_failure (keyed by session_name),
                permission.denied, command.restart (keyed by chat_id)
    system    — daemon.started/stopped/crashed/recovered,
                health.check_completed, health.check_failed,
                health.fast_check_completed,
                health.deep_check_completed, health.service_restarted,
                health.service_spawned,
                consolidation.started, consolidation.completed, consolidation.failed,
                consumer.crashed, consumer.restart_failed,
                skillify.started, skillify.completed,
                scan.started, scan.completed, scan.failed
                    (source=bug-finder|latency-finder|skillify, key=scan-{scanner}-{run_id}),
                reminder.fired, reminder.failed,
                healme.triggered, healme.completed,
                vision.analyzed, vision.failed,
                compaction.triggered, compaction.completed,
                compaction.user_waiting, compaction.auto_cleared,
                master.triggered,
                signal.connection_state,
                sdk.turn_complete, session.heartbeat (keyed by component/session_name),
                quota.fetched, quota.fetch_failed (source=daemon),
                health.haiku_verdict, health.circuit_breaker,
                health.quota_alert, health.bus_check (source=health),
                health.dependency (source=dependency_health, key=<dep name>),
                auth_dialog.event (source=auth-dialog-monitor),
                auth_dialog.approved, auth_dialog.denied,
                auth_dialog.always_approved (source=auth-dialog-approve)
    reminders — reminder.due (keyed by chat_id)
    tasks     — task.requested, task.started, task.completed, task.failed,
                task.timeout, task.skipped (keyed by requested_by chat_id)
    facts     — fact.created, fact.updated, fact.expired (keyed by contact chat_id)
    imessage.ui — tapback, typing.start, typing.stop (keyed by imessage/{chat_id})
                  Serializes all Messages.app UI automation through single consumer.

Source column semantics per topic:
    messages:   transport layer — "imessage", "signal", "test"
    sessions:   origin context — "daemon", "health", "ipc", "inject", "sdk"
    system:     component name — "daemon", "watchdog", "health", "consolidation",
                "consumer", "reminder", "healme", "vision", "compaction", "sdk", "signal",
                "dependency_health"
    reminders:  "reminder-poller"
    tasks:      "task-runner", "reminder-scheduler"
    facts:      "fact-cli", "consolidation"
"""
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

log = logging.getLogger(__name__)

# Phone number pattern: +1XXXXXXXXXX or similar international formats
_PHONE_RE = re.compile(r'\+\d{10,15}')
# Email pattern
_EMAIL_RE = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')


def redact_pii(text: str) -> str:
    """Redact PII (phone numbers, emails) from text for safe bus storage."""
    if not text:
        return text
    text = _PHONE_RE.sub('[REDACTED_PHONE]', text)
    text = _EMAIL_RE.sub('[REDACTED_EMAIL]', text)
    return text


def _ensure_json_safe(payload: dict) -> dict:
    """Ensure all values in payload are JSON-serializable, including nested.

    Returns a new dict with non-serializable values replaced by their repr().
    Fast path: try json.dumps() directly; only fall back to default=repr if needed.
    """
    try:
        json.dumps(payload)
        return payload  # Already safe, no copy needed
    except (TypeError, ValueError):
        return json.loads(json.dumps(payload, default=repr))


def produce_event(producer, topic: str, event_type: str, payload: dict,
                  key: str | None = None, source: str = "daemon",
                  headers: dict[str, str] | None = None):
    """Non-blocking event production (fire-and-forget).

    Args:
        producer: Bus Producer instance (or None — silently no-ops).
        topic: Target topic ("messages", "sessions", "system", "tasks").
        event_type: Event type (e.g. "message.received", "session.created").
        payload: JSON-serializable dict.
        key: Partition key (usually chat_id).
        source: Origin context (semantics vary by topic — see module docstring).
        headers: Optional metadata dict (trace_id, reminder_id, etc.).
                 Follows Kafka convention: headers for routing/tracing, payload for business data.
    """
    if not producer:
        return
    try:
        safe_payload = _ensure_json_safe(payload)
        producer.send(
            topic,
            payload=safe_payload,
            key=key,
            type=event_type,
            source=source,
            headers=headers,
        )
    except Exception as e:
        log.warning(f"Bus produce [{topic}/{event_type}] failed (non-fatal): {e}")


def produce_session_event(producer, chat_id: str, event_type: str, payload: dict,
                          source: str = "daemon"):
    """Convenience wrapper for session topic events."""
    produce_event(producer, "sessions", event_type, payload, key=chat_id, source=source)


# ─── Payload builders ──────────────────────────────────────────────
# Canonical schemas for event payloads. Ensures consistency across producers.

def message_sent_payload(chat_id: str, text: str | None, is_group: bool,
                         success: bool, **extra) -> dict:
    """Build a consistent message.sent / message.failed payload.

    Required fields (always present):
        chat_id, text, is_group, success
    Optional fields (transport-specific):
        elapsed_ms, has_image, has_file, etc.
    """
    payload = {
        "chat_id": chat_id,
        "text": text,
        "is_group": is_group,
        "success": success,
    }
    payload.update(extra)
    return payload


def reaction_received_payload(chat_id: str, phone: str, emoji: str,
                              target_text: str | None = None,
                              is_removal: bool = False,
                              **extra) -> dict:
    """Build a reaction.received payload."""
    payload = {
        "chat_id": chat_id,
        "phone": phone,
        "emoji": emoji,
        "is_removal": is_removal,
    }
    if target_text is not None:
        payload["target_text"] = target_text
    payload.update(extra)
    return payload


def health_check_payload(services_restarted: list[str] | None = None,
                         **extra) -> dict:
    """Build a health.check_completed payload."""
    payload = {
        "services_restarted": services_restarted or [],
    }
    payload.update(extra)
    return payload


def service_restarted_payload(service: str, reason: str, **extra) -> dict:
    """Build a health.service_restarted payload."""
    payload = {
        "service": service,
        "reason": reason,
    }
    payload.update(extra)
    return payload


def consolidation_payload(stage: str, success: bool = True, **extra) -> dict:
    """Build a consolidation.started/completed/failed payload."""
    payload = {
        "stage": stage,
        "success": success,
    }
    payload.update(extra)
    return payload


def reminder_payload(reminder_id: str, contact: str, chat_id: str,
                     title: str, schedule_type: str, success: bool = True,
                     **extra) -> dict:
    """Build a reminder.fired/failed payload."""
    payload = {
        "reminder_id": reminder_id,
        "contact": contact,
        "chat_id": chat_id,
        "title": title,
        "schedule_type": schedule_type,
        "success": success,
    }
    payload.update(extra)
    return payload


def session_injected_payload(chat_id: str, injection_type: str,
                             contact_name: str | None = None,
                             tier: str | None = None, **extra) -> dict:
    """Build a session.injected payload.

    injection_type: "message", "reaction", "group", "consolidation", "reminder"
    """
    payload = {
        "chat_id": chat_id,
        "injection_type": injection_type,
    }
    if contact_name:
        payload["contact_name"] = contact_name
    if tier:
        payload["tier"] = tier
    payload.update(extra)
    return payload


def stuck_nudge_payload(session_name: str, chat_id: str, idle_minutes: float,
                        detection: str, contact_name: str | None = None,
                        committed_text: str | None = None, **extra) -> dict:
    """Build a session.stuck_nudge payload (sessions topic).

    Emitted by the blocked-session watchdog when it nudges a session that went
    idle with an outstanding commitment to the user.

    detection: "marker" (session set an explicit pending_commitment marker) or
               "heuristic" (matched a commitment phrase in the last outbound text)
    committed_text: the marker text, or the matched snippet for the heuristic case
    """
    payload = {
        "schema_v": 1,
        "session_name": session_name,
        "chat_id": chat_id,
        "idle_minutes": round(idle_minutes, 1),
        "detection": detection,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if contact_name:
        payload["contact_name"] = contact_name
    if committed_text:
        payload["committed_text"] = committed_text
    payload.update(extra)
    return payload


def healme_payload(admin_phone: str, admin_name: str, stage: str,
                   custom_prompt: str | None = None, **extra) -> dict:
    """Build a healme.triggered/completed payload."""
    payload = {
        "admin_phone": admin_phone,
        "admin_name": admin_name,
        "stage": stage,
    }
    if custom_prompt is not None:
        payload["custom_prompt"] = custom_prompt
    payload.update(extra)
    return payload


def vision_payload(chat_id: str, image_path: str, success: bool,
                   description_length: int = 0, **extra) -> dict:
    """Build a vision.analyzed/failed payload."""
    payload = {
        "chat_id": chat_id,
        "image_path": image_path,
        "success": success,
        "description_length": description_length,
    }
    payload.update(extra)
    return payload


def compaction_triggered_payload(session_name: str, chat_id: str,
                                  contact_name: str, turn_count: int,
                                  **extra) -> dict:
    """Build a compaction.triggered payload."""
    payload = {
        "session_name": session_name,
        "chat_id": chat_id,
        "contact_name": contact_name,
        "turn_count": turn_count,
    }
    payload.update(extra)
    return payload



def compaction_user_waiting_payload(session_name: str, chat_id: str,
                                     contact_name: str, source: str,
                                     session_type: str, **extra) -> dict:
    """Build a compaction.user_waiting payload."""
    payload = {
        "session_name": session_name,
        "chat_id": chat_id,
        "contact_name": contact_name,
        "source": source,
        "session_type": session_type,
    }
    payload.update(extra)
    return payload


def service_spawned_payload(service: str, pid: int, **extra) -> dict:
    """Build a health.service_spawned payload."""
    payload = {
        "service": service,
        "pid": pid,
    }
    payload.update(extra)
    return payload


# ─── Diagnostic event schemas & builders ─────────────────────────────
#
# Schema evolution: add new optional fields freely, no version bump.
# For breaking changes, bump schema_v and note in commit.
# Consumers use .get() for optional fields. On unexpected schema_v,
# log-warn and process best-effort — don't raise.
#
# Event schemas (plain dicts, no TypedDicts — no mypy in CI):
#
# health.haiku_verdict:
#   schema_v:1, check_run_id, check_type(deep|stuck), session_name,
#   chat_id, verdict(FATAL|HEALTHY|STUCK|WORKING),
#   action_taken(restart|none — always string, never null), timestamp
#
# health.circuit_breaker:
#   schema_v:1, check_run_id?(present within check cycle, absent otherwise),
#   session_name, chat_id, transition(opened|closed), restart_count, timestamp
#
# health.quota_alert:
#   schema_v:1, quota_type(5-hour|7-day all|7-day sonnet|7-day opus|extra usage),
#   utilization, threshold, resets_at, timestamp
#
# health.chrome_check:  [LEGACY — superseded by health.dependency, kept for back-compat]
#   schema_v:1, status(ok|wedged|chrome_not_running|cli_missing),
#   action_taken(reset|none), detail, timestamp,
#   ping_rc?/ping_output?/ping_timed_out?, reset_rc?/reset_output?/reset_timed_out?
#
# health.dependency:  (source="dependency_health", key=<dep name>)
#   schema_v:1, name(chrome_control|signal_cli|bus_consumers|disk|fd_leak),
#   state(HEALTHY|DEGRADED|DOWN|RECOVERING|ESCALATED), prev_state,
#   action_taken(probe|recover|escalate|none), detail, timestamp,
#   attempt?/max_attempts?(when RECOVERING),
#   recovered?/recovered_after_escalation? flags,
#   plus flat check-specific diagnostics (probe_rc, probe_output, probe_timed_out,
#   recover_rc, recover_output, recover_timed_out, disable_reason, dead_members,
#   used_pct, free_gb, fd_actual, fd_delta, recoveries_in_window, ...).
#   Built inline by assistant/dependency_health.DependencyHealthRunner._emit().
#   Emitted on every state transition + every recovery attempt + every escalation
#   (incl. the recovery-frequency alarm); healthy steady-state is DEBUG-log only.
#
# health.bus_check:
#   status:"ok" — startup canary only


@dataclass
class CheckContext:
    """Threaded through health check cycles for cross-event correlation.

    Created once per check cycle entry. Passed explicitly through call stack.
    Closures capture by reference — safe because ctx is assigned once and never
    reassigned (NOT the same as capturing a loop variable).
    """
    check_run_id: str = field(default_factory=lambda: str(uuid4()))


def haiku_verdict_payload(ctx: CheckContext, check_type: str,
                          session_name: str, chat_id: str,
                          verdict: str, action_taken: str,
                          reasoning: str | None = None) -> dict:
    """Build a health.haiku_verdict payload.

    Pure function — no side effects. Caller handles logging for unknown patterns.
    check_type: "deep" or "stuck"
    verdict: "FATAL", "HEALTHY", "STUCK", or "WORKING"
    action_taken: "restart" or "none" (always a string, never null)
    reasoning: optional Haiku reasoning text explaining the verdict
    """
    payload = {
        "schema_v": 1,
        "check_run_id": ctx.check_run_id,
        "check_type": check_type,
        "session_name": session_name,
        "chat_id": chat_id,
        "verdict": verdict,
        "action_taken": action_taken,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if reasoning:
        payload["reasoning"] = reasoning
    return payload


def circuit_breaker_payload(session_name: str, chat_id: str,
                            transition: str, restart_count: int,
                            ctx: CheckContext | None = None) -> dict:
    """Build a health.circuit_breaker payload.

    transition: "opened" or "closed"
    ctx: present when transition occurs within a health check cycle
         (deep_health_check or _investigate_and_maybe_restart).
         Absent check_run_id means transition outside a check cycle
         (not a bug — e.g. is_open() auto-transitions open→half_open on timeout).
    """
    d = {
        "schema_v": 1,
        "session_name": session_name,
        "chat_id": chat_id,
        "transition": transition,
        "restart_count": restart_count,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if ctx:
        d["check_run_id"] = ctx.check_run_id
    return d


def quota_alert_payload(alert_dict: dict) -> dict:
    """Build a health.quota_alert payload.

    Takes raw alert dict from check_quota_thresholds() — fields:
    quota_type, utilization, threshold, resets_at.
    No check_run_id: quota polling runs on independent timer.
    No chat_id: global resource, not per-session.
    """
    return {
        "schema_v": 1,
        "quota_type": alert_dict["quota_type"],
        "utilization": alert_dict["utilization"],
        "threshold": alert_dict["threshold"],
        "resets_at": alert_dict["resets_at"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def chrome_check_payload(check: dict) -> dict:
    """Build a health.chrome_check payload from check_chrome_control()'s result.

    check fields: status (ok|wedged|chrome_not_running|cli_missing),
    action_taken (reset|none), detail, plus optional ping_*/reset_* diagnostics.
    No check_run_id: chrome-control polling runs on its own tight timer.
    No chat_id: global resource, not per-session.
    """
    payload: dict = {
        "schema_v": 1,
        "status": check.get("status", "unknown"),
        "action_taken": check.get("action_taken", "none"),
        "detail": check.get("detail", ""),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    # Carry diagnostics through when present (esp. on failure / recovery).
    for k in ("ping_rc", "ping_output", "ping_timed_out",
              "reset_rc", "reset_output", "reset_timed_out"):
        if k in check:
            payload[k] = check[k]
    return payload


def sanitize_reaction_for_bus(reaction: dict) -> dict:
    """Convert a raw reaction dict to a JSON-safe bus payload.

    Whitelists known fields and converts non-JSON-safe types.
    """
    FIELDS = {
        "rowid", "phone", "emoji", "is_removal", "target_guid",
        "target_text", "target_is_from_me", "is_group",
        "chat_identifier", "source",
    }
    payload = {}
    for k, v in reaction.items():
        if k == "timestamp" and hasattr(v, "timestamp"):
            payload["timestamp_ms"] = int(v.timestamp() * 1000)
        elif k in FIELDS:
            payload[k] = v
        else:
            try:
                json.dumps(v)
                payload[k] = v
            except (TypeError, ValueError):
                pass

    # Ensure chat_id is set
    if "chat_id" not in payload:
        is_group = reaction.get("is_group", False)
        chat_id = reaction.get("chat_identifier") if is_group else reaction.get("phone")
        if chat_id:
            payload["chat_id"] = chat_id

    return payload


def reconstruct_reaction_from_bus(payload: dict) -> dict:
    """Reconstruct a raw reaction dict from a bus payload.

    Reverses sanitize_reaction_for_bus().
    """
    from datetime import datetime

    reaction = dict(payload)

    if "timestamp_ms" in reaction:
        reaction["timestamp"] = datetime.fromtimestamp(reaction.pop("timestamp_ms") / 1000)

    reaction.pop("chat_id", None)

    return reaction


def sanitize_msg_for_bus(msg: dict) -> dict:
    """Convert a raw message dict (from chat.db/signal) to a JSON-safe bus payload.

    Lossless: preserves all fields needed by process_message() for consumer-driven
    processing. Converts non-JSON-safe types (datetime → timestamp_ms, Path → str).

    Includes original timestamp for latency measurement:
        bus record timestamp - payload.timestamp_ms = ingestion latency.
    """
    payload = {}

    # Whitelist of known fields from MessagesReader and SignalListener
    DIRECT_FIELDS = {
        "rowid", "phone", "text", "is_group", "group_name",
        "chat_identifier", "is_audio_message", "audio_transcription",
        "thread_originator_guid", "guid", "source", "sender_name",
    }

    for k, v in msg.items():
        if k == "timestamp" and hasattr(v, "timestamp"):
            # Convert datetime to unix ms for JSON safety
            payload["timestamp_ms"] = int(v.timestamp() * 1000)
        elif k == "attachments":
            # Preserve full attachment dicts (path, mime_type, name, size)
            # for downstream consumers (Gemini vision, format_message_body).
            # Paths are already strings from MessagesReader; safety-convert any Path objects.
            payload["has_attachments"] = bool(v)
            payload["attachment_count"] = len(v) if v else 0
            if v:
                safe_attachments = []
                for att in v:
                    safe_att = {}
                    for ak, av in att.items():
                        try:
                            json.dumps(av)
                            safe_att[ak] = av
                        except (TypeError, ValueError):
                            safe_att[ak] = str(av)
                    safe_attachments.append(safe_att)
                payload["attachments"] = safe_attachments
        elif k in DIRECT_FIELDS:
            payload[k] = v
        else:
            # Unknown field — include if JSON-safe, skip otherwise.
            # Fast path: common types are always safe.
            if isinstance(v, (str, int, float, bool, type(None))):
                payload[k] = v
            else:
                try:
                    json.dumps(v)
                    payload[k] = v
                except (TypeError, ValueError):
                    pass

    # Ensure chat_id is always set
    if "chat_id" not in payload:
        is_group = msg.get("is_group", False)
        chat_id = msg.get("chat_identifier") if is_group else msg.get("phone")
        if chat_id:
            payload["chat_id"] = chat_id

    return payload


def reconstruct_msg_from_bus(payload: dict) -> dict:
    """Reconstruct a raw message dict from a bus payload.

    Reverses sanitize_msg_for_bus() so that process_message() can consume
    bus records identically to raw message dicts.

    Conversions:
        timestamp_ms (int) → timestamp (datetime)
        attachments: kept as-is (string paths work for process_message)
    """
    from datetime import datetime

    msg = dict(payload)  # shallow copy

    # Restore datetime timestamp from timestamp_ms
    if "timestamp_ms" in msg:
        msg["timestamp"] = datetime.fromtimestamp(msg.pop("timestamp_ms") / 1000)

    # Remove convenience fields that aren't in the original msg dict
    msg.pop("has_attachments", None)
    msg.pop("attachment_count", None)
    msg.pop("chat_id", None)  # process_message derives this from phone/chat_identifier

    return msg


# ─── Task event payload builders ────────────────────────────────

def task_started_payload(task_id: str, title: str, requested_by: str,
                         session_name: str, timeout_minutes: int,
                         execution_mode: str = "agent", **extra) -> dict:
    """Build a task.started payload."""
    payload = {
        "task_id": task_id,
        "title": title,
        "requested_by": requested_by,
        "session_name": session_name,
        "timeout_minutes": timeout_minutes,
        "execution_mode": execution_mode,
    }
    payload.update(extra)
    return payload


def task_completed_payload(task_id: str, title: str, requested_by: str,
                           duration_seconds: float, **extra) -> dict:
    """Build a task.completed payload."""
    payload = {
        "task_id": task_id,
        "title": title,
        "requested_by": requested_by,
        "duration_seconds": round(duration_seconds, 1),
    }
    payload.update(extra)
    return payload


def task_failed_payload(task_id: str, title: str, requested_by: str,
                        error: str, **extra) -> dict:
    """Build a task.failed payload."""
    payload = {
        "task_id": task_id,
        "title": title,
        "requested_by": requested_by,
        "error": error,
    }
    payload.update(extra)
    return payload


def task_timeout_payload(task_id: str, title: str, requested_by: str,
                         timeout_minutes: int, **extra) -> dict:
    """Build a task.timeout payload."""
    payload = {
        "task_id": task_id,
        "title": title,
        "requested_by": requested_by,
        "timeout_minutes": timeout_minutes,
    }
    payload.update(extra)
    return payload


def task_skipped_payload(task_id: str, reason: str, **extra) -> dict:
    """Build a task.skipped payload."""
    payload = {
        "task_id": task_id,
        "reason": reason,
    }
    payload.update(extra)
    return payload


# ─── Scan report event payload builders ────────────────────────────

def scan_started_payload(scanner: str, run_id: str, mode: str = "interactive",
                         target_dir: str | None = None, **extra) -> dict:
    """Build a scan.started payload for bug-finder/latency-finder/skillify."""
    payload = {
        "scanner": scanner,
        "run_id": run_id,
        "mode": mode,
    }
    if target_dir:
        payload["target_dir"] = target_dir
    payload.update(extra)
    return payload


def scan_completed_payload(scanner: str, run_id: str, duration_seconds: float,
                           summary: dict, findings: list,
                           mode: str = "interactive", **extra) -> dict:
    """Build a scan.completed payload.

    Args:
        scanner: "bug-finder", "latency-finder", or "skillify"
        run_id: unique run identifier (e.g., "20260315-0200")
        duration_seconds: total scan duration
        summary: dict with counts like {candidates: N, accepted: N, refuted: N, ...}
        findings: list of accepted/refined finding dicts (full details)
        mode: "interactive" or "nightly"
    """
    payload = {
        "scanner": scanner,
        "run_id": run_id,
        "mode": mode,
        "duration_seconds": round(duration_seconds, 1),
        "summary": summary,
        "findings": findings,
    }
    payload.update(extra)
    return payload


def scan_failed_payload(scanner: str, run_id: str, error: str,
                        partial_results: bool = False, **extra) -> dict:
    """Build a scan.failed payload."""
    payload = {
        "scanner": scanner,
        "run_id": run_id,
        "error": error,
        "partial_results": partial_results,
    }
    payload.update(extra)
    return payload


def fact_event_payload(fact_id: int, contact: str, fact_type: str,
                       summary: str, details: dict | None = None,
                       confidence: str = "high",
                       starts_at: str | None = None,
                       ends_at: str | None = None, **extra) -> dict:
    """Build a fact.created/updated/expired payload."""
    payload = {
        "fact_id": fact_id,
        "contact": contact,
        "fact_type": fact_type,
        "summary": summary,
        "details": details,
        "confidence": confidence,
        "starts_at": starts_at,
        "ends_at": ends_at,
    }
    payload.update(extra)
    return payload


def produce_fact_event(producer, contact: str, event_type: str, payload: dict):
    """Convenience wrapper for fact events on the facts topic."""
    produce_event(producer, "facts", event_type, payload,
                  key=contact, source="consolidation")


def produce_scan_event(producer, scanner: str, event_type: str, payload: dict):
    """Convenience wrapper for scan events on the system topic.

    Usage:
        produce_scan_event(producer, "bug-finder", "scan.completed", payload)
    """
    run_id = payload.get("run_id", "unknown")
    produce_event(producer, "system", event_type, payload,
                  key=f"scan-{scanner}-{run_id}", source=scanner)


# ─── iMessage UI events ─────────────────────────────────────────────
# Producers for the imessage.ui topic (tapback, read, typing).
# Consumed by the single-threaded UI automation consumer in Manager.

def produce_imessage_ui_event(producer, chat_id: str, event_type: str,
                               payload: dict, source: str = "daemon"):
    """Produce an event to the imessage.ui topic.

    Args:
        chat_id: Bare phone number (e.g. "+15555550100") or group hex ID.
        event_type: One of "tapback", "read", "typing.start", "typing.stop".
        payload: Must include "chat_id" at minimum.
        source: Origin component (e.g. "daemon", "reply-cli", "sdk").
    """
    produce_event(producer, "imessage.ui", event_type, payload,
                  key=f"imessage/{chat_id}", source=source)


def produce_read_receipt(producer, chat_id: str, source: str = "daemon"):
    """Produce a read receipt event for a chat.

    When consumed, this navigates Messages.app to the chat, which
    automatically marks messages as read and sends read receipts.
    """
    produce_imessage_ui_event(producer, chat_id, "read",
                               {"chat_id": chat_id}, source=source)


def produce_typing_event(producer, chat_id: str, start: bool,
                          source: str = "daemon"):
    """Produce a typing indicator event (start or stop)."""
    event_type = "typing.start" if start else "typing.stop"
    produce_imessage_ui_event(producer, chat_id, event_type,
                               {"chat_id": chat_id}, source=source)


def query_undelivered_messages(db_path: str, chat_id: str, max_age_hours: int = 24) -> list[dict]:
    """Query bus for messages that were queued but never delivered.

    Uses a SEPARATE read-only SQLite connection (not the Producer's connection)
    to avoid contention with the background writer thread.

    Performance: Uses json_extract which is O(N*M). The records table has indexes
    on (topic, type) and (topic, key), so the WHERE clause filters efficiently.
    Acceptable at <1000 msgs/day/chat.

    Returns [{message_id, text, source, timestamp}], oldest first.
    """
    import sqlite3
    import time
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cutoff = int((time.time() - max_age_hours * 3600) * 1000)
        rows = conn.execute("""
            SELECT r1.payload as queued_payload, r1.timestamp as queued_ts
            FROM records r1
            WHERE r1.topic = 'messages'
              AND r1.type = 'message.queued'
              AND r1.key = ?
              AND r1.timestamp > ?
              AND NOT EXISTS (
                  SELECT 1 FROM records r2
                  WHERE r2.topic = 'messages'
                    AND r2.type = 'message.delivered'
                    AND r2.key = ?
                    AND json_extract(r2.payload, '$.message_id') = json_extract(r1.payload, '$.message_id')
              )
            ORDER BY r1.timestamp ASC
        """, (chat_id, cutoff, chat_id)).fetchall()

        results = []
        for row in rows:
            payload = json.loads(row["queued_payload"])
            text = payload.get("text", "")
            # Filter sentinels/control messages (belt-and-suspenders)
            if text.startswith("__") and text.endswith("__"):
                continue
            results.append({
                "message_id": payload["message_id"],
                "text": text,
                "source": payload.get("source", "unknown"),
                "timestamp": row["queued_ts"],
            })
        return results
    finally:
        conn.close()
