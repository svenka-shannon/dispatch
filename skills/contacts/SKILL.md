---
name: contacts
description: Look up contact information, tiers, and notes from macOS Contacts.app. Use when you need to know about a user, find their phone number, check their tier, or manage their contact data.
allowed-tools: Bash(~/.claude/skills/contacts/scripts/contacts*)
---

# Contacts Management Skill

This skill manages contacts using the native macOS Contacts.app. Tiers are implemented using Contact Groups.

## CLI Location

```bash
~/.claude/skills/contacts/scripts/contacts
```

## Architecture

**Tier System via Contact Groups (priority order):**
- `Claude Admin` - Full access users who can control the main Claude session
- `Claude Partner` - Special tier for partner with elevated privileges
- `Claude Family` - Family members with read access, mutations need admin approval
- `Claude Favorites` - Priority users with a dedicated locked-down Claude
- No group = Unknown/ignored (messages from contacts not in any group are ignored)

**Data Storage:**
- All contact data lives in macOS Contacts.app
- Tiers are determined by group membership
- Phone numbers are the primary identifier for incoming messages

## Quick Reference

| Subcommand | Accepts | Description |
|------------|---------|-------------|
| `list` | `--tier <tier>` (optional) | List all contacts, optionally filtered by tier |
| `lookup` | Phone number or email | Look up contact by phone/email |
| `add` | First, Last, Phone, `--tier` | Add a new contact |
| `tier` | "First Last", tier (optional) | Get or set contact tier |
| `notes` | "First Last", notes (optional) | Get or set contact notes |

**Search by name**: The CLI doesn't have a name search subcommand. Use:
```bash
~/.claude/skills/contacts/scripts/contacts list | grep -i "name"
```

## Commands

### List All Contacts
```bash
# All contacts with tiers
~/.claude/skills/contacts/scripts/contacts list

# Filter by tier
~/.claude/skills/contacts/scripts/contacts list --tier family
~/.claude/skills/contacts/scripts/contacts list --tier admin
```

### Lookup Contact by Phone Number or Email
```bash
~/.claude/skills/contacts/scripts/contacts lookup +16175551234
~/.claude/skills/contacts/scripts/contacts lookup someone@example.com
```
Returns: `Name | Phone | Tier`

**Note:** `lookup` only accepts phone numbers or emails, NOT names. To find by name, use `list | grep -i "name"`.

### Add New Contact
```bash
# With tier
~/.claude/skills/contacts/scripts/contacts add "First" "Last" "+16175551234" --tier family

# Without tier (will be ignored by system)
~/.claude/skills/contacts/scripts/contacts add "First" "Last" "+16175551234"
```
Valid tiers: admin, partner, family, favorite

### Get/Set Contact Tier
```bash
# Get current tier
~/.claude/skills/contacts/scripts/contacts tier "First Last"

# Set new tier
~/.claude/skills/contacts/scripts/contacts tier "First Last" family

# Remove from all tiers (contact will be ignored)
~/.claude/skills/contacts/scripts/contacts tier "First Last" none
```

### Get/Set Contact Notes
```bash
# Get notes
~/.claude/skills/contacts/scripts/contacts notes "First Last"

# Set notes
~/.claude/skills/contacts/scripts/contacts notes "First Last" "Prefers morning texts. Works at Google."
```
Notes are useful for remembering preferences, context, etc.

## Important Notes

- Phone numbers should include country code (e.g., +1 for US)
- The Messages.app chat.db uses phone numbers as identifiers
- When a message comes in, lookup the phone number to determine tier and routing

## Troubleshooting: writes fail, reads work

Symptoms: `contacts add` prints "Failed to create contact"; raw AppleScript
returns **`-1712` (errAETimeout)** or **`-609` (Connection is invalid)** — while
`contacts list` / `lookup` keep working fine.

**This is an Automation (AppleEvents) permission problem, not a hung app.**
Diagnosed 2026-08-19: `Contacts.app` was healthy (sampled — main thread idle in
a normal event loop), yet every write timed out. The TCC database showed the
denial:

