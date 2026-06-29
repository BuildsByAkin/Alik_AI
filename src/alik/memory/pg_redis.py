"""Postgres + Redis implementation of ``Memory``.

This is the ONLY module that imports the database drivers.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from importlib import resources

import asyncpg
import redis.asyncio as redis

from alik.memory.base import Memory
from alik.models import (
    CheckinType,
    DimensionStatus,
    MemoryRecord,
    MemoryTier,
    PendingCheckin,
    ProfileDimension,
    ProvenanceRecord,
    RetrievedContext,
)


def _working_key(user_id: str, session_id: str) -> str:
    return f"working:{user_id}:{session_id}"


def _sessions_key(user_id: str) -> str:
    return f"sessions:{user_id}"


class PgRedisMemory(Memory):
    def __init__(
        self,
        pool: asyncpg.Pool,
        redis_client: redis.Redis,
        *,
        working_ttl_seconds: int,
        reflection_after_days: int = 30,
    ) -> None:
        self._pool = pool
        self._redis = redis_client
        self._ttl = working_ttl_seconds
        self._reflection_after_days = reflection_after_days

    @classmethod
    async def connect(
        cls,
        *,
        database_url: str,
        redis_url: str,
        working_ttl_seconds: int,
        reflection_after_days: int = 30,
    ) -> PgRedisMemory:
        pool = await asyncpg.create_pool(database_url)
        redis_client = redis.from_url(redis_url, decode_responses=True)
        mem = cls(
            pool,
            redis_client,
            working_ttl_seconds=working_ttl_seconds,
            reflection_after_days=reflection_after_days,
        )
        await mem.init_db()
        return mem

    async def init_db(self) -> None:
        ddl = resources.files("alik.memory").joinpath("schema.sql").read_text(encoding="utf-8")
        async with self._pool.acquire() as conn:
            await conn.execute(ddl)

    async def aclose(self) -> None:
        await self._pool.close()
        await self._redis.aclose()

    async def write(self, record: MemoryRecord) -> None:
        if record.tier is MemoryTier.WORKING:
            await self._write_working(record)
        else:
            await self._write_episodic(record)

    async def _write_working(self, record: MemoryRecord) -> None:
        payload = json.dumps(
            {
                "role": record.role,
                "content": record.content,
                "ts": (record.created_at or datetime.now(UTC)).isoformat(),
            }
        )
        wkey = _working_key(record.user_id, record.session_id)
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.rpush(wkey, payload)
            pipe.expire(wkey, self._ttl)
            pipe.sadd(_sessions_key(record.user_id), record.session_id)
            await pipe.execute()

    async def _write_episodic(self, record: MemoryRecord) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO episodic_memory (user_id, session_id, summary) VALUES ($1, $2, $3)",
                record.user_id,
                record.session_id,
                record.content,
            )

    async def retrieve(
        self,
        user_id: str,
        session_id: str | None = None,
        *,
        episode_limit: int = 10,
    ) -> RetrievedContext:
        async with self._pool.acquire() as conn:
            earliest = await conn.fetchval(
                "SELECT min(created_at) FROM episodic_memory WHERE user_id = $1", user_id
            )
            reflection = await conn.fetchval(
                "SELECT content FROM reflections WHERE user_id = $1 "
                "ORDER BY generated_at DESC LIMIT 1",
                user_id,
            )
            # For established users (account 30+ days old) a reflection replaces the
            # full episodic list to keep the prompt lean; new users still get episodes.
            use_reflection = (
                reflection is not None
                and earliest is not None
                and datetime.now(UTC) - earliest >= timedelta(days=self._reflection_after_days)
            )
            rows = (
                []
                if use_reflection
                else await conn.fetch(
                    "SELECT user_id, session_id, summary, created_at FROM episodic_memory "
                    "WHERE user_id = $1 AND decayed_at IS NULL "
                    "ORDER BY created_at DESC LIMIT $2",
                    user_id,
                    episode_limit,
                )
            )
        episodes = [
            MemoryRecord(
                user_id=r["user_id"],
                session_id=r["session_id"],
                tier=MemoryTier.EPISODIC,
                content=r["summary"],
                created_at=r["created_at"],
            )
            for r in reversed(rows)  # recent N, returned oldest -> newest for the prompt
        ]

        working: list[MemoryRecord] = []
        if session_id is not None:
            raw = await self._redis.lrange(_working_key(user_id, session_id), 0, -1)
            for item in raw:
                turn = json.loads(item)
                working.append(
                    MemoryRecord(
                        user_id=user_id,
                        session_id=session_id,
                        tier=MemoryTier.WORKING,
                        content=turn["content"],
                        role=turn["role"],
                        created_at=datetime.fromisoformat(turn["ts"]),
                    )
                )
        return RetrievedContext(
            episodes=episodes,
            working=working,
            reflection=reflection if use_reflection else None,
        )

    async def invalidate(self, user_id: str, session_id: str) -> None:
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.delete(_working_key(user_id, session_id))
            pipe.srem(_sessions_key(user_id), session_id)
            await pipe.execute()

    async def delete(self, user_id: str) -> None:
        # Redis: clear every working buffer this user has, then the session index.
        session_ids = await self._redis.smembers(_sessions_key(user_id))
        keys = [_working_key(user_id, sid) for sid in session_ids]
        keys.append(_sessions_key(user_id))
        await self._redis.delete(*keys)
        # Postgres: erase all episodic memory, reflections, and queued check-ins.
        async with self._pool.acquire() as conn:
            await conn.execute("DELETE FROM episodic_memory WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM reflections WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM pending_checkins WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM reflect_back_cooldown WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM profile_dimensions WHERE user_id = $1", user_id)

    # --- Phase 3: episodic lifecycle ------------------------------------------

    @staticmethod
    def _episode(r: asyncpg.Record) -> MemoryRecord:
        return MemoryRecord(
            user_id=r["user_id"],
            session_id=r["session_id"],
            tier=MemoryTier.EPISODIC,
            content=r["summary"],
            created_at=r["created_at"],
            id=str(r["id"]),
        )

    async def get_active_users(self, *, within_days: int = 30) -> list[str]:
        cutoff = datetime.now(UTC) - timedelta(days=within_days)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT user_id FROM episodic_memory WHERE created_at >= $1", cutoff
            )
        return [r["user_id"] for r in rows]

    async def get_recent_episodes(self, user_id: str, *, days: int = 7) -> list[MemoryRecord]:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, user_id, session_id, summary, created_at FROM episodic_memory "
                "WHERE user_id = $1 AND created_at >= $2 "
                "AND decayed_at IS NULL AND promoted = false "
                "ORDER BY created_at ASC",
                user_id,
                cutoff,
            )
        return [self._episode(r) for r in rows]

    async def get_promoted_episodes(self, user_id: str, *, limit: int = 20) -> list[MemoryRecord]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, user_id, session_id, summary, created_at FROM episodic_memory "
                "WHERE user_id = $1 AND promoted = true AND decayed_at IS NULL "
                "ORDER BY created_at DESC LIMIT $2",
                user_id,
                limit,
            )
        return [self._episode(r) for r in rows]

    async def promote_episode(self, episode_id: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE episodic_memory SET promoted = true WHERE id = $1::uuid", episode_id
            )

    async def decay_episodes(self, user_id: str, *, older_than_days: int = 30) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "UPDATE episodic_memory SET decayed_at = now() "
                "WHERE user_id = $1 AND created_at < $2 "
                "AND promoted = false AND decayed_at IS NULL "
                "RETURNING id",
                user_id,
                cutoff,
            )
        return len(rows)

    async def save_reflection(self, user_id: str, content: str) -> None:
        # At most one reflection per user per UTC day: a same-day re-run replaces it.
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "DELETE FROM reflections WHERE user_id = $1 "
                "AND generated_at >= date_trunc('day', now() AT TIME ZONE 'UTC')",
                user_id,
            )
            await conn.execute(
                "INSERT INTO reflections (user_id, content) VALUES ($1, $2)", user_id, content
            )

    async def get_reflection(self, user_id: str) -> str | None:
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT content FROM reflections WHERE user_id = $1 "
                "ORDER BY generated_at DESC LIMIT 1",
                user_id,
            )

    # --- Phase 5: proactive check-in queue ------------------------------------

    async def queue_checkin(self, checkin: PendingCheckin) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO pending_checkins "
                "(user_id, commitment_id, checkin_type, message_hint) VALUES ($1, $2, $3, $4)",
                checkin.user_id,
                checkin.commitment_id,
                str(checkin.checkin_type),
                checkin.message_hint,
            )

    async def get_pending_checkin(self, user_id: str) -> PendingCheckin | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, user_id, commitment_id, checkin_type, message_hint, "
                "created_at, delivered_at FROM pending_checkins "
                "WHERE user_id = $1 AND delivered_at IS NULL "
                "ORDER BY created_at DESC LIMIT 1",
                user_id,
            )
        if row is None:
            return None
        return PendingCheckin(
            user_id=row["user_id"],
            checkin_type=CheckinType(row["checkin_type"]),
            message_hint=row["message_hint"],
            commitment_id=row["commitment_id"],
            created_at=row["created_at"],
            delivered_at=row["delivered_at"],
            id=str(row["id"]),
        )

    async def mark_checkin_delivered(self, checkin_id: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE pending_checkins SET delivered_at = now() WHERE id = $1::uuid", checkin_id
            )

    async def get_last_session_at(self, user_id: str) -> datetime | None:
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT max(created_at) FROM episodic_memory WHERE user_id = $1", user_id
            )

    # --- Phase 5.2: reflect-back cadence cooldown -----------------------------

    async def reflect_back_ready(self, user_id: str) -> bool:
        async with self._pool.acquire() as conn:
            remaining = await conn.fetchval(
                "SELECT remaining FROM reflect_back_cooldown WHERE user_id = $1", user_id
            )
        return not remaining  # None (no row) or 0 -> ready

    async def set_reflect_back_cooldown(self, user_id: str, sessions: int) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO reflect_back_cooldown (user_id, remaining) VALUES ($1, $2) "
                "ON CONFLICT (user_id) DO UPDATE SET remaining = $2",
                user_id,
                sessions,
            )

    async def decrement_reflect_back_cooldown(self, user_id: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE reflect_back_cooldown SET remaining = GREATEST(remaining - 1, 0) "
                "WHERE user_id = $1",
                user_id,
            )

    # --- Living profile: behavioral dimensions --------------------------------

    _DIM_COLS = (
        "id, user_id, dimension, value, content, confidence, observation_count, status, "
        "surfaced_in_session, source_session_id, provenance, valid_from, last_observed_at, "
        "updated_at"
    )

    @staticmethod
    def _profile_dim(row: asyncpg.Record) -> ProfileDimension:
        prov = row["provenance"]
        if isinstance(prov, str):
            prov = json.loads(prov) if prov else {}
        prov = prov or {}
        return ProfileDimension(
            user_id=row["user_id"],
            dimension=row["dimension"],
            value=row["value"],
            content=row["content"],
            confidence=row["confidence"],
            valid_from=row["valid_from"],
            updated_at=row["updated_at"],
            provenance=ProvenanceRecord(
                episode_ids=list(prov.get("episode_ids", [])),
                signal_ids=list(prov.get("signal_ids", [])),
            ),
            observation_count=row["observation_count"],
            status=DimensionStatus(row["status"]),
            surfaced_in_session=row["surfaced_in_session"],
            source_session_id=row["source_session_id"],
            last_observed_at=row["last_observed_at"],
            id=str(row["id"]),
        )

    async def get_profile_dimensions(self, user_id: str) -> list[ProfileDimension]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {self._DIM_COLS} FROM profile_dimensions WHERE user_id = $1 "
                "ORDER BY dimension",
                user_id,
            )
        return [self._profile_dim(r) for r in rows]

    async def put_profile_dimension(self, dimension: ProfileDimension) -> None:
        provenance = json.dumps(
            {
                "episode_ids": list(dimension.provenance.episode_ids),
                "signal_ids": list(dimension.provenance.signal_ids),
            }
        )
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO profile_dimensions "
                "(user_id, dimension, value, content, confidence, observation_count, status, "
                " surfaced_in_session, source_session_id, provenance, valid_from, "
                " last_observed_at, updated_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11, $12, now()) "
                "ON CONFLICT (user_id, dimension) DO UPDATE SET "
                "  value = excluded.value, content = excluded.content, "
                "  confidence = excluded.confidence, "
                "  observation_count = excluded.observation_count, status = excluded.status, "
                "  surfaced_in_session = excluded.surfaced_in_session, "
                "  source_session_id = excluded.source_session_id, "
                "  provenance = excluded.provenance, "
                "  last_observed_at = excluded.last_observed_at, updated_at = now()",
                dimension.user_id,
                dimension.dimension,
                dimension.value,
                dimension.content,
                dimension.confidence,
                dimension.observation_count,
                str(dimension.status),
                dimension.surfaced_in_session,
                dimension.source_session_id,
                provenance,
                dimension.valid_from,
                dimension.last_observed_at or dimension.valid_from,
            )

    async def get_dimension_to_confirm(
        self, user_id: str, session_id: str, *, min_confidence: float, min_observations: int
    ) -> ProfileDimension | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {self._DIM_COLS} FROM profile_dimensions "
                "WHERE user_id = $1 AND status = 'unconfirmed' AND confidence >= $2 "
                "AND observation_count >= $3 "
                "AND (surfaced_in_session IS NULL OR surfaced_in_session <> $4) "
                "ORDER BY confidence DESC, observation_count DESC LIMIT 1",
                user_id,
                min_confidence,
                min_observations,
                session_id,
            )
        return self._profile_dim(row) if row is not None else None

    async def confirm_dimension(
        self, user_id: str, dimension: str, *, confidence_bump: float, session_id: str | None = None
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE profile_dimensions SET status = 'confirmed', "
                "confidence = LEAST(confidence + $3, 1.0), "
                "source_session_id = COALESCE($4, source_session_id), updated_at = now() "
                "WHERE user_id = $1 AND dimension = $2",
                user_id,
                dimension,
                confidence_bump,
                session_id,
            )

    async def correct_dimension(
        self, user_id: str, dimension: str, *, session_id: str | None = None
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE profile_dimensions SET status = 'corrected', "
                "source_session_id = COALESCE($3, source_session_id), updated_at = now() "
                "WHERE user_id = $1 AND dimension = $2",
                user_id,
                dimension,
                session_id,
            )

    async def mark_dimension_surfaced(self, user_id: str, dimension: str, session_id: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE profile_dimensions SET surfaced_in_session = $3, updated_at = now() "
                "WHERE user_id = $1 AND dimension = $2",
                user_id,
                dimension,
                session_id,
            )
