from __future__ import annotations

from decimal import Decimal

from db.llm_usage import build_llm_usage_row, compute_deepseek_v4_flash_cost


def test_compute_deepseek_v4_flash_cost_uses_cache_split() -> None:
    usage = {
        "prompt_tokens": 3000,
        "completion_tokens": 500,
        "total_tokens": 3500,
        "prompt_cache_hit_tokens": 1000,
        "prompt_cache_miss_tokens": 2000,
    }

    assert compute_deepseek_v4_flash_cost(usage) == Decimal("0.00042280")


def test_build_usage_row_redacts_preview_and_omits_full_prompt(monkeypatch) -> None:
    monkeypatch.delenv("LLM_USAGE_LOG_FULL_PROMPT", raising=False)
    row = build_llm_usage_row(
        model="deepseek-v4-flash",
        usage={
            "prompt_tokens": 10,
            "completion_tokens": 2,
            "total_tokens": 12,
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": 10,
        },
        messages=[
            {
                "role": "user",
                "content": "DEEPSEEK_API_KEY=should-not-be-previewed",
            }
        ],
        discord_user_id="1234",
    )

    assert row["discord_user_id"] == "1234"
    assert row["prompt_sha256"]
    assert "should-not-be-previewed" not in row["prompt_preview"]
    assert "[REDACTED]" in row["prompt_preview"]
    assert row["full_prompt"] is None


def test_build_usage_row_full_prompt_dev_flag(monkeypatch) -> None:
    monkeypatch.setenv("LLM_USAGE_LOG_FULL_PROMPT", "true")
    row = build_llm_usage_row(
        model="deepseek-v4-flash",
        usage={
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
        },
        messages=[{"role": "user", "content": "hello"}],
    )

    assert row["full_prompt"] is not None
    assert "hello" in row["full_prompt"]
