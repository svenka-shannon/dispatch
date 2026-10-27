# Dispatch

![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue) ![macOS 14+](https://img.shields.io/badge/macOS-14%2B-lightgrey) ![License: MIT](https://img.shields.io/badge/license-MIT-green) ![Tests: 920+](https://img.shields.io/badge/tests-920%2B-brightgreen)

Turn a Mac into an always-on AI assistant with its own identity. Not acting as you — a separate entity in your household.

> **Status:** Actively developed, daily-driven since 2025. Not a toy project — this runs 24/7 managing real conversations, smart home devices, and background tasks.

**[Documentation](https://svenflow.github.io/dispatch/)** · **[Architecture](#architecture)** · **[Quick Start](#installation)**

## Overview

Dispatch runs a daemon that:
- **Receives messages** from iMessage and Signal in real-time
- **Routes them to Claude SDK sessions** based on contact tier (admin, partner, family, favorites, bots)
- **Gives Claude full computer control**: browser automation, file management, smart home, messaging
- **Maintains persistent memory** across conversations with full-text search
- **Auto-recovers from crashes** via multi-tier health monitoring and watchdog daemon
- **Records all events** to a Kafka-on-SQLite bus with 9 topics, 8+ consumer groups, FTS5 search, and tiered retention

Each contact gets their own persistent Claude session with conversation history, memories, and tier-appropriate tool access.

## What It Looks Like

```
┌─────────────────────────────────────────────┐
│ iMessage                                     │
│                                              │
│ You: can you turn on the living room lights  │
│      and set them to 50%?                    │
│                                              │
│ Assistant: done — living room lights on at   │
│ 50% 💡                                       │
│                                              │
│ You: what's the weather like in SF tomorrow? │
│                                              │
│ Assistant: SF tomorrow: 62°F, partly cloudy, │
│ 10% chance of rain. good day to be outside   │
│                                              │
│ You: remind me to call mom at 3pm            │
│                                              │
│ Assistant: reminder set for 3:00 PM today 👍 │
└─────────────────────────────────────────────┘
```

### How it works

You sign a Mac into a **separate Apple ID** so it gets its own iMessage phone number. Dispatch runs a daemon that polls `chat.db` for incoming messages, looks up the sender's contact tier, and routes the message to a persistent Claude SDK session. Claude processes the message with full computer access and responds by calling `send-sms` — the reply shows up in the sender's Messages app like a text from any other person.

The same flow works for Signal via `signal-cli`. Each contact gets their own isolated session with conversation history, memories, and tier-appropriate tool access. Behind the scenes, 67+ skill modules give Claude capabilities like browser automation, smart home control, iOS development, and more.

### Why Dispatch?

Unlike chatbot UIs or voice assistants, Dispatch gives the AI **agency on a full computer**. It can browse the web, write and run code, control smart home devices, manage files, and send messages across platforms — all triggered by a simple text. It's closer to having a live-in assistant than talking to an app.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Messages.app (iMessage)     Signal (via signal-cli)     │
└────────────────┬─────────────────────────┬──────────────┘
                 └─────────────┬───────────┘
                               ▼
                   ┌────────────────────────┐
                   │    Manager Daemon      │
                   └───────────┬────────────┘
                               │
                ┌──────────────┼──────────────┐
                ▼              ▼              ▼
     ┌──────────────┐  ┌────────────────────────────┐
     │  SDK Backend  │  │  Event Bus (SQLite)        │
     │              │  │  9 topics · 8+ consumers   │
     └──────┬───────┘  │  FTS5 · tiered retention   │
            ▼          └─────────────┬──────────────┘
 ┌────────────────┐          ┌───────┼────────┐
 │ Per-Contact    │          ▼       ▼        ▼
 │ Claude Sessions│    ┌────────┐ ┌──────┐ ┌──────┐
 │ (Opus)         │    │router  │ │audit │ │tasks │
 └──────┬─────────┘    └────────┘ └──────┘ └──────┘
        ▼
 ┌────────────────┐
 │ 67+ Skills     │
 │ Browser, Home, │
 │ Memory, Msg    │
 └────────────────┘
```

## Features

### Messaging
- **iMessage**: Polls `chat.db` for new messages, supports individual + group chats
- **Signal**: Connects via `signal-cli` daemon with JSON-RPC socket
- **Image handling**: Gemini Vision analyzes images with conversation context
- **Mid-turn steering**: New messages reach Claude between tool calls

### Contact Tiers
| Tier | Access |
|------|--------|
| **Admin** | Full computer control, browser automation, all tools |
| **Partner** | Full access with personalized warm tone |
| **Family** | Read-only, mutations need admin approval |
| **Favorite** | Own session, restricted tools |
| **Bots** | Read-only with loop detection |
| **Unknown** | Ignored (no session created) |

Tiers are managed via macOS Contacts.app groups.

### Session Management
- Per-contact SDK sessions with automatic resumption
- Transcript storage at `~/transcripts/{backend}/{chat_id}/`
- Two-tier health monitoring: fast regex (60s) + deep LLM analysis (5min)
- Idle session reaping after 2 hours
- Session registry persists across daemon restarts

### Skills System
Skills are modular capabilities in `~/.claude/skills/`. Each has:
- `SKILL.md` with YAML frontmatter (name, description, triggers)
- Optional `scripts/` directory with CLI executables
- Template placeholders: `{{CONTACT_NAME}}`, `{{TIER}}`, `{{CHAT_ID}}`

**67+ built-in skills** including browser automation, smart home control (Hue, Lutron, Sonos), persistent memory, iOS development, vision analysis, natural language reminders, and more.

### Event Bus (Kafka-on-SQLite)

Every event flows through a SQLite-based message bus modeled after Kafka: partitioned topics, consumer groups with at-least-once delivery, FTS5 search, and tiered retention (7-day hot → infinite archive).

```bash
uv run python -m bus.cli stats          # Event counts, throughput, consumer lag
uv run python -m bus.cli tail           # Live tail of events
uv run python -m bus.cli search "query" # Full-text search across all records
```

See the [Event Bus docs](https://svenflow.github.io/dispatch/operations/message-bus/) for schema, consumer framework, and query examples.

### Reliability
- **Watchdog daemon**: Auto-recovers from crashes
- **Exponential backoff**: 60s → 120s → 240s → 480s → 900s
- **Health checks**: Regex patterns + Haiku LLM analysis
- **SMS alerts**: Admin notified of recovery attempts
- **Max 5 failures** before manual intervention required

## Requirements

- macOS 14+ Sonoma or later (uses Messages.app, Contacts.app)
- Python 3.12+ with [`uv`](https://github.com/astral-sh/uv) package manager
- Claude API access (Anthropic)
- Optional: `signal-cli` daemon for Signal messaging
- Optional: Chrome with Chrome Control extension for browser automation

## Installation

### Prerequisites

1. **A dedicated Mac** signed into a **separate Apple ID** — this gives the assistant its own iMessage phone number (use an eSIM, prepaid SIM, or Google Voice number to create one)
2. **Full Disk Access** for Terminal/iTerm — System Settings → Privacy & Security (required for reading `chat.db`)
3. **Anthropic API key** — Set `ANTHROPIC_API_KEY` in your environment
4. **macOS Contacts.app groups** — Create groups named `Admin`, `Partner`, `Family`, `Favorite`, `Bots` and add contacts to set their tier
5. **Optional: signal-cli** — For Signal support ([setup guide](https://github.com/AsamK/signal-cli#installation))
6. **Optional: Google AI key** — For Gemini Vision image analysis (`GOOGLE_AI_API_KEY`)

### Setup

```bash
# Clone the repo
git clone https://github.com/svenflow/dispatch.git ~/dispatch
cd ~/dispatch

# Install dependencies
uv sync

# Copy and configure
cp config.example.yaml config.local.yaml
# Edit config.local.yaml — at minimum set assistant.name and contacts.owner_phone

# Apply required host defaults, then relaunch Messages.app so they take effect.
# Without this, macOS App Nap defers Messages' inbound chat.db writes and
# messages can arrive minutes late (see POSTMORTEMS.md, 2026-09-07).
./bin/setup-macos-defaults --restart-messages

# Start the daemon
./bin/claude-assistant start

# Install watchdog for auto-recovery (recommended)
./bin/watchdog-install
```

Verify it's running:

```bash
./bin/claude-assistant status    # Should show "running" with 0 active sessions
./bin/setup-macos-defaults --check   # Exits non-zero if the App Nap defaults are unset
```

(The daemon re-checks and re-applies those defaults itself — `imessage_app_nap` in the dependency-health runner — since a macOS update can reset them.)

Store the Mac login password in Keychain so the daemon can unlock the lock screen by itself (display sleep → "require password" otherwise blinds every GUI-driven skill, and the `screen_lock` health check would only be able to text you about it):

```bash
security add-generic-password -a "$USER" -s mac-login-password -w -U   # prompts for the password
./bin/screen-lock status                                              # locked? settings? password stored?
```

Set `screen_lock.harden: true` in `config.local.yaml` if you also want the daemon to turn off "require password after sleep / screen saver" so the lock never re-arms (a security trade-off, off by default).

Send a text to your Mac's phone number from a contact in your Admin group — the assistant should respond within a few seconds.

> **Cost note:** Dispatch uses Claude Opus for all sessions. Expect ~$5-15/day with moderate usage depending on conversation volume and tool use.

## Configuration

See `config.example.yaml` for all options. Key settings:

```yaml
assistant:
  name: "Jarvis"                  # Your assistant's name

contacts:
  owner_phone: "+1234567890"      # Your phone number (gets admin tier)

messaging:
  enabled_backends:
    - imessage                    # Requires Full Disk Access for chat.db
    - signal                      # Requires signal-cli daemon

# Optional integrations
hue:
  bridges:
    home: "192.168.1.x"          # Philips Hue bridge IP
```

## CLI Usage

```bash
# Daemon management
./bin/claude-assistant start           # Start daemon
./bin/claude-assistant stop            # Stop daemon
./bin/claude-assistant restart         # Restart (via launchctl)
./bin/claude-assistant status          # Show status and sessions
./bin/claude-assistant logs            # Tail daemon logs

# Session management
./bin/claude-assistant kill-session <name>      # Kill specific session
./bin/claude-assistant restart-session <name>   # Restart specific session
./bin/claude-assistant restart-sessions         # Restart all sessions

# Watchdog
./bin/watchdog-install     # Install auto-recovery
./bin/watchdog-uninstall   # Remove watchdog
./bin/watchdog-status      # Check watchdog status
```

## Project Structure

```
~/dispatch/
├── assistant/           # Core daemon code (manager, sessions, health, config)
├── bus/                 # Kafka-on-SQLite event bus (producer, consumers, FTS5)
├── bin/                 # CLI scripts (claude-assistant, watchdog)
├── services/            # Supporting services (dispatch-api for iOS app)
├── tests/               # Test suite (920+ tests)
├── state/               # Runtime state (sessions.json, bus.db, daemon.pid)
├── logs/                # Daemon + per-session logs
└── config.local.yaml    # Local configuration
```

See the [full architecture documentation](https://svenflow.github.io/dispatch/) for directory details, design decisions, metrics, and operational guides.

## Design Philosophy

- **No auto-send**: Claude explicitly calls `send-sms`/`send-signal` CLIs — full control over when and what to message
- **In-process sessions**: All sessions in one async event loop, no tmux or subprocesses
- **Mid-turn steering**: New messages reach Claude between tool calls via async queue
- **Skills as modules**: Shared, version-controlled, injected via symlink at session startup

Full documentation at **[svenflow.github.io/dispatch](https://svenflow.github.io/dispatch/)**.

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `chat.db` permission denied | Grant Full Disk Access to Terminal in System Settings → Privacy & Security |
| Daemon won't start | Check `ANTHROPIC_API_KEY` is set; run `./bin/claude-assistant logs` for errors |
| No response to messages | Ensure the sender is in a Contacts.app tier group (Admin, Family, etc.) — unknown contacts are ignored |
| Signal not working | Verify `signal-cli` daemon is running: `signal-cli daemon --receive-mode on-connection` |
| Session seems stuck | `./bin/claude-assistant restart-session <name>` to restart a specific session |

## Contributing

Contributions are welcome! The project uses:
- **`uv`** for Python package management
- **`ty`** for type checking (must pass with zero errors on `assistant/`)
- **`ruff`** for linting
- **`pytest`** for testing (920+ tests)

```bash
# Run the full test suite before submitting a PR
uv run pytest tests/ -v

# Quick feedback on a specific area
uv run pytest tests/test_bus.py -v

# Type check + lint
uv run ty check assistant/
uv run ruff check assistant/
```

See the [documentation site](https://svenflow.github.io/dispatch/) for architecture details and design decisions.

## License

MIT
