"""Temporal-graph memory: a ``Memory`` that composes a base store with a graph.

``GraphMemory`` wraps any base :class:`Memory` (working + episodic) and a
``GraphStore`` (current/temporal facts). It delegates the Phase 1 tiers verbatim
and adds the Phase 2 graph on top:

- ``retrieve`` enriches the context with current facts + open commitments.
- ``write_nodes`` applies temporal resolution (Facts supersede by key; signals
  and commitments are append-only).
- ``delete`` fans out to BOTH backends — full erasure is a legal requirement.

The temporal-resolution *policy* lives here (not in ``GraphStore``) so it can be
proven against an in-memory graph double without Docker. If the graph is
unreachable, reads/writes degrade to no-ops and the companion keeps working on
episodic memory alone — except ``delete``, which must not silently skip.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from typing import Protocol

from alik.memory.base import Memory
from alik.memory.graph_store import GraphStore
from alik.memory.pg_redis import PgRedisMemory
from alik.models import (
    CommitmentNode,
    CommitmentStatus,
    GraphNode,
    InferredTrait,
    MemoryRecord,
    NodeType,
    PendingCheckin,
    ProfileDimension,
    RetrievedContext,
    TraitStatus,
)

logger = logging.getLogger("alik.memory.graph")

# Two open commitments under the same key whose content is at least this similar are
# treated as the SAME commitment (re-stated), not a new one. Same key already means
# same topic; this guards against a generic key reused for genuinely different intents.
_COMMITMENT_SIMILARITY = 0.6


def _similar(a: str, b: str) -> bool:
    a_norm, b_norm = a.strip().lower(), b.strip().lower()
    if not a_norm or not b_norm:
        return False
    if a_norm == b_norm or a_norm in b_norm or b_norm in a_norm:
        return True
    return SequenceMatcher(None, a_norm, b_norm).ratio() >= _COMMITMENT_SIMILARITY


class GraphStoreLike(Protocol):
    """The graph primitives ``GraphMemory`` depends on (real store or test double)."""

    async def insert_node(self, node: GraphNode) -> None: ...
    async def find_current(
        self, user_id: str, node_type: NodeType, key: str
    ) -> tuple[str, str] | None: ...
    async def close_node(self, node_id: str, valid_until: datetime) -> None: ...
    async def current(
        self, user_id: str, node_type: NodeType, *, limit: int
    ) -> list[GraphNode]: ...
    async def decay_confidence(
        self, user_id: str, *, before: datetime, now: datetime, factor: float, floor: float
    ) -> int: ...
    async def insert_trait(self, trait: InferredTrait) -> None: ...
    async def find_current_trait(self, user_id: str, key: str) -> tuple[str, str, str] | None: ...
    async def get_current_traits(self, user_id: str, *, limit: int) -> list[InferredTrait]: ...
    async def get_trait_by_id(self, trait_id: str) -> InferredTrait | None: ...
    async def confirm_trait(
        self, trait_id: str, confidence_bump: float, *, now: datetime
    ) -> None: ...
    async def correct_trait(self, trait_id: str, *, now: datetime) -> None: ...
    async def get_trait_for_reflect(
        self, user_id: str, session_id: str, *, min_confidence: float
    ) -> InferredTrait | None: ...
    async def mark_trait_surfaced(self, trait_id: str, session_id: str) -> None: ...
    async def touch_trait(self, trait_id: str, *, last_detected_at: datetime) -> None: ...
    async def prune_stale_inferred_traits(
        self, user_id: str, *, before: datetime, now: datetime
    ) -> int: ...
    async def insert_commitment(self, c: CommitmentNode) -> None: ...
    async def find_open_commitment(self, user_id: str, key: str) -> CommitmentNode | None: ...
    async def touch_commitment(
        self, commitment_id: str, *, expected_by: datetime | None
    ) -> None: ...
    async def get_open_commitments(self, user_id: str, *, limit: int) -> list[CommitmentNode]: ...
    async def get_pending_commitments(
        self, user_id: str, *, limit: int
    ) -> list[CommitmentNode]: ...
    async def get_due_commitments(self, user_id: str, *, now: datetime) -> list[CommitmentNode]: ...
    async def get_upcoming_commitments(
        self, user_id: str, *, now: datetime, until: datetime
    ) -> list[CommitmentNode]: ...
    async def update_commitment_status(
        self,
        commitment_id: str,
        status: CommitmentStatus,
        *,
        resolved_at: datetime | None = None,
        follow_through: bool | None = None,
    ) -> None: ...
    async def mark_commitment_reminded(self, commitment_id: str, *, now: datetime) -> None: ...
    async def resolve_commitment(
        self, commitment_id: str, *, kept: bool, now: datetime
    ) -> None: ...
    async def delete_user(self, user_id: str) -> None: ...
    async def aclose(self) -> None: ...


class GraphMemory(Memory):
    def __init__(
        self,
        *,
        base: Memory,
        graph: GraphStoreLike | None,
        current_facts_limit: int,
        confidence_decay_days: int = 60,
        confidence_decay_factor: float = 0.85,
        confidence_floor: float = 0.1,
    ) -> None:
        self._base = base
        self._graph = graph
        self._limit = current_facts_limit
        self._decay_days = confidence_decay_days
        self._decay_factor = confidence_decay_factor
        self._decay_floor = confidence_floor

    @classmethod
    async def connect(
        cls,
        *,
        database_url: str,
        redis_url: str,
        falkordb_url: str,
        graph_name: str,
        working_ttl_seconds: int,
        current_facts_limit: int,
        reflection_after_days: int = 30,
        confidence_decay_days: int = 60,
        confidence_decay_factor: float = 0.85,
        confidence_floor: float = 0.1,
    ) -> GraphMemory:
        base = await PgRedisMemory.connect(
            database_url=database_url,
            redis_url=redis_url,
            working_ttl_seconds=working_ttl_seconds,
            reflection_after_days=reflection_after_days,
        )
        graph: GraphStoreLike | None
        try:
            graph = GraphStore.from_url(falkordb_url, graph_name=graph_name)
        except Exception:
            logger.exception("FalkorDB unreachable at %s — running graph-disabled", falkordb_url)
            graph = None
        return cls(
            base=base,
            graph=graph,
            current_facts_limit=current_facts_limit,
            confidence_decay_days=confidence_decay_days,
            confidence_decay_factor=confidence_decay_factor,
            confidence_floor=confidence_floor,
        )

    async def aclose(self) -> None:
        if self._graph is not None:
            try:
                await self._graph.aclose()
            except Exception:
                logger.exception("error closing graph store")
        aclose = getattr(self._base, "aclose", None)
        if aclose is not None:
            await aclose()

    # --- Phase 1 tiers: delegate verbatim ----------------------------------

    async def write(self, record: MemoryRecord) -> None:
        await self._base.write(record)

    async def invalidate(self, user_id: str, session_id: str) -> None:
        await self._base.invalidate(user_id, session_id)

    async def retrieve(
        self,
        user_id: str,
        session_id: str | None = None,
        *,
        episode_limit: int = 10,
    ) -> RetrievedContext:
        ctx = await self._base.retrieve(user_id, session_id, episode_limit=episode_limit)
        facts = await self._current(user_id, NodeType.FACT)
        commitments = await self.get_open_commitments(user_id)
        traits = await self.get_current_traits(user_id)
        dimensions = await self._base.get_profile_dimensions(user_id)
        return RetrievedContext(
            episodes=ctx.episodes,
            working=ctx.working,
            facts=facts,
            commitments=commitments,
            reflection=ctx.reflection,
            traits=traits,
            dimensions=dimensions,
        )

    # --- Phase 3: episodic lifecycle delegates verbatim to the base store -----

    async def get_active_users(self, *, within_days: int = 30) -> list[str]:
        return await self._base.get_active_users(within_days=within_days)

    async def get_recent_episodes(self, user_id: str, *, days: int = 7) -> list[MemoryRecord]:
        return await self._base.get_recent_episodes(user_id, days=days)

    async def get_promoted_episodes(self, user_id: str, *, limit: int = 20) -> list[MemoryRecord]:
        return await self._base.get_promoted_episodes(user_id, limit=limit)

    async def promote_episode(self, episode_id: str) -> None:
        await self._base.promote_episode(episode_id)

    async def decay_episodes(self, user_id: str, *, older_than_days: int = 30) -> int:
        return await self._base.decay_episodes(user_id, older_than_days=older_than_days)

    async def save_reflection(self, user_id: str, content: str) -> None:
        await self._base.save_reflection(user_id, content)

    async def get_reflection(self, user_id: str) -> str | None:
        return await self._base.get_reflection(user_id)

    async def delete(self, user_id: str) -> None:
        # Erase Phase 1 tiers first (idempotent), then the graph. We must NOT report
        # success if the graph can't be erased — better to fail loudly and retry.
        await self._base.delete(user_id)
        if self._graph is None:
            raise RuntimeError(
                f"cannot fully delete user {user_id}: graph store unavailable "
                "(Postgres/Redis erased; re-run delete when FalkorDB is reachable)"
            )
        await self._graph.delete_user(user_id)

    # --- Phase 2 graph ------------------------------------------------------

    async def write_nodes(self, nodes: list[GraphNode]) -> None:
        """Persist extracted nodes, applying temporal resolution to Facts."""
        if self._graph is None or not nodes:
            if nodes:
                logger.warning("graph disabled — dropping %d extracted nodes", len(nodes))
            return
        try:
            for node in nodes:
                if node.type is NodeType.FACT:
                    existing = await self._graph.find_current(node.user_id, NodeType.FACT, node.key)
                    if existing is not None:
                        old_id, old_content = existing
                        if old_content == node.content:
                            continue  # unchanged truth — don't churn the validity window
                        await self._graph.close_node(old_id, node.valid_from)
                    await self._graph.insert_node(node)
                else:
                    # EmotionalSignals are append-only (decision 1). Commitments now go
                    # through write_commitments (Phase 5) as their own CommitmentNode type.
                    await self._graph.insert_node(node)
        except Exception:
            logger.exception("failed writing %d graph nodes for user", len(nodes))

    async def get_current_facts(self, user_id: str) -> list[GraphNode]:
        """Only what is true NOW (valid_until IS NULL) for this user."""
        return await self._current(user_id, NodeType.FACT)

    async def get_open_commitments(self, user_id: str) -> list[CommitmentNode]:
        """Pending + due commitments (Phase 5 lifecycle), or [] if the graph is down."""
        if self._graph is None:
            return []
        try:
            return await self._graph.get_open_commitments(user_id, limit=self._limit)
        except Exception:
            logger.exception("commitment read failed for user %s", user_id)
            return []

    async def get_emotional_signals(self, user_id: str) -> list[GraphNode]:
        return await self._current(user_id, NodeType.EMOTIONAL_SIGNAL)

    async def resolve_duplicate_facts(self, user_id: str) -> list[dict]:
        """Sleep-pass RESOLVE: close drifted duplicate current Facts (same key).

        Extraction resolves contradictions in real time, but drift can accumulate.
        For each key with >1 current Fact, keep the highest-confidence node and close
        the rest. Returns an audit row per closure. Degrades to [] if graph is down.
        """
        if self._graph is None:
            return []
        try:
            facts = await self._graph.current(user_id, NodeType.FACT, limit=self._limit)
        except Exception:
            logger.exception("resolve: graph read failed for user %s", user_id)
            return []

        by_key: dict[str, list[GraphNode]] = {}
        for f in facts:
            by_key.setdefault(f.key, []).append(f)

        now = datetime.now(UTC)
        resolutions: list[dict] = []
        for key, group in by_key.items():
            if len(group) < 2:
                continue
            # Keep highest confidence; tie-break on most recent valid_from.
            group.sort(key=lambda n: (n.confidence, n.valid_from), reverse=True)
            kept, losers = group[0], group[1:]
            for loser in losers:
                try:
                    await self._graph.close_node(loser.id, now)
                except Exception:
                    logger.exception("resolve: failed closing node %s", loser.id)
                    continue
                row = {
                    "user_id": user_id,
                    "key": key,
                    "kept_id": kept.id,
                    "closed_id": loser.id,
                }
                logger.info("resolve: closed duplicate fact %s", row)
                resolutions.append(row)
        return resolutions

    async def decay_stale_facts(self, user_id: str) -> int:
        """Sleep-pass DECAY: lower confidence on facts unmentioned for the window."""
        if self._graph is None:
            return 0
        now = datetime.now(UTC)
        before = now - timedelta(days=self._decay_days)
        try:
            return await self._graph.decay_confidence(
                user_id,
                before=before,
                now=now,
                factor=self._decay_factor,
                floor=self._decay_floor,
            )
        except Exception:
            logger.exception("decay: graph update failed for user %s", user_id)
            return 0

    # --- Phase 4: inferred-trait pattern layer ------------------------------
    #
    # The supersede-by-key policy lives HERE (not GraphStore), mirroring write_nodes
    # for Facts, so it's provable against the in-memory double. A detect-driven
    # supersede only CLOSES the old window (close_node) and keeps its status — that's
    # historical inference, not a user correction. correct_trait (status=corrected) is
    # reserved for the reflect-back path in companion.py. A user-CONFIRMED trait is
    # authoritative: detect() never supersedes it (that would silently undo a
    # confirmation or re-open a correction) — confirmed traits change only via the
    # reflect-back loop. This also makes a repeat sleep pass deterministically
    # idempotent regardless of detection wording drift.

    async def write_traits(self, traits: list[InferredTrait]) -> None:
        """Persist inferred traits, applying temporal resolution by key."""
        if self._graph is None or not traits:
            if traits:
                logger.warning("graph disabled — dropping %d inferred traits", len(traits))
            return
        try:
            for trait in traits:
                existing = await self._graph.find_current_trait(trait.user_id, trait.key)
                if existing is not None:
                    old_id, old_content, old_status = existing
                    if old_status == TraitStatus.CONFIRMED:
                        # Confirmed is authoritative — detect must not clobber it, but a
                        # re-detection still corroborates it: refresh last_detected_at.
                        await self._graph.touch_trait(old_id, last_detected_at=trait.valid_from)
                        continue
                    if old_content == trait.content or _similar(old_content, trait.content):
                        # Same pattern re-detected (exact or reworded) — no churn, just
                        # refresh last_detected_at so it isn't pruned as stale.
                        await self._graph.touch_trait(old_id, last_detected_at=trait.valid_from)
                        continue
                    # Genuinely different content under the same key -> supersede.
                    await self._graph.close_node(old_id, trait.valid_from)
                await self._graph.insert_trait(trait)
        except Exception:
            logger.exception("failed writing %d inferred traits", len(traits))

    async def get_current_traits(self, user_id: str) -> list[InferredTrait]:
        if self._graph is None:
            return []
        try:
            return await self._graph.get_current_traits(user_id, limit=self._limit)
        except Exception:
            logger.exception("trait read failed for user %s", user_id)
            return []

    async def get_trait_by_id(self, trait_id: str) -> InferredTrait | None:
        if self._graph is None:
            return None
        try:
            return await self._graph.get_trait_by_id(trait_id)
        except Exception:
            logger.exception("get_trait_by_id failed for trait %s", trait_id)
            return None

    async def get_trait_for_reflect(
        self, user_id: str, session_id: str, *, min_confidence: float
    ) -> InferredTrait | None:
        if self._graph is None:
            return None
        try:
            return await self._graph.get_trait_for_reflect(
                user_id, session_id, min_confidence=min_confidence
            )
        except Exception:
            logger.exception("get_trait_for_reflect failed for user %s", user_id)
            return None

    async def confirm_trait(self, trait_id: str, *, confidence_bump: float) -> None:
        if self._graph is None:
            return
        try:
            await self._graph.confirm_trait(trait_id, confidence_bump, now=datetime.now(UTC))
        except Exception:
            logger.exception("confirm_trait failed for trait %s", trait_id)

    async def correct_trait(self, trait_id: str) -> None:
        if self._graph is None:
            return
        try:
            await self._graph.correct_trait(trait_id, now=datetime.now(UTC))
        except Exception:
            logger.exception("correct_trait failed for trait %s", trait_id)

    async def mark_trait_surfaced(self, trait_id: str, session_id: str) -> None:
        if self._graph is None:
            return
        try:
            await self._graph.mark_trait_surfaced(trait_id, session_id)
        except Exception:
            logger.exception("mark_trait_surfaced failed for trait %s", trait_id)

    async def consolidate_traits(self, user_id: str, groups: list[list[str]]) -> int:
        """Sleep-pass CONSOLIDATE (Phase 5.3): merge cross-key duplicate INFERRED traits.

        ``groups`` is a list of key-groups the model flagged as the same pattern. For
        each group we keep the highest-confidence inferred member and close the rest
        (close_node), bumping the kept one's last_detected_at. CONFIRMED traits are never
        merged — they're filtered out, so a group that loses its only inferred members is
        skipped. Merges are closes (auditable), not deletes. Returns how many were closed.
        """
        if self._graph is None or not groups:
            return 0
        try:
            current = await self._graph.get_current_traits(user_id, limit=self._limit)
        except Exception:
            logger.exception("consolidate: trait read failed for user %s", user_id)
            return 0
        by_key = {t.key: t for t in current if t.status is TraitStatus.INFERRED}
        now = datetime.now(UTC)
        merged = 0
        used: set[str] = set()  # a key can't be merged under two groups
        try:
            for group in groups:
                members = [by_key[k] for k in group if k in by_key and k not in used]
                if len(members) < 2:
                    continue
                used.update(m.key for m in members)
                # Keep highest confidence; tie-break on most recent valid_from.
                members.sort(key=lambda t: (t.confidence, t.valid_from), reverse=True)
                kept, losers = members[0], members[1:]
                await self._graph.touch_trait(kept.id, last_detected_at=now)
                for loser in losers:
                    await self._graph.close_node(loser.id, now)
                    merged += 1
        except Exception:
            logger.exception("consolidate: merge failed for user %s", user_id)
        return merged

    async def prune_stale_traits(self, user_id: str, *, stale_days: int) -> int:
        """Sleep-pass PRUNE: close INFERRED traits not re-detected in ``stale_days``.
        Confirmed traits are never pruned. Bounds unbounded inferred-trait growth."""
        if self._graph is None:
            return 0
        now = datetime.now(UTC)
        before = now - timedelta(days=stale_days)
        try:
            return await self._graph.prune_stale_inferred_traits(user_id, before=before, now=now)
        except Exception:
            logger.exception("prune_stale_traits failed for user %s", user_id)
            return 0

    # --- Phase 5: commitment lifecycle (graph) --------------------------------
    # Append-only writes; lifecycle mutation by id. All degrade to no-op/[] when the
    # graph is down so proactivity falls through to the Postgres-only general check-in.

    async def write_commitments(self, commitments: list[CommitmentNode]) -> None:
        """Persist commitments with SOFT DEDUP of unresolved duplicates (Phase 5.1/5.4).

        Append-only was creating noise (a chatty user re-stating the same intent made a
        new node each day). Policy: if an OPEN (pending/due) commitment with the same key
        already exists, just bump its mention_count and refresh expected_by instead of
        inserting. MERGE ON KEY ALONE (Phase 5.4): commitment keys are now reliable —
        extraction feeds the user's open commitments back so the model reuses a key only
        for the SAME intent (see transcript_for_extraction / EXTRACTION_SYSTEM), and keys
        are descriptive-per-intent, so a genuinely different commitment gets a different
        key. The old difflib content gate (>= 0.6) is dropped here: it was the same
        char-similarity that proved unsound for traits and, post key-fix, only blocked
        merging reworded restatements of the same intent under their reused key (one
        intent restated each session scored as low as 0.07, piling up duplicate nodes).
        Resolved commitments are untouched — history is preserved; only open ones merge.
        """
        if self._graph is None or not commitments:
            if commitments:
                logger.warning("graph disabled — dropping %d commitments", len(commitments))
            return
        try:
            for c in commitments:
                existing = await self._graph.find_open_commitment(c.user_id, c.key)
                if existing is not None:
                    # Same key on an open commitment ⇒ same intent. Refresh the time only
                    # if this restatement supplied a (newer) one.
                    new_expected = c.expected_by if c.expected_by is not None else None
                    await self._graph.touch_commitment(existing.id, expected_by=new_expected)
                else:
                    await self._graph.insert_commitment(c)
        except Exception:
            logger.exception("failed writing %d commitments", len(commitments))

    async def consolidate_commitments(self, user_id: str, id_groups: list[list[str]]) -> int:
        """Sleep-pass: merge cross-key duplicate OPEN commitments (Phase 5.3). For each
        group of commitment ids the model flagged as the same intent, keep the most
        actionable one (soonest expected_by, else newest) and close the rest (close_node —
        auditable). RESOLVED commitments are never candidates (get_open returns only
        pending/due), so follow-through history is preserved. Returns how many were closed.
        """
        if self._graph is None or not id_groups:
            return 0
        try:
            openc = await self._graph.get_open_commitments(user_id, limit=self._limit)
        except Exception:
            logger.exception("consolidate-commitments: read failed for user %s", user_id)
            return 0
        by_id = {c.id: c for c in openc}
        now = datetime.now(UTC)
        merged = 0
        used: set[str] = set()
        try:
            for group in id_groups:
                members = [by_id[i] for i in group if i in by_id and i not in used]
                if len(members) < 2:
                    continue
                used.update(m.id for m in members)
                with_deadline = [m for m in members if m.expected_by is not None]
                if with_deadline:
                    kept = min(with_deadline, key=lambda c: c.expected_by)
                else:
                    kept = max(members, key=lambda c: c.valid_from)
                soonest = min(
                    (m.expected_by for m in members if m.expected_by is not None), default=None
                )
                # Record the absorption + keep the best deadline on the survivor.
                await self._graph.touch_commitment(kept.id, expected_by=soonest)
                for loser in members:
                    if loser.id == kept.id:
                        continue
                    await self._graph.close_node(loser.id, now)
                    merged += 1
        except Exception:
            logger.exception("consolidate-commitments: merge failed for user %s", user_id)
        return merged

    async def get_pending_commitments(self, user_id: str) -> list[CommitmentNode]:
        if self._graph is None:
            return []
        try:
            return await self._graph.get_pending_commitments(user_id, limit=self._limit)
        except Exception:
            logger.exception("pending-commitment read failed for user %s", user_id)
            return []

    async def get_due_commitments(self, user_id: str) -> list[CommitmentNode]:
        if self._graph is None:
            return []
        try:
            return await self._graph.get_due_commitments(user_id, now=datetime.now(UTC))
        except Exception:
            logger.exception("due-commitment read failed for user %s", user_id)
            return []

    async def get_upcoming_commitments(
        self, user_id: str, *, within_hours: int
    ) -> list[CommitmentNode]:
        if self._graph is None:
            return []
        now = datetime.now(UTC)
        try:
            return await self._graph.get_upcoming_commitments(
                user_id, now=now, until=now + timedelta(hours=within_hours)
            )
        except Exception:
            logger.exception("upcoming-commitment read failed for user %s", user_id)
            return []

    async def mark_commitment_due(self, commitment_id: str) -> None:
        if self._graph is None:
            return
        try:
            await self._graph.update_commitment_status(commitment_id, CommitmentStatus.DUE)
        except Exception:
            logger.exception("mark_commitment_due failed for %s", commitment_id)

    async def mark_commitment_reminded(self, commitment_id: str) -> None:
        if self._graph is None:
            return
        try:
            await self._graph.mark_commitment_reminded(commitment_id, now=datetime.now(UTC))
        except Exception:
            logger.exception("mark_commitment_reminded failed for %s", commitment_id)

    async def resolve_commitment(self, commitment_id: str, *, kept: bool) -> None:
        if self._graph is None:
            return
        try:
            await self._graph.resolve_commitment(commitment_id, kept=kept, now=datetime.now(UTC))
        except Exception:
            logger.exception("resolve_commitment failed for %s", commitment_id)

    # --- Phase 5: proactive check-in queue (delegates to the Postgres base) ----

    async def queue_checkin(self, checkin: PendingCheckin) -> None:
        await self._base.queue_checkin(checkin)

    async def get_pending_checkin(self, user_id: str) -> PendingCheckin | None:
        return await self._base.get_pending_checkin(user_id)

    async def mark_checkin_delivered(self, checkin_id: str) -> None:
        await self._base.mark_checkin_delivered(checkin_id)

    async def get_last_session_at(self, user_id: str) -> datetime | None:
        return await self._base.get_last_session_at(user_id)

    async def reflect_back_ready(self, user_id: str) -> bool:
        return await self._base.reflect_back_ready(user_id)

    async def set_reflect_back_cooldown(self, user_id: str, sessions: int) -> None:
        await self._base.set_reflect_back_cooldown(user_id, sessions)

    async def decrement_reflect_back_cooldown(self, user_id: str) -> None:
        await self._base.decrement_reflect_back_cooldown(user_id)

    # --- Living profile: behavioral dimensions (Postgres — delegate to base) ---

    async def get_profile_dimensions(self, user_id: str) -> list[ProfileDimension]:
        return await self._base.get_profile_dimensions(user_id)

    async def put_profile_dimension(self, dimension: ProfileDimension) -> None:
        await self._base.put_profile_dimension(dimension)

    async def get_dimension_to_confirm(
        self, user_id: str, session_id: str, *, min_confidence: float, min_observations: int
    ) -> ProfileDimension | None:
        return await self._base.get_dimension_to_confirm(
            user_id, session_id, min_confidence=min_confidence, min_observations=min_observations
        )

    async def confirm_dimension(
        self, user_id: str, dimension: str, *, confidence_bump: float, session_id: str | None = None
    ) -> None:
        await self._base.confirm_dimension(
            user_id, dimension, confidence_bump=confidence_bump, session_id=session_id
        )

    async def correct_dimension(
        self, user_id: str, dimension: str, *, session_id: str | None = None
    ) -> None:
        await self._base.correct_dimension(user_id, dimension, session_id=session_id)

    async def mark_dimension_surfaced(self, user_id: str, dimension: str, session_id: str) -> None:
        await self._base.mark_dimension_surfaced(user_id, dimension, session_id)

    async def _current(self, user_id: str, node_type: NodeType) -> list[GraphNode]:
        if self._graph is None:
            return []
        try:
            return await self._graph.current(user_id, node_type, limit=self._limit)
        except Exception:
            logger.exception("graph read failed for user %s (%s)", user_id, node_type)
            return []
