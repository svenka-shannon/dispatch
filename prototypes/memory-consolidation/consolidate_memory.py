#!/usr/bin/env -S uv run --script
"""
consolidate_memory.py — Unified Nightly Memory Consolidation

Replaces: consolidate_3pass.py + consolidate_facts.py
Model: claude-sonnet-4-5 (not Opus — 10x cheaper, fast enough)
Message source: bus.db records table (not read-sms subprocess)

Architecture: tiered model per pass (right model for right task)
  Pass A-facts : Extract fact candidates (Haiku — cheap bulk scan)
  Pass B       : Verify grounding with quotes (Sonnet — nuance needed)
  Commit       : All-or-nothing write to DB

Key design goals:
  - FACT ACCURACY: layered grounding rejects hallucinations before commit
  - COST: Haiku for volume scan, Sonnet only for verification
  - BUS.DB: no subprocess, direct SQLite read

FIRST DEPLOY:
  1. python consolidate_memory.py --add-schema-version-column   (idempotent)
  2. python consolidate_memory.py --migrate-facts               (archives schema_version=0 facts)
  3. Deploy and run nightly

Usage:
    consolidate_memory.py <contact>               # Run for one contact (by name)
    consolidate_memory.py --all                   # Run for all known contacts
    consolidate_memory.py --dry-run [<contact>]   # Print proposed changes, no writes
    consolidate_memory.py --dry-run --all         # Dry run all
    consolidate_memory.py --verbose [<contact>]   # Show LLM input/output
    consolidate_memory.py --add-schema-version-column
    consolidate_memory.py --migrate-facts
"""
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Paths ──────────────────────────────────────────────────────
HOME = Path.home()
DISPATCH_DIR = HOME / "dispatch"
BUS_DB_PATH = DISPATCH_DIR / "state" / "bus.db"
LOGS_DIR = DISPATCH_DIR / "logs"
LOG_FILE = LOGS_DIR / "memory-consolidation.log"
MEMORIES_DIR = HOME / "memories"
CHECKPOINTS_FILE = MEMORIES_DIR / "memory_checkpoints.json"
BACKUP_DIR = MEMORIES_DIR / "backups"
CONTACTS_CLI = HOME / ".claude/skills/contacts/scripts/contacts"
SEND_SMS_CLI = HOME / ".claude/skills/sms-assistant/scripts/send-sms"
SENSITIVE_PATTERNS_FILE = DISPATCH_DIR / "config" / "sensitive_patterns.txt"

# ── Constants ──────────────────────────────────────────────────
CURRENT_SCHEMA_VERSION = 1
MAX_MESSAGES_LIMIT = 300         # rows from bus.db per contact per run
MAX_MESSAGES_CHARS = 15_000      # truncate oldest messages if exceeded
MAX_EXISTING_FACTS_CHARS = 5_000
HIGH_DROP_RATE_THRESHOLD = 0.40  # warn if >40% of proposed items dropped by Pass B
CONTACT_SLEEP_SECONDS = 5        # stagger between contacts (skip in dry-run)
VALID_FACT_TYPES = {"travel", "event", "preference", "project", "relationship", "deadline"}
UPDATABLE_FIELDS = {"summary", "details", "ends_at", "confidence"}

# ── Sensitive content patterns ─────────────────────────────────
_SENSITIVE_BASELINE = [
    "proposal", "propose", "engagement ring", "will you marry",
    "museum of science", "planetarium", "may 9", "may 9th",
    "surprise party", "surprise", "don't tell", "dont tell", "keep it secret",
    "secret plan", "top secret",
]
_WORD_BOUNDARY_PATTERNS = [
    r"\bpropose[sd]?\b",
    r"\bengagement ring\b",
    r"\bwill you marry\b",
    r"\bplanetar(?:ium|y)?\b",
    r"\bmay 9\b",
    r"\bsurprise party\b",
    r"\bdon['\u2019]?t tell\b",
    r"\bkeep it secret\b",
]


def load_sensitive_patterns() -> list[str]:
    """Baseline always active. File extends, never replaces."""
    patterns = list(_SENSITIVE_BASELINE)
    if SENSITIVE_PATTERNS_FILE.exists():
        extra = [
            line.strip().lower()
            for line in SENSITIVE_PATTERNS_FILE.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
        patterns.extend(extra)
    return list(set(patterns))


_SENSITIVE_PATTERNS: list[str] = load_sensitive_patterns()


def check_sensitive(text: str) -> bool:
    text_lower = text.lower()
    if any(p in text_lower for p in _SENSITIVE_PATTERNS):
        return True
    return any(re.search(pat, text_lower) for pat in _WORD_BOUNDARY_PATTERNS)


# ── Partner name (for leak protection) ────────────────────────
def _load_partner_name() -> str:
    try:
        import yaml
        config_path = DISPATCH_DIR / "config.local.yaml"
        if config_path.exists():
            config = yaml.safe_load(config_path.read_text())
            return config.get("partner", {}).get("name", "").split()[0].lower()
    except Exception:
        pass
    return ""


_PARTNER_FIRST_NAME = _load_partner_name()


def is_partner_contact(contact_name: str) -> bool:
    if not _PARTNER_FIRST_NAME:
        return False
    return _PARTNER_FIRST_NAME in (contact_name or "").lower()


# ── Admin phone ────────────────────────────────────────────────
def _load_admin_phone() -> str:
    try:
        sys.path.insert(0, str(DISPATCH_DIR))
        from assistant import config as cfg
        return cfg.require("owner.phone")
    except Exception:
        return ""


_ADMIN_PHONE = _load_admin_phone()

# ── Exceptions ─────────────────────────────────────────────────
class ConsolidationError(Exception):
    pass

class AbortContactRun(ConsolidationError):
    """Abort processing for a single contact (not the whole run)."""
    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)

