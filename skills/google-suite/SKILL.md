---
name: google-suite
description: API reference for the gws CLI (Google Workspace - Calendar, Drive, Docs, Sheets, Tasks, Contacts, Chat). NOTE - gws is NOT installed on this machine and there are no Google credentials, so nothing here runs until someone installs and authenticates it; for Gmail use the `email` skill (IMAP) instead. Trigger words - google workspace, gws, calendar, drive, docs, sheets, tasks, meeting, event.
---

# Google Suite Skill

Reference for the `gws` CLI (googleworkspace/cli), the official Google Workspace CLI.

## ⚠️ STATUS: NOT INSTALLED ON THIS MACHINE

**`gws` is not installed and no Google OAuth credentials exist here.** Everything below is
API *reference* for after someone installs and authenticates it — none of it runs today.
This file previously claimed the binary lived at `~/.local/bin/gws` and was "already
authenticated via browser OAuth"; both claims were false and cost a session real time
(2026-08-16). Verify before relying on it:

```bash
command -v gws || echo "gws NOT installed"
```

### For Gmail, use the `email` skill instead

`~/.claude/skills/email/scripts/email` reads Gmail over IMAP with Python stdlib only —
no OAuth, no browser, no `gws`. It works today (credential: a Gmail App Password in the
macOS Keychain). See `/email`.

```bash
email list --limit 20 --unread
email read <uid>
email search 'from:stripe has:attachment' --gmail
```

Calendar / Drive / Docs / Sheets / Tasks have **no working CLI on this machine** right now.

### Installing gws (if you actually need it)

```bash
brew install googleworkspace-cli   # installs the `gws` binary
gws auth login                     # browser-based OAuth — needs a human at the browser
gws auth status                    # confirm
```

Installing gives you the binary, not access: `gws auth login` opens an OAuth consent flow
that a human must complete, and a Google Cloud project with the relevant APIs enabled may
be required. Do not report Google Workspace access as available until `gws auth status`
succeeds.

## CLI Syntax

```bash
gws <service> <resource> <method> [flags]
```

### Global Flags

| Flag | Description |
|------|-------------|
| `--format <FORMAT>` | Output format: `json` (default), `table`, `yaml`, `csv` |
| `--dry-run` | Validate locally without calling the API |
| `--params '{"key": "val"}'` | URL/query parameters |
| `--json '{"key": "val"}'` | Request body |
| `-o, --output <PATH>` | Save binary responses to file |
| `--upload <PATH>` | Upload file content (multipart) |
| `--page-all` | Auto-paginate (NDJSON output) |

## Sub-Skills

This skill includes 107 sub-skills in the `skills/` folder. **Use the appropriate sub-skill when working with a specific feature.**

### Core Services

| Sub-skill | Description | Use when |
|-----------|-------------|----------|
| `skills/gws-gmail/` | Gmail API reference | Reading/managing emails |
| `skills/gws-gmail-send/` | Send email helper | Sending emails |
| `skills/gws-gmail-triage/` | Inbox summary | Checking unread emails |
| `skills/gws-calendar/` | Calendar API reference | Managing calendars/events |
| `skills/gws-calendar-agenda/` | Show upcoming events | Checking schedule |
| `skills/gws-calendar-insert/` | Create events | Scheduling meetings |
| `skills/gws-drive/` | Drive API reference | File management |
| `skills/gws-drive-upload/` | Upload files | Uploading to Drive |
| `skills/gws-docs/` | Docs API reference | Document operations |
| `skills/gws-docs-write/` | Write to docs | Creating/editing docs |
| `skills/gws-sheets/` | Sheets API reference | Spreadsheet operations |
| `skills/gws-sheets-read/` | Read sheet data | Reading spreadsheets |
| `skills/gws-sheets-append/` | Append rows | Adding data to sheets |
| `skills/gws-tasks/` | Tasks API reference | Task management |
| `skills/gws-people/` | People/Contacts API | Contact management |
| `skills/gws-chat/` | Chat API reference | Workspace Chat |
| `skills/gws-chat-send/` | Send chat messages | Messaging in Chat |
| `skills/gws-meet/` | Meet API reference | Meeting management |
| `skills/gws-keep/` | Keep API reference | Notes management |
| `skills/gws-forms/` | Forms API reference | Form operations |
| `skills/gws-slides/` | Slides API reference | Presentation operations |

### Admin & Enterprise

