"""get_surfaceable_matches: would_click + final_confidence gate, ordering, join to the kernel
explanation, and the deletion wipe."""

from __future__ import annotations

import pytest

from connections_service.models import (
    CandidateScore,
    EvalResult,
    KernelExplanation,
)
from connections_service.store import InMemoryStore


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


def _cand(a, b, *, score=0.8):
    exp = KernelExplanation(interest_broad=["gaming"], match_type="broad_only")
    return CandidateScore(a, b, score, score, 0.0, 0.0, 0.7, False, exp)


def _eval(a, b, *, would_click=True, final=0.7, reason="they'd get on"):
    return EvalResult(
        user_id_a=a,
        user_id_b=b,
        would_click=would_click,
        llm_confidence=0.8,
        final_confidence=final,
        reason=reason,
        eval_model="test-model",
    )


async def test_surfaceable_threshold_and_order(store):
    for b, final in (("b", 0.7), ("c", 0.6), ("d", 0.5)):
        await store.save_candidate_score(_cand("a", b))
        await store.save_eval_result(_eval("a", b, final=final))
    # below-threshold (d=0.5) excluded; rest ordered by final_confidence desc.
    out = await store.get_surfaceable_matches("a", "MN", surface_threshold=0.55)
    assert [m.user_id_b for m in out] == ["b", "c"]
    assert out[0].kernel_score == 0.8  # joined from candidate_scores
    assert out[0].reason == "they'd get on"
    assert out[0].explanation.match_type == "broad_only"


async def test_surfaceable_excludes_would_click_false(store):
    await store.save_candidate_score(_cand("a", "b"))
    await store.save_eval_result(_eval("a", "b", would_click=False, final=0.9))
    assert await store.get_surfaceable_matches("a", "MN", surface_threshold=0.55) == []


async def test_surfaceable_skips_eval_without_candidate(store):
    # eval present but its candidate_scores row is gone → not surfaceable (no kernel data).
    await store.save_eval_result(_eval("a", "b", final=0.9))
    assert await store.get_surfaceable_matches("a", "MN", surface_threshold=0.55) == []


async def test_delete_user_wipes_eval_results(store):
    await store.save_candidate_score(_cand("a", "b"))
    await store.save_eval_result(_eval("a", "b"))
    await store.delete_user("b")  # b appears only as user_id_b
    assert await store.get_surfaceable_matches("a", "MN", surface_threshold=0.55) == []
    assert await store.get_eval_result("a", "b") is None
