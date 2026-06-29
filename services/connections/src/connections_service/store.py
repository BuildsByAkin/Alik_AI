"""Persistence for all match state (this service's own Postgres — never the brain's DBs).

A ``Store`` ABC with ``PgStore`` (asyncpg) and ``InMemoryStore`` (tests, no infra). Part 2
adds the ingested snapshot + people<->interest graph. The interest taxonomy is seeded via
``ensure_interest_nodes`` (single source of truth in ``interests.py``); ``InMemoryStore``
self-seeds on construction for test convenience.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import replace
from datetime import UTC, datetime
from importlib import resources

from connections_service import interests
from connections_service.models import (
    CandidateScore,
    DimensionMatch,
    DimensionSnapshot,
    InterestEdge,
    InterestMatch,
    InterestNode,
    KernelExplanation,
    SharedInterests,
    UserPoolEntry,
)


def explanation_to_json(exp: KernelExplanation) -> dict:
    """Serialize a KernelExplanation to the stored jsonb shape (pure; PgStore + tests use it)."""
    return {
        "interest": {
            "specific": [
                {
                    "node": m.node_id,
                    "broad": m.broad_category,
                    "specific": m.specific_interest,
                    "wA": m.weight_a,
                    "wB": m.weight_b,
                }
                for m in exp.interest_specific
            ],
            "broad": list(exp.interest_broad),
            "match_type": exp.match_type,
        },
        "dimensions": [
            {
                "axis": d.axis,
                "valueA": d.value_a,
                "valueB": d.value_b,
                "score": d.axis_score,
                "mode": d.scoring_mode,
            }
            for d in exp.dimensions
        ],
        "values": {"shared_causes": list(exp.values_causes)},
    }


def explanation_from_json(d: dict) -> KernelExplanation:
    interest = d.get("interest", {})
    return KernelExplanation(
        interest_specific=[
            InterestMatch(m["node"], m["broad"], m["specific"], m["wA"], m["wB"])
            for m in interest.get("specific", [])
        ],
        interest_broad=list(interest.get("broad", [])),
        dimensions=[
            DimensionMatch(x["axis"], x["valueA"], x["valueB"], x["score"], x["mode"])
            for x in d.get("dimensions", [])
        ],
        values_causes=list(d.get("values", {}).get("shared_causes", [])),
        match_type=interest.get("match_type", "none"),
    )


class Store(ABC):
    @abstractmethod
    async def ensure_interest_nodes(self, nodes: list[InterestNode]) -> None:
        """Idempotently seed/refresh the interest taxonomy (called at startup)."""

    @abstractmethod
    async def upsert_user_pool(self, entry: UserPoolEntry) -> None: ...

    @abstractmethod
    async def get_pool_users(self, state: str) -> list[UserPoolEntry]:
        """All pool_ready users in ``state``."""

    @abstractmethod
    async def upsert_user_interests(self, user_id: str, interests: list[InterestEdge]) -> None:
        """Replace the user's full edge set (idempotent per ingest)."""

    @abstractmethod
    async def get_user_interests(self, user_id: str) -> list[InterestEdge]: ...

    @abstractmethod
    async def upsert_profile_dimensions(
        self, user_id: str, dimensions: list[DimensionSnapshot]
    ) -> None: ...

    @abstractmethod
    async def get_profile_dimensions(self, user_id: str) -> list[DimensionSnapshot]: ...

    @abstractmethod
    async def get_users_by_interest(self, interest_node_id: str, state: str) -> list[str]:
        """pool_ready user_ids in ``state`` with an edge to ``interest_node_id`` (Part-6 join)."""

    @abstractmethod
    async def get_shared_interests(self, a: str, b: str) -> SharedInterests:
        """Exact specific-node overlap + shared broad categories (a helper view)."""

    # --- Part 3: candidate scores -------------------------------------------------
    @abstractmethod
    async def save_candidate_score(self, score: CandidateScore) -> None:
        """Upsert a directed (A→B) score (replaced on each scoring run)."""

    @abstractmethod
    async def get_candidate_scores(self, user_id: str) -> list[CandidateScore]:
        """The user's candidates (user_id_a = user_id), highest score first."""

    @abstractmethod
    async def get_shown_user_ids(self, user_id: str) -> list[str]:
        """Users already shown/accepted/skipped — excluded from candidate generation.
        STUB in Part 3 (returns []); the real match_state lands in Part 5."""

    @abstractmethod
    async def delete_user(self, user_id: str) -> None:
        """Erase ALL of this service's data for the user (cross-service deletion seam)."""


