"""Dump every connections-service table to JSON for the review report.

Pure asyncpg (no connections_service import needed) so it can run from any venv that has
asyncpg. Reads the DB URL from CONN_DB_URL (default: the docker-compose connections Postgres).

  CONN_DB_URL=postgresql://alik:alik@localhost:5434/connections \
      uv run --directory services/connections python scripts/connections_stress/dump_connections.py
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import asyncpg

OUT = (
    Path(__file__).resolve().parent.parent.parent / "output" / "connections_stress" / "_connections"
)
DB_URL = os.environ.get("CONN_DB_URL", "postgresql://alik:alik@localhost:5434/connections")

TABLES = [
    "users_pool",
    "user_interests",
    "profile_dimensions",
    "candidate_scores",
    "eval_results",
    "match_state",
    "group_candidates",
]


def _jsonable(value):
    # asyncpg returns jsonb as str; leave it, the report re-parses. Datetimes -> str via default.
    return value


async def _run() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    conn = await asyncpg.connect(DB_URL)
    try:
        summary = {}
        for table in TABLES:
            try:
                rows = await conn.fetch(f"SELECT * FROM {table} ORDER BY 1")
            except Exception as exc:  # table may not exist yet on a fresh DB
                print(f"  {table}: SKIP ({exc})")
                continue
            data = [{k: _jsonable(v) for k, v in dict(r).items()} for r in rows]
            with (OUT / f"{table}.json").open("w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, default=str, ensure_ascii=False)
            summary[table] = len(data)
            print(f"  {table}: {len(data)} rows")
        with (OUT / "_row_counts.json").open("w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(_run())
