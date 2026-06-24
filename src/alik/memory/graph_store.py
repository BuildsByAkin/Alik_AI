"""FalkorDB access for the temporal graph.

This is the ONLY module that imports the FalkorDB driver. Everything else goes
through :class:`alik.memory.graph.GraphMemory`, which holds a ``GraphStore`` and
adds the temporal-resolution logic.

The store deals only in primitives (insert / find-current / close / query /
delete); it does not decide *when* to supersede a node — that policy lives in
``GraphMemory`` so it stays infra-free and testable against a double.
"""

from __future__ import annotations

from datetime import UTC, datetime

from falkordb.asyncio import FalkorDB

from alik.models import (
    CommitmentNode,
    CommitmentStatus,
    GraphNode,
    InferredTrait,
    NodeType,
    ProvenanceRecord,
    TraitStatus,
)

# Cypher labels can't be parameterized, so map the enum to a fixed label string.
# Values come from our own enum (never user input), so f-string interpolation is safe.
_LABELS: dict[NodeType, str] = {
    NodeType.FACT: "Fact",
    NodeType.EMOTIONAL_SIGNAL: "EmotionalSignal",
    NodeType.COMMITMENT: "Commitment",
}


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _parse_dt(value: str | None) -> datetime | None:
    """Parse an ISO timestamp back to a TZ-AWARE datetime.

    Model-sourced timestamps (e.g. a commitment's ``expected_by``) can arrive without
    an offset; we treat a naive value as UTC so it never collides with the TZ-aware
    ``datetime.now(UTC)`` used in comparisons (e.g. the commitment tick pass)."""
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