class InMemoryStore(Store):
    """Infra-free double mirroring PgStore semantics. Self-seeds the taxonomy on construction."""

    def __init__(self) -> None:
        self._nodes: dict[str, InterestNode] = {}
        self._pool: dict[str, UserPoolEntry] = {}
        self._interests: dict[str, list[InterestEdge]] = {}
        self._dims: dict[str, list[DimensionSnapshot]] = {}
        self._candidates: dict[tuple[str, str], CandidateScore] = {}
        for node in interests.all_interest_nodes():
            self._nodes[node.id] = node

    async def ensure_interest_nodes(self, nodes: list[InterestNode]) -> None:
        for node in nodes:
            self._nodes[node.id] = node

    async def upsert_user_pool(self, entry: UserPoolEntry) -> None:
        self._pool[entry.user_id] = entry

    async def get_pool_users(self, state: str) -> list[UserPoolEntry]:
        return [e for e in self._pool.values() if e.pool_ready and e.state == state]

    async def upsert_user_interests(self, user_id: str, interests: list[InterestEdge]) -> None:
        self._interests[user_id] = list(interests)

    async def get_user_interests(self, user_id: str) -> list[InterestEdge]:
        return list(self._interests.get(user_id, []))

    async def upsert_profile_dimensions(
        self, user_id: str, dimensions: list[DimensionSnapshot]
    ) -> None:
        self._dims[user_id] = list(dimensions)

    async def get_profile_dimensions(self, user_id: str) -> list[DimensionSnapshot]:
        return list(self._dims.get(user_id, []))

    async def get_users_by_interest(self, interest_node_id: str, state: str) -> list[str]:
        out = []
        for uid, edges in self._interests.items():
            entry = self._pool.get(uid)
            if entry and entry.pool_ready and entry.state == state:
                if any(e.interest_node_id == interest_node_id for e in edges):
                    out.append(uid)
        return sorted(out)

    async def get_shared_interests(self, a: str, b: str) -> SharedInterests:
        a_ids = {e.interest_node_id for e in self._interests.get(a, [])}
        b_ids = {e.interest_node_id for e in self._interests.get(b, [])}
        specific = [self._nodes[i] for i in (a_ids & b_ids) if i in self._nodes]
        a_broad = {self._nodes[i].broad_category for i in a_ids if i in self._nodes}
        b_broad = {self._nodes[i].broad_category for i in b_ids if i in self._nodes}
        return SharedInterests(
            specific=sorted(specific, key=lambda n: n.id),
            broad=sorted(a_broad & b_broad),
        )

    async def save_candidate_score(self, score: CandidateScore) -> None:
        stamped = score if score.scored_at else replace(score, scored_at=datetime.now(UTC))
        self._candidates[(score.user_id_a, score.user_id_b)] = stamped

    async def get_candidate_scores(self, user_id: str) -> list[CandidateScore]:
        rows = [s for (a, _), s in self._candidates.items() if a == user_id]
        rows.sort(key=lambda s: (s.score, s.user_id_b), reverse=True)
        return rows

    async def get_shown_user_ids(self, user_id: str) -> list[str]:
        return []  # Part-5 match_state will implement this

    async def delete_user(self, user_id: str) -> None:
        self._pool.pop(user_id, None)
        self._interests.pop(user_id, None)
        self._dims.pop(user_id, None)
        self._candidates = {
            (a, b): s for (a, b), s in self._candidates.items() if user_id not in (a, b)
        }