```bash
sqlite3 ~/Library/Application\ Support/com.apple.TCC/TCC.db \
  "SELECT client, indirect_object_identifier, auth_value FROM access WHERE service='kTCCServiceAppleEvents';"
# python3.14 | com.apple.AddressBook | 0   <-- 0 = DENIED (2 = allowed)
```

Fix (**needs the human — an agent cannot grant this**):
System Settings → Privacy & Security → Automation → enable **Contacts** for the
python/uv binary. Then `killall Contacts AddressBookSourceSync` and retry.

Notes:
- `-609` typically follows a `pkill -9 Contacts` — a stale AppleEvent connection.
  Quit cleanly (`osascript -e 'tell application "Contacts" to quit'`) instead.
- Do NOT interpret the hang as "Contacts.app is wedged" and start force-killing.
  Sample it first: `/usr/bin/sample <pid> 2 -file /tmp/c.txt`.
- A *pending* (invisible) Automation prompt causes the same timeout on reads too.

**Reads never need AppleEvents** — query SQLite directly:

```bash
sqlite3 "$HOME/Library/Application Support/AddressBook/Sources/"*/AddressBook-v22.abcddb \
  "SELECT r.ZFIRSTNAME, r.ZLASTNAME, p.ZFULLNUMBER FROM ZABCDPHONENUMBER p
   JOIN ZABCDRECORD r ON r.Z_PK=p.ZOWNER;"
# emails: ZABCDEMAILADDRESS e ... e.ZADDRESS, JOIN on r.Z_PK=e.ZOWNER
```

Query **every** source dir, newest mtime first — iCloud can keep duplicate cards
across containers, and a stale one will answer first if you let it.

## Ask the code, not the filesystem

When checking what this module reads, **call `_get_all_addressbook_dbs()`** — do
not `ls` the `Sources/` directory or glob for `*.abcddb`.

```python
from contacts_core import _get_all_addressbook_dbs
print(list(_get_all_addressbook_dbs()))   # the truth
```

It returns the per-source DBs **plus the root** `AddressBook-v22.abcddb`, so
`ls Sources/ | wc -l` undercounts. On 2026-08-19 that proxy produced a wrong
public claim ("only one source DB, so the bug was latent") when the real count
was 2. A second agent independently made the same class of error by globbing
instead of calling the function, and got the right answer only by luck.

Note `Z_PK` is unique only **within one** database. Any structure keyed by pk
across DBs will merge unrelated people. Scope per-DB, or derive state from a
per-DB dict so the bad state cannot be represented at all.

Useful follow-up when auditing: *structural* exposure and *empirical* firing are
different questions. A machine can read N databases yet never collide because
only one holds tier rows:

```bash
sqlite3 "$DB" "SELECT COUNT(DISTINCT pg.Z_22CONTACTS) FROM Z_22PARENTGROUPS pg
  JOIN ZABCDRECORD g ON g.Z_PK=pg.Z_19PARENTGROUPS1 WHERE g.ZNAME LIKE 'Claude%';"
```

## Known gap: `contacts add` takes a phone only

`add` has no `--email` option, so an email-only handle (common for Signal/iMessage
via Apple ID) cannot be recorded with the CLI as-is. Writes are also gated on the
Automation permission above.

## Technical Details

**Hybrid read/write path:**
- **Reads**: SQLite queries against AddressBook-v22.abcddb (fast, no Contacts.app dependency)
- **Writes**: AppleScript via Contacts.app (required by macOS for mutations)

**Multi-source database architecture:**
macOS stores contacts across multiple SQLite databases when iCloud is enabled:
- `~/Library/Application Support/AddressBook/AddressBook-v22.abcddb` (root, may be stale)
- `~/Library/Application Support/AddressBook/Sources/<UUID>/AddressBook-v22.abcddb` (per-source)

The same contact can exist in multiple source DBs with different data. AppleScript writes go to
the active iCloud source (most recently modified). SQLite reads query all sources, sorted by
modification time (newest first), so "first match wins" returns the most recent version.

If notes appear to not persist, it may be an iCloud sync issue - check all source DBs manually.
