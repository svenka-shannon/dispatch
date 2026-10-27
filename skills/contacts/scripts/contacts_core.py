"""
contacts_core - Core functions for macOS Contacts.app management

This module can be imported directly by other Python code to avoid subprocess overhead.
The CLI wrapper (contacts) imports from here.

Read path uses SQLite directly against AddressBook DB (fast, no Contacts.app dependency).
Write path uses AppleScript (Contacts.app required for mutations).
"""

import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Optional, Dict, List

ADDRESSBOOK_DIR = Path.home() / "Library/Application Support/AddressBook"
ADDRESSBOOK_DB = ADDRESSBOOK_DIR / "AddressBook-v22.abcddb"

# Map group names to tier strings
TIER_GROUP_MAP = {
    "Claude Admin": "admin",
    "Claude Partner": "partner",
    "Claude Family": "family",
    "Claude Favorites": "favorite",
    "Claude Bots": "bots",
}

# Explicit tier precedence — MOST privileged wins (lower number = higher rank).
#
# Do NOT rely on "first match wins" over a SQL result: the group query has no
# ORDER BY, so row order is undefined and a contact in two tier groups would get
# a random tier that can change between calls. Tier IS access control, so that
# is a security bug, not a cosmetic one: an admin who is also in Favorites could
# resolve to 'favorite' and silently lose access (or the reverse).
TIER_PRECEDENCE = {
    "admin": 0,
    "partner": 1,
    "family": 2,
    "favorite": 3,
    "bots": 4,
}


def _assign_tier(tier_map: Dict[int, str], contact_pk: int, group_name: str) -> None:
    """Record the HIGHEST-precedence tier for a contact, independent of row order.

    Policy is highest-privilege-wins, matching what the docs and the AppleScript
    path already claimed. That is a deliberate choice, NOT obviously the right
    one — see the note below — so multi-group membership is also reported loudly
    rather than resolved in silence.

    Dual membership is ALWAYS a data error. The danger is not which way it
    resolves, it's that either policy resolves it invisibly and the bad data
    survives. Warn, so a human fixes the card.
    """
    tier = TIER_GROUP_MAP[group_name]
    current = tier_map.get(contact_pk)

    # Conflict state is derived from tier_map itself — deliberately NOT module
    # state keyed by pk. Z_PK is only unique WITHIN one AddressBook source DB,
    # and callers walk several, so a module-global set would merge pk=30 in one
    # DB with pk=30 in another and report two unrelated people as one contact in
    # "multiple tier groups". tier_map is already per-DB, so scoping to it makes
    # the false positive structurally impossible rather than merely unlikely.
    if current is not None and current != tier:
        winner = min((current, tier), key=lambda t: TIER_PRECEDENCE[t])
        print(
            f"WARNING: contact pk={contact_pk} is in MULTIPLE tier groups "
            f"({current}, {tier}) — resolving to '{winner}' "
            f"(highest privilege of {current}/{tier}). "
            f"This is a data error: fix the card in Contacts.app so it belongs "
            f"to exactly one Claude tier group.",
            file=sys.stderr,
        )

    if current is None or TIER_PRECEDENCE[tier] < TIER_PRECEDENCE[current]:
        tier_map[contact_pk] = tier


# ──────────────────────────────────────────────────────────────
# SQLite read path (fast, no Contacts.app dependency)
# ──────────────────────────────────────────────────────────────

def _get_all_addressbook_dbs() -> List[Path]:
    """Get all AddressBook database paths (root + all sources).

    macOS stores contacts across multiple SQLite databases:
    - Root: AddressBook-v22.abcddb (may be stale)
    - Sources: Sources/<UUID>/AddressBook-v22.abcddb (one per account/source)

    iCloud sync writes to per-source DBs, so we must query ALL of them.

    IMPORTANT: Returns DBs sorted by modification time (newest first).
    This ensures single-item queries like get_notes return the most recent
    version when the same contact exists in multiple synced sources.
    AppleScript writes go to the active iCloud source (most recently modified),
    so SQLite reads should prefer that same source for consistency.
    """
    dbs = []

    # Add root DB if it exists
    if ADDRESSBOOK_DB.exists():
        dbs.append(ADDRESSBOOK_DB)

    # Add all per-source databases
    sources_dir = ADDRESSBOOK_DIR / "Sources"
    if sources_dir.exists():
        for source_dir in sources_dir.iterdir():
            source_db = source_dir / "AddressBook-v22.abcddb"
            if source_db.exists():
                dbs.append(source_db)

    # Sort by modification time (newest first) so queries prefer the active source
    dbs.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    return dbs


