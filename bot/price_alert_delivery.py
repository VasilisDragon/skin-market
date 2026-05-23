"""Background delivery loop for triggered price alerts."""

from __future__ import annotations

import asyncio
import logging
import os
from decimal import Decimal
from typing import Any

from bot.tools import SkinMarketBotError, evaluate_triggered_price_alerts

logger = logging.getLogger(__name__)

DEFAULT_PRICE_ALERT_POLL_SECONDS = 60
DEFAULT_PRICE_ALERT_BATCH_LIMIT = 100


def price_alert_poll_seconds() -> int:
    return int(os.environ.get("PRICE_ALERT_POLL_SECONDS", DEFAULT_PRICE_ALERT_POLL_SECONDS))


def price_alert_batch_limit() -> int:
    return int(os.environ.get("PRICE_ALERT_BATCH_LIMIT", DEFAULT_PRICE_ALERT_BATCH_LIMIT))


def format_price_alert_message(alert: dict[str, Any]) -> str:
    direction = "at or below" if alert["direction"] == "at_or_below" else "at or above"
    currency = "USD" if alert["currency"] == "usd" else "SC"
    return (
        f"Price alert triggered: {alert['display_name']}\n"
        f"- Target: {direction} {Decimal(str(alert['threshold_price'])):.2f} {currency}\n"
        f"- Current: {Decimal(str(alert['trigger_price'])):.2f} {currency}"
        f" on {alert['trigger_source']}\n"
        f"- Alert id: `{alert['id']}`"
    )


async def price_alert_delivery_loop(client) -> None:
    """Poll triggered alerts and deliver them to stored Discord channels."""
    poll_seconds = price_alert_poll_seconds()
    if poll_seconds <= 0:
        logger.info("Price alert delivery disabled by PRICE_ALERT_POLL_SECONDS=%s", poll_seconds)
        return

    await client.wait_until_ready()
    while not client.is_closed():
        try:
            payload = await asyncio.to_thread(
                evaluate_triggered_price_alerts,
                price_alert_batch_limit(),
            )
            for alert in payload.get("triggered") or []:
                await _send_alert(client, alert)
        except SkinMarketBotError as exc:
            logger.warning("Price alert delivery API error: %s", exc)
        except Exception:
            logger.exception("Unexpected price alert delivery failure")

        await asyncio.sleep(poll_seconds)


async def _send_alert(client, alert: dict[str, Any]) -> None:
    channel_id = alert.get("discord_channel_id")
    if not channel_id:
        logger.warning("Triggered alert %s has no channel id", alert.get("id"))
        return
    try:
        channel_int = int(channel_id)
    except (TypeError, ValueError):
        logger.warning("Triggered alert %s has invalid channel id %r", alert.get("id"), channel_id)
        return

    channel = client.get_channel(channel_int)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_int)
        except Exception:
            logger.exception(
                "Could not resolve Discord channel %s for alert %s",
                channel_id,
                alert.get("id"),
            )
            return

    await channel.send(format_price_alert_message(alert))
