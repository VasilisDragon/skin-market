"""Database connection.

Reads ``DATABASE_URL`` from the environment (loaded from ``.env`` if present)
and returns a singleton SQLAlchemy engine. All DB access throughout the project
goes through ``get_engine()`` so connection settings live in exactly one place.
"""

from __future__ import annotations

import os
from functools import lru_cache

from dotenv import load_dotenv
from sqlalchemy import Engine, create_engine

load_dotenv()


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Return a SQLAlchemy engine configured from ``DATABASE_URL``."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL environment variable is not set. "
            "Copy .env.example to .env and fill it in."
        )
    return create_engine(url, pool_pre_ping=True, future=True)
