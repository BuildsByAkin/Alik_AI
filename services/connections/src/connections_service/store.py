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
    EvalResult,
    GroupCandidate,
    GroupStatus,
    InterestEdge,
    InterestMatch,
    InterestNode,
    KernelExplanation,
    MatchStateEntry,
    MatchStatus,
    PassRun,
    SharedInterests,
    SurfaceableMatch,
    UserPoolEntry,
)

# Statuses that take a candidate out of the pool: once surfaced, never re-scored/re-surfaced.
_SHOWN_STATUSES = (MatchStatus.SHOWN, MatchStatus.ACCEPTED, MatchStatus.SKIPPED)


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
        """candidate_ids already shown/accepted/skipped — excluded from scoring + surfacing."""

    # --- Part 5: match state ------------------------------------------------------
    @abstractmethod
    async def save_match_state(self, entry: MatchStateEntry) -> None:
        """Upsert a surfaced pair (user_id, candidate_id)."""

    @abstractmethod
    async def get_match_state(self, user_id: str, candidate_id: str) -> MatchStateEntry | None: ...

    @abstractmethod
    async def update_match_status(
        self, user_id: str, candidate_id: str, status: MatchStatus, responded_at
    ) -> None:
        """Move a surfaced pair to accepted/skipped when the user responds via the companion."""

    # --- Part 4: LLM cross-evaluation ---------------------------------------------
    @abstractmethod
    async def save_eval_result(self, result: EvalResult) -> None:
        """Upsert a directed (A→B) eval verdict (replaced on each eval run)."""

    @abstractmethod
    async def get_eval_result(self, user_id_a: str, user_id_b: str) -> EvalResult | None: ...

    @abstractmethod
    async def get_surfaceable_matches(
        self, user_id: str, state: str, *, surface_threshold: float
    ) -> list[SurfaceableMatch]:
        """would_click + final_confidence >= threshold, joined to the kernel score/explanation,
        excluding already-shown users (stub), highest final_confidence first. Read by Part 5."""

    # --- Part 6: group clustering -------------------------------------------------
    @abstractmethod
    async def get_clusterable_interest_nodes(self, state: str, min_users: int) -> list[str]:
        """Specific interest nodes (excl. :_general) with >= min_users pool_ready users in state."""

    @abstractmethod
    async def get_pairwise_scores(self, user_ids: list[str]) -> dict[frozenset[str], float]:
        """Symmetric candidate scores among the set (max per unordered pair)."""

    @abstractmethod
    async def get_skipped_pairs(self, user_ids: list[str]) -> set[frozenset[str]]:
        """Pairs in the set where either side skipped the other in match_state."""

    @abstractmethod
    async def save_group_candidate(self, group: GroupCandidate) -> None:
        """Upsert a group; on (interest, members) conflict keep group_id + status, refresh score."""

    @abstractmethod
    async def get_group_candidate(self, group_id: str) -> GroupCandidate | None: ...

    @abstractmethod
    async def get_proposed_groups(self) -> list[GroupCandidate]: ...

    @abstractmethod
    async def update_group_status(self, group_id: str, status: GroupStatus) -> None: ...

    @abstractmethod
    async def get_surfaced_group_member_ids(self, interest_node_id: str) -> list[frozenset[str]]:
        """Member sets of surfacing/surfaced groups for this interest (overlap dedup)."""

    @abstractmethod
    async def delete_user(self, user_id: str) -> None:
        """Erase ALL of this service's data for the user (cross-service deletion seam)."""

    # --- Monitoring: pass-run history for the digest ------------------------------
    @abstractmethod
    async def record_pass_run(self, run: PassRun) -> None:
        """Persist one finished pass's summary (best-effort — callers never let it raise)."""

    @abstractmethod
    async def get_recent_pass_runs(self, since: datetime) -> list[PassRun]:
        """All recorded pass runs at/after ``since`` (newest first)."""


