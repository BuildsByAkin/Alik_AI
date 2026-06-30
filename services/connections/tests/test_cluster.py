"""Group clustering: Bron-Kerbosch, the clustering pass, gates, dedup, surfacing (no infra)."""

from __future__ import annotations

import itertools

import pytest

from connections_service.cluster import bron_kerbosch, clustering_pass
from connections_service.config import Settings
from connections_service.models import (
    CandidateScore,
    GroupStatus,
    InterestEdge,
    KernelExplanation,
    MatchStateEntry,
    MatchStatus,
    UserPoolEntry,
)
from connections_service.store import InMemoryStore
from tests.conftest import FakeBrain

S = Settings()  # min_group_size=3, max_group_size=5, group_score_threshold=0.5
RUN = "outdoor_active:running"


def _cand(a, b, score):
    return CandidateScore(
        a, b, score, score, 0.0, 0.0, 0.7, False, KernelExplanation(match_type="none")
    )


async def _seed(store, users, *, node=RUN, scores=None, state="MN"):
    for u in users:
        await store.upsert_user_pool(UserPoolEntry(user_id=u, state=state, pool_ready=True))
        await store.upsert_user_interests(u, [InterestEdge(node, 1.0, "primary_hobby")])
    for a, b in itertools.combinations(users, 2):
        s = (scores or {}).get(frozenset((a, b)), 0.8)
        await store.save_candidate_score(_cand(a, b, s))
        await store.save_candidate_score(_cand(b, a, s))


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def brain() -> FakeBrain:
    return FakeBrain()


# --- pure Bron-Kerbosch -----------------------------------------------------------------


def test_bron_kerbosch_full_and_split():
    adj = {x: {y for y in "abcd" if y != x} for x in "abcd"}  # K4 → one clique
    assert {frozenset(c) for c in bron_kerbosch(adj)} == {frozenset("abcd")}
    adj["c"].discard("d")
    adj["d"].discard("c")  # drop the c-d edge → two triangles
    assert {frozenset(c) for c in bron_kerbosch(adj)} == {frozenset("abc"), frozenset("abd")}


# --- the clustering pass ----------------------------------------------------------------


async def test_four_compatible_users_form_one_group(store, brain):
    await _seed(store, ["a", "b", "c", "d"])
    counts = await clustering_pass(store, brain, S)

    assert counts["proposed"] == 1 and counts["surfaced"] == 1
    groups = list(store._groups.values())
    assert len(groups) == 1
    assert groups[0].member_ids == ["a", "b", "c", "d"] and groups[0].status is GroupStatus.SURFACED
    assert len(brain.queued) == 4  # one check-in per member
    # each member's group check-in lists the OTHER members, with the shared interest label.
    payloads = {uid: ck for uid, ck in brain.queued}
    assert payloads["a"].candidate_ids == ["b", "c", "d"]
    assert payloads["a"].shared_interest == "running"
    assert payloads["a"].group_id == groups[0].group_id


async def test_below_threshold_pair_breaks_the_clique(store, brain):
    await _seed(store, ["a", "b", "c", "d"], scores={frozenset(("c", "d")): 0.3})
    await clustering_pass(store, brain, S)
    group = next(iter(store._groups.values()))
    assert len(group.member_ids) == 3  # the c-d edge is gone → max clique is a triangle
    assert not {"c", "d"} <= set(group.member_ids)  # not both of the incompatible pair


async def test_general_node_is_not_clustered(store, brain):
    await _seed(store, ["a", "b", "c"], node="outdoor_active:_general")
    counts = await clustering_pass(store, brain, S)
    assert counts["nodes"] == 0 and store._groups == {}


async def test_skipped_pair_excluded(store, brain):
    await _seed(store, ["a", "b", "c"])
    await store.save_match_state(MatchStateEntry("a", "b", MatchStatus.SKIPPED))
    await clustering_pass(store, brain, S)
    assert store._groups == {}  # a-b edge removed → no triangle → no group of 3


async def test_min_group_size_pool_check(store, brain):
    await _seed(store, ["a", "b"])  # only 2 users share the interest
    counts = await clustering_pass(store, brain, S)
    assert counts["nodes"] == 0 and store._groups == {}


async def test_oversized_clique_trims_to_max_size(store, brain):
    await _seed(store, ["a", "b", "c", "d", "e", "f"])  # K6 > MAX
    await clustering_pass(store, brain, S)
    groups = list(store._groups.values())
    assert len(groups) == 1
    assert len(groups[0].member_ids) == S.max_group_size  # trimmed to 5, not dropped


async def test_exactly_max_size_is_accepted(store, brain):
    await _seed(store, ["a", "b", "c", "d", "e"])  # K5 == MAX
    await clustering_pass(store, brain, S)
    assert len(next(iter(store._groups.values())).member_ids) == 5


async def test_dedup_same_members_one_row(store, brain):
    await _seed(store, ["a", "b", "c"])
    await clustering_pass(store, brain, S)
    gid = next(iter(store._groups))
    await clustering_pass(store, brain, S)  # re-run: same members → upsert, not a new row
    assert list(store._groups) == [gid]  # stable group_id, single row


async def test_overlapping_group_not_re_surfaced(store, brain):
    await _seed(store, ["a", "b", "c"])
    await clustering_pass(store, brain, S)  # surfaces {a,b,c}
    await _seed(store, ["a", "b", "c", "d"])  # d joins; K4 now overlaps the surfaced trio
    await clustering_pass(store, brain, S)
    assert len(store._groups) == 1  # the overlapping K4 is excluded, no new group


async def test_clustering_pass_isolated_on_error(store, brain, monkeypatch):
    await _seed(store, ["a", "b", "c"])

    async def boom(_user_ids):
        raise RuntimeError("boom")

    monkeypatch.setattr(store, "get_pairwise_scores", boom)
    counts = await clustering_pass(store, brain, S)  # must not raise
    assert counts["proposed"] == 0