def _get_db_connection(db_path: Path | None = None) -> sqlite3.Connection:
    """Open a read-only connection to an AddressBook database."""
    if db_path is None:
        # Default to first available DB (for backwards compat)
        dbs = _get_all_addressbook_dbs()
        db_path = dbs[0] if dbs else ADDRESSBOOK_DB
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _query_all_dbs(query_func):
    """Run a query function across all AddressBook databases and merge results.

    query_func takes a sqlite3.Connection and returns results.
    For list queries, results are merged (deduped by name).
    For single-item queries, first non-None result wins.
    """
    dbs = _get_all_addressbook_dbs()
    all_results = []

    for db_path in dbs:
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
            conn.row_factory = sqlite3.Row
            result = query_func(conn)
            conn.close()
            if result is not None:
                if isinstance(result, list):
                    all_results.extend(result)
                else:
                    return result  # First non-None wins for single-item queries
        except Exception:
            continue

    return all_results if all_results else None


def list_contacts_sqlite(tier_filter: str | None = None) -> List[Dict[str, str]]:
    """List all contacts with tiers via direct SQLite read, querying ALL source databases.

    Returns list of dicts with 'name', 'phone', 'emails', 'tier', 'notes'.
    Merges contacts from all databases, deduping by name (preferring entries with notes).
    """
    all_contacts: Dict[str, Dict] = {}  # Keyed by name for deduping

    for db_path in _get_all_addressbook_dbs():
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
            conn.row_factory = sqlite3.Row

            # Build tier mapping: contact Z_PK -> tier
            tier_map: Dict[int, str] = {}
            cursor = conn.execute("""
                SELECT pg.Z_22CONTACTS AS contact_pk, g.ZNAME AS group_name
                FROM Z_22PARENTGROUPS pg
                JOIN ZABCDRECORD g ON g.Z_PK = pg.Z_19PARENTGROUPS1
                WHERE g.ZNAME IN ({})
            """.format(",".join(f"'{g}'" for g in TIER_GROUP_MAP)))
            for row in cursor:
                _assign_tier(tier_map, row["contact_pk"], row["group_name"])

            # Get all contacts with phones and emails (local to this DB)
            contacts_by_pk: Dict[int, Dict] = {}

            # Get all people (not groups - groups have ZFIRSTNAME=NULL and no phone)
            cursor = conn.execute("""
                SELECT Z_PK, ZFIRSTNAME, ZLASTNAME
                FROM ZABCDRECORD
                WHERE ZFIRSTNAME IS NOT NULL OR ZLASTNAME IS NOT NULL
            """)
            for row in cursor:
                pk = row["Z_PK"]
                first = row["ZFIRSTNAME"] or ""
                last = row["ZLASTNAME"] or ""
                name = f"{first} {last}".strip()
                tier = tier_map.get(pk, "unknown")
                if tier_filter and tier != tier_filter:
                    continue
                contacts_by_pk[pk] = {
                    "name": name,
                    "phone": None,
                    "emails": [],
                    "tier": tier,
                }

            # Attach phone numbers
            if contacts_by_pk:
                cursor = conn.execute("SELECT ZOWNER, ZFULLNUMBER FROM ZABCDPHONENUMBER")
                for row in cursor:
                    pk = row["ZOWNER"]
                    if pk in contacts_by_pk and not contacts_by_pk[pk]["phone"]:
                        contacts_by_pk[pk]["phone"] = row["ZFULLNUMBER"]

            # Attach emails
            if contacts_by_pk:
                cursor = conn.execute("SELECT ZOWNER, ZADDRESS FROM ZABCDEMAILADDRESS")
                for row in cursor:
                    pk = row["ZOWNER"]
                    if pk in contacts_by_pk and row["ZADDRESS"]:
                        contacts_by_pk[pk]["emails"].append(row["ZADDRESS"].lower())

            # Attach notes
            if contacts_by_pk:
                cursor = conn.execute("""
                    SELECT r.Z_PK, n.ZTEXT
                    FROM ZABCDRECORD r
                    JOIN ZABCDNOTE n ON n.Z_PK = r.ZNOTE
                    WHERE n.ZTEXT IS NOT NULL
                """)
                for row in cursor:
                    pk = row["Z_PK"]
                    if pk in contacts_by_pk:
                        contacts_by_pk[pk]["notes"] = row["ZTEXT"]

            conn.close()

            # Merge into all_contacts (prefer entries with notes or better tier)
            for contact in contacts_by_pk.values():
                name = contact["name"]
                if name not in all_contacts:
                    all_contacts[name] = contact
                else:
                    existing = all_contacts[name]
                    # Prefer the entry with notes
                    if contact.get("notes") and not existing.get("notes"):
                        all_contacts[name] = contact
                    # Or prefer non-unknown tier
                    elif contact["tier"] != "unknown" and existing["tier"] == "unknown":
                        all_contacts[name] = contact

        except Exception:
            continue

    return list(all_contacts.values())


