"""Background delivery loop for recurring portfolio monitor updates."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from bot.tools import (
    SkinMarketBotError,
    evaluate_portfolio_monitors,
    mark_portfolio_monitor_delivery,
)

logger = logging.getLogger(__name__)

DEFAULT_PORTFOLIO_MONITOR_POLL_SECONDS = 300
DEFAULT_PORTFOLIO_MONITOR_BATCH_LIMIT = 25


def portfolio_monitor_poll_seconds() -> int:
    return int(
        os.environ.get(
            "PORTFOLIO_MONITOR_POLL_SECONDS",
            DEFAULT_PORTFOLIO_MONITOR_POLL_SECONDS,
        )
    )


def portfolio_monitor_batch_limit() -> int:
    return int(
        os.environ.get(
            "PORTFOLIO_MONITOR_BATCH_LIMIT",
            DEFAULT_PORTFOLIO_MONITOR_BATCH_LIMIT,
        )
    )


def format_portfolio_monitor_message(payload: dict[str, Any]) -> str:
    monitor = payload["monitor"]
    result = payload["snapshot_result"]
    snapshot = result["snapshot"]
    baseline = snapshot.get("portfolio_baseline") or {}
    delta = result.get("delta_vs_previous")
    event = payload["event_type"]
    title = (
        "Portfolio baseline monitor: initial snapshot"
        if event == "initial_snapshot"
        else "Portfolio baseline monitor: threshold crossed"
    )
    lines = [
        title,
        f"- Mid baseline: {baseline.get('mid', 'n/a')} USD",
        (
            f"- Priced/unpriced: {baseline.get('priced_count', 0)}/"
            f"{baseline.get('unpriced_count', 0)}"
        ),
        f"- Monitor id: `{monitor['id']}`",
    ]
    if delta is not None:
        lines.insert(
            2,
            (
                f"- Change: {delta.get('mid_change', 'n/a')} USD "
                f"({delta.get('mid_change_pct', 'n/a')}%)"
            ),
        )
    lines.append(
        "Market-baseline movement only; not realized P/L or float/sticker repricing."
    )
    return "\n".join(lines)


async def portfolio_monitor_delivery_loop(client) -> None:
    """Poll due portfolio monitors and deliver deterministic updates."""
    poll_seconds = portfolio_monitor_poll_seconds()
    if poll_seconds <= 0:
        logger.info(
            "Portfolio monitor delivery disabled by "
            "PORTFOLIO_MONITOR_POLL_SECONDS=%s",
            poll_seconds,
        )
        return

    await client.wait_until_ready()
    while not client.is_closed():
        try:
            payload = await asyncio.to_thread(
                evaluate_portfolio_monitors,
                portfolio_monitor_batch_limit(),
            )
            for due in payload.get("due") or []:
                delivered, error = await _send_monitor_update(client, due)
                monitor = due.get("monitor") or {}
                result = due.get("snapshot_result") or {}
                snapshot = result.get("snapshot") or {}
                monitor_id = monitor.get("id")
                if not monitor_id:
                    logger.warning("Portfolio monitor payload missing id: %r", due)
                    continue
                await asyncio.to_thread(
                    mark_portfolio_monitor_delivery,
                    monitor_id,
                    delivered,
                    snapshot.get("id") if delivered else None,
                    error,
                )
        except SkinMarketBotError as exc:
            logger.warning("Portfolio monitor delivery API error: %s", exc)
        except Exception:
            logger.exception("Unexpected portfolio monitor delivery failure")

        await asyncio.sleep(poll_seconds)


async def _send_monitor_update(
    client,
    payload: dict[str, Any],
) -> tuple[bool, str | None]:
    monitor = payload.get("monitor") or {}
    channel_id = monitor.get("discord_channel_id")
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
                "Could not resolve Discord channel %s for portfolio monitor %s",
                channel_id,
                monitor.get("id"),
            )
            return False, f"could not resolve Discord channel {channel_id}"

    try:
        await channel.send(format_portfolio_monitor_message(payload))
    except Exception:
        logger.exception(
            "Could not send portfolio monitor %s to channel %s",
            monitor.get("id"),
            channel_id,
        )
        return False, f"could not send Discord message to channel {channel_id}"
    return True, None
