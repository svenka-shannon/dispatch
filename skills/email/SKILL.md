---
name: email
description: Read Gmail over IMAP from the command line — list, read, and search messages with no browser and no OAuth. Use for anything involving email, gmail, inbox, unread mail, reading a message, searching mail, checking for a receipt/invoice/confirmation email. Trigger words - email, gmail, inbox, mail, unread, message, read email, check email, search email.
---

# email — Gmail over IMAP

`~/.claude/skills/email/scripts/email` reads Gmail directly over IMAP. Python
stdlib only (`imaplib` + `email`), no third-party packages, no browser, no
OAuth.

**Use this instead of browser automation for Gmail.** Reading Gmail through
Chrome is fragile: Gmail enforces Trusted Types (JS injection is blocked), the
DOM churns, and the whole path dies whenever the chrome-control extension is
unhealthy. IMAP keeps working when Chrome does not.

> The `gws` CLI referenced by the `google-suite` skill is **not installed** on
> this machine and no Google OAuth credentials exist. For Gmail, this is the
> working tool.

## Setup (one time)

The credential is a **Gmail App Password** in the macOS Keychain — never in a
file, never in the repo.

1. Generate one at <https://myaccount.google.com/apppasswords> while signed in
   as `svenka.shannon@gmail.com` (requires 2-Step Verification).
2. Store it:

```bash
security add-generic-password -a svenka -s gmail-app-password -w 'xxxxxxxxxxxxxxxx'
# add -U to overwrite an existing entry
```

Verify: `security find-generic-password -a svenka -s gmail-app-password -w`

The CLI prints this exact command if the credential is missing, so a first-run
failure is self-explaining. An **account password will not work** — Google
rejects them for IMAP.

## Usage

```bash
email list [--limit N] [--from ADDR] [--subject TEXT] [--since DAYS] [--unread]
email read <uid> [--mark-read] [--headers-all] [--raw]
email search <query> [--limit N] [--gmail | --imap]
email mailboxes

# global flags: --mailbox NAME (default INBOX), --json, --account ADDR
```

### Examples

```bash
email list --limit 20                       # 20 newest in the inbox
email list --unread --limit 50              # unread only
email list --from stripe.com --since 7      # last week, from Stripe
email list --subject "order confirmation"
email read 184213                           # full body, never truncated
email read 184213 --json                    # structured, for parsing
email search 'from:amazon has:attachment' --gmail
email search 'SUBJECT "invoice" SINCE 01-Aug-2026' --imap
email list --mailbox "[Gmail]/Sent Mail"
```

## Behaviour worth knowing

- **IDs are IMAP UIDs**, stable per mailbox. `list`/`search` print them in
  `[184213]`; pass the same `--mailbox` to `read` that you used to list.
- **Nothing is truncated.** Bodies print in full; `--limit` caps the number of
  *records* only.
- **Read-only by default.** Mailboxes are opened `readonly` and fetched with
  `BODY.PEEK`, so listing and reading do **not** mark mail as read. Use
  `read --mark-read` when you deliberately want the `\Seen` flag set.
- **Body preference:** `text/plain` if present, otherwise the `text/html` part
  run through a stdlib HTML stripper (scripts/styles dropped, link hrefs kept
  inline as `<url>`). The chosen source is printed in the `--- body (...) ---`
  header.
- **Attachments** are listed (name, MIME type, byte size) but not downloaded.
- **Search syntax auto-detects:** a query starting with an IMAP key (`FROM`,
  `SUBJECT`, `SINCE`, `UNSEEN`, …) is sent as raw IMAP `SEARCH`; anything else
  is sent as Gmail search syntax via `X-GM-RAW`. Force either with `--imap` /
  `--gmail`.
- `--json` on any command gives an agent-friendly object.

## Overrides (env)

| Variable | Default |
|---|---|
| `DISPATCH_EMAIL_ADDRESS` | `svenka.shannon@gmail.com` |
| `DISPATCH_EMAIL_KEYCHAIN_SERVICE` | `gmail-app-password` |
| `DISPATCH_EMAIL_KEYCHAIN_ACCOUNT` | `svenka` |
| `DISPATCH_IMAP_HOST` / `DISPATCH_IMAP_PORT` | `imap.gmail.com` / `993` |

Useful for a second account: store its app password under a different Keychain
account and pass `--account other@gmail.com` with
`DISPATCH_EMAIL_KEYCHAIN_ACCOUNT=other`.

## Troubleshooting

| Symptom | Meaning / fix |
|---|---|
| `No Gmail app password in the Keychain` | Not stored yet — run the `security add-generic-password` line above. |
| `IMAP login failed … [AUTHENTICATIONFAILED]` | Wrong or revoked App Password, or an account password was stored. Re-generate and re-store with `-U`. |
| `Could not reach imap.gmail.com:993` | Network/DNS problem, not a credential problem. |
| `No message with UID N in this mailbox` | UIDs are per-mailbox; pass the `--mailbox` you listed from. |
| Keychain prompt / timeout | The login keychain is locked; unlock it and retry. |

## Sending mail

Not supported — this tool is read-only by design. If sending is ever needed,
add an `smtp.gmail.com:587` path using the same Keychain credential rather than
introducing a browser dependency.
