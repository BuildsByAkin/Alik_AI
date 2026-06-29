"""Candidate generation + scoring run (InMemoryStore, no infra) + explanation jsonb roundtrip."""

from __future__ import annotations

import pytest

import connections_service.scoring as scoring_mod
from connections_service.config import Settings
from connections_service.kernel import MatchInput, kernel
from connections_service.models import InterestEdge, UserPoolEntry
from connections_service.scoring import generate_candidates, scoring_pass
from connections_service.store import InMemoryStore, explanation_from_json, explanation_to_json

S = Settings()


def edge(node: str, w: float = 1.0, src: str = "primary_hobby") -> InterestEdge:
    return InterestEdge(node, w, src)


async def _seed(store, uid, edges, *, ready=True, state="MN"):
    await store.upsert_user_pool(UserPoolEntry(user_id=uid, state=state, pool_ready=ready))
    await store.upsert_user_interests(uid, edges)


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


async def test_generate_candidates_excludes_self(store):
    for uid in ("a", "b", "c"):
        await _seed(store, uid, [edge("gaming:dnd")])
    cands = await generate_candidates(store, "a", "MN", S)
    assert {c.user_id_b for c in cands} == {"b", "c"}
    assert all(c.user_id_a == "a" for c in cands)


async def test_generate_candidates_sorted_strong_first(store):
    await _seed(store, "a", [edge("gaming:dnd"), edge("outdoor_active:running")])
    await _seed(store, "b", [edge("gaming:dnd"), edge("outdoor_active:running")])  # strong
    await _seed(store, "c", [edge("creative:writing")])  # no overlap
    cands = await generate_candidates(store, "a", "MN", S)
    assert cands[0].user_id_b == "b"
    assert cands[0].score >= cands[-1].score


async def test_top_n_cap(store):
    await _seed(store, "a", [edge("gaming:dnd")])
    for i in range(15):
        await _seed(store, f"u{i}", [edge("gaming:dnd")])
    assert len(await generate_candidates(store, "a", "MN", S)) == S.top_n_candidates


async def test_pool_ready_and_state_filter_candidates(store):
    await _seed(store, "a", [edge("gaming:dnd")])
    await _seed(store, "b", [edge("gaming:dnd")], ready=False)  # not ready
    await _seed(store, "c", [edge("gaming:dnd")], state="WI")  # wrong state
    assert {c.user_id_b for c in await generate_candidates(store, "a", "MN", S)} == set()


async def test_scoring_pass_saves_scores(store):
    for uid in ("a", "b"):
        await _seed(store, uid, [edge("gaming:dnd")])
    counts = await scoring_pass(store, S)
    assert counts["users"] == 2
    saved = await store.get_candidate_scores("a")
    assert [c.user_id_b for c in saved] == ["b"]


async def test_scoring_pass_is_per_user_isolated(store, monkeypatch):
    for uid in ("a", "b"):
        await _seed(store, uid, [edge("gaming:dnd")])
    original = scoring_mod.generate_candidates

    async def boom(st, uid, state, settings):
        if uid == "a":
            raise RuntimeError("boom")
        return await original(st, uid, state, settings)

    monkeypatch.setattr(scoring_mod, "generate_candidates", boom)
    counts = await scoring_pass(store, S)

    assert counts["users"] == 1  # a's failure isolated; b still scored
    assert await store.get_candidate_scores("b")
    assert await store.get_candidate_scores("a") == []


async def test_delete_user_wipes_candidate_scores_both_sides(store):
    for uid in ("a", "b"):
        await _seed(store, uid, [edge("gaming:dnd")])
    await scoring_pass(store, S)
    assert await store.get_candidate_scores("a")  # a→b row exists

    await store.delete_user("b")  # b only appears as user_id_b in a's row
    assert await store.get_candidate_scores("a") == []  # wiped from the b side too


def test_explanation_json_roundtrip():
    cs = kernel(
        MatchInput(
            "a",
            [edge("gaming:dnd", 1.0), edge("social_causes:environment", 0.5, "values_core")],
            [],
        ),
        MatchInput(
            "b",
            [edge("gaming:dnd", 0.5), edge("social_causes:environment", 0.5, "values_core")],
            [],
        ),
        S,
    )
    assert explanation_from_json(explanation_to_json(cs.explanation)) == cs.explanation
