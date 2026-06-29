"""Persistence for the recommendation log + per-user engagement flag.

A ``Store`` ABC with two implementations: ``PgStore`` (asyncpg, this service's own
Postgres) and ``InMemoryStore`` (tests, no infra). Mirrors the pattern the brain uses for
its ``Memory`` seam. All reads return ``Recommendation`` objects, newest-first where listed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib import resources

from matching_service.models import JobOutcome, Recommendation


class Store(ABC):
    @abstractmethod
    async def log_recommendation(
        self, user_id: str, job_id: str, *, follow_up_after_days: int
    ) -> str:
        """Insert an open recommendation (follow_up_after = now + N days). Returns the id."""

    @abstractmethod
    async def get_recommended_job_ids(self, user_id: str) -> list[str]:
        """Job ids already recommended to this user (dedup — never repeat a job)."""

    @abstractmethod
    async def get_recommendations(self, user_id: str) -> list[Recommendation]:
        """All of the user's recommendation rows, newest first (drives gating/cooldowns)."""

    @abstractmethod
    async def open_undelivered(self, user_id: str) -> Recommendation | None:
        """The open recommendation not yet shown to the user (for delivery setup)."""

    @abstractmethod
    async def mark_delivered(self, rec_id: str) -> None: ...

    @abstractmethod
    async def due_followup(self, user_id: str) -> Recommendation | None:
        """A delivered recommendation past follow_up_after with no follow-up sent yet."""

    @abstractmethod
    async def mark_followup_sent(self, rec_id: str) -> None: ...

    @abstractmethod
    async def pending_followup(self, user_id: str) -> Recommendation | None:
        """The recommendation whose follow-up was queued and awaits an outcome."""

    @abstractmethod
    async def set_outcome(self, rec_id: str, outcome: JobOutcome) -> None: ...

    @abstractmethod
    async def set_job_active(self, user_id: str, active: bool) -> None: ...

    @abstractmethod
    async def get_job_active(self, user_id: str) -> bool: ...

    @abstractmethod
    async def delete_user(self, user_id: str) -> None:
        """Erase all of this service's data for the user (cross-service deletion)."""


class InMemoryStore(Store):
    """Infra-free double mirroring PgStore semantics (used by the tests)."""

    def __init__(self) -> None:
        self._recs: list[Recommendation] = []
        self._active: dict[str, bool] = {}

    def _user(self, user_id: str) -> list[Recommendation]:
        recs = [r for r in self._recs if r.user_id == user_id]
        recs.sort(key=lambda r: r.recommended_at, reverse=True)
        return recs

    def _replace(self, rec_id: str, **changes) -> None:
        self._recs = [replace(r, **changes) if r.id == rec_id else r for r in self._recs]

    async def log_recommendation(
        self, user_id: str, job_id: str, *, follow_up_after_days: int
    ) -> str:
        now = datetime.now(UTC)
        rec = Recommendation(
            user_id=user_id,
            job_id=job_id,
            recommended_at=now,
            follow_up_after=now + timedelta(days=follow_up_after_days),
        )
        self._recs.append(rec)
        return rec.id

    async def get_recommended_job_ids(self, user_id: str) -> list[str]:
        return list({r.job_id for r in self._recs if r.user_id == user_id})

    async def get_recommendations(self, user_id: str) -> list[Recommendation]:
        return self._user(user_id)

    async def open_undelivered(self, user_id: str) -> Recommendation | None:
        return next(
            (r for r in self._user(user_id) if r.outcome is None and r.delivered_at is None), None
        )

    async def mark_delivered(self, rec_id: str) -> None:
        self._replace(rec_id, delivered_at=datetime.now(UTC))

    async def due_followup(self, user_id: str) -> Recommendation | None:
        now = datetime.now(UTC)
        due = [
            r
            for r in self._user(user_id)
            if r.delivered_at is not None
            and r.follow_up_after is not None
            and r.follow_up_after < now
            and r.follow_up_sent_at is None
            and r.outcome is None
        ]
        due.sort(key=lambda r: r.recommended_at)
        return due[0] if due else None

    async def mark_followup_sent(self, rec_id: str) -> None:
        self._replace(rec_id, follow_up_sent_at=datetime.now(UTC))

    async def pending_followup(self, user_id: str) -> Recommendation | None:
        pending = [
            r for r in self._user(user_id) if r.follow_up_sent_at is not None and r.outcome is None
        ]
        return pending[0] if pending else None

    async def set_outcome(self, rec_id: str, outcome: JobOutcome) -> None:
        self._replace(rec_id, outcome=outcome)

    async def set_job_active(self, user_id: str, active: bool) -> None:
        self._active[user_id] = active

    async def get_job_active(self, user_id: str) -> bool:
        return self._active.get(user_id, False)

    async def delete_user(self, user_id: str) -> None:
        self._recs = [r for r in self._recs if r.user_id != user_id]
        self._active.pop(user_id, None)


