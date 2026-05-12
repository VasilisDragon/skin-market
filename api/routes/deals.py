"""``POST /deals/evaluate`` — opinionated verdict on a price offer.

Currency-driven split:

- ``offer.currency`` selects which sources are *candidate* comparables
  (matching denomination). Other sources are *informational* with
  ``reason="denomination_mismatch"``.
- Among the candidate comparables, sources whose latest observation is
  older than ``COMPARABLE_FRESHNESS_HOURS`` are demoted to
  *informational* with ``reason="stale"``. Verdict math reads only
  fresh, currency-matched comparables.
- If no comparables remain (all stale, or none match the currency), the
  verdict is ``no_comparable_data`` and the response still includes the
  informational rows so the bot can show context.

Verdict math compares ``offer.amount`` to ``min(comparable.current)``:

- ``below_market``: ``offer < cheapest * (1 - AT_MARKET_TOLERANCE_PCT)``
- ``at_market``:    ``offer`` within ±tolerance of cheapest
- ``above_market``: ``offer > cheapest * (1 + AT_MARKET_TOLERANCE_PCT)``

ADR 014 §4 has the full reasoning. Both thresholds are named module
constants here — change one, change the ADR alongside it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from api.schemas import (
    ComparableSource,
    DealEvaluateRequest,
    DealEvaluateResponse,
    InformationalSource,
)
from db.connection import get_engine

router = APIRouter(tags=["deals"])

# 5% — within this band of the cheapest comparable, the offer is "at
# market". Outside, it's clearly above or below. Calibrated as the
# typical noise floor across Skinport/DMarket spreads; tunable.
AT_MARKET_TOLERANCE_PCT: Decimal = Decimal("0.05")

# 4 hours — older than this, a comparable source is demoted to
# informational. Picked against current cadences (Steam 60min,
# Skinport 15min, DMarket 15min): 4h gives 4-16x grace beyond a
# healthy cycle interval, so a single missed cycle doesn't degrade
# the verdict, but a multi-cycle outage does.
COMPARABLE_FRESHNESS_HOURS: int = 4


@router.post("/deals/evaluate", response_model=DealEvaluateResponse)
def evaluate_deal(req: DealEvaluateRequest) -> DealEvaluateResponse:
    engine = get_engine()
    now = datetime.now(UTC)
    freshness_floor = now - timedelta(hours=COMPARABLE_FRESHNESS_HOURS)

    with Session(engine) as session:
        item = session.execute(
            text(
                "SELECT id, display_name FROM items WHERE slug = :slug"
            ),
            {"slug": req.slug},
        ).mappings().first()
        if item is None:
            raise HTTPException(
                status_code=404, detail=f"Item not found: {req.slug!r}"
            )

        latest_per_source = session.execute(
            text(
                """
                SELECT DISTINCT ON (p.source_id)
                    s.name AS source_name,
                    s.denomination,
                    p.price,
                    p.timestamp AS observed_at
                FROM prices p
                JOIN sources s ON s.id = p.source_id
                WHERE p.item_id = :item_id
                  AND s.enabled = TRUE
                ORDER BY p.source_id, p.timestamp DESC
                """
            ),
            {"item_id": item["id"]},
        ).mappings().all()

    comparable: list[ComparableSource] = []
    informational: list[InformationalSource] = []
    offer_currency = req.offer.currency

    for row in latest_per_source:
        # Stage 1: currency split. Denomination mismatch is always
        # informational — no amount of freshness rescues a wallet-credit
        # price into a USD comparison.
        if row["denomination"] != offer_currency:
            note = _denomination_note(row["denomination"], offer_currency)
            informational.append(
                InformationalSource(
                    source=row["source_name"],
                    denomination=row["denomination"],
                    current=row["price"],
                    observed_at=row["observed_at"],
                    reason="denomination_mismatch",
                    note=note,
                )
            )
            continue

        # Stage 2: freshness split. Stale comparable becomes
        # informational with reason='stale' — same source name, same
        # denomination match, but excluded from the verdict math.
        if row["observed_at"] < freshness_floor:
            informational.append(
                InformationalSource(
                    source=row["source_name"],
                    denomination=row["denomination"],
                    current=row["price"],
                    observed_at=row["observed_at"],
                    reason="stale",
                    note=(
                        f"Last observed >"
                        f"{COMPARABLE_FRESHNESS_HOURS}h ago "
                        f"({row['observed_at'].isoformat()}); excluded "
                        f"from verdict computation."
                    ),
                )
            )
            continue

        # Fresh + currency match → comparable.
        delta = req.offer.amount - row["price"]
        delta_pct = (
            (delta / row["price"]) if row["price"] != 0 else Decimal("0")
        )
        comparable.append(
            ComparableSource(
                source=row["source_name"],
                denomination=row["denomination"],
                current=row["price"],
                observed_at=row["observed_at"],
                delta=delta,
                delta_pct=_format_pct(delta_pct),
            )
        )

    verdict, summary = _decide_verdict(
        offer_amount=req.offer.amount,
        offer_currency=offer_currency,
        comparable=comparable,
        informational=informational,
        display_name=item["display_name"],
    )

    return DealEvaluateResponse(
        slug=req.slug,
        display_name=item["display_name"],
        offer=req.offer,
        verdict=verdict,
        comparable=comparable,
        informational=informational,
        summary=summary,
    )


def _denomination_note(
    source_denomination: str, offer_currency: str
) -> str:
    """Human-readable note explaining why a denomination mismatch
    means this source can't anchor the verdict."""
    if (
        source_denomination == "wallet_credit"
        and offer_currency == "usd"
    ):
        return (
            "Steam Wallet credit; not directly comparable to USD "
            "offers (~1:1 at deposit for buyers, but the structural "
            "premium accrues to sellers)."
        )
    if (
        source_denomination == "usd"
        and offer_currency == "wallet_credit"
    ):
        return (
            "Real-money USD; not directly comparable to wallet-credit "
            "offers since wallet credit cannot be withdrawn."
        )
    # Future denominations land here.
    return (
        f"Denominated in {source_denomination}; not directly "
        f"comparable to {offer_currency} offers."
    )


