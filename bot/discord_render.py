"""Discord-side rendering + access control helpers for the bot.

Three responsibilities:

1. **User allowlist** — ``is_allowed(user_id, allowlist)``. Empty
   allowlist returns False for everyone (fail closed; defense in depth).
   ADR 016 §"User allowlist" has the rationale.
2. **Mention stripping** — when a user @-mentions the bot in a guild
   channel, the raw ``message.content`` carries the mention token.
   ``strip_bot_mention`` removes it so the LLM sees the actual
   question.
3. **Attachment → discord.File** — convert a ``bot.tools.Attachment``
   into the shape discord.py's ``send(file=...)`` accepts.

These are pure functions where possible (string handling, set
membership); the only one that touches Discord types is
``attachment_to_file``, and that import is lazy so the rest of the
module is testable without discord.py installed (matters for CI on
environments where discord.py's transitive deps are flaky).
"""

from __future__ import annotations

import io
import logging
import os
import re

from bot.tools import Attachment

logger = logging.getLogger(__name__)


def parse_allowlist(env_value: str | None) -> set[int]:
    """Parse ``DISCORD_ALLOWED_USER_IDS`` — comma-separated decimal
    Discord user IDs. Whitespace stripped, empty entries dropped,
    non-numeric entries logged and dropped. Empty set means "nobody
    allowed"; the caller must enforce fail-closed.
    """
    if not env_value:
        return set()
    out: set[int] = set()
    for raw in env_value.split(","):
        token = raw.strip()
        if not token:
            continue
        try:
            out.add(int(token))
        except ValueError:
            logger.warning(
                "Ignoring non-numeric entry in "
                "DISCORD_ALLOWED_USER_IDS: %r",
                token,
            )
    return out


def is_allowed(user_id: int, allowlist: set[int]) -> bool:
    """Set membership. Empty allowlist returns False — fail closed."""
    if not allowlist:
        return False
    return user_id in allowlist


def get_allowlist_from_env() -> set[int]:
    return parse_allowlist(os.environ.get("DISCORD_ALLOWED_USER_IDS"))


# Discord mentions look like <@1234567890> or <@!1234567890> (the
# bang form was the nickname-mention variant in older clients).
_MENTION_RE = re.compile(r"<@!?(\d+)>")


def strip_bot_mention(content: str, bot_user_id: int) -> str:
    """Remove all instances of the bot's own @-mention from a message
    body so the LLM sees the user's intent without the noise."""
    pattern = re.compile(rf"<@!?{bot_user_id}>")
    return pattern.sub("", content).strip()


def attachment_to_file(attachment: Attachment):
    """Wrap a ``bot.tools.Attachment`` in a ``discord.File`` so it can
    be passed to ``channel.send(file=...)``. Imported lazily so this
    module remains importable in test environments that don't have
    discord.py wired (the rest of the helpers in this file are
    discord.py-free)."""
    import discord  # noqa: PLC0415 — lazy

    return discord.File(
        io.BytesIO(attachment.content),
        filename=attachment.filename,
    )


# Canned messages — collected here so they're easy to change.
DENIED_MESSAGE: str = (
    "I'm not authorized to chat with you. If you think this is a "
    "mistake, ask for your Discord user ID to be added to "
    "`DISCORD_ALLOWED_USER_IDS`."
)

EMPTY_ALLOWLIST_MESSAGE: str = (
    "I'm not configured for any users yet. Populate "
    "`DISCORD_ALLOWED_USER_IDS` before I can respond."
)