def lookup_phone_sqlite(phone: str) -> Optional[Dict[str, str]]:
    """Look up contact by phone number via SQLite, querying ALL source databases.

    Returns {name, phone, tier} or None if not found.
    """
    normalized = ''.join(c for c in phone if c.isdigit() or c == '+')
    if not normalized:
        return None

    def query_phone(conn):
        # Build tier mapping for this DB
        tier_map: Dict[int, str] = {}
        cursor = conn.execute("""
            SELECT pg.Z_22CONTACTS, g.ZNAME
            FROM Z_22PARENTGROUPS pg
            JOIN ZABCDRECORD g ON g.Z_PK = pg.Z_19PARENTGROUPS1
            WHERE g.ZNAME IN ({})
        """.format(",".join(f"'{g}'" for g in TIER_GROUP_MAP)))
        for row in cursor:
            _assign_tier(tier_map, row[0], row[1])

        # Find phone number owner
        cursor = conn.execute(
            "SELECT ZOWNER, ZFULLNUMBER FROM ZABCDPHONENUMBER"
        )
        for row in cursor:
            full = row["ZFULLNUMBER"] or ""
            clean = ''.join(c for c in full if c.isdigit() or c == '+')
            if clean == normalized or (normalized.startswith('+') and clean == normalized[1:]) or clean.endswith(normalized[-10:]):
                owner_pk = row["ZOWNER"]
                # Get contact name
                name_row = conn.execute(
                    "SELECT ZFIRSTNAME, ZLASTNAME FROM ZABCDRECORD WHERE Z_PK = ?",
                    (owner_pk,)
                ).fetchone()
                if name_row:
                    first = name_row["ZFIRSTNAME"] or ""
                    last = name_row["ZLASTNAME"] or ""
                    name = f"{first} {last}".strip()
                    tier = tier_map.get(owner_pk, "unknown")
                    return {"name": name, "phone": full, "tier": tier}
        return None

    return _query_all_dbs(query_phone)


