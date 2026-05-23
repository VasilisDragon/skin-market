"""Background delivery loop for recurring signal digest subscriptions."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from bot.tools import (
    SkinMarketBotError,
    evaluate_signal_subscriptions,
    mark_signal_subscription_delivery,
)

logger = logging.getLogger(__name__)

DEFAULT_SIGNAL_SUBSCRIPTION_POLL_SECONDS = 60
DEFAULT_SIGNAL_SUBSCRIPTION_BATCH_LIMIT = 100


def signal_subscription_poll_seconds() -> int:
    return int(
        os.environ.get(
            "SIGNAL_SUBSCRIPTION_POLL_SECONDS",
            DEFAULT_SIGNAL_SUBSCRIPTION_POLL_SECONDS,
        )
    )


def signal_subscription_batch_limit() -> int:
    return int(
        os.environ.get(
            "SIGNAL_SUBSCRIPTION_BATCH_LIMIT",
            DEFAULT_SIGNAL_SUBSCRIPTION_BATCH_LIMIT,
        )
    )


def format_signal_digest_message(payload: dict[str, Any]) -> str:
    subscription = payload["subscription"]
    digest = payload["digest"]
    signals = digest.get("signals") or []
    lines = [
        "Market signal digest",
        (
            f"- Window: last {digest['hours']}h; "
            f"{digest['total_anomalies']} qualifying signals"
        ),
        f"- Subscription id: `{subscription['id']}`",
        "",
    ]
    for idx, row in enumerate(signals[: subscription["limit"]], start=1):
        lines.append(
            f"{idx}. {row['severity'].title()} | {row['display_name']} "
            f"({row['z_score']} z)"
        )
        lines.append(f"   {row['summary']}")
    lines.append("")
    lines.append("Watchlist signals only; not buy/sell instructions.")
    return "\n".join(lines)


async def signal_subscription_delivery_loop(client) -> None:
    """Poll due signal subscriptions and deliver deterministic digests."""
    poll_seconds = signal_subscription_poll_seconds()
    if poll_seconds <= 0:
        logger.info(
            "Signal subscription delivery disabled by "
            "SIGNAL_SUBSCRIPTION_POLL_SECONDS=%s",
            poll_seconds,
        )
        return

    await client.wait_until_ready()
    while not client.is_closed():
        try:
            payload = await asyncio.to_thread(
                evaluate_signal_subscriptions,
                signal_subscription_batch_limit(),
            )
            for due in payload.get("due") or []:
                delivered, error = await _send_digest(client, due)
                subscription_id = (due.get("subscription") or {}).get("id")
                if not subscription_id:
                    logger.warning("Signal digest payload missing id: %r", due)
                    continue
                await asyncio.to_thread(
                    mark_signal_subscription_delivery,
                    subscription_id,
                    delivered,
                    due.get("digest_fingerprint") if delivered else None,
                    error,
                )
        except SkinMarketBotError as exc:
            logger.warning("Signal subscription delivery API error: %s", exc)
        except Exception:
            logger.exception("Unexpected signal subscription delivery failure")

        await asyncio.sleep(poll_seconds)


async def _send_digest(client, payload: dict[str, Any]) -> tuple[bool, str | None]:
    subscription = payload.get("subscription") or {}
    channel_id = subscription.get("discord_channel_id")
    if not channel_id:
        return False, "missing Discord channel id"
    try:
        channel_int = int(channel_id)
    except (TypeError, ValueError):
        return False, f"invalid Discord channel id: {channel_id!r}"

    channel = client.get_channel(channel_int)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_int)
        except Exception:
            logger.exception(
                "Could not resolve Discord channel %s for signal subscription %s",
                channel_id,
                subscription.get("id"),
            )
            return False, f"could not resolve Discord channel {channel_id}"

    try:
        await channel.send(format_signal_digest_message(payload))
    except Exception:
        logger.exception(
            "Could not send signal digest subscription %s to channel %s",
            subscription.get("id"),
            channel_id,
        )
        return False, f"could not send Discord message to channel {channel_id}"
    return True, None
