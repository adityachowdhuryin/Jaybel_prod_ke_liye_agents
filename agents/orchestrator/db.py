"""asyncpg pool for orchestrator chat persistence."""

from __future__ import annotations

import logging
import os

import asyncpg

logger = logging.getLogger("orchestrator.db")

_pool: asyncpg.Pool | None = None


async def init_db() -> None:
    global _pool
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL is required for orchestrator chat persistence "
            "(same Postgres as cost agent / docker compose postgres service)."
        )
    if _pool is not None:
        return
    _pool = await asyncpg.create_pool(dsn, min_size=1, max_size=10)
    logger.info("Orchestrator DB pool initialized")


async def close_db() -> None:
    global _pool
    if _pool is None:
        return
    await _pool.close()
    _pool = None
    logger.info("Orchestrator DB pool closed")


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool not initialized (lifespan init_db failed?)")
    return _pool


async def check_db() -> bool:
    """Return True if a simple query succeeds."""
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return True
    except Exception:
        return False