def lookup_email_sqlite(email: str) -> Optional[Dict[str, str]]:
    """Look up contact by email via SQLite, querying ALL source databases.

    Returns {name, email, tier} or None if not found.
    """
    email_lower = email.lower().strip()
    if not email_lower:
        return None

    def query_email(conn):
        # Build tier mapping for this DB
        tier_map: Dict[int, str] = {}
        cursor = conn.execute("""
            SELECT pg.Z_22CONTACTS, g.ZNAME
            FROM Z_22PARENTGROUPS pg
            JOIN ZABCDRECORD g ON g.Z_PK = pg.Z_19PARENTGROUPS1
            WHERE g.ZNAME IN ({})
        """.format(",".join(f"'{g}'" for g in TIER_GROUP_MAP)))
        for row in cursor:
            _assign_tier(tier_map, row[0], row[1])

        row = conn.execute(
            "SELECT ZOWNER, ZADDRESS FROM ZABCDEMAILADDRESS WHERE ZADDRESSNORMALIZED = ?",
            (email_lower,)
        ).fetchone()
        if row:
            owner_pk = row["ZOWNER"]
            name_row = conn.execute(
                "SELECT ZFIRSTNAME, ZLASTNAME FROM ZABCDRECORD WHERE Z_PK = ?",
                (owner_pk,)
            ).fetchone()
            if name_row:
                first = name_row["ZFIRSTNAME"] or ""
                last = name_row["ZLASTNAME"] or ""
                name = f"{first} {last}".strip()
                tier = tier_map.get(owner_pk, "unknown")
                return {"name": name, "email": row["ZADDRESS"], "tier": tier}
        return None

    return _query_all_dbs(query_email)


def get_notes_sqlite(name: str) -> Optional[str]:
    """Get contact notes via SQLite, querying ALL source databases.

    iCloud sync writes notes to per-source DBs, so we check all of them
    and return the first non-empty notes found.
    """
    def query_notes(conn):
        row = conn.execute("""
            SELECT n.ZTEXT FROM ZABCDRECORD r
            JOIN ZABCDNOTE n ON n.Z_PK = r.ZNOTE
            WHERE (r.ZFIRSTNAME || ' ' || COALESCE(r.ZLASTNAME, '')) LIKE ?
            OR r.ZSORTINGFIRSTNAME LIKE ?
        """, (f"%{name}%", f"%{name.lower()}%")).fetchone()
        if row and row["ZTEXT"]:
            return row["ZTEXT"]
        return None

    return _query_all_dbs(query_notes)


def get_tier_sqlite(name: str) -> Optional[str]:
    """Get a contact's tier by name via SQLite, querying ALL source databases.

    Returns tier string ('admin', 'partner', 'family', 'favorite') or 'unknown'.
    """
    def query_tier(conn):
        # Build tier mapping for this DB
        tier_map: Dict[int, str] = {}
        cursor = conn.execute("""
            SELECT pg.Z_22CONTACTS, g.ZNAME
            FROM Z_22PARENTGROUPS pg
            JOIN ZABCDRECORD g ON g.Z_PK = pg.Z_19PARENTGROUPS1
            WHERE g.ZNAME IN ({})
        """.format(",".join(f"'{g}'" for g in TIER_GROUP_MAP)))
        for row in cursor:
            _assign_tier(tier_map, row[0], row[1])

        # Find contact by name
        row = conn.execute("""
            SELECT Z_PK FROM ZABCDRECORD
            WHERE (ZFIRSTNAME || ' ' || COALESCE(ZLASTNAME, '')) LIKE ?
            OR ZSORTINGFIRSTNAME LIKE ?
        """, (f"%{name}%", f"%{name.lower()}%")).fetchone()

        if row:
            pk = row["Z_PK"]
            tier = tier_map.get(pk)
            if tier:
                return tier
        return None

    result = _query_all_dbs(query_tier)
    return result if result else "unknown"


# ──────────────────────────────────────────────────────────────
# AppleScript path (used for writes and as fallback)
# ──────────────────────────────────────────────────────────────

# AppleScript to get group members (reused across functions)
GET_GROUP_MEMBERS = '''
tell application "Contacts"
    set adminMembers to {}
    set partnerMembers to {}
    set familyMembers to {}
    set favMembers to {}

    try
        set adminMembers to name of every person of group "Claude Admin"
    end try
    try
        set partnerMembers to name of every person of group "Claude Partner"
    end try
    try
        set familyMembers to name of every person of group "Claude Family"
    end try
    try
        set favMembers to name of every person of group "Claude Favorites"
    end try
'''


def ensure_contacts_running():
    """Ensure Contacts.app is running. Launch it if not."""
    subprocess.run(
        ["osascript", "-e", 'tell application "Contacts" to launch'],
        capture_output=True, text=True, timeout=5
    )