class PgStore(Store):
    """asyncpg-backed store over this service's own Postgres."""

    _COLS = (
        "id, user_id, job_id, recommended_at, delivered_at, "
        "follow_up_after, follow_up_sent_at, outcome"
    )

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
        ddl = resources.files("matching_service").joinpath("schema.sql").read_text(encoding="utf-8")
        async with self._pool.acquire() as conn:
            await conn.execute(ddl)

    async def aclose(self) -> None:
        await self._pool.close()

    @staticmethod
    def _rec(row) -> Recommendation:
        return Recommendation(
            user_id=row["user_id"],
            job_id=row["job_id"],
            recommended_at=row["recommended_at"],
            delivered_at=row["delivered_at"],
            follow_up_after=row["follow_up_after"],
            follow_up_sent_at=row["follow_up_sent_at"],
            outcome=JobOutcome(row["outcome"]) if row["outcome"] else None,
            id=str(row["id"]),
        )

    async def log_recommendation(
        self, user_id: str, job_id: str, *, follow_up_after_days: int
    ) -> str:
        async with self._pool.acquire() as conn:
            rec_id = await conn.fetchval(
                "INSERT INTO job_recommendations_log (user_id, job_id, follow_up_after) "
                "VALUES ($1, $2, now() + ($3 || ' days')::interval) RETURNING id",
                user_id,
                job_id,
                str(follow_up_after_days),
            )
        return str(rec_id)

    async def get_recommended_job_ids(self, user_id: str) -> list[str]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT job_id FROM job_recommendations_log WHERE user_id = $1", user_id
            )
        return [r["job_id"] for r in rows]

    async def get_recommendations(self, user_id: str) -> list[Recommendation]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {self._COLS} FROM job_recommendations_log "
                "WHERE user_id = $1 ORDER BY recommended_at DESC",
                user_id,
            )
        return [self._rec(r) for r in rows]

    async def open_undelivered(self, user_id: str) -> Recommendation | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {self._COLS} FROM job_recommendations_log "
                "WHERE user_id = $1 AND delivered_at IS NULL AND outcome IS NULL "
                "ORDER BY recommended_at DESC LIMIT 1",
                user_id,
            )
        return self._rec(row) if row is not None else None

    async def mark_delivered(self, rec_id: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE job_recommendations_log SET delivered_at = now() "
                "WHERE id = $1::uuid AND delivered_at IS NULL",
                rec_id,
            )

    async def due_followup(self, user_id: str) -> Recommendation | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {self._COLS} FROM job_recommendations_log "
                "WHERE user_id = $1 AND delivered_at IS NOT NULL AND follow_up_after < now() "
                "AND follow_up_sent_at IS NULL AND outcome IS NULL "
                "ORDER BY recommended_at ASC LIMIT 1",
                user_id,
            )
        return self._rec(row) if row is not None else None

    async def mark_followup_sent(self, rec_id: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE job_recommendations_log SET follow_up_sent_at = now() WHERE id = $1::uuid",
                rec_id,
            )

    async def pending_followup(self, user_id: str) -> Recommendation | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {self._COLS} FROM job_recommendations_log "
                "WHERE user_id = $1 AND follow_up_sent_at IS NOT NULL AND outcome IS NULL "
                "ORDER BY recommended_at DESC LIMIT 1",
                user_id,
            )
        return self._rec(row) if row is not None else None

    async def set_outcome(self, rec_id: str, outcome: JobOutcome) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE job_recommendations_log SET outcome = $2 WHERE id = $1::uuid",
                rec_id,
                str(outcome),
            )

    async def set_job_active(self, user_id: str, active: bool) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO user_job_state (user_id, job_active) VALUES ($1, $2) "
                "ON CONFLICT (user_id) DO UPDATE SET job_active = $2",
                user_id,
                active,
            )

    async def get_job_active(self, user_id: str) -> bool:
        async with self._pool.acquire() as conn:
            val = await conn.fetchval(
                "SELECT job_active FROM user_job_state WHERE user_id = $1", user_id
            )
        return bool(val)

    async def delete_user(self, user_id: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("DELETE FROM job_recommendations_log WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM user_job_state WHERE user_id = $1", user_id)