class GraphStore:
    """Thin async wrapper over a FalkorDB graph. Stores timestamps as ISO strings."""

    def __init__(self, db: FalkorDB, graph_name: str) -> None:
        self._db = db
        self._graph = db.select_graph(graph_name)

    @classmethod
    def from_url(cls, url: str, *, graph_name: str) -> GraphStore:
        """Connect to FalkorDB. Raises if the server is unreachable (handled by
        ``GraphMemory.connect``, which then runs in graph-disabled / degraded mode)."""
        return cls(FalkorDB.from_url(url), graph_name)

    async def aclose(self) -> None:
        await self._db.aclose()

    async def insert_node(self, node: GraphNode) -> None:
        label = _LABELS[node.type]
        await self._graph.query(
            f"CREATE (n:{label} {{"
            "id: $id, user_id: $user_id, key: $key, content: $content, "
            "valid_from: $valid_from, valid_until: $valid_until, "
            "confidence: $confidence, source_session_id: $source_session_id})",
            {
                "id": node.id,
                "user_id": node.user_id,
                "key": node.key,
                "content": node.content,
                "valid_from": _iso(node.valid_from),
                "valid_until": _iso(node.valid_until),
                "confidence": node.confidence,
                "source_session_id": node.source_session_id,
            },
        )

    async def find_current(
        self, user_id: str, node_type: NodeType, key: str
    ) -> tuple[str, str] | None:
        """Return ``(id, content)`` of the current node for this entity, or None."""
        label = _LABELS[node_type]
        res = await self._graph.query(
            f"MATCH (n:{label} {{user_id: $user_id, key: $key}}) "
            "WHERE n.valid_until IS NULL "
            "RETURN n.id, n.content LIMIT 1",
            {"user_id": user_id, "key": key},
        )
        if not res.result_set:
            return None
        row = res.result_set[0]
        return row[0], row[1]

    async def close_node(self, node_id: str, valid_until: datetime) -> None:
        await self._graph.query(
            "MATCH (n {id: $id}) SET n.valid_until = $valid_until",
            {"id": node_id, "valid_until": _iso(valid_until)},
        )

    async def current(self, user_id: str, node_type: NodeType, *, limit: int) -> list[GraphNode]:
        """All currently-true nodes of one type for a user (newest first)."""
        label = _LABELS[node_type]
        res = await self._graph.query(
            f"MATCH (n:{label} {{user_id: $user_id}}) "
            "WHERE n.valid_until IS NULL "
            "RETURN n.id, n.key, n.content, n.valid_from, n.confidence, n.source_session_id "
            "ORDER BY n.valid_from DESC LIMIT $limit",
            {"user_id": user_id, "limit": limit},
        )
        nodes: list[GraphNode] = []
        for row in res.result_set:
            node_id, key, content, valid_from, confidence, source = row
            nodes.append(
                GraphNode(
                    user_id=user_id,
                    type=node_type,
                    key=key,
                    content=content,
                    valid_from=datetime.fromisoformat(valid_from),
                    valid_until=None,
                    confidence=float(confidence) if confidence is not None else 1.0,
                    source_session_id=source,
                    id=node_id,
                )
            )
        return nodes

    async def decay_confidence(
        self,
        user_id: str,
        *,
        before: datetime,
        now: datetime,
        factor: float,
        floor: float,
    ) -> int:
        """Multiply confidence of stale current Facts by ``factor`` (clamped to ``floor``).

        Stale = current Fact whose ``valid_from`` (our last-mentioned proxy, see
        CLAUDE.md) is at or before ``before``. The ``last_decayed_at`` guard ensures
        a fact is decayed at most once per window, so a daily cron can't compound it.
        Returns the number of facts decayed.
        """
        res = await self._graph.query(
            "MATCH (n:Fact {user_id: $user_id}) "
            "WHERE n.valid_until IS NULL AND n.valid_from <= $before "
            "AND (n.last_decayed_at IS NULL OR n.last_decayed_at <= $before) "
            "SET n.confidence = "
            "CASE WHEN n.confidence * $factor < $floor THEN $floor "
            "ELSE n.confidence * $factor END, "
            "n.last_decayed_at = $now "
            "RETURN count(n)",
            {
                "user_id": user_id,
                "before": _iso(before),
                "now": _iso(now),
                "factor": factor,
                "floor": floor,
            },
        )
        return int(res.result_set[0][0]) if res.result_set else 0

    # --- Phase 4: InferredTrait primitives ----------------------------------
    #
    # Pure primitives only (insert / find-current / query / status mutation). The
    # supersede-by-key POLICY lives in GraphMemory.write_traits, mirroring Facts,
    # so it stays infra-free and testable against the in-memory double.

    # Stable column order shared by every trait-returning query.
    _TRAIT_COLS = (
        "n.id, n.user_id, n.key, n.content, n.confidence, n.valid_from, n.valid_until, "
        "n.status, n.status_updated_at, n.surfaced_in_session, n.source_session_id, "
        "n.provenance_episode_ids, n.provenance_signal_ids, "
        "COALESCE(n.last_detected_at, n.valid_from)"
    )

    @staticmethod
    def _trait_from_row(row: list) -> InferredTrait:
        (
            tid,
            user_id,
            key,
            content,
            confidence,
            valid_from,
            valid_until,
            status,
            status_updated_at,
            surfaced_in_session,
            source_session_id,
            episode_ids,
            signal_ids,
            last_detected_at,
        ) = row
        return InferredTrait(
            user_id=user_id,
            key=key,
            content=content,
            confidence=float(confidence) if confidence is not None else 0.0,
            valid_from=_parse_dt(valid_from),
            valid_until=_parse_dt(valid_until),
            status=TraitStatus(status),
            status_updated_at=_parse_dt(status_updated_at),
            surfaced_in_session=surfaced_in_session,
            source_session_id=source_session_id,
            last_detected_at=_parse_dt(last_detected_at),
            provenance=ProvenanceRecord(
                episode_ids=list(episode_ids or []),
                signal_ids=list(signal_ids or []),
            ),
            id=tid,
        )

    async def insert_trait(self, trait: InferredTrait) -> None:
        """Persist one trait verbatim. No resolution — caller (GraphMemory) decides."""
        last_detected = trait.last_detected_at or trait.valid_from
        await self._graph.query(
            "CREATE (n:InferredTrait {"
            "id: $id, user_id: $user_id, key: $key, content: $content, "
            "confidence: $confidence, valid_from: $valid_from, valid_until: $valid_until, "
            "status: $status, status_updated_at: $status_updated_at, "
            "surfaced_in_session: $surfaced_in_session, source_session_id: $source_session_id, "
            "last_detected_at: $last_detected_at, "
            "provenance_episode_ids: $episode_ids, provenance_signal_ids: $signal_ids})",
            {
                "id": trait.id,
                "user_id": trait.user_id,
                "key": trait.key,
                "content": trait.content,
                "confidence": trait.confidence,
                "valid_from": _iso(trait.valid_from),
                "valid_until": _iso(trait.valid_until),
                "status": str(trait.status),
                "status_updated_at": _iso(trait.status_updated_at),
                "surfaced_in_session": trait.surfaced_in_session,
                "source_session_id": trait.source_session_id,
                "last_detected_at": _iso(last_detected),
                "episode_ids": trait.provenance.episode_ids,
                "signal_ids": trait.provenance.signal_ids,
            },
        )

    async def touch_trait(self, trait_id: str, *, last_detected_at: datetime) -> None:
        """Refresh last_detected_at when detect() re-sees a pattern (the no-op path).
        This is the trait 'last_seen' — what keeps a re-corroborated pattern from being
        pruned as stale."""
        await self._graph.query(
            "MATCH (n:InferredTrait {id: $id}) SET n.last_detected_at = $ts",
            {"id": trait_id, "ts": _iso(last_detected_at)},
        )

    async def prune_stale_inferred_traits(
        self, user_id: str, *, before: datetime, now: datetime
    ) -> int:
        """Close current INFERRED traits not re-detected since ``before`` (stale: the
        pattern hasn't shown up in conversation for the staleness window). CONFIRMED
        traits are never touched — only user action closes those. Returns the count."""
        res = await self._graph.query(
            "MATCH (n:InferredTrait {user_id: $user_id}) "
            "WHERE n.valid_until IS NULL AND n.status = $inferred "
            "AND COALESCE(n.last_detected_at, n.valid_from) <= $before "
            "SET n.valid_until = $now RETURN count(n)",
            {
                "user_id": user_id,
                "inferred": str(TraitStatus.INFERRED),
                "before": _iso(before),
                "now": _iso(now),
            },
        )
        return int(res.result_set[0][0]) if res.result_set else 0

    async def find_current_trait(self, user_id: str, key: str) -> tuple[str, str, str] | None:
        """Return ``(id, content, status)`` of the current trait for this key, or None."""
        res = await self._graph.query(
            "MATCH (n:InferredTrait {user_id: $user_id, key: $key}) "
            "WHERE n.valid_until IS NULL "
            "RETURN n.id, n.content, n.status LIMIT 1",
            {"user_id": user_id, "key": key},
        )
        if not res.result_set:
            return None
        row = res.result_set[0]
        return row[0], row[1], row[2]

    async def get_current_traits(self, user_id: str, *, limit: int) -> list[InferredTrait]:
        """All currently-active traits for a user (newest first)."""
        res = await self._graph.query(
            f"MATCH (n:InferredTrait {{user_id: $user_id}}) "
            f"WHERE n.valid_until IS NULL "
            f"RETURN {self._TRAIT_COLS} "
            "ORDER BY n.valid_from DESC LIMIT $limit",
            {"user_id": user_id, "limit": limit},
        )
        return [self._trait_from_row(row) for row in res.result_set]

    async def get_trait_by_id(self, trait_id: str) -> InferredTrait | None:
        """Fetch a single trait by id regardless of status/window (for --explain-trait)."""
        res = await self._graph.query(
            f"MATCH (n:InferredTrait {{id: $id}}) RETURN {self._TRAIT_COLS} LIMIT 1",
            {"id": trait_id},
        )
        if not res.result_set:
            return None
        return self._trait_from_row(res.result_set[0])

    async def confirm_trait(self, trait_id: str, confidence_bump: float, *, now: datetime) -> None:
        """Reflect-back confirm: status=confirmed, confidence += bump (capped at 1.0)."""
        await self._graph.query(
            "MATCH (n:InferredTrait {id: $id}) "
            "SET n.status = $status, n.status_updated_at = $now, "
            "n.confidence = CASE WHEN n.confidence + $bump > 1.0 THEN 1.0 "
            "ELSE n.confidence + $bump END",
            {
                "id": trait_id,
                "status": str(TraitStatus.CONFIRMED),
                "now": _iso(now),
                "bump": confidence_bump,
            },
        )

    async def correct_trait(self, trait_id: str, *, now: datetime) -> None:
        """Reflect-back correct: close the window and mark the trait corrected."""
        await self._graph.query(
            "MATCH (n:InferredTrait {id: $id}) "
            "SET n.valid_until = $now, n.status = $status, n.status_updated_at = $now",
            {"id": trait_id, "now": _iso(now), "status": str(TraitStatus.CORRECTED)},
        )

    async def get_trait_for_reflect(
        self, user_id: str, session_id: str, *, min_confidence: float
    ) -> InferredTrait | None:
        """Single eligible trait to surface: inferred, confident, not yet surfaced here."""
        res = await self._graph.query(
            f"MATCH (n:InferredTrait {{user_id: $user_id}}) "
            f"WHERE n.valid_until IS NULL AND n.status = $status "
            f"AND n.confidence >= $min_confidence "
            f"AND (n.surfaced_in_session IS NULL OR n.surfaced_in_session <> $session_id) "
            f"RETURN {self._TRAIT_COLS} "
            "ORDER BY n.confidence DESC, n.valid_from DESC LIMIT 1",
            {
                "user_id": user_id,
                "session_id": session_id,
                "status": str(TraitStatus.INFERRED),
                "min_confidence": min_confidence,
            },
        )
        if not res.result_set:
            return None
        return self._trait_from_row(res.result_set[0])

    async def mark_trait_surfaced(self, trait_id: str, session_id: str) -> None:
        """Record that we reflected this trait back in this session (no repeats here)."""
        await self._graph.query(
            "MATCH (n:InferredTrait {id: $id}) SET n.surfaced_in_session = $session_id",
            {"id": trait_id, "session_id": session_id},
        )

    # --- Phase 5: Commitment lifecycle primitives ---------------------------
    #
    # Commitments are append-only (no supersede). The store only reads/mutates
    # lifecycle props; the WHEN-to-mark-due policy lives in sleep_pass.tick_commitments.
    # Pre-Phase-5 nodes have no status property, so every read COALESCEs it to 'pending'.

    _COMMIT_COLS = (
        "n.id, n.user_id, n.key, n.content, n.valid_from, n.valid_until, "
        "COALESCE(n.status, 'pending'), n.expected_by, n.resolved_at, n.follow_through, "
        "COALESCE(n.reminded_count, 0), n.last_reminded_at, n.confidence, n.source_session_id, "
        "COALESCE(n.mention_count, 1)"
    )

    @staticmethod
    def _commitment_from_row(row: list) -> CommitmentNode:
        (
            cid,
            user_id,
            key,
            content,
            valid_from,
            valid_until,
            status,
            expected_by,
            resolved_at,
            follow_through,
            reminded_count,
            last_reminded_at,
            confidence,
            source_session_id,
            mention_count,
        ) = row
        return CommitmentNode(
            user_id=user_id,
            key=key,
            content=content,
            valid_from=_parse_dt(valid_from),
            valid_until=_parse_dt(valid_until),
            status=CommitmentStatus(status),
            expected_by=_parse_dt(expected_by),
            resolved_at=_parse_dt(resolved_at),
            follow_through=follow_through,
            reminded_count=int(reminded_count) if reminded_count is not None else 0,
            last_reminded_at=_parse_dt(last_reminded_at),
            confidence=float(confidence) if confidence is not None else 1.0,
            source_session_id=source_session_id,
            mention_count=int(mention_count) if mention_count is not None else 1,
            id=cid,
        )

    async def insert_commitment(self, c: CommitmentNode) -> None:
        """Persist one commitment with its full lifecycle props (append-only)."""
        await self._graph.query(
            "CREATE (n:Commitment {"
            "id: $id, user_id: $user_id, key: $key, content: $content, "
            "valid_from: $valid_from, valid_until: $valid_until, status: $status, "
            "expected_by: $expected_by, resolved_at: $resolved_at, "
            "follow_through: $follow_through, reminded_count: $reminded_count, "
            "last_reminded_at: $last_reminded_at, confidence: $confidence, "
            "source_session_id: $source_session_id, mention_count: $mention_count})",
            {
                "id": c.id,
                "user_id": c.user_id,
                "key": c.key,
                "content": c.content,
                "valid_from": _iso(c.valid_from),
                "valid_until": _iso(c.valid_until),
                "status": str(c.status),
                "expected_by": _iso(c.expected_by),
                "resolved_at": _iso(c.resolved_at),
                "follow_through": c.follow_through,
                "reminded_count": c.reminded_count,
                "last_reminded_at": _iso(c.last_reminded_at),
                "confidence": c.confidence,
                "source_session_id": c.source_session_id,
                "mention_count": c.mention_count,
            },
        )

    async def find_open_commitment(self, user_id: str, key: str) -> CommitmentNode | None:
        """The most recent OPEN (pending/due, valid_until IS NULL) commitment for this
        key, or None — the dedup target for write_commitments (Phase 5.1)."""
        res = await self._graph.query(
            f"MATCH (n:Commitment {{user_id: $user_id, key: $key}}) "
            f"WHERE n.valid_until IS NULL AND COALESCE(n.status, 'pending') IN ['pending', 'due'] "
            f"RETURN {self._COMMIT_COLS} ORDER BY n.valid_from DESC LIMIT 1",
            {"user_id": user_id, "key": key},
        )
        if not res.result_set:
            return None
        return self._commitment_from_row(res.result_set[0])

    async def touch_commitment(self, commitment_id: str, *, expected_by: datetime | None) -> None:
        """Dedup hit: bump mention_count and refresh expected_by (only if a new time was
        supplied) — no new node, history of the open commitment is preserved as one."""
        await self._graph.query(
            "MATCH (n:Commitment {id: $id}) "
            "SET n.mention_count = COALESCE(n.mention_count, 1) + 1, "
            "n.expected_by = CASE WHEN $expected_by IS NULL THEN n.expected_by "
            "ELSE $expected_by END",
            {"id": commitment_id, "expected_by": _iso(expected_by)},
        )

    async def get_open_commitments(self, user_id: str, *, limit: int) -> list[CommitmentNode]:
        """Pending + due commitments (not yet resolved), newest first."""
        res = await self._graph.query(
            f"MATCH (n:Commitment {{user_id: $user_id}}) "
            f"WHERE n.valid_until IS NULL "
            f"AND COALESCE(n.status, 'pending') IN ['pending', 'due'] "
            f"RETURN {self._COMMIT_COLS} ORDER BY n.valid_from DESC LIMIT $limit",
            {"user_id": user_id, "limit": limit},
        )
        return [self._commitment_from_row(r) for r in res.result_set]

    async def get_pending_commitments(self, user_id: str, *, limit: int) -> list[CommitmentNode]:
        """All not-yet-due, not-yet-resolved commitments — input to the tick pass."""
        res = await self._graph.query(
            f"MATCH (n:Commitment {{user_id: $user_id}}) "
            f"WHERE n.valid_until IS NULL AND COALESCE(n.status, 'pending') = 'pending' "
            f"RETURN {self._COMMIT_COLS} ORDER BY n.valid_from ASC LIMIT $limit",
            {"user_id": user_id, "limit": limit},
        )
        return [self._commitment_from_row(r) for r in res.result_set]

    async def get_due_commitments(self, user_id: str, *, now: datetime) -> list[CommitmentNode]:
        """Due commitments not yet asked about today (reminded_count 0 or last < today).

        Ordered so an explicit, most-overdue deadline is followed up first: commitments
        with an ``expected_by`` (earliest first) precede fallback-due ones (no deadline).
        ISO strings sort lexicographically, so COALESCE-to-far-future puts nulls last.
        """
        today = datetime(now.year, now.month, now.day, tzinfo=now.tzinfo)
        res = await self._graph.query(
            f"MATCH (n:Commitment {{user_id: $user_id}}) "
            f"WHERE n.valid_until IS NULL AND COALESCE(n.status, 'pending') = 'due' "
            f"AND (COALESCE(n.reminded_count, 0) = 0 OR n.last_reminded_at < $today) "
            f"RETURN {self._COMMIT_COLS} "
            f"ORDER BY COALESCE(n.expected_by, '9999-12-31') ASC, n.valid_from ASC",
            {"user_id": user_id, "today": _iso(today)},
        )
        return [self._commitment_from_row(r) for r in res.result_set]

    async def get_upcoming_commitments(
        self, user_id: str, *, now: datetime, until: datetime
    ) -> list[CommitmentNode]:
        """Pending commitments whose expected_by falls within (now, until]."""
        res = await self._graph.query(
            f"MATCH (n:Commitment {{user_id: $user_id}}) "
            f"WHERE n.valid_until IS NULL AND COALESCE(n.status, 'pending') = 'pending' "
            f"AND n.expected_by IS NOT NULL AND n.expected_by > $now AND n.expected_by <= $until "
            f"RETURN {self._COMMIT_COLS} ORDER BY n.expected_by ASC",
            {"user_id": user_id, "now": _iso(now), "until": _iso(until)},
        )
        return [self._commitment_from_row(r) for r in res.result_set]

    async def update_commitment_status(
        self,
        commitment_id: str,
        status: CommitmentStatus,
        *,
        resolved_at: datetime | None = None,
        follow_through: bool | None = None,
    ) -> None:
        await self._graph.query(
            "MATCH (n:Commitment {id: $id}) "
            "SET n.status = $status, n.resolved_at = $resolved_at, "
            "n.follow_through = $follow_through",
            {
                "id": commitment_id,
                "status": str(status),
                "resolved_at": _iso(resolved_at),
                "follow_through": follow_through,
            },
        )

    async def mark_commitment_reminded(self, commitment_id: str, *, now: datetime) -> None:
        await self._graph.query(
            "MATCH (n:Commitment {id: $id}) "
            "SET n.reminded_count = COALESCE(n.reminded_count, 0) + 1, n.last_reminded_at = $now",
            {"id": commitment_id, "now": _iso(now)},
        )

    async def resolve_commitment(self, commitment_id: str, *, kept: bool, now: datetime) -> None:
        status = CommitmentStatus.RESOLVED_KEPT if kept else CommitmentStatus.RESOLVED_DROPPED
        await self.update_commitment_status(
            commitment_id, status, resolved_at=now, follow_through=kept
        )

    async def delete_user(self, user_id: str) -> None:
        """Erase every node for a user. Legal requirement — must fully succeed.

        Label-agnostic, so InferredTrait (Phase 4) and Commitment (Phase 5) nodes are
        erased here too. (pending_checkins lives in Postgres; PgRedisMemory.delete erases it.)
        """
        await self._graph.query("MATCH (n {user_id: $user_id}) DELETE n", {"user_id": user_id})
