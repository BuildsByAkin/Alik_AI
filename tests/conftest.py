"""Shared fixtures and helpers.

Strategy: the LLM is always faked (deterministic, no network). The companion's
cross-session logic is proven against an in-memory ``Memory`` double (always runs,
no infra). The real ``PgRedisMemory`` is exercised by the infra tests, which skip
gracefully when the docker-compose Postgres + Redis URLs aren't set.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from alik.memory.base import Memory
from alik.memory.graph import GraphMemory
from alik.memory.graph_store import GraphStore
from alik.memory.pg_redis import PgRedisMemory
from alik.models import (
    CommitmentNode,
    CommitmentStatus,
    GraphNode,
    InferredTrait,
    MemoryRecord,
    MemoryTier,
    NodeType,
    PendingCheckin,
    RetrievedContext,
    TraitStatus,
)

DATABASE_URL = os.environ.get("ALIK_DATABASE_URL") or os.environ.get("DATABASE_URL")
REDIS_URL = os.environ.get("ALIK_REDIS_URL") or os.environ.get("REDIS_URL")
FALKORDB_URL = os.environ.get("ALIK_FALKORDB_URL") or os.environ.get("FALKORDB_URL")

requires_infra = pytest.mark.skipif(
    not (DATABASE_URL and REDIS_URL),
    reason="set ALIK_DATABASE_URL and ALIK_REDIS_URL (docker compose up -d) to run infra tests",
)

requires_graph = pytest.mark.skipif(
    not FALKORDB_URL,
    reason="set ALIK_FALKORDB_URL (docker compose up -d falkordb) to run graph infra tests",
)


@pytest_asyncio.fixture
async def memory() -> AsyncIterator[PgRedisMemory]:
    mem = await PgRedisMemory.connect(
        database_url=DATABASE_URL,
        redis_url=REDIS_URL,
        working_ttl_seconds=3600,
    )
    try:
        yield mem
    finally:
        await mem.aclose()


@pytest.fixture
def user_id() -> str:
    return f"test-{uuid.uuid4().hex}"


class FakeLLM:
    """Deterministic stand-in for the runtime model.

    Records the last system prompt it received (so a test can assert what context
    was injected), and "summarizes" by echoing the user's words so any stated fact
    survives into episodic memory.
    """

    def __init__(self, reply: str = "Got it.") -> None:
        self.reply = reply
        self.last_system: str | None = None
        self.last_messages: list[dict] | None = None

    async def stream_reply(self, *, system: str, messages: Sequence[dict]) -> AsyncIterator[str]:
        self.last_system = system
        self.last_messages = list(messages)
        for word in self.reply.split():
            yield word + " "

    async def complete(self, *, system: str, messages: Sequence[dict]) -> str:
        self.last_system = system
        self.last_messages = list(messages)
        user_text = " ".join(m["content"] for m in messages if m.get("role") == "user")
        return f"Summary of session. User said: {user_text}"


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


class InMemoryMemory(Memory):
    """Faithful, infra-free ``Memory`` double mirroring PgRedisMemory semantics.

    Lets the companion's cross-session logic and the sleep pass be proven
    deterministically without Docker. The real DB behavior is covered by infra tests.
    Episodes are stored as dicts so they can carry id/promoted/decayed_at.
    """

    def __init__(self, *, reflection_after_days: int = 30) -> None:
        self._working: dict[tuple[str, str], list[MemoryRecord]] = {}
        self._episodic: dict[str, list[dict]] = {}
        self._reflections: dict[str, list[dict]] = {}
        self._checkins: list[PendingCheckin] = []  # Phase 5 queue
        self._rb_cooldown: dict[str, int] = {}  # Phase 5.2 reflect-back cadence
        self._reflection_after_days = reflection_after_days

    def _to_record(self, user_id: str, ep: dict) -> MemoryRecord:
        return MemoryRecord(
            user_id=user_id,
            session_id=ep["session_id"],
            tier=MemoryTier.EPISODIC,
            content=ep["summary"],
            created_at=ep["created_at"],
            id=ep["id"],
        )

    async def write(self, record: MemoryRecord) -> None:
        if record.tier is MemoryTier.WORKING:
            self._working.setdefault((record.user_id, record.session_id), []).append(record)
        else:
            self._episodic.setdefault(record.user_id, []).append(
                {
                    "id": uuid.uuid4().hex,
                    "session_id": record.session_id,
                    "summary": record.content,
                    "created_at": record.created_at or datetime.now(UTC),
                    "promoted": False,
                    "decayed_at": None,
                }
            )

    async def retrieve(
        self,
        user_id: str,
        session_id: str | None = None,
        *,
        episode_limit: int = 10,
    ) -> RetrievedContext:
        eps = self._episodic.get(user_id, [])
        earliest = min((e["created_at"] for e in eps), default=None)
        reflection = self._reflections.get(user_id, [])
        latest_reflection = reflection[-1]["content"] if reflection else None
        use_reflection = (
            latest_reflection is not None
            and earliest is not None
            and datetime.now(UTC) - earliest >= timedelta(days=self._reflection_after_days)
        )
        if use_reflection:
            episodes: list[MemoryRecord] = []
        else:
            live = sorted(
                (e for e in eps if e["decayed_at"] is None), key=lambda e: e["created_at"]
            )[-episode_limit:]
            episodes = [self._to_record(user_id, e) for e in live]
        working: list[MemoryRecord] = []
        if session_id is not None:
            working = list(self._working.get((user_id, session_id), []))
        return RetrievedContext(
            episodes=episodes,
            working=working,
            reflection=latest_reflection if use_reflection else None,
        )

    async def invalidate(self, user_id: str, session_id: str) -> None:
        self._working.pop((user_id, session_id), None)

    async def delete(self, user_id: str) -> None:
        self._episodic.pop(user_id, None)
        self._reflections.pop(user_id, None)
        self._checkins = [c for c in self._checkins if c.user_id != user_id]
        self._rb_cooldown.pop(user_id, None)
        for key in [k for k in self._working if k[0] == user_id]:
            self._working.pop(key, None)

    # --- Phase 5: proactive check-in queue --------------------------------

    async def queue_checkin(self, checkin: PendingCheckin) -> None:
        stamped = replace(checkin, created_at=checkin.created_at or datetime.now(UTC))
        self._checkins.append(stamped)

    async def get_pending_checkin(self, user_id: str) -> PendingCheckin | None:
        undelivered = [c for c in self._checkins if c.user_id == user_id and c.delivered_at is None]
        undelivered.sort(key=lambda c: c.created_at or datetime.now(UTC))
        return undelivered[-1] if undelivered else None

    async def mark_checkin_delivered(self, checkin_id: str) -> None:
        self._checkins = [
            replace(c, delivered_at=datetime.now(UTC)) if c.id == checkin_id else c
            for c in self._checkins
        ]

    async def get_last_session_at(self, user_id: str) -> datetime | None:
        eps = self._episodic.get(user_id, [])
        return max((e["created_at"] for e in eps), default=None)

    async def reflect_back_ready(self, user_id: str) -> bool:
        return self._rb_cooldown.get(user_id, 0) == 0

    async def set_reflect_back_cooldown(self, user_id: str, sessions: int) -> None:
        self._rb_cooldown[user_id] = sessions

    async def decrement_reflect_back_cooldown(self, user_id: str) -> None:
        self._rb_cooldown[user_id] = max(0, self._rb_cooldown.get(user_id, 0) - 1)

    # --- Phase 3 ----------------------------------------------------------

    async def get_active_users(self, *, within_days: int = 30) -> list[str]:
        cutoff = datetime.now(UTC) - timedelta(days=within_days)
        return [
            uid
            for uid, eps in self._episodic.items()
            if any(e["created_at"] >= cutoff for e in eps)
        ]

    async def get_recent_episodes(self, user_id: str, *, days: int = 7) -> list[MemoryRecord]:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        eps = [
            e
            for e in self._episodic.get(user_id, [])
            if e["created_at"] >= cutoff and e["decayed_at"] is None and not e["promoted"]
        ]
        eps.sort(key=lambda e: e["created_at"])
        return [self._to_record(user_id, e) for e in eps]

    async def get_promoted_episodes(self, user_id: str, *, limit: int = 20) -> list[MemoryRecord]:
        eps = [
            e for e in self._episodic.get(user_id, []) if e["promoted"] and e["decayed_at"] is None
        ]
        eps.sort(key=lambda e: e["created_at"], reverse=True)
        return [self._to_record(user_id, e) for e in eps[:limit]]

    async def promote_episode(self, episode_id: str) -> None:
        for eps in self._episodic.values():
            for e in eps:
                if e["id"] == episode_id:
                    e["promoted"] = True
                    return

    async def decay_episodes(self, user_id: str, *, older_than_days: int = 30) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
        count = 0
        for e in self._episodic.get(user_id, []):
            if e["created_at"] < cutoff and not e["promoted"] and e["decayed_at"] is None:
                e["decayed_at"] = datetime.now(UTC)
                count += 1
        return count

    async def save_reflection(self, user_id: str, content: str) -> None:
        today = datetime.now(UTC).date()
        kept = [r for r in self._reflections.get(user_id, []) if r["generated_at"].date() != today]
        kept.append({"content": content, "generated_at": datetime.now(UTC)})
        self._reflections[user_id] = kept

    async def get_reflection(self, user_id: str) -> str | None:
        reflections = self._reflections.get(user_id, [])
        return reflections[-1]["content"] if reflections else None


@pytest.fixture
def inmemory() -> InMemoryMemory:
    return InMemoryMemory()


class InMemoryGraphStore:
    """Infra-free double mirroring ``GraphStore`` primitives.

    Lets ``GraphMemory``'s temporal-resolution policy be proven without FalkorDB.
    The real Cypher binding is covered separately by test_graph_write.py.
    """

    def __init__(self) -> None:
        self._nodes: dict[str, GraphNode] = {}
        self._traits: dict[str, InferredTrait] = {}
        self._commitments: dict[str, CommitmentNode] = {}
        self._last_decayed: dict[str, datetime] = {}

    async def insert_node(self, node: GraphNode) -> None:
        self._nodes[node.id] = node

    async def find_current(
        self, user_id: str, node_type: NodeType, key: str
    ) -> tuple[str, str] | None:
        for n in self._nodes.values():
            if (
                n.user_id == user_id
                and n.type == node_type
                and n.key == key
                and n.valid_until is None
            ):
                return n.id, n.content
        return None

    async def close_node(self, node_id: str, valid_until: datetime) -> None:
        # Label-agnostic in the real store; here it may target a node, trait, or commitment.
        if node_id in self._nodes:
            self._nodes[node_id] = replace(self._nodes[node_id], valid_until=valid_until)
        elif node_id in self._traits:
            self._traits[node_id] = replace(self._traits[node_id], valid_until=valid_until)
        elif node_id in self._commitments:
            self._commitments[node_id] = replace(
                self._commitments[node_id], valid_until=valid_until
            )

    async def current(self, user_id: str, node_type: NodeType, *, limit: int) -> list[GraphNode]:
        nodes = [
            n
            for n in self._nodes.values()
            if n.user_id == user_id and n.type == node_type and n.valid_until is None
        ]
        nodes.sort(key=lambda n: n.valid_from, reverse=True)
        return nodes[:limit]

    async def decay_confidence(
        self,
        user_id: str,
        *,
        before: datetime,
        now: datetime,
        factor: float,
        floor: float,
    ) -> int:
        count = 0
        for nid, n in list(self._nodes.items()):
            if not (n.user_id == user_id and n.type == NodeType.FACT and n.valid_until is None):
                continue
            if n.valid_from > before:
                continue
            last = self._last_decayed.get(nid)
            if last is not None and last > before:
                continue  # already decayed this window — don't compound
            self._nodes[nid] = replace(n, confidence=max(n.confidence * factor, floor))
            self._last_decayed[nid] = now
            count += 1
        return count

    # --- Phase 4: InferredTrait primitives --------------------------------

    async def insert_trait(self, trait: InferredTrait) -> None:
        # Mirror the real store: last_detected_at defaults to valid_from on insert.
        self._traits[trait.id] = replace(
            trait, last_detected_at=trait.last_detected_at or trait.valid_from
        )

    async def touch_trait(self, trait_id: str, *, last_detected_at: datetime) -> None:
        self._traits[trait_id] = replace(self._traits[trait_id], last_detected_at=last_detected_at)

    async def prune_stale_inferred_traits(
        self, user_id: str, *, before: datetime, now: datetime
    ) -> int:
        count = 0
        for tid, t in list(self._traits.items()):
            if (
                t.user_id == user_id
                and t.valid_until is None
                and t.status is TraitStatus.INFERRED
                and (t.last_detected_at or t.valid_from) <= before
            ):
                self._traits[tid] = replace(t, valid_until=now)
                count += 1
        return count

    async def find_current_trait(self, user_id: str, key: str) -> tuple[str, str, str] | None:
        for t in self._traits.values():
            if t.user_id == user_id and t.key == key and t.valid_until is None:
                return t.id, t.content, str(t.status)
        return None

    async def get_current_traits(self, user_id: str, *, limit: int) -> list[InferredTrait]:
        traits = [
            t for t in self._traits.values() if t.user_id == user_id and t.valid_until is None
        ]
        traits.sort(key=lambda t: t.valid_from, reverse=True)
        return traits[:limit]

    async def get_trait_by_id(self, trait_id: str) -> InferredTrait | None:
        return self._traits.get(trait_id)

    async def confirm_trait(self, trait_id: str, confidence_bump: float, *, now: datetime) -> None:
        t = self._traits[trait_id]
        self._traits[trait_id] = replace(
            t,
            status=TraitStatus.CONFIRMED,
            confidence=min(t.confidence + confidence_bump, 1.0),
            status_updated_at=now,
        )

    async def correct_trait(self, trait_id: str, *, now: datetime) -> None:
        t = self._traits[trait_id]
        self._traits[trait_id] = replace(
            t, valid_until=now, status=TraitStatus.CORRECTED, status_updated_at=now
        )

    async def get_trait_for_reflect(
        self, user_id: str, session_id: str, *, min_confidence: float
    ) -> InferredTrait | None:
        candidates = [
            t
            for t in self._traits.values()
            if t.user_id == user_id
            and t.valid_until is None
            and t.status is TraitStatus.INFERRED
            and t.confidence >= min_confidence
            and t.surfaced_in_session != session_id
        ]
        candidates.sort(key=lambda t: (t.confidence, t.valid_from), reverse=True)
        return candidates[0] if candidates else None

    async def mark_trait_surfaced(self, trait_id: str, session_id: str) -> None:
        self._traits[trait_id] = replace(self._traits[trait_id], surfaced_in_session=session_id)

    # --- Phase 5: Commitment lifecycle primitives -------------------------

    async def insert_commitment(self, c: CommitmentNode) -> None:
        self._commitments[c.id] = c

    async def find_open_commitment(self, user_id: str, key: str) -> CommitmentNode | None:
        matches = [
            c
            for c in self._open(user_id)
            if c.key == key and c.status in (CommitmentStatus.PENDING, CommitmentStatus.DUE)
        ]
        matches.sort(key=lambda c: c.valid_from, reverse=True)
        return matches[0] if matches else None

    async def touch_commitment(self, commitment_id: str, *, expected_by: datetime | None) -> None:
        c = self._commitments[commitment_id]
        self._commitments[commitment_id] = replace(
            c,
            mention_count=c.mention_count + 1,
            expected_by=expected_by if expected_by is not None else c.expected_by,
        )

    def _open(self, user_id: str) -> list[CommitmentNode]:
        return [
            c for c in self._commitments.values() if c.user_id == user_id and c.valid_until is None
        ]

    async def get_open_commitments(self, user_id: str, *, limit: int) -> list[CommitmentNode]:
        cs = [
            c
            for c in self._open(user_id)
            if c.status in (CommitmentStatus.PENDING, CommitmentStatus.DUE)
        ]
        cs.sort(key=lambda c: c.valid_from, reverse=True)
        return cs[:limit]

    async def get_pending_commitments(self, user_id: str, *, limit: int) -> list[CommitmentNode]:
        cs = [c for c in self._open(user_id) if c.status is CommitmentStatus.PENDING]
        cs.sort(key=lambda c: c.valid_from)
        return cs[:limit]

    async def get_due_commitments(self, user_id: str, *, now: datetime) -> list[CommitmentNode]:
        today = datetime(now.year, now.month, now.day, tzinfo=now.tzinfo)
        far = datetime.max.replace(tzinfo=now.tzinfo)
        due = [
            c
            for c in self._open(user_id)
            if c.status is CommitmentStatus.DUE
            and (c.reminded_count == 0 or (c.last_reminded_at or today) < today)
        ]
        # Explicit, most-overdue deadline first; fallback-due (no expected_by) last.
        due.sort(key=lambda c: (c.expected_by or far, c.valid_from))
        return due

    async def get_upcoming_commitments(
        self, user_id: str, *, now: datetime, until: datetime
    ) -> list[CommitmentNode]:
        return [
            c
            for c in self._open(user_id)
            if c.status is CommitmentStatus.PENDING
            and c.expected_by is not None
            and now < c.expected_by <= until
        ]

    async def update_commitment_status(
        self,
        commitment_id: str,
        status: CommitmentStatus,
        *,
        resolved_at: datetime | None = None,
        follow_through: bool | None = None,
    ) -> None:
        self._commitments[commitment_id] = replace(
            self._commitments[commitment_id],
            status=status,
            resolved_at=resolved_at,
            follow_through=follow_through,
        )

    async def mark_commitment_reminded(self, commitment_id: str, *, now: datetime) -> None:
        c = self._commitments[commitment_id]
        self._commitments[commitment_id] = replace(
            c, reminded_count=c.reminded_count + 1, last_reminded_at=now
        )

    async def resolve_commitment(self, commitment_id: str, *, kept: bool, now: datetime) -> None:
        status = CommitmentStatus.RESOLVED_KEPT if kept else CommitmentStatus.RESOLVED_DROPPED
        await self.update_commitment_status(
            commitment_id, status, resolved_at=now, follow_through=kept
        )

    async def delete_user(self, user_id: str) -> None:
        self._nodes = {k: v for k, v in self._nodes.items() if v.user_id != user_id}
        self._traits = {k: v for k, v in self._traits.items() if v.user_id != user_id}
        self._commitments = {k: v for k, v in self._commitments.items() if v.user_id != user_id}

    async def aclose(self) -> None:
        pass


@pytest.fixture
def graph_memory_fake(inmemory: InMemoryMemory) -> GraphMemory:
    """GraphMemory wired to in-memory backends — always runs, no Docker."""
    return GraphMemory(base=inmemory, graph=InMemoryGraphStore(), current_facts_limit=50)


@pytest_asyncio.fixture
async def graph_memory_real() -> AsyncIterator[GraphMemory]:
    """GraphMemory over a REAL FalkorDB (in-memory base for working/episodic)."""
    store = GraphStore.from_url(FALKORDB_URL, graph_name="alik_test")
    mem = GraphMemory(base=InMemoryMemory(), graph=store, current_facts_limit=50)
    try:
        yield mem
    finally:
        await store.aclose()
