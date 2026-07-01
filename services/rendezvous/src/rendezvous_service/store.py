"""Persistence for meet state (this service's own Postgres — never the brain's DBs).

``Store`` ABC with ``PgStore`` (asyncpg) and ``InMemoryStore`` (tests, no infra). A meet is
saved whole (``save_meet``) — the lifecycle/callbacks load a Meet, ``dataclasses.replace`` the
changed fields, and save it back — so there's exactly one write path and no per-column SQL.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import replace
from datetime import UTC, datetime

from rendezvous_service.models import Meet, MeetStatus

_COLS = (
    "id, user_a, user_b, desc_a, desc_b, status, pref_a, pref_b, pref_asked_a, pref_asked_b, "
    "plan, confirm_a, confirm_b, confirm_asked_a, confirm_asked_b, followup_a, followup_b, "
    "followup_asked_a, followup_asked_b, created_at, updated_at"
)


def now_utc() -> datetime:
    return datetime.now(UTC)


class Store(ABC):
    @abstractmethod
    async def save_meet(self, meet: Meet) -> None:
        """Insert or fully replace a meet (upsert on id)."""

    @abstractmethod
    async def get_meet(self, meet_id: str) -> Meet | None: ...

    @abstractmethod
    async def get_active_meets(self) -> list[Meet]:
        """Meets still in flight (coordinating/confirming/confirmed) — read by the advance pass."""

    @abstractmethod
    async def delete_user(self, user_id: str) -> None:
        """Erase every meet the user is in (full erasure — the counterpart's row goes too)."""


class InMemoryStore(Store):
    def __init__(self) -> None:
        self._meets: dict[str, Meet] = {}

    async def save_meet(self, meet: Meet) -> None:
        stamped = replace(meet, created_at=meet.created_at or now_utc(), updated_at=now_utc())
        self._meets[meet.id] = stamped

    async def get_meet(self, meet_id: str) -> Meet | None:
        return self._meets.get(meet_id)

    async def get_active_meets(self) -> list[Meet]:
        return [m for m in self._meets.values() if m.status.is_active]

    async def delete_user(self, user_id: str) -> None:
        self._meets = {
            mid: m for mid, m in self._meets.items() if user_id not in (m.user_a, m.user_b)
        }


class PgStore(Store):
    def __init__(self, pool) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, database_url: str) -> PgStore:
        import asyncpg

        pool = await asyncpg.create_pool(database_url)
        store = cls(pool)
        await store.init_db()
        return store

    async def init_db(self) -> None:
        from importlib import resources

        ddl = resources.files("rendezvous_service").joinpath("schema.sql").read_text("utf-8")
        async with self._pool.acquire() as conn:
            await conn.execute(ddl)

    async def aclose(self) -> None:
        await self._pool.close()

    @staticmethod
    def _meet(r) -> Meet:
        return Meet(
            id=r["id"],
            user_a=r["user_a"],
            user_b=r["user_b"],
            desc_a=r["desc_a"],
            desc_b=r["desc_b"],
            status=MeetStatus(r["status"]),
            pref_a=r["pref_a"],
            pref_b=r["pref_b"],
            pref_asked_a=r["pref_asked_a"],
            pref_asked_b=r["pref_asked_b"],
            plan=r["plan"],
            confirm_a=r["confirm_a"],
            confirm_b=r["confirm_b"],
            confirm_asked_a=r["confirm_asked_a"],
            confirm_asked_b=r["confirm_asked_b"],
            followup_a=r["followup_a"],
            followup_b=r["followup_b"],
            followup_asked_a=r["followup_asked_a"],
            followup_asked_b=r["followup_asked_b"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )

    async def save_meet(self, meet: Meet) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO meets (id, user_a, user_b, desc_a, desc_b, status, pref_a, pref_b, "
                "pref_asked_a, pref_asked_b, plan, confirm_a, confirm_b, confirm_asked_a, "
                "confirm_asked_b, followup_a, followup_b, followup_asked_a, followup_asked_b, "
                "created_at, updated_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,"
                "$15,$16,$17,$18,$19, COALESCE($20, now()), now()) "
                "ON CONFLICT (id) DO UPDATE SET status=excluded.status, pref_a=excluded.pref_a, "
                "pref_b=excluded.pref_b, pref_asked_a=excluded.pref_asked_a, "
                "pref_asked_b=excluded.pref_asked_b, plan=excluded.plan, "
                "confirm_a=excluded.confirm_a, confirm_b=excluded.confirm_b, "
                "confirm_asked_a=excluded.confirm_asked_a, "
                "confirm_asked_b=excluded.confirm_asked_b, "
                "followup_a=excluded.followup_a, followup_b=excluded.followup_b, "
                "followup_asked_a=excluded.followup_asked_a, "
                "followup_asked_b=excluded.followup_asked_b, updated_at=now()",
                meet.id,
                meet.user_a,
                meet.user_b,
                meet.desc_a,
                meet.desc_b,
                str(meet.status),
                meet.pref_a,
                meet.pref_b,
                meet.pref_asked_a,
                meet.pref_asked_b,
                meet.plan,
                meet.confirm_a,
                meet.confirm_b,
                meet.confirm_asked_a,
                meet.confirm_asked_b,
                meet.followup_a,
                meet.followup_b,
                meet.followup_asked_a,
                meet.followup_asked_b,
                meet.created_at,
            )

    async def get_meet(self, meet_id: str) -> Meet | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(f"SELECT {_COLS} FROM meets WHERE id = $1", meet_id)
        return self._meet(row) if row is not None else None

    async def get_active_meets(self) -> list[Meet]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {_COLS} FROM meets "
                "WHERE status IN ('coordinating', 'confirming', 'confirmed') ORDER BY created_at"
            )
        return [self._meet(r) for r in rows]

    async def delete_user(self, user_id: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("DELETE FROM meets WHERE user_a = $1 OR user_b = $1", user_id)
