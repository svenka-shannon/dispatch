---
name: discord
description: Send and receive Discord messages via bot. Use for Discord channel communication. Trigger words - discord, send discord.
---

# Discord Skill

Send and receive messages in Discord channels via a bot integration.

## Usage

### Send a message
```bash
~/.claude/skills/discord/scripts/send-discord <channel_id> "message"
```

### Reply from a Discord transcript directory
```bash
# From ~/transcripts/discord/{channel_id}/
~/.claude/skills/sms-assistant/scripts/reply "message"
```

### Read recent messages (for context)
```bash
~/.claude/skills/discord/scripts/read-discord <channel_id>              # Last 25 messages
~/.claude/skills/discord/scripts/read-discord <channel_id> --limit 50   # Last 50 messages
~/.claude/skills/discord/scripts/read-discord <channel_id> --before <message_id>  # Before a message
~/.claude/skills/discord/scripts/read-discord <channel_id> --after <message_id>   # After a message
~/.claude/skills/discord/scripts/read-discord <channel_id> --json       # Raw JSON output
```

Use this to get context when you are tagged — read the last N messages to understand what the conversation is about before responding.

### React to a message with an emoji
```bash
~/.claude/skills/discord/scripts/discord-react <channel_id> <message_id> "🦀"
```

To get message IDs, fetch recent messages via the Discord API:
```python
httpx.get(f"https://discord.com/api/v10/channels/{channel_id}/messages?limit=10",
          headers={"Authorization": f"Bot {token}"})
```

Use reactions to add personality — react to funny messages, acknowledge things without a full reply, etc.

## How It Works

- Bot connects to Discord Gateway via discord.py (runs in a daemon thread)
- Incoming messages are normalized and routed through the bus pipeline (same as iMessage/Signal)
- Outgoing messages use the Discord REST API (httpx POST, no Gateway needed)
- Each channel gets its own SDK session at `~/transcripts/discord/{channel_id}/`
- Messages are auto-chunked at 2000 characters (Discord's limit)

## Configuration

Discord config lives in `~/dispatch/config.local.yaml`:

```yaml
discord:
  channel_ids:
    - "1234567890"
  bot_role_ids:           # Role IDs that trigger the bot when @mentioned
    - "BOT_ROLE_ID"
  bot_names:              # Name strings that trigger the bot (case-insensitive)
    - "sven"
  users:
    "DISCORD_USER_ID":
      name: "Name"
      tier: "admin"
```

### Mention Filtering

The listener only queues messages that are directed at the bot:
1. **@mention** of the bot user
2. **Role mention** matching `bot_role_ids`
3. **Bot name** in message text (case-insensitive, from `bot_names`)
4. **Reply** to a bot message

All other messages are silently dropped at the listener level (zero token cost).

Bot token stored in macOS Keychain as `discord_bot_token`.

## Token

Stored in keychain:
```bash
# Store
security add-generic-password -s discord_bot_token -a sven -w "TOKEN"

# Read
security find-generic-password -s discord_bot_token -w
```

## Response Behavior

**Default: respond only when tagged** (`@bot`). Do not reply to every message in a channel.

**Exception — active turn-based games:** If context from recent messages makes it clearly your turn (e.g., a game prompt directed at you, the previous player completed their turn), respond without requiring a tag. Read game state from the last several messages to determine if it is your turn before acting.

## Bot Commands

Discord sessions recognize dot-commands (e.g., `.imdb`, `.help`) as special bot commands.

### `.imdb <title>`

Looks up movie or TV show information using the imdb-lookup skill CLI.

```bash
~/.claude/skills/imdb-lookup/scripts/imdb-lookup "<title>" --format discord
```

- Uses `--format discord` for markdown-formatted output suitable for Discord channels
- Card output includes: title, year, rating, runtime, genres, and IMDb link
- Powered by the TMDB API via the imdb-lookup CLI (no web scraping)

## Project server mode (one channel = one project session)

```yaml
discord:
  guild_ids: ["<guild id>"]              # accept EVERY channel in this server (no per-channel config/restart)
  respond_all_guild_ids: ["<guild id>"]  # no @mention needed in this server
  users:
    "<discord user id>": {name: "...", tier: "admin"}
```
New channels in a `guild_ids` server work immediately. Each one gets its own session at `~/transcripts/discord/{channel_id}/`. The bot role has Administrator, so you can create channels via REST: `POST /guilds/{id}/channels {"name":..., "type":0}`.

**Finding a user's Discord ID:** have them post once. The listener logs `[discord] Unmapped user: <name> (<id>)`. Add that ID to `discord.users`. Config is read at startup, so a detached daemon restart is needed after editing it.

**Setup gotchas:**
- The developer portal's "Create app" and the bot OAuth "Authorize" step both show an hCaptcha, so a human has to tick it.
- "Reset Token" asks for the account password (Keychain `discord-account`, typed with `chrome os-type --secret-service`).
- Store the token without echoing it: `printf 'add-generic-password -U -a sven -s discord_bot_token -w %s\n' "$TOK" | security -i`.
- Turn on the Message Content intent and save it **on its own**. Unticking "Public Bot" in the same save fails validation ("Private application cannot have a default authorization link"), and the whole save is dropped.
- The OAuth authorize flow can silently drop Manage Channels from the requested permissions. Check `GET /guilds/{id}/roles` afterwards.
- `GID` is a read-only zsh variable, so don't use it as a shell variable name.
