"""llm_usage_log: per-request LLM token and cost accounting

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-23

Adds a dedicated append-only table for DeepSeek API usage. One row is
written per DeepSeek request from either LLM call site: the Discord bot
tool loop or the nightly narrative job.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_usage_log",
        sa.Column(
            "request_id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("discord_user_id", sa.Text),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("prompt_tokens", sa.Integer, nullable=False),
        sa.Column("completion_tokens", sa.Integer, nullable=False),
        sa.Column("total_tokens", sa.Integer, nullable=False),
        sa.Column("prompt_cache_hit_tokens", sa.Integer),
        sa.Column("prompt_cache_miss_tokens", sa.Integer),
        sa.Column(
            "input_cache_hit_price_per_million",
            sa.Numeric(14, 8),
            nullable=False,
        ),
        sa.Column(
            "input_cache_miss_price_per_million",
            sa.Numeric(14, 8),
            nullable=False,
        ),
        sa.Column("output_price_per_million", sa.Numeric(14, 8), nullable=False),
        sa.Column("cost_usd", sa.Numeric(12, 8), nullable=False),
        sa.Column("prompt_sha256", sa.String(64), nullable=False),
        sa.Column("prompt_preview", sa.Text, nullable=False),
        sa.Column("full_prompt", sa.Text),
        sa.Column("raw_usage", JSONB),
    )
    op.create_index(
        "ix_llm_usage_log_created_at",
        "llm_usage_log",
        ["created_at"],
    )
    op.create_index(
        "ix_llm_usage_log_discord_user_created",
        "llm_usage_log",
        ["discord_user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_llm_usage_log_discord_user_created", table_name="llm_usage_log")
    op.drop_index("ix_llm_usage_log_created_at", table_name="llm_usage_log")
    op.drop_table("llm_usage_log")
