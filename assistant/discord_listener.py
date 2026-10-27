"""
Discord listener — threading.Thread that runs discord.py in its own event loop.

Follows the same pattern as SignalListener:
  - Runs in a daemon thread
  - Pushes normalized messages to a queue.Queue
  - Main poll loop drains the queue and produces to bus

Key threading note: discord.py is async-native. We run it in a dedicated thread
with its own asyncio event loop (asyncio.new_event_loop + run_until_complete),
not asyncio.run() which tries to set signal handlers (main thread only).
Only primitive data (dicts) cross the thread boundary via queue — never discord.py
objects, which are not thread-safe.
"""

import asyncio
import logging
import queue
import time
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# Guard import — discord.py may not be installed
try:
    import discord
    DISCORD_AVAILABLE = True
except ImportError:
    DISCORD_AVAILABLE = False


class DiscordListener:
    """Listens to Discord messages and queues them for processing.

    Follows Sven's listener interface:
      - Runs as a daemon thread (call .start())
      - Pushes messages to a queue.Queue
      - Main poll loop drains queue → produces to bus

    Mention filtering: Only queues messages that are directed at the bot via:
      - @mention of the bot user
      - Role mention matching bot_role_ids
      - Bot name appearing in message text (case-insensitive)
      - Reply to a bot message
    """

    def __init__(
        self,
        message_queue: queue.Queue,
        channel_ids: list[str],
        bot_token: str,
        bot_role_ids: list[str] | None = None,
        bot_names: list[str] | None = None,
        guild_ids: list[str] | None = None,
        respond_all_guild_ids: list[str] | None = None,
    ):
        if not DISCORD_AVAILABLE:
            raise ImportError("discord.py is not installed. Run: uv add 'discord.py>=2.3.0'")

        import threading
        self._thread = threading.Thread(target=self._run, daemon=True, name="DiscordListener")
        self.message_queue = message_queue
        self.channel_ids = set(int(c) for c in channel_ids)
        self.bot_token = bot_token
        self.bot_role_ids = set(int(r) for r in (bot_role_ids or []))
        self.bot_names = [n.lower() for n in (bot_names or ["sven"])]
        # 2026-10-26: per-project chat server. Every channel in these guilds is accepted
        # (no config edit + restart per new channel), and in respond_all guilds every
        # message is processed without an @mention: each channel = one project session.
        self.guild_ids = set(int(g) for g in (guild_ids or []))
        self.respond_all_guild_ids = set(int(g) for g in (respond_all_guild_ids or []))
        self.running = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: discord.Client | None = None

    def start(self):
        """Start the listener thread."""
        self._thread.start()

    def is_alive(self) -> bool:
        """Check if the listener thread is alive."""
        return self._thread.is_alive()

    def _run(self):
        """Start discord.py client in a new event loop (runs in thread)."""
        self.running = True
        while self.running:
            try:
                self._loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self._loop)
                self._loop.run_until_complete(self._connect())
            except Exception as e:
                log.error(f"DiscordListener error: {e}")
            finally:
                if self._loop and not self._loop.is_closed():
                    self._loop.close()
                self._loop = None
                self._client = None

            if self.running:
                log.info("[discord] Reconnecting in 5s...")
                time.sleep(5)  # Backoff before reconnect

    async def _connect(self):
        """Connect to Discord Gateway and listen for messages."""
        intents = discord.Intents.default()
        intents.message_content = True  # Privileged intent — required for message.content

        client = discord.Client(intents=intents)
        self._client = client

        @client.event
        async def on_ready():
            log.info(f"DiscordListener connected as {client.user} (channels: {self.channel_ids})")

        @client.event
        async def on_message(message):
            # Ignore own messages
            if message.author == client.user:
                return

            # Ignore DMs — bot only responds in guild channels
            if not message.guild:
                return

            # Defense-in-depth channel filter (Discord permissions are primary gate)
            in_project_guild = message.guild.id in self.guild_ids
            if message.channel.id not in self.channel_ids and not in_project_guild:
                return

            text = message.content
            if not text:
                if message.attachments:
                    text = "(attachment)"
                else:
                    return

            # ── Mention filter: only process messages directed at the bot ──
            is_mentioned = message.guild.id in self.respond_all_guild_ids

            # 1. Direct @mention of the bot user
            if client.user in message.mentions:
                is_mentioned = True

            # 2. Role mention matching bot role IDs
            if not is_mentioned and self.bot_role_ids:
                mentioned_role_ids = {r.id for r in message.role_mentions}
                if self.bot_role_ids & mentioned_role_ids:
                    is_mentioned = True

            # 3. Bot name appears in message text (case-insensitive)
            if not is_mentioned:
                text_lower = text.lower()
                for name in self.bot_names:
                    if name in text_lower:
                        is_mentioned = True
                        break

            # 4. Reply to a bot message
            if not is_mentioned and message.reference and message.reference.resolved:
                ref_msg = message.reference.resolved
                if hasattr(ref_msg, 'author') and ref_msg.author == client.user:
                    is_mentioned = True

            if not is_mentioned:
                log.debug(f"DiscordListener: skipping non-directed message from {message.author.display_name} in #{message.channel.name}")
                return

            sender_id = str(message.author.id)
            sender_name = message.author.display_name or message.author.name
            channel_id = str(message.channel.id)

            # Normalize to match SignalListener message format exactly
            # NOTE: No "date" field — SignalListener doesn't have it either.
            # Only "timestamp" (datetime) is used by process_message().
            msg = {
                "rowid": message.id,  # Discord message snowflake as unique ID
                "phone": sender_id,  # Discord user ID as "phone" field
                "is_from_me": 0,
                "text": text,
                "is_group": True,  # Discord channels are always group-like
                "is_audio_message": False,  # Discord doesn't have audio messages
                "group_name": message.channel.name,
                "chat_identifier": channel_id,
                "attachments": [
                    {
                        "mime_type": a.content_type or "application/octet-stream",
                        "name": a.filename,
                        "size": a.size or 0,
                        "path": a.url,  # Discord CDN URL
                    }
                    for a in message.attachments
                ],
                "audio_transcription": None,
                "thread_originator_guid": None,
                "source": "discord",
                "sender_name": sender_name,  # Display name for group message wrapping
                "timestamp": message.created_at.astimezone(timezone.utc),  # datetime for Gemini vision context
            }

            log.info(f"DiscordListener: queued message from {sender_name} ({sender_id}) in #{message.channel.name}: {text[:80]}...")
            self.message_queue.put(msg)

        @client.event
        async def on_disconnect():
            log.warning("[discord] Disconnected from Gateway — discord.py will auto-reconnect")

        @client.event
        async def on_resumed():
            log.info("[discord] Resumed Gateway connection")

        try:
            await client.start(self.bot_token)
        except Exception as e:
            log.error(f"[discord] Fatal connection error: {e}")
            raise
        finally:
            if not client.is_closed():
                await client.close()
            self._client = None

    def stop(self):
        """Gracefully stop the listener.

        1. Close the Discord client (async, via threadsafe call)
        2. Stop the event loop
        3. The _run() while loop exits because self.running is False
        """
        self.running = False
        # Gracefully close the Discord client before stopping the loop
        if self._client and self._loop and self._loop.is_running():
            try:
                future = asyncio.run_coroutine_threadsafe(self._client.close(), self._loop)
                future.result(timeout=5)  # Wait for graceful close
            except Exception as e:
                log.warning(f"[discord] Error during graceful close: {e}")
            # Loop should exit naturally after client.close()
        elif self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)

    @property
    def is_ready(self) -> bool:
        """Check if the Discord client is connected and ready."""
        return self._client is not None and self._client.is_ready()

    @property
    def latency(self) -> float | None:
        """Get the Discord Gateway latency in seconds, or None if not connected."""
        if self._client and self._client.is_ready():
            return self._client.latency
        return None