def run_applescript(script: str, retry_on_app_error: bool = True, timeout: int = 15) -> tuple[bool, str]:
    """Run AppleScript and return (success, output).

    If Contacts.app isn't running (error -600), launch it and retry.
    If Contacts.app is hung (timeout), kill it and retry once.
    """
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        # Contacts.app is hung — kill it and retry once
        subprocess.run(["pkill", "-x", "Contacts"], capture_output=True)
        subprocess.run(["pkill", "-f", "osascript.*Contacts"], capture_output=True)
        import time
        time.sleep(1)
        ensure_contacts_running()
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            return False, "Contacts.app hung (timed out twice)"

    if result.returncode == 0:
        return True, result.stdout.strip()

    # Check for "Application isn't running" error (-600)
    if retry_on_app_error and "-600" in result.stderr:
        ensure_contacts_running()
        # Retry once after launching
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            return False, "Contacts.app hung after relaunch"
        if result.returncode == 0:
            return True, result.stdout.strip()

    return False, result.stderr.strip()


def lookup_email(email: str) -> Optional[Dict[str, str]]:
    """Look up a contact by email address.

    Returns dict with 'name', 'email', 'tier' or None if not found.
    """
    email_lower = email.lower().strip()
    email_esc = email_lower.replace('"', '\\"')

    script = GET_GROUP_MEMBERS + f'''
    -- Search all contacts
    set allPeople to every person
    repeat with p in allPeople
        try
            set emailCount to count of emails of p
            repeat with i from 1 to emailCount
                set em to value of email i of p
                -- Case-insensitive comparison
                if em is not missing value then
                    considering case
                        set emLower to em
                    end considering
                    -- AppleScript doesn't have lowercase, so we compare as-is
                    -- The Python side normalizes to lowercase
                    if em is "{email_esc}" then
                        set n to name of p

                        -- Determine tier
                        if adminMembers contains n then
                            return "FOUND|" & n & "|" & em & "|admin"
                        else if partnerMembers contains n then
                            return "FOUND|" & n & "|" & em & "|partner"
                        else if familyMembers contains n then
                            return "FOUND|" & n & "|" & em & "|family"
                        else if favMembers contains n then
                            return "FOUND|" & n & "|" & em & "|favorite"
                        else
                            return "NOT_FOUND|" & "{email}"
                        end if
                    end if
                end if
            end repeat
        end try
    end repeat

    return "NOT_FOUND|{email}"
end tell
'''
    success, output = run_applescript(script)
    if not success:
        return None

    if output.startswith("FOUND|"):
        parts = output.split("|")
        return {
            "name": parts[1],
            "email": parts[2],
            "tier": parts[3]
        }
    return None


def lookup_phone(phone: str) -> Optional[Dict[str, str]]:
    """Look up a contact by phone number.

    Returns dict with 'name', 'phone', 'tier' or None if not found.
    """
    # Normalize phone for comparison
    normalized = ''.join(c for c in phone if c.isdigit() or c == '+')

    script = GET_GROUP_MEMBERS + f'''
    -- Search all contacts
    set allPeople to every person
    repeat with p in allPeople
        try
            set phoneCount to count of phones of p
            repeat with i from 1 to phoneCount
                set ph to value of phone i of p
                set cleanPh to ""
                repeat with c in ph
                    if c is in "0123456789+" then
                        set cleanPh to cleanPh & c
                    end if
                end repeat

                if cleanPh contains "{normalized}" or "{normalized}" contains cleanPh then
                    set n to name of p

                    -- Determine tier
                    if adminMembers contains n then
                        return "FOUND|" & n & "|" & ph & "|admin"
                    else if partnerMembers contains n then
                        return "FOUND|" & n & "|" & ph & "|partner"
                    else if familyMembers contains n then
                        return "FOUND|" & n & "|" & ph & "|family"
                    else if favMembers contains n then
                        return "FOUND|" & n & "|" & ph & "|favorite"
                    else
                        return "NOT_FOUND|" & "{phone}"
                    end if
                end if
            end repeat
        end try
    end repeat

    return "NOT_FOUND|{phone}"
end tell
'''
    success, output = run_applescript(script)
    if not success:
        return None

    if output.startswith("FOUND|"):
        parts = output.split("|")
        return {
            "name": parts[1],
            "phone": parts[2],
            "tier": parts[3]
        }
    return None


