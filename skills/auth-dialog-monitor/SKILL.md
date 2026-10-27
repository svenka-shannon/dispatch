---
name: auth-dialog-monitor
description: macOS auth dialog monitor — auto-approves safe SecurityAgent dialogs, escalates unknown ones and ALL permission/TCC prompts (screen recording, accessibility, etc.) to admin. Use when managing auth dialog auto-approve rules, investigating pending escalations, or approving/denying dialogs. Trigger words - auth dialog, permission dialog, permission prompt, TCC, screen recording prompt, approve dialog, escalation, focus stolen.
---

# Auth Dialog Monitor

Monitors macOS authentication dialogs (SecurityAgent), auto-resolves safe ones via rule engine, and escalates unknown dialogs to admin via SMS with a CLI for resolution.

It also watches **permission (TCC) prompts that SecurityAgent does not own**. Example, 2026-10-19: `"python3.14" is requesting to bypass the system private window picker and directly access your screen and audio` (buttons Allow / Open System Settings), owned by **UserNotificationCenter**. These prompts steal keyboard focus, so `cliclick t:` typing and Chrome focus break until someone answers. They are **never auto-approved**, whatever the rules say. They are always escalated to admin with the requesting app and the full prompt text.

## Architecture

```
SecurityAgent dialog appears
        ↓
AuthDialogMonitor (~/dispatch/assistant/auth_dialog.py)
  - Detects via PyObjC AX tree polling
  - Traces provenance to SDK session via process tree
  - Classifies via rule engine (config.local.yaml)
        ↓
   ┌────────────┬─────────────────┐
   │ Safe rule  │  Unknown/unsafe │
   │ → auto-    │  → escalate to  │
   │   approve  │  admin via SMS  │
   └────────────┴─────────────────┘
        ↓
Admin replies → auth-dialog-approve CLI resolves it
```

Runs as part of the main dispatch daemon (not a LaunchAgent), so code changes to `auth_dialog.py` only take effect after `claude-assistant restart`. Config changes reload on SIGHUP, but the admin escalation target (`escalation_chat_id`) is read once at daemon start. Bus events logged to `system` topic.

### Permission prompt detection
- Each poll (3s) checks `axctl apps` for the window owners in `permission_prompt_owners`. Defaults: `UserNotificationCenter` (screen recording / "bypass private window picker"), `universalAccessAuthWarn` ("would like to control this computer"), `CoreServicesUIAgent` (Gatekeeper "downloaded from the Internet").
- If one is listed, it runs `COLUMNS=4000 axctl tree <owner>` (COLUMNS stops rich from wrapping long text at 80 columns). It parses only the first `AXWindow`, not the menu bar, and reads the static texts, buttons, and the requesting app (the quoted name, e.g. `python3.14`).
- `dialog_type: permission_prompt`. `classify()` returns ESCALATE before any rule is checked. `validate()` rejects an auto_approve rule that targets `permission_prompt`.
- The same prompt is re-escalated at most every `permission_prompt_reescalate_seconds` (default 6h) while it stays on screen. SecurityAgent dialogs use 5 min.
- Pending entries also store `owner`, so `approve`/`deny`/`press` click in the right process.
- To find the owner of a new kind of prompt, list on-screen windows by owner with Quartz `CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, ...)` (or `axctl apps`). Then add the owner under `auth_dialog_monitor.permission_prompt_owners` in config.local.yaml.

## CLI: auth-dialog-approve

```bash
~/dispatch/bin/auth-dialog-approve list                  # List pending escalated dialogs
~/dispatch/bin/auth-dialog-approve info <dialog_id>      # Show dialog details
~/dispatch/bin/auth-dialog-approve approve <dialog_id>   # Click OK/Allow
~/dispatch/bin/auth-dialog-approve deny <dialog_id>      # Click Cancel
~/dispatch/bin/auth-dialog-approve always <dialog_id>    # Approve + add auto-approve rule to config (REFUSED for permission_prompt)
~/dispatch/bin/auth-dialog-approve press <dialog_id> "Open System Settings"  # Click any named button on the dialog's owner window
~/dispatch/bin/auth-dialog-approve scan                  # One-shot, read-only: list SecurityAgent + permission prompts on screen now
```

Permission prompts: `approve` clicks **Allow**. `deny` clicks Don't Allow/Deny/Cancel if the prompt has one. The screen-recording prompt has no deny button, so either leave it or `press <id> "Open System Settings"`, which dismisses it without granting and opens the Settings pane. Only approve when the admin explicitly says so, after naming the requesting app.

Pending dialogs stored in: `~/dispatch/state/auth_dialog_pending.json`
Resolution via PyObjC AXUIElement (no daemon IPC needed).

## Escalation Flow

Escalations go to `auth_dialog_monitor.resolution.escalation_chat_id` in `~/dispatch/config.local.yaml`, which is set to the owner's phone. **If it's empty, nothing reaches the admin**: you get only a log line, a bus event and the pending file. It was unset until 2026-10-19.

1. Unknown dialog or any permission prompt detected → `[SYSTEM AUTH_DIALOG_ESCALATION]` injected into the admin session with the app, the prompt text and the dialog id. The session texts the admin.
2. Admin replies (e.g. "approve" or "deny") → triggers inject-prompt into admin session
3. Admin session runs `auth-dialog-approve approve <id>` or `deny <id>`
4. Use `always` to also add a permanent auto-approve rule

## Auto-Approve Rules

Rules live in `~/dispatch/config.local.yaml` under `auth_dialog.auto_approve_rules`:

```yaml
auth_dialog:
  auto_approve_rules:
    - name: "homebrew installs"
      app_pattern: "installer"
      action_pattern: "install"
      action: approve
    - name: "my script"
      app_pattern: "python"
      action_pattern: "keychain"
      action: approve
```

All patterns are regex, case-insensitive. Fields: `app_pattern`, `action_pattern`, `dialog_type`.

Config hot-reloads via SIGHUP — no daemon restart needed.

## Bus Events

| Event | Topic | When |
|-------|-------|------|
| `auth_dialog.detected` | system | Dialog appears |
| `auth_dialog.approved` | system | Auto-approved by rule |
| `auth_dialog.escalated` | system | Sent to admin |
| `auth_dialog.approved` | system | Human approved |
| `auth_dialog.denied` | system | Human denied |
| `auth_dialog.always_approved` | system | Approved + rule added |

Search bus: `cd ~/dispatch && uv run python -m bus.cli search "auth_dialog" --limit 20`

## Dialog Types

- `password` — requires password entry (APPROVE_WITH_PASSWORD if rule matches)
- `allow_deny` — simple Allow/Deny buttons
- `ok_cancel` — OK/Cancel buttons
- `permission_prompt` — TCC/consent prompt owned by a non-SecurityAgent process (escalate only, never auto-approved)

## Notes

- Resolution is done in-process via PyObjC — passwords never leave the process
- `always` command adds rule to config.local.yaml and auto-approves existing pending dialog
- If dialog disappears before resolution, `approve`/`deny` will report "dialog not found"
