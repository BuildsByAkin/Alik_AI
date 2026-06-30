"""The LLM cross-eval: privacy-safe summary, label-table completeness, shared-signal render,
response parsing, the final-confidence formula, and eval_pass (InMemoryStore + a fake LLM)."""

from __future__ import annotations

import pytest

from connections_service import eval as ev
from connections_service.config import Settings
from connections_service.eval import (
    build_person_summary,
    compute_final_confidence,
    eval_pass,
    parse_eval_response,
    render_shared_signals,
)
from connections_service.models import (
    CandidateScore,
    DimensionMatch,
    DimensionSnapshot,
    InterestEdge,
    InterestMatch,
    KernelExplanation,
    UserPoolEntry,
)
from connections_service.store import InMemoryStore

S = Settings()

GOOD = '{"would_click": true, "confidence": 0.8, "reason": "both climb", "flag_for_review": false}'


# --- summary builder --------------------------------------------------------------------


def test_build_person_summary_includes_safe_excludes_sensitive():
    entry = UserPoolEntry(user_id="u1", state="MN", age=31, city="Minneapolis")
    interests = [
        InterestEdge("outdoor_active:rock_climbing", 1.0, "primary_hobby"),
        InterestEdge("outdoor_active:_general", 0.5, "primary_exercise"),  # excluded (general)
        InterestEdge("creative:photography", 0.7, "secondary_hobby"),
    ]
    dims = [
        DimensionSnapshot("sensory_sensitivity", "high", 0.8, "confirmed"),
        DimensionSnapshot("topic_focus", "deep_narrow", 0.5, "confirmed"),  # below floor
        DimensionSnapshot("structure_preference", "needs_structure", 0.9, "corrected"),  # corrected
    ]
    s = build_person_summary(entry, interests, dims, max_interests=8, dimension_floor=0.6)

    assert "Minneapolis" in s
    assert "rock climbing" in s and "photography" in s  # canonical labels, lowercased
    assert "Prefers calmer, lower-key environments" in s  # sensory high (bullet-capitalized)
    assert "general" not in s.lower()  # _general node excluded
    assert "31" not in s and "u1" not in s  # age + id never included
    assert "deep" not in s  # below-floor dimension excluded
    assert "clear plan" not in s  # corrected dimension excluded


# --- label table completeness + loud validation -----------------------------------------


def test_every_taxonomy_value_has_a_label_and_axis_name():
    for axis, values in ev.DIMENSION_TAXONOMY.items():
        assert axis in ev.AXIS_HUMAN
        for value in values:
            assert (axis, value) in ev.DIMENSION_LABELS
            assert ev.DIMENSION_LABELS[(axis, value)]  # non-empty


def test_validate_labels_raises_loudly_when_a_key_is_missing(monkeypatch):
    broken = dict(ev.DIMENSION_LABELS)
    broken.pop(("sensory_sensitivity", "high"))
    monkeypatch.setattr(ev, "DIMENSION_LABELS", broken)
    with pytest.raises(RuntimeError):
        ev._validate_labels()


# --- shared signals ---------------------------------------------------------------------


def test_render_shared_signals():
    exp = KernelExplanation(
        interest_specific=[
            InterestMatch(
                "outdoor_active:rock_climbing", "outdoor_active", "rock_climbing", 1.0, 0.8
            )
        ],
        interest_broad=["outdoor_active", "gaming"],
        dimensions=[
            DimensionMatch(
                "structure_preference", "needs_structure", "needs_structure", 1.0, "compatibility"
            ),
            DimensionMatch(
                "topic_focus", "deep_narrow", "balanced", 0.6, "similarity"
            ),  # below 0.7
        ],
        values_causes=["social_causes:environment"],
        match_type="specific",
    )
    out = render_shared_signals(exp, shared_dimension_threshold=0.7)
    assert "Both into: rock climbing" in out
    assert "Aligned on how much they like a plan" in out
    assert "how they dive into interests" not in out  # topic_focus 0.6 < threshold → omitted
    assert "Both care about: environment / climate" in out


def test_render_shared_signals_broad_only():
    exp = KernelExplanation(interest_broad=["outdoor_active"], match_type="broad_only")
    assert "Both drawn to: outdoor active" in render_shared_signals(
        exp, shared_dimension_threshold=0.7
    )


# --- parsing + formula ------------------------------------------------------------------


def test_parse_valid_response():
    p = parse_eval_response('noise {"would_click": true, "confidence": 0.9, "reason": "x"} end')
    assert p == {
        "would_click": True,
        "confidence": 0.9,
        "reason": "x",
        "flag_for_review": False,
        "flag_reason": None,
    }


