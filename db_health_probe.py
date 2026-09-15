"""Bounded, payload-free database readiness probe for the watchdog."""

from __future__ import annotations

import asyncio

import asyncpg

from db_config import local_dsn


CONNECT_TIMEOUT_SEC = 5.0
QUERY_TIMEOUT_SEC = 2.0


async def database_query_is_ready() -> bool:
    connection = None
    try:
        connection = await asyncio.wait_for(
            asyncpg.connect(local_dsn(), timeout=CONNECT_TIMEOUT_SEC),
            timeout=CONNECT_TIMEOUT_SEC + 1.0,
        )
        await asyncio.wait_for(
            connection.execute("SELECT 1"),
            timeout=QUERY_TIMEOUT_SEC,
        )
        return True
    except Exception:
        return False
    finally:
        if connection is not None:
            try:
                await connection.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(database_query_is_ready()) else 1)