class ValidationError(ConsolidationError):
    pass


# ── Audit structures ───────────────────────────────────────────
@dataclass
class SectionAudit:
    a_proposed: int = 0
    pre_verify_dropped: int = 0
    b_accepted: int = 0
    b_refuted: int = 0
    b_implicit_refuted: int = 0
    quote_verify_dropped: int = 0
    schema_dropped_new: int = 0
    schema_dropped_update: int = 0
    unexpected_schema_errors: int = 0
    dedup_dropped: int = 0
    committed: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class AuditRecord:
    contact: str
    timestamp: str
    messages_scanned: int = 0
    facts: SectionAudit = field(default_factory=SectionAudit)
    committed: bool = False
    abort_reason: str = ""
    sensitive_content_detected: bool = False
    errors: list[str] = field(default_factory=list)


# ── Logging ────────────────────────────────────────────────────
def log(msg: str, verbose: bool = False, always: bool = False):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(f"{ts} | {msg}\n")
    if verbose or always:
        print(f"  {msg}", file=sys.stderr)


# ── Checkpoints ────────────────────────────────────────────────
def load_checkpoints() -> dict:
    if CHECKPOINTS_FILE.exists():
        try:
            return json.loads(CHECKPOINTS_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_checkpoints(checkpoints: dict):
    MEMORIES_DIR.mkdir(parents=True, exist_ok=True)
    CHECKPOINTS_FILE.write_text(json.dumps(checkpoints, indent=2) + "\n")


def _phone_to_chat_id(phone: str) -> str:
    """Contacts.app may store a number as '5555550100' or '(555) 555-0100', but
    bus.db chat_ids are E.164 ('+15555550100'). Without this, those contacts
    silently got '0 messages' every night (found 2026-10-15)."""
    p = phone.strip()
    digits = re.sub(r"\D", "", p)
    if p.startswith("+"):
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return p


# ── Message fetching from bus.db ───────────────────────────────
def fetch_messages_from_bus(
    chat_id: str,
    since_ts: Optional[int],
    conn: sqlite3.Connection,
) -> list[tuple]:
    """
    Fetch up to MAX_MESSAGES_LIMIT messages for chat_id from bus.db.
    Returns list of (timestamp_int, type, payload_dict) in chronological order.
    """
    if since_ts is not None:
        rows = conn.execute(
            """SELECT timestamp, type, payload FROM records
               WHERE topic = 'messages'
                 AND json_extract(payload, '$.chat_id') = ?
                 AND type IN ('message.received', 'message.queued', 'message.admin_inject')
                 AND timestamp > ?
               ORDER BY timestamp ASC LIMIT ?""",
            (chat_id, since_ts, MAX_MESSAGES_LIMIT),
        ).fetchall()
    else:
        # No checkpoint — get the most recent MAX_MESSAGES_LIMIT messages
        rows = conn.execute(
            """SELECT timestamp, type, payload FROM records
               WHERE topic = 'messages'
                 AND json_extract(payload, '$.chat_id') = ?
                 AND type IN ('message.received', 'message.queued', 'message.admin_inject')
               ORDER BY timestamp DESC LIMIT ?""",
            (chat_id, MAX_MESSAGES_LIMIT),
        ).fetchall()
        rows = list(reversed(rows))  # chronological

    result = []
    for ts, msg_type, payload_str in rows:
        try:
            payload = json.loads(payload_str) if isinstance(payload_str, str) else payload_str
        except Exception:
            payload = {"text": str(payload_str)}
        result.append((ts, msg_type, payload))
    return result


def format_messages_text(rows: list[tuple]) -> str:
    """Format bus.db message rows as human-readable text for prompts."""
    lines = []
    for ts, msg_type, payload in rows:
        # Convert unix timestamp to readable datetime
        dt = datetime.fromtimestamp(ts / 1000 if ts > 1e10 else ts, tz=timezone.utc)
        ts_str = dt.strftime("%Y-%m-%d %H:%M")

        text = payload.get("text", "") or payload.get("message", "") or ""
        if not text:
            continue

        direction = "IN" if msg_type == "message.received" else "OUT"
        lines.append(f"[{ts_str}] {direction}: {text}")
    return "\n".join(lines)


def truncate_to_budget(text: str, max_chars: int) -> str:
    """Truncate oldest messages (beginning) to stay within char budget."""
    if len(text) <= max_chars:
        return text
    return "...[oldest messages truncated]...\n" + text[-max_chars:]


# ── Existing facts loading ─────────────────────────────────────
def load_existing_facts(
    conn: sqlite3.Connection,
    contact_name: str,
    audit: SectionAudit,
) -> list[dict]:
    """
    Load active facts for contact from bus.db facts table.
    Excludes schema_version mismatches with audit trail.
    Raises AbortContactRun if ALL existing facts are excluded (missed migration).
    """
    rows = conn.execute(
        """SELECT id, fact_type, summary, details, starts_at, ends_at,
                  confidence, schema_version, updated_at, created_at
           FROM facts WHERE contact=? AND active=1 LIMIT 100""",
        (contact_name,),
    ).fetchall()

    cols = ["id", "fact_type", "summary", "details", "starts_at", "ends_at",
            "confidence", "schema_version", "updated_at", "created_at"]

    valid = []
    excluded = 0
    for row in rows:
        d = dict(zip(cols, row))
        sv = d.get("schema_version", 0) or 0
        if sv != CURRENT_SCHEMA_VERSION:
            excluded += 1
            log(f"[SCHEMA] fact {d['id']} has schema_version={sv}, expected {CURRENT_SCHEMA_VERSION} — excluded from dedup")
            continue
        # Parse details JSON if stored as string
        if isinstance(d.get("details"), str):
            try:
                d["details"] = json.loads(d["details"])
            except Exception:
                d["details"] = {}
        valid.append(d)

    total = len(rows)
    if excluded > 0:
        log(f"[SCHEMA] {contact_name}: excluded {excluded}/{total} fact(s) due to schema_version mismatch")

    if excluded == total and total >= 1:
        audit.unexpected_schema_errors += 1
        raise AbortContactRun(
            "schema_mismatch_flood",
            f"all {total} facts have wrong schema_version — run --migrate-facts",
        )

    return valid


# ── Grounding checks (code-level, no LLM) ─────────────────────
def word_overlap(claim: str, messages_lower: str) -> float:
    """
    Compute word overlap. Stop words filtered to avoid trivial passes.
    Threshold is 35% — Haiku paraphrases (drop→dropping, db→database) so
    we use a loose threshold here and rely on Pass B (Sonnet) as the main gate.
    This only catches complete hallucinations with zero message grounding.
    """
    STOP_WORDS = {
        "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "shall", "to", "of", "in", "on", "at",
        "for", "with", "by", "from", "and", "or", "but", "not", "no",
        "it", "its", "he", "she", "they", "we", "i", "you", "my", "his",
        "her", "their", "our", "your", "this", "that", "these", "those",
    }
    words = [w for w in claim.lower().split() if w not in STOP_WORDS and len(w) > 2]
    if not words:
        return 1.0  # Nothing to check
    matched = sum(1 for w in words if w in messages_lower)
    return matched / len(words)


def check_entities(claim: str, messages_lower: str) -> bool:
    """
    Extract named entity-like tokens from claim and require each appears in messages.
    Focuses on proper nouns, abbreviations, and numeric values.
    """
    # Only look for uppercase runs and numbers (skip sentence-initial capitals)
    # Strategy: extract tokens that are ALL-CAPS (abbreviations) or digit-heavy
    abbrevs = re.findall(r'\b[A-Z]{2,}\b', claim)        # BOS, JetBlue, WWDC
    numbers = re.findall(r'\b\d{2,}[\d\-/]*\b', claim)   # 2026, 2026-03-20, 50

    # Multi-word proper nouns (two+ title-case words in a row, mid-sentence)
    # Avoid sentence-start by requiring non-start position OR checking context
    multi_proper = re.findall(r'(?<=[a-z.,!?] )([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})+)', claim)

    entities = abbrevs + numbers + multi_proper
    if not entities:
        return True  # No entities to verify
    return all(e.lower() in messages_lower for e in entities)


def _normalize_apostrophes(text: str) -> str:
    """Normalize smart quotes/apostrophes to ASCII for substring matching."""
    return (
        text
        .replace('\u2019', "'")   # right single quotation mark
        .replace('\u2018', "'")   # left single quotation mark
        .replace('\u201c', '"')   # left double quotation mark
        .replace('\u201d', '"')   # right double quotation mark
        .replace('\u2014', '-')   # em dash
        .replace('\u2013', '-')   # en dash
    )


def verify_supporting_quote(quote: str, messages_text: str, max_chars: int = 200) -> bool:
    """
    Check that supporting quote (capped to 200 chars) appears as substring.
    Normalizes smart apostrophes/quotes before matching.
    Returns True if found, False if not found or quote is empty.
    """
    if not quote or len(quote.strip()) < 5:
        return False
    snippet = _normalize_apostrophes(quote[:max_chars].strip().lower())
    haystack = _normalize_apostrophes(messages_text.lower())
    return snippet in haystack


# ── Circuit breaker ────────────────────────────────────────────
def circuit_breaker_check(
    new_facts: list,
    messages_scanned: int,
    verbose: bool = False,
) -> bool:
    """Proportional cap: abort if proposed volume suggests hallucination spike."""
    max_facts = max(8, int(messages_scanned * 0.10))
    triggered = len(new_facts) > max_facts
    if triggered:
        log(
            f"Circuit breaker: {len(new_facts)} facts > {max_facts} (messages_scanned={messages_scanned})",
            verbose=verbose, always=True,
        )
    return triggered


# ── Schema validation ──────────────────────────────────────────
def validate_new_fact(fact: dict) -> None:
    """Raise ValidationError if new fact is malformed."""
    ft = fact.get("fact_type")
    if ft and ft not in VALID_FACT_TYPES:
        raise ValidationError(f"Unknown fact_type: {ft!r}")
    if not fact.get("summary", "").strip():
        raise ValidationError("summary is required")
    details = fact.get("details")
    if details is not None and not isinstance(details, dict):
        raise ValidationError(f"details must be dict, got {type(details).__name__}")


def validate_updated_fact(upd: dict, existing_by_id: dict) -> None:
    """Raise ValidationError if fact update is malformed or references unknown ID."""
    fid = upd.get("existing_fact_id")
    if fid not in existing_by_id:
        raise ValidationError(f"unknown existing_fact_id: {fid!r}")
    mutating = {k for k in upd if k in UPDATABLE_FIELDS}
    if not mutating:
        raise ValidationError(f"updated_fact {fid} has no mutating fields — no-op")


# ── Model tiers ────────────────────────────────────────────────
# Haiku: cheap bulk scan (Pass A — extraction from 300 messages)
# Sonnet: nuanced verification (Pass B — grounding checks, quote matching)
MODEL_PASS_A = "haiku"    # claude-haiku-4-5 via CLI shorthand
MODEL_PASS_B = "sonnet"   # claude-sonnet-4-5 via CLI shorthand


# ── LLM calls ─────────────────────────────────────────────────
def call_claude(
    system_prompt: str,
    user_prompt: str,
    model: str = MODEL_PASS_B,
    timeout: int = 90,
    verbose: bool = False,
) -> str:
    """
    Call claude -p CLI. Returns raw text output.
    model: use MODEL_PASS_A (haiku) for extraction, MODEL_PASS_B (sonnet) for verification.
    Raises AbortContactRun("timeout") on timeout, RuntimeError on other failures.
    """
    clean_env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    cmd = [
        "claude", "-p",
        "--model", model,
        "--system-prompt", system_prompt,
    ]
    if verbose:
        log(f"  [LLM/{model}] system={len(system_prompt)}c user={len(user_prompt)}c", verbose=verbose)

    try:
        result = subprocess.run(
            cmd,
            input=user_prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=clean_env,
        )
        if result.returncode != 0:
            raise RuntimeError(f"claude CLI failed ({result.returncode}): {result.stderr[:300]}")
        output = result.stdout.strip()
        if not output:
            raise RuntimeError("claude CLI returned empty output")
        if verbose:
            log(f"  [LLM/{model}] response={len(output)}c: {output[:200]}", verbose=verbose)
        return output
    except subprocess.TimeoutExpired:
        raise AbortContactRun("timeout", f"claude CLI timed out after {timeout}s")


def parse_json_response(text: str) -> dict | list:
    """
    Parse LLM response that should be JSON.
    Handles: raw JSON, ```json fences, wrapped in {"result": "..."}.
    """
    text = text.strip()

    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try unwrapping {"result": "..."} wrapper from claude -p
    try:
        wrapper = json.loads(text)
        if isinstance(wrapper, dict) and "result" in wrapper:
            inner = wrapper["result"]
            if isinstance(inner, str):
                return json.loads(inner)
            elif isinstance(inner, (dict, list)):
                return inner
    except (json.JSONDecodeError, AttributeError):
        pass

    # Strip markdown fences
    fence_match = re.search(r'```(?:json)?\s*\n?([\s\S]*?)\n?```', text)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass

    # Try raw_decode to find JSON object anywhere in the text
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in ('{', '['):
            try:
                obj, _ = decoder.raw_decode(text, i)
                if isinstance(obj, (dict, list)):
                    return obj
            except json.JSONDecodeError:
                continue

    raise RuntimeError(f"Could not parse JSON from LLM response: {text[:300]}")


# ── PASS A-FACTS: Extract structured facts ────────────────────
_PASS_A_FACTS_SYSTEM = """\
You extract structured facts from messages for long-term storage.

FACT TYPES and optional detail keys:
- travel:       {"destination": str, "depart": "YYYY-MM-DD", "return": "YYYY-MM-DD", "purpose": str}
- event:        {"description": str, "location": str}
- preference:   {"domain": str, "value": str}
- project:      {"description": str, "status": "active|completed"}
- relationship: {"person": str, "relation": str}
- deadline:     {"description": str, "due_date": "YYYY-MM-DD"}

CONFIDENCE:
- high: Direct first-person statement with details ("I'm flying to SF March 20-25")
- medium: Third-party or indirect mention ("Mom said she's visiting")
- low: Vague or speculative ("might go to SF")

RULES:
- Extract concrete, session-spanning facts. Not small talk, weather, or transient content.
- Do NOT extract system metadata or engagement/proposal content.
- Do NOT extract instructions or requests ABOUT this assistant system — e.g. "use Sonnet",
  "restart the session", "fix the skill", "run nightly at 2am". These are directives to the
  system, not personal facts about the contact.
- For "preference" type: only extract preferences about the contact's OWN life (food, music,
  tools they personally use, activities). NOT preferences about how this assistant should operate.
- Resolve relative dates ("next Saturday") against message timestamps.
- Only expire facts with EXPLICIT contradiction ("trip canceled").

Output JSON:
{
  "new_facts": [
    {"fact_type": "travel", "summary": "...", "confidence": "high",
     "details": {"destination": "..."}, "starts_at": "2026-03-20T00:00:00Z", "ends_at": null}
  ],
  "updated_facts": [
    {"existing_fact_id": 42, "summary": "...", "ends_at": "2026-03-25T00:00:00Z"}
  ],
  "expired_fact_ids": [7, 12]
}

Output ONLY valid JSON. If nothing to extract: {"new_facts": [], "updated_facts": [], "expired_fact_ids": []}"""

def run_pass_a_facts(
    contact_name: str,
    messages_text: str,
    existing_facts: list[dict],
    verbose: bool = False,
) -> dict:
    # Slim facts for prompt (id + type + summary + key dates only)
    slim_facts = []
    for f in existing_facts:
        slim = {"id": f["id"], "fact_type": f["fact_type"], "summary": f["summary"]}
        if f.get("details"):
            slim["details"] = f["details"]
        if f.get("starts_at"):
            slim["starts_at"] = f["starts_at"]
        if f.get("ends_at"):
            slim["ends_at"] = f["ends_at"]
        slim_facts.append(slim)

    facts_str = json.dumps(slim_facts, indent=2)
    if len(facts_str) > MAX_EXISTING_FACTS_CHARS:
        # Trim to most recent 30 by updated_at
        trimmed = sorted(existing_facts, key=lambda f: f.get("updated_at") or f.get("created_at") or "", reverse=True)[:30]
        slim_facts = [{"id": f["id"], "fact_type": f["fact_type"], "summary": f["summary"]} for f in trimmed]
        facts_str = json.dumps(slim_facts, indent=2)

    user_prompt = f"""Contact: {contact_name}

Today's messages:
{messages_text}

Existing active facts (id + summary for dedup):
{facts_str}

Extract structured facts. Output JSON only."""

    raw = call_claude(_PASS_A_FACTS_SYSTEM, user_prompt, model=MODEL_PASS_A, verbose=verbose)
    result = parse_json_response(raw)
    if not isinstance(result, dict):
        raise RuntimeError(f"Pass A-facts returned {type(result).__name__}, expected dict")
    return result


# ── PASS B: Grounding verification ────────────────────────────
_PASS_B_SYSTEM = """\
You are a fact-checker verifying proposed facts against source messages.

For each fact, find a VERBATIM supporting quote in the messages.

Rules:
- ACCEPT: Quote found AND fact is correctly derived from it
- REFUTE: Quote not found, or fact misinterprets/overstates the quote, or negation missed
- Subject check: the contact must be the SUBJECT of the fact (not just the messenger)
  - "My cousin got married" → REFUTE (fact is about cousin, not sender)
  - "I got married" → ACCEPT
- Negation check: "I don't have a dog" → REFUTE any "has a dog" claim
- Confidence match: only low-confidence facts for vague/speculative mentions

Output JSON:
{
  "facts": {
    "accepted": [{"item": "Flying to SF March 20-25", "supporting_quote": "Flying to SF March 20-25..."}],
    "refuted":  [{"item": "...", "reason": "..."}]
  }
}

Be strict. When in doubt, REFUTE."""

def run_pass_b(
    contact_name: str,
    messages_text: str,
    proposed_facts: list[dict],
    verbose: bool = False,
) -> dict:
    facts_summary = json.dumps([{"fact_type": f.get("fact_type"), "summary": f.get("summary")} for f in proposed_facts])

    user_prompt = f"""Contact: {contact_name}

Messages:
{messages_text}

Proposed facts to verify:
{facts_summary}

Verify each fact against the messages. Output JSON only."""

    raw = call_claude(_PASS_B_SYSTEM, user_prompt, model=MODEL_PASS_B, verbose=verbose)
    result = parse_json_response(raw)
    if not isinstance(result, dict):
        raise RuntimeError(f"Pass B returned {type(result).__name__}, expected dict")
    return result


# ── Pass B resolution ─────────────────────────────────────────
def normalize(s: str) -> str:
    return re.sub(r'\s+', ' ', s.strip().lower())


def resolve_pass_b_section(
    proposed: list,
    b_accepted: list[dict],
    b_refuted: list[dict],
    proposed_key: str = "item",
) -> tuple[list[dict], list[dict]]:
    """
    Items not explicitly accepted or refuted are treated as implicitly refuted.
    - b_accepted / b_refuted always have {"item": "..."} (Pass B output format)
    - proposed items are either strings (bullets) or dicts with proposed_key (facts)
    """
    accepted_norm = {normalize(x["item"]) for x in b_accepted}
    refuted_norm  = {normalize(x["item"]) for x in b_refuted}

    def get_item_str(x) -> str:
        if isinstance(x, str):
            return x
        return x.get(proposed_key) or x.get("item") or str(x)

    implicit = [
        {"item": get_item_str(p), "reason": "not mentioned by verifier"}
        for p in proposed
        if normalize(get_item_str(p)) not in accepted_norm
        and normalize(get_item_str(p)) not in refuted_norm
    ]
    return b_accepted, b_refuted + implicit


# ── DB commit ─────────────────────────────────────────────────
def commit_facts_to_db(
    conn: sqlite3.Connection,
    contact_name: str,
    new_facts: list[dict],
    updated_facts: list[dict],
    expired_fact_ids: list[int],
) -> int:
    """All-or-nothing transaction. Returns count of changes committed."""
    now_ts = datetime.utcnow().isoformat()
    committed = 0

    with conn:
        for fact in new_facts:
            details_str = json.dumps(fact.get("details") or {})
            conn.execute(
                """INSERT INTO facts
                   (contact, fact_type, summary, details, confidence,
                    starts_at, ends_at, active, created_at, source, schema_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, 'consolidate_memory', ?)""",
                (
                    contact_name,
                    fact["fact_type"],
                    fact["summary"],
                    details_str,
                    fact.get("confidence", "high"),
                    fact.get("starts_at"),
                    fact.get("ends_at"),
                    now_ts,
                    CURRENT_SCHEMA_VERSION,  # stamped here
                ),
            )
            committed += 1

        for upd in updated_facts:
            fid = upd["existing_fact_id"]
            updates = {k: upd[k] for k in UPDATABLE_FIELDS if k in upd and upd[k] is not None}
            if not updates:
                continue
            updates["updated_at"] = now_ts
            set_clause = ", ".join(f"{k}=?" for k in updates)
            conn.execute(
                f"UPDATE facts SET {set_clause} WHERE id=? AND active=1",
                [*updates.values(), fid],
            )
            committed += 1

        for fid in expired_fact_ids:
            conn.execute(
                "UPDATE facts SET active=0, updated_at=? WHERE id=? AND active=1",
                (now_ts, fid),
            )
            committed += 1

    return committed


# ── Audit health check ────────────────────────────────────────
def check_audit_health(audit: AuditRecord) -> list[str]:
    warnings = []
    sec = audit.facts
    if sec.a_proposed > 0:
        drop = 1 - (sec.b_accepted / sec.a_proposed)
        if drop > HIGH_DROP_RATE_THRESHOLD:
            warnings.append(f"facts: {drop:.0%} drop rate ({sec.b_refuted + sec.b_implicit_refuted} refuted of {sec.a_proposed})")
    if sec.schema_dropped_update > 0:
        warnings.append(f"facts: {sec.schema_dropped_update} update(s) dropped (bad IDs or schema)")
    if sec.unexpected_schema_errors > 0:
        warnings.append(f"[ACTION REQUIRED] facts: {sec.unexpected_schema_errors} unexpected validator error(s)")
    return warnings


# ── Watermark advancement ─────────────────────────────────────
def advance_watermark(
    contact_key: str,
    rows: list[tuple],
    cutoff_ts: int,
    checkpoints: dict,
) -> None:
    """
    Update watermark after successful run.
    - Hit limit (300): advance to newest processed timestamp → next run picks up from there
    - Under limit: advance to cutoff_ts (current time)
    """
    if len(rows) == MAX_MESSAGES_LIMIT:
        # rows are chronological (ASC), so [-1] is newest
        newest_ts = rows[-1][0]
        checkpoints[contact_key] = newest_ts
        log(f"Hit message limit — watermark advanced to newest batch message")
    else:
        checkpoints[contact_key] = cutoff_ts


# ── Main per-contact pipeline ─────────────────────────────────
def consolidate_contact(
    contact_name: str,
    tier: str,
    chat_id: str,
    conn: sqlite3.Connection,
    checkpoints: dict,
    cutoff_ts: int,
    dry_run: bool = False,
    verbose: bool = False,
) -> AuditRecord:
    audit = AuditRecord(contact=contact_name, timestamp=datetime.utcnow().isoformat())

    try:
        # ── 1. Fetch messages ──────────────────────────────────
        since_ts = checkpoints.get(chat_id) or checkpoints.get(contact_name)
        rows = fetch_messages_from_bus(chat_id, since_ts, conn)
        audit.messages_scanned = len(rows)
        log(f"[{contact_name}] {len(rows)} messages since {since_ts}", verbose=verbose)

        if not rows:
            log(f"[{contact_name}] No new messages — skipping")
            # Advance watermark to now (no gap)
            checkpoints[chat_id] = cutoff_ts
            return audit

        messages_text = truncate_to_budget(format_messages_text(rows), MAX_MESSAGES_CHARS)
        messages_lower = messages_text.lower()

        # ── 2. Load existing facts ────────────────────────────
        existing_facts = load_existing_facts(conn, contact_name, audit.facts)
        existing_by_id = {f["id"]: f for f in existing_facts}

        # ── 3. Pass A-facts ───────────────────────────────────
        a_facts = run_pass_a_facts(contact_name, messages_text, existing_facts, verbose=verbose)
        new_facts_raw    = a_facts.get("new_facts") or []
        updated_facts    = a_facts.get("updated_facts") or []
        expired_fact_ids = [int(x) for x in (a_facts.get("expired_fact_ids") or [])]
        audit.facts.a_proposed = len(new_facts_raw)
        log(f"[{contact_name}] Pass A-facts: {len(new_facts_raw)} new, {len(updated_facts)} updates, {len(expired_fact_ids)} expired", verbose=verbose)

        # ── 4. Circuit breaker ────────────────────────────────
        if circuit_breaker_check(new_facts_raw, len(rows), verbose=verbose):
            raise AbortContactRun("circuit_breaker", f"{len(new_facts_raw)} facts")

        # ── 5. Schema validation ──────────────────────────────
        valid_new_facts = []
        for fact in new_facts_raw:
            try:
                validate_new_fact(fact)
                valid_new_facts.append(fact)
            except ValidationError as e:
                audit.facts.schema_dropped_new += 1
                log(f"[{contact_name}] Schema drop (new): {e}")
            except Exception as e:
                audit.facts.schema_dropped_new += 1
                audit.facts.unexpected_schema_errors += 1
                log(f"[{contact_name}] Unexpected schema error (new): {type(e).__name__}: {e}")

        valid_updated_facts = []
        for upd in updated_facts:
            try:
                validate_updated_fact(upd, existing_by_id)
                valid_updated_facts.append(upd)
            except ValidationError as e:
                audit.facts.schema_dropped_update += 1
                log(f"[{contact_name}] Schema drop (update): {e}")
            except Exception as e:
                audit.facts.schema_dropped_update += 1
                audit.facts.unexpected_schema_errors += 1
                log(f"[{contact_name}] Unexpected schema error (update): {type(e).__name__}: {e}")

        # ── 6. Pre-verify grounding (facts only) ──────────────
        grounded_facts = []
        for fact in valid_new_facts:
            summary = fact.get("summary", "")
            if word_overlap(summary, messages_lower) < 0.35:
                audit.facts.pre_verify_dropped += 1
                log(f"[{contact_name}] Pre-verify drop (word overlap): {summary[:60]}", verbose=verbose)
            elif not check_entities(summary, messages_lower):
                audit.facts.pre_verify_dropped += 1
                log(f"[{contact_name}] Pre-verify drop (entity check): {summary[:60]}", verbose=verbose)
            else:
                grounded_facts.append(fact)

        # ── 7. Sensitive content check ────────────────────────
        all_proposed_text = " ".join(f.get("summary", "") for f in grounded_facts)
        if check_sensitive(all_proposed_text):
            # Advance watermark BEFORE raising so we don't re-trigger next run
            checkpoints[chat_id] = cutoff_ts
            save_checkpoints(checkpoints)
            if _ADMIN_PHONE:
                try:
                    subprocess.run(
                        [str(SEND_SMS_CLI), _ADMIN_PHONE,
                         f"[MEMORY] Sensitive content detected for {contact_name} — batch skipped"],
                        timeout=10, capture_output=True,
                    )
                except Exception:
                    pass
            audit.sensitive_content_detected = True
            raise AbortContactRun("sensitive_content", f"contact={contact_name}")

        # ── 8. Pass B: Grounding verification (facts only) ────
        if grounded_facts:
            b_result = run_pass_b(contact_name, messages_text, grounded_facts, verbose=verbose)
            b_facts = b_result.get("facts", {})

            b_facts_accepted = b_facts.get("accepted") or []
            b_facts_refuted  = b_facts.get("refuted") or []

            # Implicit refutation
            b_facts_accepted, b_facts_refuted = resolve_pass_b_section(
                grounded_facts, b_facts_accepted, b_facts_refuted, proposed_key="summary"
            )

            audit.facts.b_accepted = len(b_facts_accepted)
            audit.facts.b_refuted = len(b_facts_refuted)

            # Quote verification
            verified_facts = []
            accepted_fact_summaries = {normalize(x["item"]) for x in b_facts_accepted}
            for fact in grounded_facts:
                if normalize(fact.get("summary", "")) in accepted_fact_summaries:
                    quote = ""
                    for x in b_facts_accepted:
                        if normalize(x["item"]) == normalize(fact.get("summary", "")):
                            quote = x.get("supporting_quote", "")
                            break
                    if verify_supporting_quote(quote, messages_text):
                        verified_facts.append(fact)
                    else:
                        audit.facts.quote_verify_dropped += 1
                        log(f"[{contact_name}] Quote verify failed (fact): {fact.get('summary', '')[:60]}", verbose=verbose)
        else:
            verified_facts = []

        # ── 9. Audit health ────────────────────────────────────
        warnings = check_audit_health(audit)
        for w in warnings:
            audit.facts.warnings.append(w)
            log(f"[{contact_name}] WARN: {w}", verbose=verbose)

        # ── 10. Dry-run output ─────────────────────────────────
        if dry_run:
            _print_dry_run(contact_name, verified_facts, valid_updated_facts, expired_fact_ids)
            # Don't advance watermark or write anything
            return audit

        # ── 11. Commit facts (all-or-nothing) ─────────────────
        if not verified_facts and not valid_updated_facts and not expired_fact_ids:
            log(f"[{contact_name}] Nothing to commit after verification")
        else:
            # Validate expired IDs are known
            for fid in expired_fact_ids:
                if fid not in existing_by_id:
                    raise AbortContactRun("validation_error", f"expired_fact_id {fid} not in existing facts")

            facts_committed = commit_facts_to_db(
                conn, contact_name, verified_facts, valid_updated_facts, expired_fact_ids
            )
            audit.facts.committed = facts_committed
            log(f"[{contact_name}] Committed: {facts_committed} fact changes")

        # ── 12. Advance watermark ──────────────────────────────
        advance_watermark(chat_id, rows, cutoff_ts, checkpoints)
        save_checkpoints(checkpoints)
        audit.committed = True

    except AbortContactRun as e:
        audit.abort_reason = e.reason
        log(f"[{contact_name}] ABORT: {e}", always=True)
        # Don't advance watermark (unless sensitive_content — already saved above)

    except Exception as e:
        audit.errors.append(f"{type(e).__name__}: {e}")
        log(f"[{contact_name}] ERROR: {e}", always=True)

    return audit


def _print_dry_run(
    contact_name: str,
    facts: list[dict],
    updated_facts: list[dict],
    expired_ids: list[int],
) -> None:
    print(f"\n{'='*60}")
    print(f"DRY RUN: {contact_name}")
    print(f"{'='*60}")
    if facts:
        print(f"\nNew facts ({len(facts)}):")
        for f in facts:
            print(f"  + [{f.get('fact_type', '?')}] {f['summary']}")
    if updated_facts:
        print(f"\nFact updates ({len(updated_facts)}):")
        for u in updated_facts:
            print(f"  ~ #{u['existing_fact_id']}: {u}")
    if expired_ids:
        print(f"\nFacts to expire: {expired_ids}")
    if not any([facts, updated_facts, expired_ids]):
        print("  (nothing to commit)")


# ── Contact enumeration ────────────────────────────────────────
def get_all_contacts() -> list[dict]:
    """Get all contacts with phone numbers and tiers."""
    try:
        result = subprocess.run(
            [str(CONTACTS_CLI), "list"],
            capture_output=True, text=True, timeout=30,
        )
        contacts = []
        for line in result.stdout.strip().splitlines():
            if "|" not in line:
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 3:
                name, phone, tier = parts[0], parts[1], parts[2]
                if phone and phone != "(no phone)":
                    contacts.append({"name": name, "phone": phone, "tier": tier})
        return contacts
    except Exception as e:
        log(f"Failed to list contacts: {e}", always=True)
        return []


# ── Migration commands ─────────────────────────────────────────
def cmd_add_schema_version_column():
    """Add schema_version column to existing deployments. Idempotent."""
    conn = sqlite3.connect(str(BUS_DB_PATH))
    try:
        conn.execute("ALTER TABLE facts ADD COLUMN schema_version INTEGER DEFAULT 0 NOT NULL")
        conn.commit()
        print("✓ Added schema_version column to facts table")
    except sqlite3.OperationalError:
        print("✓ schema_version column already exists — nothing to do")
    finally:
        conn.close()


def cmd_migrate_facts():
    """Archive facts with schema_version < CURRENT_SCHEMA_VERSION. Idempotent."""
    conn = sqlite3.connect(str(BUS_DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        # Create archive table if needed
        conn.execute("""
            CREATE TABLE IF NOT EXISTS facts_archive (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_id INTEGER NOT NULL,
                contact TEXT NOT NULL,
                fact_type TEXT NOT NULL,
                summary TEXT NOT NULL,
                details TEXT,
                confidence TEXT DEFAULT 'high',
                starts_at TEXT,
                ends_at TEXT,
                active INTEGER DEFAULT 0 NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT,
                source TEXT NOT NULL,
                schema_version INTEGER DEFAULT 0 NOT NULL,
                archived_at TEXT NOT NULL,
                archive_reason TEXT
            )
        """)
        conn.commit()

        old_facts = conn.execute(
            "SELECT * FROM facts WHERE (schema_version IS NULL OR schema_version < ?) AND active=1",
            (CURRENT_SCHEMA_VERSION,)
        ).fetchall()

        if not old_facts:
            print("✓ No facts to migrate")
            return

        print(f"Migrating {len(old_facts)} facts to facts_archive...")
        now_ts = datetime.utcnow().isoformat()
        with conn:
            for row in old_facts:
                d = dict(row)
                conn.execute(
                    """INSERT INTO facts_archive
                       (original_id, contact, fact_type, summary, details,
                        confidence, starts_at, ends_at, active, created_at, updated_at,
                        source, schema_version, archived_at, archive_reason)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, 'migration')""",
                    (d["id"], d["contact"], d["fact_type"], d["summary"], d.get("details"),
                     d.get("confidence", "high"), d.get("starts_at"), d.get("ends_at"),
                     d["created_at"], d.get("updated_at"), d["source"],
                     d.get("schema_version", 0), now_ts),
                )
                conn.execute(
                    "UPDATE facts SET active=0, updated_at=? WHERE id=?",
                    (now_ts, d["id"]),
                )
        print(f"✓ Archived {len(old_facts)} facts (active=0). Rollback: UPDATE facts SET active=1 WHERE updated_at='{now_ts}'")
    finally:
        conn.close()


# ── Summary printing ───────────────────────────────────────────
def print_summary(results: list[AuditRecord]) -> None:
    total = len(results)
    committed = sum(1 for r in results if r.committed)
    aborted = sum(1 for r in results if r.abort_reason)
    errored = sum(1 for r in results if r.errors)
    total_facts = sum(r.facts.committed for r in results)

    print(f"\n{'='*50}")
    print(f"Memory consolidation complete")
    print(f"  Contacts: {total} total, {committed} committed, {aborted} aborted, {errored} errors")
    print(f"  Facts: {total_facts} committed")

    if aborted:
        print(f"\nAborted contacts:")
        for r in results:
            if r.abort_reason:
                print(f"  {r.contact}: {r.abort_reason}")

    if errored:
        print(f"\nErrors:")
        for r in results:
            for e in r.errors:
                print(f"  {r.contact}: {e}")

    warnings = []
    for r in results:
        for w in r.facts.warnings:
            warnings.append(f"  {r.contact}: {w}")
    if warnings:
        print(f"\nWarnings:")
        for w in warnings:
            print(w)


# ── Main ───────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Unified nightly memory consolidation")
    parser.add_argument("contact", nargs="?", help="Contact name to consolidate")
    parser.add_argument("--all", action="store_true", help="Run for all contacts")
    parser.add_argument("--dry-run", action="store_true", help="Print proposed changes without writing")
    parser.add_argument("--verbose", action="store_true", help="Show LLM input/output")
    parser.add_argument("--add-schema-version-column", action="store_true")
    parser.add_argument("--migrate-facts", action="store_true")
    args = parser.parse_args()

    if args.add_schema_version_column:
        cmd_add_schema_version_column()
        return

    if args.migrate_facts:
        cmd_migrate_facts()
        return

    if not args.contact and not args.all:
        parser.print_help()
        sys.exit(1)

    # Open shared DB connection (row_factory set here, not in sub-functions)
    conn = sqlite3.connect(str(BUS_DB_PATH))
    conn.row_factory = sqlite3.Row

    checkpoints = load_checkpoints()
    cutoff_ts = int(time.time())

    # Get contacts to process
    if args.all:
        contacts_raw = get_all_contacts()
    else:
        # Single contact: look up phone from contacts CLI
        all_contacts = get_all_contacts()
        match = next((c for c in all_contacts if c["name"].lower() == args.contact.lower()), None)
        if match:
            contacts_raw = [match]
        else:
            # Fallback: treat as phone/chat_id directly
            contacts_raw = [{"name": args.contact, "phone": args.contact, "tier": "admin"}]

    results = []
    for i, c in enumerate(contacts_raw):
        name = c["name"]
        tier = c.get("tier", "favorite")
        phone = c.get("phone", "")
        chat_id = _phone_to_chat_id(phone) if phone else name

        # Skip partner contact for sensitive leak protection
        if is_partner_contact(name):
            log(f"[{name}] Skipping partner contact (sensitive leak protection)", always=True)
            continue

        if i > 0 and not args.dry_run:
            time.sleep(CONTACT_SLEEP_SECONDS)

        audit = consolidate_contact(
            contact_name=name,
            tier=tier,
            chat_id=chat_id,
            conn=conn,
            checkpoints=checkpoints,
            cutoff_ts=cutoff_ts,
            dry_run=args.dry_run,
            verbose=args.verbose,
        )
        results.append(audit)

    conn.close()
    print_summary(results)


if __name__ == "__main__":
    main()