def get_tier(name: str) -> Optional[str]:
    """Get a contact's tier by name.

    Returns tier string ('admin', 'partner', 'family', 'favorite') or None.
    """
    name_esc = name.replace('"', '\\"')

    script = GET_GROUP_MEMBERS + f'''
    if adminMembers contains "{name_esc}" then
        return "admin"
    else if partnerMembers contains "{name_esc}" then
        return "partner"
    else if familyMembers contains "{name_esc}" then
        return "family"
    else if favMembers contains "{name_esc}" then
        return "favorite"
    else
        return ""
    end if
end tell
'''
    success, output = run_applescript(script)
    if success and output:
        return output
    return None


def set_tier(name: str, tier: str) -> bool:
    """Set a contact's tier.

    Args:
        name: Contact's full name
        tier: One of 'admin', 'partner', 'family', 'favorite', or 'none' to remove

    Returns True on success.
    """
    # Proactively ensure Contacts.app is running before write operations
    ensure_contacts_running()

    name_esc = name.replace('"', '\\"')

    script = f'''
tell application "Contacts"
    set p to person "{name_esc}"

    -- Ensure groups exist
    try
        set adminGroup to group "Claude Admin"
    on error
        set adminGroup to make new group with properties {{name:"Claude Admin"}}
    end try
    try
        set partnerGroup to group "Claude Partner"
    on error
        set partnerGroup to make new group with properties {{name:"Claude Partner"}}
    end try
    try
        set familyGroup to group "Claude Family"
    on error
        set familyGroup to make new group with properties {{name:"Claude Family"}}
    end try
    try
        set favGroup to group "Claude Favorites"
    on error
        set favGroup to make new group with properties {{name:"Claude Favorites"}}
    end try

    -- Remove from all Claude groups first
    try
        remove p from adminGroup
    end try
    try
        remove p from partnerGroup
    end try
    try
        remove p from familyGroup
    end try
    try
        remove p from favGroup
    end try

    -- Add to new tier group
    if "{tier}" is "admin" then
        add p to adminGroup
    else if "{tier}" is "partner" then
        add p to partnerGroup
    else if "{tier}" is "family" then
        add p to familyGroup
    else if "{tier}" is "favorite" then
        add p to favGroup
    end if

    save
    return "UPDATED"
end tell
'''
    success, output = run_applescript(script)
    return success and output == "UPDATED"


def add_contact(first: str, last: str, phone: str, tier: str = "none") -> bool:
    """Add a new contact.

    Args:
        first: First name
        last: Last name
        phone: Phone number
        tier: Optional tier to assign

    Returns True on success.
    """
    # Proactively ensure Contacts.app is running before write operations
    ensure_contacts_running()

    tier_group_map = {
        "admin": "Claude Admin",
        "partner": "Claude Partner",
        "family": "Claude Family",
        "favorite": "Claude Favorites"
    }

    # Escape quotes in names
    first_esc = first.replace('"', '\\"')
    last_esc = last.replace('"', '\\"')

    script = f'''
tell application "Contacts"
    set newPerson to make new person with properties {{first name:"{first_esc}", last name:"{last_esc}"}}
    make new phone at end of phones of newPerson with properties {{label:"mobile", value:"{phone}"}}
'''

    if tier in tier_group_map:
        group_name = tier_group_map[tier]
        script += f'''
    try
        set targetGroup to group "{group_name}"
    on error
        set targetGroup to make new group with properties {{name:"{group_name}"}}
    end try
    add newPerson to targetGroup
'''

    script += '''
    save
    return "CREATED"
end tell
'''

    success, output = run_applescript(script)
    return success and output == "CREATED"


