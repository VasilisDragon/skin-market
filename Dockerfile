# syntax=docker/dockerfile:1.7
#
# Image for the v1 collector scheduler service. Single-stage; uv-managed
# dependencies; runs as a non-root user; entrypoint is the scheduler module.

FROM python:3.12-slim

# uv binary from Astral's official multi-arch image (amd64 + aarch64).
COPY --from=ghcr.io/astral-sh/uv:0.11.13 /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Install dependencies in a dedicated layer for build-cache stability:
# code changes don't invalidate the (slow) dep install.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Application code. Listing dirs explicitly so adding e.g. a new top-level
# directory doesn't silently sneak into the image without a Dockerfile change.
COPY collectors/ ./collectors/
COPY db/ ./db/
COPY data/ ./data/
COPY scripts/ ./scripts/
COPY analytics/ ./analytics/
COPY api/ ./api/
COPY alembic.ini ./

# Drop root for runtime. Image is then immutable — the container can't
# write to /app (good for a stateless service whose state lives in DB).
RUN useradd --create-home --uid 1000 collector \
 && chown -R collector:collector /app
USER collector

CMD ["python", "-m", "collectors.scheduler"]