def _format_pct(ratio: Decimal) -> str:
    """``+51.8%`` or ``-3.2%`` — always signed, one decimal."""
    pct = ratio * Decimal("100")
    sign = "+" if pct >= 0 else "-"
    return f"{sign}{abs(pct):.1f}%"


def _decide_verdict(
    *,
    offer_amount: Decimal,
    offer_currency: str,
    comparable: list[ComparableSource],
    informational: list[InformationalSource],
    display_name: str,
) -> tuple[str, str]:
    """Return ``(verdict, summary)``. Pure function; the only data
    dependency is the comparable list and the offer."""
    currency_label = "USD" if offer_currency == "usd" else "wallet credit"

    if not comparable:
        return (
            "no_comparable_data",
            (
                f"No fresh comparable data for {display_name} in "
                f"{currency_label}. "
                f"{len(informational)} informational source(s) returned "
                f"— see the `informational` block for context."
            ),
        )

    cheapest = min(c.current for c in comparable)
    lower = cheapest * (Decimal("1") - AT_MARKET_TOLERANCE_PCT)
    upper = cheapest * (Decimal("1") + AT_MARKET_TOLERANCE_PCT)

    if offer_amount < lower:
        verdict = "below_market"
        adjective = "below"
    elif offer_amount > upper:
        verdict = "above_market"
        adjective = "above"
    else:
        verdict = "at_market"
        adjective = "near"

    cheapest_str = f"{cheapest}"
    if offer_currency == "usd":
        offer_render = f"${offer_amount}"
        cheapest_render = f"${cheapest_str}"
    else:
        offer_render = f"{offer_amount} SC"
        cheapest_render = f"{cheapest_str} SC"

    cheapest_source = min(comparable, key=lambda c: c.current).source
    summary = (
        f"{offer_render} {currency_label} is {adjective} market for "
        f"{display_name} — cheapest comparable is {cheapest_render} on "
        f"{cheapest_source} (tolerance "
        f"±{int(AT_MARKET_TOLERANCE_PCT * 100)}%)."
    )
    return verdict, summary
