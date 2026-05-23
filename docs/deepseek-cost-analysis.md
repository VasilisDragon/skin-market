# DeepSeek Cost Analysis

Date: 2026-05-23

This analysis covers the Discord bot's `deepseek-v4-flash` tool-routing path
after the DeepSeek cutover. Measurements were taken through
`bot.deepseek_client.handle_user_message`, using the live DeepSeek API, the live
local API, and `llm_usage_log` rows tagged with measurement-specific
`discord_user_id` prefixes.

## Verified Pricing

Official DeepSeek pricing was rechecked on 2026-05-23:

- Source: https://api-docs.deepseek.com/quick_start/pricing/
- Model: `deepseek-v4-flash`
- Input cache hit: `$0.0028 / 1M tokens`
- Input cache miss: `$0.14 / 1M tokens`
- Output: `$0.28 / 1M tokens`

Cost formula:

```text
cost_usd =
  prompt_cache_hit_tokens  * 0.0028 / 1_000_000
+ prompt_cache_miss_tokens * 0.14   / 1_000_000
+ completion_tokens        * 0.28   / 1_000_000
```

The constants already in `db/llm_usage.py` matched the official pricing on the
verification date, so no pricing-code change was required.

## Context Caching

Official DeepSeek context caching docs were rechecked on 2026-05-23:

- Source: https://api-docs.deepseek.com/guides/kv_cache/

DeepSeek context caching is automatic and enabled by default. There is no
application-side cache-key API to implement for this bot path. The usage block
reports cache status through `prompt_cache_hit_tokens` and
`prompt_cache_miss_tokens`, which `llm_usage_log` already records.

The implementation action was therefore to keep the request prefix stable and
smaller:

- `SYSTEM_PROMPT` chars: `5,621 -> 4,733`
- `TOOL_DEFINITIONS` JSON chars: `4,901 -> 3,048`
- Stable prefix chars: `10,522 -> 7,781`
- System/tool prefix character reduction: `26.1%`

## Measurement Runs

Baseline run:

- Prefix: `phase1-baseline-20260523:%`
- Prompt/tool schema before optimization
- 10 representative queries, 22 DeepSeek turns

Final quality run:

- Prefix: `phase1-gate1-final-accepted-20260523:%`
- Final prompt/tool schema after optimization
- Same 10 representative queries, 19 DeepSeek turns

Aggregate result:

| Run | Turns | Prompt tokens | Completion tokens | Total tokens | Cache hit | Cache miss | Cost |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Baseline | 22 | 72,827 | 2,823 | 75,650 | 64,384 | 8,443 | `$0.00215266` |
| Final | 19 | 46,524 | 1,664 | 48,188 | 41,344 | 5,180 | `$0.00130683` |

Delta:

- Prompt tokens: `-36.1%`
- Total tokens: `-36.3%`
- Cost: `-39.3%`
- Average cost per representative query: `$0.000215266 -> $0.000130683`

Per-query cost:

| Case | Baseline | Final | Notes |
| --- | ---: | ---: | --- |
| Price lookup | `$0.00021604` | `$0.00017514` | Same two-turn price flow. |
| Drift check | `$0.00011574` | `$0.00012250` | Slightly higher final cache miss in this run. |
| History | `$0.00015738` | `$0.00012530` | Shorter final rendering. |
| Watchlist | `$0.00015982` | `$0.00011970` | Added tier-coverage caveat. |
| Deal | `$0.00029556` | `$0.00016086` | Shorter response than baseline. |
| Wear omitted | `$0.00037692` | `$0.00002513` | Now asks clarification instead of querying multiple wears. |
| Featured price | `$0.00017244` | `$0.00012586` | Correct explicit FN featured-tier response. |
| Chart | `$0.00009662` | `$0.00007546` | Same chart attachment path. |
| Interesting | `$0.00037066` | `$0.00031402` | Large anomaly payload still dominates. |
| Not tracked | `$0.00019148` | `$0.00006286` | No longer adds unsupported lore. |

## Subscription Projection

Using the final average representative query cost of `$0.000130683`:

| Monthly queries | Baseline LLM cost | Final LLM cost |
| ---: | ---: | ---: |
| 100 | `$0.0215` | `$0.0131` |
| 300 | `$0.0646` | `$0.0392` |
| 1,000 | `$0.2153` | `$0.1307` |
| 3,000 | `$0.6458` | `$0.3920` |
| 10,000 | `$2.1527` | `$1.3068` |

At a `1,000` query/month heavy-user assumption, DeepSeek cost is about
`$0.1307` per active user/month:

| Plan price | LLM cost at 1,000 queries | LLM-only gross margin |
| ---: | ---: | ---: |
| `$3/mo` | `$0.1307` | `95.6%` |
| `$5/mo` | `$0.1307` | `97.4%` |

At an extreme `10,000` query/month user, DeepSeek cost is about `$1.3068`:

| Plan price | LLM cost at 10,000 queries | LLM-only gross margin |
| ---: | ---: | ---: |
| `$3/mo` | `$1.3068` | `56.4%` |
| `$5/mo` | `$1.3068` | `73.9%` |

Approximate LLM-only break-even query counts:

- `$3/mo`: `22,956` representative queries/month
- `$5/mo`: `38,260` representative queries/month

These projections exclude infrastructure, payment processing, support, and
non-bot collector/API costs.

## Capturing Operator Load Later

The live bot already logs one row per DeepSeek request in `llm_usage_log`.
Operator-generated heavy-usage load can be measured with the same table.

For a tagged test harness:

```sql
select
  split_part(discord_user_id, ':', 1) as run,
  count(*) as turns,
  sum(prompt_tokens) as prompt_tokens,
  sum(completion_tokens) as completion_tokens,
  sum(total_tokens) as total_tokens,
  sum(coalesce(prompt_cache_hit_tokens, 0)) as cache_hit_tokens,
  sum(coalesce(prompt_cache_miss_tokens, 0)) as cache_miss_tokens,
  sum(cost_usd) as cost_usd
from llm_usage_log
where discord_user_id like 'phase1-gate1-final-accepted-20260523:%'
group by run;
```

For real Discord traffic, filter by `created_at` window and, when useful, by the
known Discord user ID. Do not enable `LLM_USAGE_LOG_FULL_PROMPT` for normal
traffic unless debugging requires it.

## Related Orientation Notes

- Deterministic core remains intact: the LLM parses the user question, calls
  Python tools, and phrases tool results. It does not fetch marketplaces,
  scrape, or compute market pricing/math.
- Secrets remain environment-driven. DeepSeek, Pricempire, Discord, and API
  keys are read from env vars, are not printed, and are not stored in
  `llm_usage_log`.
- Pricempire's public docs expose `/v4/paid/inventory` keyed by `steam_id` and
  `app_id`, with optional `force`. The example response includes `asset_id`,
  `d`, `low_rank`, `high_rank`, `float_value`, `paint_seed`, `stickers`,
  `charms`, and nested `item.paint_id`. Source:
  https://pricempire.com/docs
