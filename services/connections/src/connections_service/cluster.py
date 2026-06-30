"""Group-awareness clustering: recognize when several mutually-compatible people share a
specific activity and surface ONE group introduction instead of separate 1:1s.

A clustering pass over the existing people<->interest graph + candidate_scores — pure SQL +
Python, no new graph DB, no re-scoring, no LLM.
"""

from __future__ import annotations

import logging
import time
import uuid

from connections_service.config import Settings
from connections_service.eval import _interest_label
from connections_service.models import GroupCandidate, GroupCheckin, GroupStatus
from connections_service.passlog import format_pass_summary
from connections_service.store import Store

logger = logging.getLogger("connections.cluster")


def _build_adjacency(
    users: list[str],
    scores: dict[frozenset[str], float],
    skipped: set[frozenset[str]],
    threshold: float,
) -> dict[str, set[str]]:
    """An undirected edge exists iff the pair scores at/above threshold and neither skipped."""
    adj: dict[str, set[str]] = {u: set() for u in users}
    for pair, score in scores.items():
        if score >= threshold and pair not in skipped:
            a, b = tuple(pair)
            if a in adj and b in adj:
                adj[a].add(b)
                adj[b].add(a)
    return adj


def bron_kerbosch(adj: dict[str, set[str]]) -> list[set[str]]:
    """All maximal cliques (with pivoting). Subgraphs are tiny at MN launch (≤~20 nodes per
    interest), so exact clique finding is fine. At scale: degeneracy-ordering BK, cap the
    candidate set, or switch to approximate community detection — the seam is right here."""
    cliques: list[set[str]] = []

    def expand(r: set[str], p: set[str], x: set[str]) -> None:
        if not p and not x:
            cliques.append(r)
            return
        pivot = max(p | x, key=lambda u: len(adj[u] & p))
        for v in list(p - adj[pivot]):
            expand(r | {v}, p & adj[v], x & adj[v])
            p = p - {v}
            x = x | {v}

    expand(set(), set(adj), set())
    return cliques


def _mean_pair_score(members: set[str], scores: dict[frozenset[str], float]) -> float:
    ms = sorted(members)
    pairs = [
        scores.get(frozenset((ms[i], ms[j])), 0.0)
        for i in range(len(ms))
        for j in range(i + 1, len(ms))
    ]
    return round(sum(pairs) / len(pairs), 4) if pairs else 0.0


def _trim_to_size(members: set[str], scores: dict[frozenset[str], float], target: int) -> set[str]:
    """Greedily shrink an over-large clique to ``target``: repeatedly drop the member with the
    lowest summed edge weight to the rest, keeping the tightest-knit subset."""
    group = set(members)
    while len(group) > target:
        worst = min(
            group,
            key=lambda u: sum(scores.get(frozenset((u, v)), 0.0) for v in group if v != u),
        )
        group.discard(worst)
    return group


async def clustering_pass(store: Store, brain_client, settings: Settings) -> dict[str, int]:
    counts = {"nodes": 0, "proposed": 0, "surfaced": 0}
    failures = 0  # summary-only (node-cluster + group-surface errors); counts dict unchanged.
    start = time.perf_counter()
    try:
        for state in sorted(settings.launch_states_set):
            for node in await store.get_clusterable_interest_nodes(state, settings.min_group_size):
                counts["nodes"] += 1
                try:
                    if await _cluster_node(store, settings, state, node):
                        counts["proposed"] += 1
                except Exception:
                    failures += 1
                    logger.exception("clustering failed for node %s", node)
        counts["surfaced"], surface_failures = await _surface_groups(store, brain_client, settings)
        failures += surface_failures
    finally:
        logger.info(
            format_pass_summary(
                "cluster",
                groups_proposed=counts["proposed"],
                groups_surfaced=counts["surfaced"],
                failures=failures,
                duration_s=round(time.perf_counter() - start, 1),
            )
        )
    return counts


async def _cluster_node(store: Store, settings: Settings, state: str, node: str) -> bool:
    users = await store.get_users_by_interest(node, state)
    if len(users) < settings.min_group_size:
        return False
    scores = await store.get_pairwise_scores(users)
    skipped = await store.get_skipped_pairs(users)
    adj = _build_adjacency(users, scores, skipped, settings.group_score_threshold)
    # A clique larger than MAX is trimmed to its tightest-knit MAX-subset (not dropped);
    # a clique smaller than MIN is skipped.
    cliques: list[set[str]] = []
    for clique in bron_kerbosch(adj):
        if len(clique) > settings.max_group_size:
            clique = _trim_to_size(clique, scores, settings.max_group_size)
        if len(clique) >= settings.min_group_size:
            cliques.append(clique)
    if not cliques:
        return False
    cliques.sort(key=lambda c: _mean_pair_score(c, scores), reverse=True)
    surfaced_sets = await store.get_surfaced_group_member_ids(node)
    top = next((c for c in cliques if not any(c & s for s in surfaced_sets)), None)
    if top is None:
        return False  # the only candidate(s) overlap an already-surfaced group
    await store.save_group_candidate(
        GroupCandidate(
            group_id=uuid.uuid4().hex,
            interest_node_id=node,
            member_ids=sorted(top),
            mean_score=_mean_pair_score(top, scores),
            status=GroupStatus.PROPOSED,
        )
    )
    return True


async def _surface_groups(store: Store, brain_client, settings: Settings) -> tuple[int, int]:
    """Queue group openers for every PROPOSED group. Returns (surfaced, failures)."""
    surfaced = 0
    failures = 0
    for group in await store.get_proposed_groups():
        try:
            await store.update_group_status(group.group_id, GroupStatus.SURFACING)
            label = _interest_label(group.interest_node_id)
            reason = (
                f"A few people nearby are also really into {label} — "
                "could be a good crew to get out with."
            )
            all_queued = True
            for member in group.member_ids:
                checkin = GroupCheckin(
                    group_id=group.group_id,
                    candidate_ids=[m for m in group.member_ids if m != member],
                    shared_interest=label,
                    reason=reason,
                    match_confidence=group.mean_score,
                )
                if await brain_client.queue_checkin(member, checkin) is None:
                    all_queued = False
            await store.update_group_status(
                group.group_id, GroupStatus.SURFACED if all_queued else GroupStatus.SURFACING
            )
            if all_queued:
                surfaced += 1
        except Exception:
            failures += 1
            logger.exception("group surfacing failed for %s", group.group_id)
    return surfaced, failures


def main() -> None:
    """One-shot clustering from the CLI (the `connections-cluster` console script)."""
    import asyncio

    from connections_service.brain_client import BrainClient
    from connections_service.config import settings
    from connections_service.store import PgStore

    async def _run() -> None:
        store = await PgStore.connect(settings.database_url)
        brain = BrainClient(
            base_url=settings.brain_url, service_token=settings.service_token.get_secret_value()
        )
        try:
            await clustering_pass(store, brain, settings)
        finally:
            await brain.aclose()
            await store.aclose()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
