"""Discord bot entrypoint.

Connects to Discord via discord.py, listens for messages addressed to
the bot (@-mentions in guild channels, or DMs), routes each through
``bot.deepseek_client.handle_user_message``, and replies with the
returned text (and optional chart attachment).

## Triggering

The bot responds to TWO message kinds:

- **DM** — any direct message to the bot.
- **@-mention** — guild channel messages that mention the bot by ID.

It does NOT respond to non-addressed messages. No passive listening.
ARCHITECTURE.md §"out of scope" makes this an architectural commitment.

## Access control

Every message author is checked against ``DISCORD_ALLOWED_USER_IDS``
(parsed by ``bot.discord_render.parse_allowlist``). Empty allowlist
rejects everyone with a config-error reply. Disallowed users get a
single "not authorized" reply per process lifetime — subsequent
messages from them are silently ignored.

## Required Discord intents

- ``message_content``: required since 2022 to read message bodies.
  Must be toggled both in code (here) AND in the Discord developer
  portal under "Privileged Gateway Intents". README documents both.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import discord

from bot.deepseek_client import DeepSeekError, handle_user_message, validate_config
from bot.discord_render import (
    DENIED_MESSAGE,
    EMPTY_ALLOWLIST_MESSAGE,
    attachment_to_file,
    get_allowlist_from_env,
    is_allowed,
    strip_bot_mention,
)
from bot.price_alert_delivery import price_alert_delivery_loop

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format=(
            '{"ts":"%(asctime)s","level":"%(levelname)s",'
            '"name":"%(name)s","msg":%(message)r}'
        ),
    )


def _discord_token() -> str:
    token = (os.environ.get("DISCORD_BOT_TOKEN") or "").strip()
    if not token:
        logger.error(
            "DISCORD_BOT_TOKEN is not set. Create a Discord "
            "application + bot at "
            "https://discord.com/developers/applications and add "
            "the token to .env."
        )
        sys.exit(1)
    return token


def build_client() -> discord.Client:
    """Construct the discord.Client with the required intents.

    Factored out for testability — tests can call this with the
    DeepSeek path mocked and inspect the client's event handlers."""
    intents = discord.Intents.default()
    # MESSAGE_CONTENT is a Privileged Gateway Intent — must be
    # enabled in BOTH the developer portal and here. README covers
    # the portal toggle.
    intents.message_content = True

    client = discord.Client(intents=intents)
    allowlist = get_allowlist_from_env()
    price_alert_task: asyncio.Task | None = None
    # In-memory note: users we've already told "not authorized" once
    # this process; we suppress further replies to them to avoid
    # spam if they keep poking.
    suppressed_users: set[int] = set()

    @client.event
    async def on_ready() -> None:  # type: ignore[misc]
        nonlocal price_alert_task
        logger.info(
            "Bot connected as %s (id=%s); allowlist size=%d",
            client.user,
            client.user.id if client.user else "?",
            len(allowlist),
        )
        if not allowlist:
            logger.warning(
                "DISCORD_ALLOWED_USER_IDS is empty — bot will "
                "refuse every message until configured."
            )
        if price_alert_task is None or price_alert_task.done():
            price_alert_task = asyncio.create_task(price_alert_delivery_loop(client))

    @client.event
    async def on_message(message: discord.Message) -> None:  # type: ignore[misc]
        # Don't reply to ourselves; cheap test that runs first.
        if client.user is None or message.author.id == client.user.id:
            return

        # Two trigger conditions: DM, or @-mention in guild.
        is_dm = message.guild is None
        is_mention = client.user in message.mentions
        if not (is_dm or is_mention):
            return

        # Access control. Empty allowlist → fail closed.
        if not allowlist:
            await message.channel.send(EMPTY_ALLOWLIST_MESSAGE)
            return
        if not is_allowed(message.author.id, allowlist):
            if message.author.id in suppressed_users:
                logger.info(
                    "Suppressing reply to disallowed user "
                    "%s (already told)",
                    message.author.id,
                )
                return
            suppressed_users.add(message.author.id)
            await message.channel.send(DENIED_MESSAGE)
            return

        # Strip the bot's own @-mention so the LLM sees the question.
        query = strip_bot_mention(message.content, client.user.id)
        if not query:
            await message.channel.send(
                "You @-mentioned me but didn't ask anything. Try "
                "'@me what's the AK Redline FT price?'"
            )
            return

        logger.info(
            "Handling message from user=%s in channel=%s: %r",
            message.author.id,
            message.channel.id,
            query[:200],
        )

        async with message.channel.typing():
            reply = await handle_user_message(
                query,
                discord_user_id=str(message.author.id),
                discord_channel_id=str(message.channel.id),
            )

        file = (
            attachment_to_file(reply.attachment)
            if reply.attachment is not None
            else None
        )
        # Discord caps single messages at 2000 chars; the LLM is
        # told to keep replies short, but we truncate defensively.
        text = reply.text
        if len(text) > 2000:
            text = text[:1990] + "\n…[truncated]"
        await message.channel.send(text, file=file)

    return client


def main() -> int:
    _configure_logging()
    token = _discord_token()
    try:
        validate_config()
    except DeepSeekError as exc:
        logger.error("%s", exc)
        return 1
    client = build_client()
    # ``client.run`` runs its own asyncio loop and blocks until the
    # connection is dropped or the process is killed.
    client.run(token, log_handler=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
