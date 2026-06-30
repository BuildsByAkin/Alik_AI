"""The surfacing pass + real match-state (InMemoryStore + fake brain, no infra)."""

from __future__ import annotations

import pytest

from connections_service.config import Settings
from connections_service.models import (
    CandidateScore,
    EvalResult,
    InterestMatch,
    KernelExplanation,
    MatchStatus,
    UserPoolEntry,
)
from connections_service.store import InMemoryStore
from connections_service.surface import surface_pass
from tests.conftest import FakeBrain

S = Settings()


def _cand(a, b, *, score=0.8):
    exp = KernelExplanation(
        interest_specific=[
            InterestMatch(
                "outdoor_active:rock_climbing", "outdoor_active", "rock_climbing", 1.0, 1.0
            )
        ],
        match_type="specific",
    )
    return CandidateScore(a, b, score, score, 0.0, 0.0, 0.7, False, exp)


def _eval(a, b, *, final=0.7):
    return EvalResult(a, b, True, 0.8, final, "they both light up about climbing", "test-model")


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def brain() -> FakeBrain:
    return FakeBrain()


async def _seed_surfaceable(store, a, b, *, final=0.7):
    for uid in (a, b):
        await store.upsert_user_pool(UserPoolEntry(user_id=uid, state="MN", pool_ready=True))
    await store.save_candidate_score(_cand(a, b))
    await store.save_eval_result(_eval(a, b, final=final))


async def test_surface_pass_queues_and_marks_shown(store, brain):
    await _seed_surfaceable(store, "a", "b")
    counts = await surface_pass(store, brain, S)

    assert counts["surfaced"] == 1
    assert brain.queued and brain.queued[0][0] == "a"
    checkin = brain.queued[0][1]
    assert checkin.candidate_id == "b" and checkin.shared_interests == ["rock climbing"]
    ms = await store.get_match_state("a", "b")
    assert ms.status is MatchStatus.SHOWN and ms.checkin_id == "ck1"


async def test_surface_pass_brain_failure_saves_no_state(store, brain):
    await _seed_surfaceable(store, "a", "b")
    brain.checkin_id = None  # queue_checkin "fails"
    counts = await surface_pass(store, brain, S)

    assert counts["surfaced"] == 0 and counts["skipped"] == 1
    assert await store.get_match_state("a", "b") is None  # retry next pass


async def test_shown_user_excluded_from_future_surfacing(store, brain):
    await _seed_surfaceable(store, "a", "b")
    await surface_pass(store, brain, S)

    assert await store.get_shown_user_ids("a") == ["b"]
    again = await store.get_surfaceable_matches("a", "MN", surface_threshold=S.surface_threshold)
    assert again == []  # already shown → never re-surfaced


async def test_max_surface_per_pass_one_at_a_time(store, brain):
    await _seed_surfaceable(store, "a", "b", final=0.7)
    await store.upsert_user_pool(UserPoolEntry(user_id="c", state="MN", pool_ready=True))
    await store.save_candidate_score(_cand("a", "c"))
    await store.save_eval_result(_eval("a", "c", final=0.6))

    await surface_pass(store, brain, S)  # MAX_SURFACE_PER_PASS defaults to 1
    assert len(await store.get_shown_user_ids("a")) == 1  # only the top match surfaced


async def test_delete_user_wipes_match_state_both_sides(store, brain):
    await _seed_surfaceable(store, "a", "b")
    await surface_pass(store, brain, S)
    assert await store.get_match_state("a", "b") is not None

    await store.delete_user("b")  # b appears only as candidate_id
    assert await store.get_match_state("a", "b") is None
    assert await store.get_shown_user_ids("a") == []