class InMemoryStore(Store):
    """Infra-free double mirroring PgStore semantics. Self-seeds the taxonomy on construction."""

    def __init__(self) -> None:
        self._nodes: dict[str, InterestNode] = {}
        self._pool: dict[str, UserPoolEntry] = {}
        self._interests: dict[str, list[InterestEdge]] = {}
        self._dims: dict[str, list[DimensionSnapshot]] = {}
        self._candidates: dict[tuple[str, str], CandidateScore] = {}
        self._evals: dict[tuple[str, str], EvalResult] = {}
        self._match: dict[tuple[str, str], MatchStateEntry] = {}
        self._groups: dict[str, GroupCandidate] = {}
        self._pass_runs: list[PassRun] = []
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
        return [
            c for (u, c), m in self._match.items() if u == user_id and m.status in _SHOWN_STATUSES
        ]

    async def save_match_state(self, entry: MatchStateEntry) -> None:
        stamped = entry if entry.created_at else replace(entry, created_at=datetime.now(UTC))
        self._match[(entry.user_id, entry.candidate_id)] = stamped

    async def get_match_state(self, user_id: str, candidate_id: str) -> MatchStateEntry | None:
        return self._match.get((user_id, candidate_id))

    async def update_match_status(
        self, user_id: str, candidate_id: str, status: MatchStatus, responded_at
    ) -> None:
        entry = self._match.get((user_id, candidate_id))
        if entry is not None:
            self._match[(user_id, candidate_id)] = replace(
                entry, status=status, responded_at=responded_at
            )

    async def get_clusterable_interest_nodes(self, state: str, min_users: int) -> list[str]:
        per_node: dict[str, set[str]] = {}
        for uid, edges in self._interests.items():
            entry = self._pool.get(uid)
            if not (entry and entry.pool_ready and entry.state == state):
                continue
            for e in edges:
                if e.interest_node_id.endswith(":_general"):
                    continue
                per_node.setdefault(e.interest_node_id, set()).add(uid)
        return sorted(n for n, users in per_node.items() if len(users) >= min_users)

    async def get_pairwise_scores(self, user_ids: list[str]) -> dict[frozenset[str], float]:
        ids = set(user_ids)
        out: dict[frozenset[str], float] = {}
        for (a, b), cand in self._candidates.items():
            if a in ids and b in ids and a != b:
                key = frozenset((a, b))
                out[key] = max(out.get(key, 0.0), cand.score)
        return out

    async def get_skipped_pairs(self, user_ids: list[str]) -> set[frozenset[str]]:
        ids = set(user_ids)
        return {
            frozenset((u, c))
            for (u, c), m in self._match.items()
            if u in ids and c in ids and m.status is MatchStatus.SKIPPED
        }

    async def save_group_candidate(self, group: GroupCandidate) -> None:
        members = frozenset(group.member_ids)
        now = datetime.now(UTC)
        for gid, g in self._groups.items():
            if g.interest_node_id == group.interest_node_id and frozenset(g.member_ids) == members:
                self._groups[gid] = replace(g, mean_score=group.mean_score, updated_at=now)
                return
        self._groups[group.group_id] = replace(
            group,
            member_ids=sorted(group.member_ids),
            created_at=group.created_at or now,
            updated_at=now,
        )

    async def get_group_candidate(self, group_id: str) -> GroupCandidate | None:
        return self._groups.get(group_id)

    async def get_proposed_groups(self) -> list[GroupCandidate]:
        return [g for g in self._groups.values() if g.status is GroupStatus.PROPOSED]

    async def update_group_status(self, group_id: str, status: GroupStatus) -> None:
        g = self._groups.get(group_id)
        if g is not None:
            self._groups[group_id] = replace(g, status=status, updated_at=datetime.now(UTC))

    async def get_surfaced_group_member_ids(self, interest_node_id: str) -> list[frozenset[str]]:
        return [
            frozenset(g.member_ids)
            for g in self._groups.values()
            if g.interest_node_id == interest_node_id
            and g.status in (GroupStatus.SURFACING, GroupStatus.SURFACED)
        ]

    async def save_eval_result(self, result: EvalResult) -> None:
        stamped = result if result.evaled_at else replace(result, evaled_at=datetime.now(UTC))
        self._evals[(result.user_id_a, result.user_id_b)] = stamped

    async def get_eval_result(self, user_id_a: str, user_id_b: str) -> EvalResult | None:
        return self._evals.get((user_id_a, user_id_b))

    async def get_surfaceable_matches(
        self, user_id: str, state: str, *, surface_threshold: float
    ) -> list[SurfaceableMatch]:
        shown = set(await self.get_shown_user_ids(user_id))
        out: list[SurfaceableMatch] = []
        for (a, b), ev in self._evals.items():
            if a != user_id or not ev.would_click or ev.final_confidence < surface_threshold:
                continue
            if b in shown:
                continue
            cand = self._candidates.get((a, b))
            if cand is None:
                continue
            out.append(
                SurfaceableMatch(
                    a,
                    b,
                    cand.score,
                    ev.llm_confidence,
                    ev.final_confidence,
                    ev.reason,
                    cand.explanation,
                )
            )
        out.sort(key=lambda m: m.final_confidence, reverse=True)
        return out

    async def delete_user(self, user_id: str) -> None:
        self._pool.pop(user_id, None)
        self._interests.pop(user_id, None)
        self._dims.pop(user_id, None)
        self._candidates = {
            (a, b): s for (a, b), s in self._candidates.items() if user_id not in (a, b)
        }
        self._evals = {(a, b): e for (a, b), e in self._evals.items() if user_id not in (a, b)}
        self._match = {(u, c): m for (u, c), m in self._match.items() if user_id not in (u, c)}
        self._groups = {gid: g for gid, g in self._groups.items() if user_id not in g.member_ids}

    async def record_pass_run(self, run: PassRun) -> None:
        stamped = run if run.ran_at else replace(run, ran_at=datetime.now(UTC))
        self._pass_runs.append(stamped)

    async def get_recent_pass_runs(self, since: datetime) -> list[PassRun]:
        rows = [r for r in self._pass_runs if r.ran_at is not None and r.ran_at >= since]
        return sorted(rows, key=lambda r: r.ran_at, reverse=True)


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
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT candidate_id FROM match_state "
                "WHERE user_id = $1 AND status IN ('shown', 'accepted', 'skipped')",
                user_id,
            )
        return [r["candidate_id"] for r in rows]

    async def save_match_state(self, entry: MatchStateEntry) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO match_state "
                "(user_id, candidate_id, status, checkin_id, surfaced_at, responded_at) "
                "VALUES ($1, $2, $3, $4, $5, $6) "
                "ON CONFLICT (user_id, candidate_id) DO UPDATE SET status = excluded.status, "
                "checkin_id = excluded.checkin_id, surfaced_at = excluded.surfaced_at, "
                "responded_at = excluded.responded_at",
                entry.user_id,
                entry.candidate_id,
                str(entry.status),
                entry.checkin_id,
                entry.surfaced_at,
                entry.responded_at,
            )

    async def get_match_state(self, user_id: str, candidate_id: str) -> MatchStateEntry | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT user_id, candidate_id, status, checkin_id, surfaced_at, responded_at, "
                "created_at FROM match_state WHERE user_id = $1 AND candidate_id = $2",
                user_id,
                candidate_id,
            )
        if row is None:
            return None
        return MatchStateEntry(
            user_id=row["user_id"],
            candidate_id=row["candidate_id"],
            status=MatchStatus(row["status"]),
            checkin_id=row["checkin_id"],
            surfaced_at=row["surfaced_at"],
            responded_at=row["responded_at"],
            created_at=row["created_at"],
        )

    async def update_match_status(
        self, user_id: str, candidate_id: str, status: MatchStatus, responded_at
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE match_state SET status = $3, responded_at = $4 "
                "WHERE user_id = $1 AND candidate_id = $2",
                user_id,
                candidate_id,
                str(status),
                responded_at,
            )

    async def get_clusterable_interest_nodes(self, state: str, min_users: int) -> list[str]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT ui.interest_node_id FROM user_interests ui "
                "JOIN users_pool p ON p.user_id = ui.user_id "
                "WHERE p.state = $1 AND p.pool_ready "
                "AND split_part(ui.interest_node_id, ':', 2) <> '_general' "
                "GROUP BY ui.interest_node_id "
                "HAVING count(DISTINCT ui.user_id) >= $2 ORDER BY ui.interest_node_id",
                state,
                min_users,
            )
        return [r["interest_node_id"] for r in rows]

    async def get_pairwise_scores(self, user_ids: list[str]) -> dict[frozenset[str], float]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT user_id_a, user_id_b, score FROM candidate_scores "
                "WHERE user_id_a = ANY($1) AND user_id_b = ANY($1)",
                user_ids,
            )
        out: dict[frozenset[str], float] = {}
        for r in rows:
            key = frozenset((r["user_id_a"], r["user_id_b"]))
            out[key] = max(out.get(key, 0.0), r["score"])
        return out

    async def get_skipped_pairs(self, user_ids: list[str]) -> set[frozenset[str]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT user_id, candidate_id FROM match_state "
                "WHERE status = 'skipped' AND user_id = ANY($1) AND candidate_id = ANY($1)",
                user_ids,
            )
        return {frozenset((r["user_id"], r["candidate_id"])) for r in rows}

    @staticmethod
    def _group(row) -> GroupCandidate:
        return GroupCandidate(
            group_id=row["group_id"],
            interest_node_id=row["interest_node_id"],
            member_ids=list(row["member_ids"]),
            mean_score=row["mean_score"],
            status=GroupStatus(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def save_group_candidate(self, group: GroupCandidate) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO group_candidates "
                "(group_id, interest_node_id, member_ids, mean_score, status) "
                "VALUES ($1, $2, $3, $4, $5) "
                "ON CONFLICT (interest_node_id, member_ids) DO UPDATE SET "
                "mean_score = excluded.mean_score, updated_at = now()",
                group.group_id,
                group.interest_node_id,
                sorted(group.member_ids),
                group.mean_score,
                str(group.status),
            )

    async def get_group_candidate(self, group_id: str) -> GroupCandidate | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT group_id, interest_node_id, member_ids, mean_score, status, "
                "created_at, updated_at FROM group_candidates WHERE group_id = $1",
                group_id,
            )
        return self._group(row) if row is not None else None

    async def get_proposed_groups(self) -> list[GroupCandidate]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT group_id, interest_node_id, member_ids, mean_score, status, "
                "created_at, updated_at FROM group_candidates WHERE status = 'proposed'"
            )
        return [self._group(r) for r in rows]

    async def update_group_status(self, group_id: str, status: GroupStatus) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE group_candidates SET status = $2, updated_at = now() WHERE group_id = $1",
                group_id,
                str(status),
            )

    async def get_surfaced_group_member_ids(self, interest_node_id: str) -> list[frozenset[str]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT member_ids FROM group_candidates "
                "WHERE interest_node_id = $1 AND status IN ('surfacing', 'surfaced')",
                interest_node_id,
            )
        return [frozenset(r["member_ids"]) for r in rows]

    async def save_eval_result(self, result: EvalResult) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO eval_results "
                "(user_id_a, user_id_b, would_click, llm_confidence, final_confidence, reason, "
                " flag_for_review, flag_reason, eval_model) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) "
                "ON CONFLICT (user_id_a, user_id_b) DO UPDATE SET "
                "would_click = excluded.would_click, "
                "llm_confidence = excluded.llm_confidence, "
                "final_confidence = excluded.final_confidence, reason = excluded.reason, "
                "flag_for_review = excluded.flag_for_review, flag_reason = excluded.flag_reason, "
                "eval_model = excluded.eval_model, evaled_at = now()",
                result.user_id_a,
                result.user_id_b,
                result.would_click,
                result.llm_confidence,
                result.final_confidence,
                result.reason,
                result.flag_for_review,
                result.flag_reason,
                result.eval_model,
            )

    @staticmethod
    def _eval(row) -> EvalResult:
        return EvalResult(
            user_id_a=row["user_id_a"],
            user_id_b=row["user_id_b"],
            would_click=row["would_click"],
            llm_confidence=row["llm_confidence"],
            final_confidence=row["final_confidence"],
            reason=row["reason"],
            eval_model=row["eval_model"],
            flag_for_review=row["flag_for_review"],
            flag_reason=row["flag_reason"],
            evaled_at=row["evaled_at"],
        )

    async def get_eval_result(self, user_id_a: str, user_id_b: str) -> EvalResult | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT user_id_a, user_id_b, would_click, llm_confidence, final_confidence, "
                "reason, flag_for_review, flag_reason, eval_model, evaled_at FROM eval_results "
                "WHERE user_id_a = $1 AND user_id_b = $2",
                user_id_a,
                user_id_b,
            )
        return self._eval(row) if row is not None else None

    async def get_surfaceable_matches(
        self, user_id: str, state: str, *, surface_threshold: float
    ) -> list[SurfaceableMatch]:
        # candidate_scores are already state-scoped (scored within state); `state` is advisory.
        # Exclude candidates already surfaced (shown/accepted/skipped) for this user.
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT e.user_id_a, e.user_id_b, e.llm_confidence, e.final_confidence, e.reason, "
                "c.score, c.explanation FROM eval_results e "
                "JOIN candidate_scores c "
                "  ON c.user_id_a = e.user_id_a AND c.user_id_b = e.user_id_b "
                "WHERE e.user_id_a = $1 AND e.would_click AND e.final_confidence >= $2 "
                "AND e.user_id_b NOT IN ("
                "  SELECT candidate_id FROM match_state "
                "  WHERE user_id = $1 AND status IN ('shown', 'accepted', 'skipped')) "
                "ORDER BY e.final_confidence DESC",
                user_id,
                surface_threshold,
            )
        out: list[SurfaceableMatch] = []
        for r in rows:
            raw = r["explanation"]
            data = json.loads(raw) if isinstance(raw, str) else (raw or {})
            out.append(
                SurfaceableMatch(
                    user_id_a=r["user_id_a"],
                    user_id_b=r["user_id_b"],
                    kernel_score=r["score"],
                    llm_confidence=r["llm_confidence"],
                    final_confidence=r["final_confidence"],
                    reason=r["reason"],
                    explanation=explanation_from_json(data),
                )
            )
        return out

    async def delete_user(self, user_id: str) -> None:
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("DELETE FROM user_interests WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM profile_dimensions WHERE user_id = $1", user_id)
            await conn.execute(
                "DELETE FROM candidate_scores WHERE user_id_a = $1 OR user_id_b = $1", user_id
            )
            await conn.execute(
                "DELETE FROM eval_results WHERE user_id_a = $1 OR user_id_b = $1", user_id
            )
            await conn.execute(
                "DELETE FROM match_state WHERE user_id = $1 OR candidate_id = $1", user_id
            )
            await conn.execute(
                "DELETE FROM group_candidates WHERE member_ids @> ARRAY[$1]", user_id
            )
            await conn.execute("DELETE FROM users_pool WHERE user_id = $1", user_id)

    async def record_pass_run(self, run: PassRun) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO pass_runs (pass_name, fields, failures, ran_at) "
                "VALUES ($1, $2::jsonb, $3, COALESCE($4, now()))",
                run.pass_name,
                json.dumps(run.fields),
                run.failures,
                run.ran_at,
            )

    async def get_recent_pass_runs(self, since: datetime) -> list[PassRun]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT pass_name, fields, failures, ran_at FROM pass_runs "
                "WHERE ran_at >= $1 ORDER BY ran_at DESC",
                since,
            )
        out: list[PassRun] = []
        for r in rows:
            raw = r["fields"]
            fields = json.loads(raw) if isinstance(raw, str) else (raw or {})
            out.append(
                PassRun(
                    pass_name=r["pass_name"],
                    fields=fields,
                    failures=r["failures"],
                    ran_at=r["ran_at"],
                )
            )
        return out


# Keep a UTC helper importable for callers building UserPoolEntry timestamps.
def now_utc() -> datetime:
    return datetime.now(UTC)
