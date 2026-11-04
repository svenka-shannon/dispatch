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

**Backticks, `$`, `!` or code in the message → use stdin with a quoted heredoc**, never a double-quoted arg. zsh runs backticks as commands and silently drops the text (2026-11-03, `/lfg`):
```bash
~/.claude/skills/discord/scripts/send-discord <channel_id> - <<'EOF'
Use `/lfg` to find a match. Costs $0!
EOF
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
New channels in a `guild_ids` server work immediately. Each one gets its own session at `~/transcripts/discord/{channel_id}/`. The bot role has Administrator. Create channels with `discord-admin create-channel --guild project NAME` (see "Guild and channel admin" below), not raw REST.

**Finding a user's Discord ID:** have them post once. The listener logs `[discord] Unmapped user: <name> (<id>)`. Add that ID to `discord.users`. Config is read at startup, so a detached daemon restart is needed after editing it.

**Setup gotchas:**
- The developer portal's "Create app" and the bot OAuth "Authorize" step both show an hCaptcha, so a human has to tick it.
- "Reset Token" asks for the account password (Keychain `discord-account`, typed with `chrome os-type --secret-service`).
- Store the token without echoing it: `printf 'add-generic-password -U -a sven -s discord_bot_token -w %s\n' "$TOK" | security -i`.
- Turn on the Message Content intent and save it **on its own**. Unticking "Public Bot" in the same save fails validation ("Private application cannot have a default authorization link"), and the whole save is dropped.
- The OAuth authorize flow can silently drop Manage Channels from the requested permissions. Check `GET /guilds/{id}/roles` afterwards.
- `GID` is a read-only zsh variable, so don't use it as a shell variable name.

## Guild and channel admin (use this, never raw curl)

All guild and channel admin work goes through `discord-admin`. It enforces the rules in the next section, so you can't skip them by accident.

```bash
A=~/.claude/skills/discord/scripts/discord-admin
$A guilds                                          # guilds the bot is in (id, name)
$A channels --guild project                        # category -> channel tree with ids and types
$A perms --guild project [--channel CHANNEL_ID]    # bot roles, decoded perms, channel overwrites
$A create-category --guild project "Name"
$A create-channel --guild project name [--category CAT_ID] [--type text|voice]
$A delete ID [ID ...] --guild project [--recursive] [--dry-run] [--reason "why"]
```

- **`--guild` is mandatory** on every guild command. If it's missing, the CLI exits 2. Pass a numeric guild ID or `project`, which resolves via `identity discord.guild_ids`. The CLI first confirms that the bot is a member of that guild and refuses if it isn't.
- **Delete by ID only.** Get the IDs from `channels --guild ...` first. Names are rejected. Before anything is deleted, every ID is fetched, and if *any* of them has a `guild_id` other than `--guild`, the whole run is refused and nothing is deleted.
- A category with children is refused unless you pass `--recursive`. With `--recursive`, the CLI deletes the children first and then the category. Use `--dry-run` to see the plan without deleting anything.
- The result comes from **re-listing the guild**, not from HTTP codes. It prints GONE / STILL PRESENT and the new channel tree. Report that to the user, not your own count.
- Each mutation sends an `X-Audit-Log-Reason` header (`--reason`). The CLI honors 429 Retry-After.
- Exit codes: 0 ok, 1 API/env error, 2 usage error, 3 refused by a guard, 4 delete incomplete.
- **Token:** the CLI reads only from Keychain `discord_bot_token`. There is no `--token` flag, and `DISCORD_TOKEN` and other env vars and `.env` files are ignored. **Never** use a token from a project `.env` (for example `~/code/*/.env`). Those belong to other bots in other guilds.
- **Never** write a `curl -X DELETE` loop or any other ad-hoc REST script for admin ops. If the CLI can't do something you need, **extend the CLI** (add a subcommand and a test in `~/.claude/skills/discord/tests/`) instead of falling back to curl.
- Ownership transfer, guild delete and kick/ban are deliberately **not implemented**. Don't do them by any other route either. Ask the admin to do them in the Discord UI if they explicitly want them.

Tests: `uv run --with pytest --with httpx pytest ~/.claude/skills/discord/tests/ -q`

## Destructive ops: check the guild first

On 2026-11-01 a session deleted 17 channels in the wrong server: a `.env` for a different bot pointed at a different guild. It then told the user it "couldn't reach" channels in the project server, even though the Sven bot has Administrator there, which overrides every channel overwrite. **Use `discord-admin` for every delete/create/permission check. It enforces all of the rules below.**
- Before any DELETE or PATCH, confirm `guild_id` is the server the user means. `discord-admin delete` does this for every ID. The project server is the one in `config.local.yaml` (`--guild project`), and "this server" means the guild that the session's channel lives in.
- Use the token for the bot that is actually in that guild. `discord-admin` reads only the Sven bot token from Keychain `discord_bot_token` and checks membership with `GET /users/@me/guilds`. Tokens in project `.env` files belong to other bots and guilds.
- A 403 with a bot token usually means you used the wrong token or the wrong guild. It is not a reason to hand the job to the user. Run `discord-admin perms --guild ... --channel ...` to see why.
- Never transfer server ownership unless the user explicitly asks for it. The CLI deliberately can't do it.


## Project channels: answer the person first
- **Every owner message gets a reply, even a single line, BEFORE you go back to background work.** Reply to anything pending before you launch build agents. On 2026-11-03 the owner wrote "You don't reply" and "You didn't address my comments about rank etc" while agents ran.
- **When the owner says he doesn't want X, don't build X.** He said "don't really care about tiers", and the session built a 10-tier ranked ladder anyway.
- Don't copy a competitor's whole feature table. Build what was asked for, then list extras as options and ask before building them.