def list_contacts(tier_filter: str | None = None) -> List[Dict[str, str]]:
    """List all contacts with their tiers.

    Returns list of dicts with 'name', 'phone', 'emails', 'tier'.
    """
    script = GET_GROUP_MEMBERS + '''
    set output to ""

    set allPeople to every person
    repeat with p in allPeople
        set n to name of p

        -- Determine tier (check in priority order)
        if adminMembers contains n then
            set tier to "admin"
        else if partnerMembers contains n then
            set tier to "partner"
        else if familyMembers contains n then
            set tier to "family"
        else if favMembers contains n then
            set tier to "favorite"
        else
            set tier to "unknown"
        end if

        -- Get phone if available
        set ph to ""
        try
            set ph to value of first phone of p
        end try

        -- Get all emails
        set emailList to ""
        try
            set emailCount to count of emails of p
            repeat with i from 1 to emailCount
                set em to value of email i of p
                if i > 1 then
                    set emailList to emailList & ","
                end if
                set emailList to emailList & em
            end repeat
        end try

        set output to output & n & "|" & ph & "|" & tier & "|" & emailList & linefeed
    end repeat

    return output
end tell
'''
    success, output = run_applescript(script)
    if not success:
        return []

    contacts = []
    for line in output.strip().split('\n'):
        if not line.strip():
            continue
        parts = line.split('|')
        if len(parts) >= 3:
            name, phone, tier = parts[0], parts[1], parts[2]
            emails = parts[3].split(',') if len(parts) > 3 and parts[3] else []

            # Filter by tier if specified
            if tier_filter and tier != tier_filter:
                continue

            contacts.append({
                "name": name,
                "phone": phone if phone else None,
                "emails": [e.strip().lower() for e in emails if e.strip()],
                "tier": tier
            })

    return contacts


def get_notes(name: str) -> Optional[str]:
    """Get a contact's notes."""
    name_esc = name.replace('"', '\\"')

    script = f'''
tell application "Contacts"
    try
        set thePerson to person "{name_esc}"
        set theNote to note of thePerson
        if theNote is missing value then
            return ""
        else
            return theNote
        end if
    on error
        return "ERROR|Contact not found"
    end try
end tell
'''
    success, output = run_applescript(script)
    if output.startswith("ERROR|"):
        return None
    return output


def set_notes(name: str, content: str) -> bool:
    """Set a contact's notes. Returns True on success."""
    # Proactively ensure Contacts.app is running before write operations
    ensure_contacts_running()

    name_esc = name.replace('"', '\\"')
    content_esc = content.replace('\\', '\\\\').replace('"', '\\"')

    script = f'''
tell application "Contacts"
    try
        set thePerson to person "{name_esc}"
        set note of thePerson to "{content_esc}"
        save
        return "SAVED"
    on error
        return "ERROR|Contact not found"
    end try
end tell
'''
    success, output = run_applescript(script)
    return success and not output.startswith("ERROR|")


class ContactsCache:
    """Thin wrapper around SQLite lookups for backwards compatibility.

    No in-memory cache needed — SQLite reads are fast (~1ms).
    Kept as a class so existing code (manager.py) doesn't need restructuring.
    """

    def __init__(self, auto_load: bool = True):
        pass  # No cache to load

    def refresh(self) -> int:
        """Return count of blessed contacts. No cache to refresh."""
        contacts = list_contacts_sqlite()
        return len([c for c in contacts if c["tier"] != "unknown"])

    def lookup_phone(self, phone: str) -> Optional[Dict[str, str]]:
        return lookup_phone_sqlite(phone)

    def lookup_email(self, email: str) -> Optional[Dict[str, str]]:
        return lookup_email_sqlite(email)

    def lookup_name(self, name: str) -> Optional[Dict[str, str]]:
        """Lookup by name via SQLite."""
        contacts = list_contacts_sqlite()
        name_lower = name.lower()
        for c in contacts:
            if c["name"].lower() == name_lower:
                return c
        return None

    @property
    def count(self) -> int:
        contacts = list_contacts_sqlite()
        return len([c for c in contacts if c["tier"] != "unknown"])


def cached_lookup_phone(phone: str) -> Optional[Dict[str, str]]:
    """Look up contact by phone via SQLite."""
    return lookup_phone_sqlite(phone)


def cached_lookup_email(email: str) -> Optional[Dict[str, str]]:
    """Look up contact by email via SQLite."""
    return lookup_email_sqlite(email)