def test_parse_malformed_returns_none():
    assert parse_eval_response("not json") is None
    assert parse_eval_response('{"would_click": true}') is None  # missing confidence
    assert (
        parse_eval_response('{"would_click": true, "confidence": 0.5, "reason": ""}') is None
    )  # empty


def test_final_confidence_formula():
    assert compute_final_confidence(0.5, 0.8, S) == round(0.6 * 0.5 + 0.4 * 0.8, 4)


# --- eval_pass --------------------------------------------------------------------------


class FakeLLM:
    def __init__(self, behavior=GOOD) -> None:
        self.behavior = behavior
        self.calls = 0

    async def complete(self, *, system, messages):
        self.calls += 1
        content = messages[0]["content"]
        if callable(self.behavior):
            return self.behavior(content)
        return self.behavior


async def _seed(store, uid, *, city="Minneapolis"):
    await store.upsert_user_pool(UserPoolEntry(user_id=uid, state="MN", city=city, pool_ready=True))
    await store.upsert_user_interests(
        uid, [InterestEdge("outdoor_active:rock_climbing", 1.0, "primary_hobby")]
    )
    await store.upsert_profile_dimensions(uid, [])


def _cand(a, b, *, score=0.8, conf=0.7, flag=False):
    exp = KernelExplanation(
        interest_specific=[
            InterestMatch(
                "outdoor_active:rock_climbing", "outdoor_active", "rock_climbing", 1.0, 1.0
            )
        ],
        match_type="specific",
    )
    return CandidateScore(a, b, score, score, 0.0, 0.0, conf, flag, exp)


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


async def test_eval_pass_saves_result(store):
    await _seed(store, "a")
    await _seed(store, "b")
    await store.save_candidate_score(_cand("a", "b", conf=0.7))
    counts = await eval_pass(store, FakeLLM(), S)
    assert counts["evaluated"] == 1
    result = await store.get_eval_result("a", "b")
    assert result.would_click is True
    assert result.final_confidence == round(0.6 * 0.7 + 0.4 * 0.8, 4)
    assert result.eval_model == S.eval_model
    assert result.reason == "both climb"


async def test_eval_pass_shortlist_excludes_flagged_and_low(store):
    for uid in ("a", "b", "c", "d"):
        await _seed(store, uid)
    await store.save_candidate_score(_cand("a", "b", score=0.8, flag=True))  # flagged
    await store.save_candidate_score(_cand("a", "c", score=0.3))  # below min_kernel_score
    await store.save_candidate_score(_cand("a", "d", score=0.6))  # eligible
    await eval_pass(store, FakeLLM(), S)
    assert await store.get_eval_result("a", "d") is not None
    assert await store.get_eval_result("a", "b") is None
    assert await store.get_eval_result("a", "c") is None


async def test_eval_pass_malformed_json_skips(store):
    await _seed(store, "a")
    await _seed(store, "b")
    await store.save_candidate_score(_cand("a", "b"))
    counts = await eval_pass(store, FakeLLM(behavior="not json"), S)
    assert counts["evaluated"] == 0 and counts["skipped"] == 1
    assert await store.get_eval_result("a", "b") is None


async def test_eval_pass_per_pair_isolated(store):
    await _seed(store, "a")
    await _seed(store, "b", city="Duluth")
    await _seed(store, "c", city="Rochester")
    await store.save_candidate_score(_cand("a", "b"))
    await store.save_candidate_score(_cand("a", "c"))

    def behavior(content):
        if "Duluth" in content:
            raise RuntimeError("boom")
        return GOOD

    counts = await eval_pass(store, FakeLLM(behavior=behavior), S)
    assert await store.get_eval_result("a", "c") is not None  # c still evaluated
    assert await store.get_eval_result("a", "b") is None  # b's failure isolated
    assert counts["evaluated"] == 1 and counts["skipped"] == 1


async def test_eval_pass_flag_propagates(store):
    await _seed(store, "a")
    await _seed(store, "b")
    await store.save_candidate_score(_cand("a", "b"))
    flagged = (
        '{"would_click": false, "confidence": 0.3, "reason": "thin overlap", '
        '"flag_for_review": true, "flag_reason": "only one shared interest"}'
    )
    await eval_pass(store, FakeLLM(behavior=flagged), S)
    result = await store.get_eval_result("a", "b")
    assert result.flag_for_review is True
    assert result.flag_reason == "only one shared interest"
    assert result.would_click is False