| Sub-skill | Description |
|-----------|-------------|
| `skills/gws-admin/` | Admin SDK operations |
| `skills/gws-admin-reports/` | Admin reports |
| `skills/gws-alertcenter/` | Security alerts |
| `skills/gws-cloudidentity/` | Cloud Identity |
| `skills/gws-groupssettings/` | Groups settings |
| `skills/gws-licensing/` | License management |
| `skills/gws-reseller/` | Reseller operations |
| `skills/gws-vault/` | Vault/eDiscovery |
| `skills/gws-modelarmor/` | Content safety screening |

### Automation & Scripts

| Sub-skill | Description |
|-----------|-------------|
| `skills/gws-apps-script/` | Apps Script API |
| `skills/gws-apps-script-push/` | Deploy scripts |
| `skills/gws-events/` | Event subscriptions |
| `skills/gws-classroom/` | Google Classroom |

### Workflow Recipes (Automation Patterns)

| Sub-skill | Description |
|-----------|-------------|
| `skills/gws-workflow-email-to-task/` | Convert emails to tasks |
| `skills/gws-workflow-file-announce/` | Announce file uploads |
| `skills/gws-workflow-meeting-prep/` | Prepare for meetings |
| `skills/gws-workflow-standup-report/` | Generate standup reports |
| `skills/gws-workflow-weekly-digest/` | Weekly email digest |

### Recipes (Common Tasks)

The `recipe-*` sub-skills provide step-by-step guides for common operations:

| Recipe | Description |
|--------|-------------|
| `recipe-send-personalized-emails` | Bulk personalized emails |
| `recipe-create-events-from-sheet` | Events from spreadsheet data |
| `recipe-save-email-attachments` | Download attachments |
| `recipe-batch-rename-files` | Bulk file renaming |
| `recipe-find-free-time` | Find available slots |
| `recipe-create-doc-from-template` | Doc from template |
| `recipe-sync-contacts-to-sheet` | Export contacts |
| `recipe-share-folder-with-team` | Bulk sharing |
| ... and 40+ more recipes |

### Persona Guides (Role-based)

| Persona | Description |
|---------|-------------|
| `persona-exec-assistant` | Executive assistant workflows |
| `persona-project-manager` | PM workflows |
| `persona-it-admin` | IT administration |
| `persona-researcher` | Research workflows |
| `persona-sales-ops` | Sales operations |
| ... and more |

## Quick Examples

**Reminder: none of these run on this machine yet — `gws` is not installed (see the status
section at the top). For Gmail, use `email list` / `email read` / `email search` instead.**

### Gmail

```bash
# Search emails
gws gmail users messages list --params '{"q": "from:boss is:unread", "maxResults": 10}'

# Get message (need to specify fields)
gws gmail users messages get --params '{"userId": "me", "id": "MESSAGE_ID"}'

# Send email (use the helper skill for easier syntax)
# See: skills/gws-gmail-send/SKILL.md
```

### Calendar

```bash
# List upcoming events
gws calendar events list --params '{"calendarId": "primary", "timeMin": "2026-03-05T00:00:00Z", "maxResults": 10}'

# Create event (use the helper skill for easier syntax)
# See: skills/gws-calendar-insert/SKILL.md
```

### Drive

```bash
# List files
gws drive files list --params '{"pageSize": 10}'

# Search files
gws drive files list --params '{"q": "name contains '\''report'\'' and mimeType='\''application/pdf'\''", "pageSize": 10}'

# Download file
gws drive files get --params '{"fileId": "FILE_ID", "alt": "media"}' -o output.pdf
```

### Sheets

```bash
# Read values
gws sheets spreadsheets values get --params '{"spreadsheetId": "SHEET_ID", "range": "Sheet1!A1:D10"}'

# Append row
gws sheets spreadsheets values append --params '{"spreadsheetId": "SHEET_ID", "range": "Sheet1", "valueInputOption": "USER_ENTERED"}' --json '{"values": [["Col1", "Col2", "Col3"]]}'
```

## Discovering Commands

```bash
# List available services
gws --help

# List resources for a service
gws gmail --help

# Get method schema (params, types, defaults)
gws schema gmail.users.messages.list
```

## Shared Reference

For auth details, global flags, and security rules, see: `skills/gws-shared/SKILL.md`

## Legacy: gog CLI

The old `gog` CLI (brew install steipete/tap/gogcli) is still installed but deprecated. Use `gws` for all new operations.