class PgStore(Store):
    """asyncpg-backed store over this service's own Postgres."""

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
        ddl = (
            resources.files("connections_service")
            .joinpath("schema.sql")
            .read_text(encoding="utf-8")
        )
        has_sql = any(
            line.strip() and not line.strip().startswith("--") for line in ddl.splitlines()
        )
        if has_sql:
            async with self._pool.acquire() as conn:
                await conn.execute(ddl)

    async def aclose(self) -> None:
        await self._pool.close()

    async def ensure_interest_nodes(self, nodes: list[InterestNode]) -> None:
        async with self._pool.acquire() as conn:
            await conn.executemany(
                "INSERT INTO interest_nodes "
                "(id, broad_category, specific_interest, canonical_label) "
                "VALUES ($1, $2, $3, $4) ON CONFLICT (id) DO UPDATE SET "
                "broad_category = excluded.broad_category, "
                "specific_interest = excluded.specific_interest, "
                "canonical_label = excluded.canonical_label",
                [(n.id, n.broad_category, n.specific_interest, n.canonical_label) for n in nodes],
            )

    async def upsert_user_pool(self, entry: UserPoolEntry) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO users_pool (user_id, state, age, city, pool_ready, last_ingested_at) "
                "VALUES ($1, $2, $3, $4, $5, COALESCE($6, now())) "
                "ON CONFLICT (user_id) DO UPDATE SET state = excluded.state, age = excluded.age, "
                "city = excluded.city, pool_ready = excluded.pool_ready, "
                "last_ingested_at = excluded.last_ingested_at",
                entry.user_id,
                entry.state,
                entry.age,
                entry.city,
                entry.pool_ready,
                entry.last_ingested_at,
            )

    async def get_pool_users(self, state: str) -> list[UserPoolEntry]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT user_id, state, age, city, pool_ready, last_ingested_at FROM users_pool "
                "WHERE state = $1 AND pool_ready ORDER BY user_id",
                state,
            )
        return [
            UserPoolEntry(
                user_id=r["user_id"],
                state=r["state"],
                age=r["age"],
                city=r["city"],
                pool_ready=r["pool_ready"],
                last_ingested_at=r["last_ingested_at"],
            )
            for r in rows
        ]

    async def upsert_user_interests(self, user_id: str, interests: list[InterestEdge]) -> None:
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("DELETE FROM user_interests WHERE user_id = $1", user_id)
            if interests:
                await conn.executemany(
                    "INSERT INTO user_interests "
                    "(user_id, interest_node_id, weight, source_fact_key) VALUES ($1, $2, $3, $4)",
                    [(user_id, e.interest_node_id, e.weight, e.source_fact_key) for e in interests],
                )

    async def get_user_interests(self, user_id: str) -> list[InterestEdge]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT interest_node_id, weight, source_fact_key FROM user_interests "
                "WHERE user_id = $1",
                user_id,
            )
        return [
            InterestEdge(r["interest_node_id"], r["weight"], r["source_fact_key"]) for r in rows
        ]

    async def upsert_profile_dimensions(
        self, user_id: str, dimensions: list[DimensionSnapshot]
    ) -> None:
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("DELETE FROM profile_dimensions WHERE user_id = $1", user_id)
            if dimensions:
                await conn.executemany(
                    "INSERT INTO profile_dimensions "
                    "(user_id, dimension, value, confidence, status) VALUES ($1, $2, $3, $4, $5)",
                    [(user_id, d.dimension, d.value, d.confidence, d.status) for d in dimensions],
                )

    async def get_profile_dimensions(self, user_id: str) -> list[DimensionSnapshot]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT dimension, value, confidence, status FROM profile_dimensions "
                "WHERE user_id = $1",
                user_id,
            )
        return [
            DimensionSnapshot(r["dimension"], r["value"], r["confidence"], r["status"])
            for r in rows
        ]

    async def get_users_by_interest(self, interest_node_id: str, state: str) -> list[str]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT ui.user_id FROM user_interests ui "
                "JOIN users_pool p ON p.user_id = ui.user_id "
                "WHERE ui.interest_node_id = $1 AND p.state = $2 AND p.pool_ready "
                "ORDER BY ui.user_id",
                interest_node_id,
                state,
            )
        return [r["user_id"] for r in rows]

    async def get_shared_interests(self, a: str, b: str) -> SharedInterests:
        async with self._pool.acquire() as conn:
            specific_rows = await conn.fetch(
                "SELECT n.id, n.broad_category, n.specific_interest, n.canonical_label "
                "FROM interest_nodes n WHERE n.id IN ("
                "  SELECT interest_node_id FROM user_interests WHERE user_id = $1 "
                "  INTERSECT "
                "  SELECT interest_node_id FROM user_interests WHERE user_id = $2"
                ") ORDER BY n.id",
                a,
                b,
            )
            broad_rows = await conn.fetch(
                "SELECT broad FROM ("
                "  SELECT DISTINCT n.broad_category AS broad FROM user_interests ui "
                "    JOIN interest_nodes n ON n.id = ui.interest_node_id WHERE ui.user_id = $1 "
                "  INTERSECT "
                "  SELECT DISTINCT n.broad_category AS broad FROM user_interests ui "
                "    JOIN interest_nodes n ON n.id = ui.interest_node_id WHERE ui.user_id = $2 "
                ") s ORDER BY broad",
                a,
                b,
            )
        specific = [
            InterestNode(r["id"], r["broad_category"], r["specific_interest"], r["canonical_label"])
            for r in specific_rows
        ]
        return SharedInterests(specific=specific, broad=[r["broad"] for r in broad_rows])

    _CAND_COLS = (
        "user_id_a, user_id_b, score, interest_score, dimension_score, values_score, "
        "confidence, human_review_flag, explanation, scored_at"
    )

    async def save_candidate_score(self, score: CandidateScore) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO candidate_scores "
                "(user_id_a, user_id_b, score, interest_score, dimension_score, values_score, "
                " confidence, human_review_flag, explanation) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb) "
                "ON CONFLICT (user_id_a, user_id_b) DO UPDATE SET score = excluded.score, "
                "interest_score = excluded.interest_score, "
                "dimension_score = excluded.dimension_score, "
                "values_score = excluded.values_score, confidence = excluded.confidence, "
                "human_review_flag = excluded.human_review_flag, "
                "explanation = excluded.explanation, scored_at = now()",
                score.user_id_a,
                score.user_id_b,
                score.score,
                score.interest_score,
                score.dimension_score,
                score.values_score,
                score.confidence,
                score.human_review_flag,
                json.dumps(explanation_to_json(score.explanation)),
            )

    async def get_candidate_scores(self, user_id: str) -> list[CandidateScore]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {self._CAND_COLS} FROM candidate_scores "
                "WHERE user_id_a = $1 ORDER BY score DESC, user_id_b DESC",
                user_id,
            )
        return [self._candidate(r) for r in rows]

    @staticmethod
    def _candidate(row) -> CandidateScore:
        raw = row["explanation"]
        data = json.loads(raw) if isinstance(raw, str) else (raw or {})
        return CandidateScore(
            user_id_a=row["user_id_a"],
            user_id_b=row["user_id_b"],
            score=row["score"],
            interest_score=row["interest_score"],
            dimension_score=row["dimension_score"],
            values_score=row["values_score"],
            confidence=row["confidence"],
            human_review_flag=row["human_review_flag"],
            explanation=explanation_from_json(data),
            scored_at=row["scored_at"],
        )

    async def get_shown_user_ids(self, user_id: str) -> list[str]:
        return []  # Part-5 match_state will implement this

    async def delete_user(self, user_id: str) -> None:
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("DELETE FROM user_interests WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM profile_dimensions WHERE user_id = $1", user_id)
            await conn.execute(
                "DELETE FROM candidate_scores WHERE user_id_a = $1 OR user_id_b = $1", user_id
            )
            await conn.execute("DELETE FROM users_pool WHERE user_id = $1", user_id)


# Keep a UTC helper importable for callers building UserPoolEntry timestamps.
def now_utc() -> datetime:
    return datetime.now(UTC)
