# ADR 039 - Baseline reliability and midpoint suppression

**Status:** Accepted
**Date:** 2026-05-23
**Related:** ADR 028, ADR 030, ADR 038

## Context

The market baseline is intentionally a market-name range, not an appraisal for
the exact asset. Before this change, every baseline returned Low, Mid, and High,
where Mid was the median of the available local USD price points.

That is misleading when sources disagree by orders of magnitude or when only one
or two sources are available. A two-source Souvenir Dragon Lore baseline with
Low near $1,030 and High near $515,158 produced a Mid near $258,094. That
midpoint looked precise even though it was only arithmetic over incompatible or
thin source data.

## Decision

`build_market_baseline` now classifies every baseline with
`baseline_reliability`:

- `reliable`: source count and spread are sufficient to show a midpoint.
- `wide_spread`: `high / low >= 5.0`.
- `thin_sources`: fewer than 3 USD source points.

The constants are:

```text
BASELINE_WIDE_SPREAD_RATIO = 5.0
BASELINE_MIN_RELIABLE_SOURCE_COUNT = 3
```

`wide_spread` takes precedence over `thin_sources`, so a two-source 500x
Dragon-Lore-like range is reported as wide spread rather than merely thin.

Reliable baselines keep the existing Low/Mid/High shape. `wide_spread` and
`thin_sources` baselines keep Low/High and return a `reliability` object with
the reason, thresholds, high/low ratio, and `mid_suppressed=true`; they do not
return a `mid` field.

Portfolio baselines inherit this rule. A portfolio Mid is shown only when every
priced item baseline has a reliable Mid and the summed range is not itself too
wide. Otherwise the portfolio response keeps summed Low/High and explains why a
total midpoint is suppressed.

## Bot Rendering

The bot prompt now renders Low and High for market baselines. It renders Mid
only when:

- `baseline_reliability == "reliable"`, and
- `mid` exists.

For `wide_spread` and `thin_sources`, the bot must render
`reliability.message` and must not state a single midpoint as the answer.

## Consequences

- Normal tight multi-source baselines render as before.
- Thin or divergent exact-asset baselines no longer produce a confident-looking
  midpoint.
- Unit 2 on-demand substrate pricing can safely surface thin single-source
  results through the same honesty rule.

## Rejected

- **Keep Mid but add a disclaimer.** Rejected. Users still anchor on the number.
- **Raise the spread threshold much higher.** Rejected for the first pass. A 5x
  high/low spread is already too divergent to treat a median as a useful
  midpoint.
- **Hide Low/High on unreliable baselines.** Rejected. The raw observed range is
  honest context; only the synthesized midpoint is the problem.
