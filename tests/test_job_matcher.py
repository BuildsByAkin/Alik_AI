"""Pure job-matcher logic (Phase 7), infra-free.

Proves: available_to_all always scores 1.0; occupation-fact OR-matching; already-recommended
jobs are never returned; fallback to available_to_all when nothing specific matches. Loads the
real data/jobs.json (so this doubles as catalog validation).
"""

from __future__ import annotations

from datetime import UTC, datetime

from alik.job_matcher import load_catalog, match_jobs_for_user, score_job
from alik.models import GraphNode, InferredTrait, NodeType, ProvenanceRecord, TraitStatus

CATALOG = load_catalog("data/jobs.json")
GENERAL_ID = "outlier-general-001"
MEDICAL_ID = "mindrift-medical-eval-001"


def _job(job_id: str):
    return next(j for j in CATALOG if j.id == job_id)


def _fact(key: str, content: str) -> GraphNode:
    return GraphNode(
        user_id="u", type=NodeType.FACT, key=key, content=content, valid_from=datetime.now(UTC)
    )


def _trait(confidence: float, status: TraitStatus = TraitStatus.CONFIRMED) -> InferredTrait:
    now = datetime.now(UTC)
    return InferredTrait(
        user_id="u",
        key="detail_oriented",
        content="pays close attention to detail",
        confidence=confidence,
        valid_from=now,
        status_updated_at=now,
        provenance=ProvenanceRecord(episode_ids=["e1"]),
        status=status,
    )


def test_available_to_all_always_scores_one() -> None:
    assert score_job(_job(GENERAL_ID), [], []) == 1.0


def test_occupation_fact_match_scores_positive_else_zero() -> None:
    medical = _job(MEDICAL_ID)
    # Matching occupation (substring, case-insensitive) → positive.
    assert score_job(medical, [_fact("occupation", "ICU Nurse")], []) > 0
    # No occupation fact → hard gate fails → 0.0.
    assert score_job(medical, [_fact("primary_hobby", "gardening")], []) == 0.0


def test_confirmed_trait_gate_for_dataannotation() -> None:
    da = _job("dataannotation-general-ranking-001")
    assert score_job(da, [], [_trait(0.85)]) > 0  # confirmed ≥ 0.8
    assert score_job(da, [], [_trait(0.7)]) == 0.0  # below min_confidence
    # INFERRED traits never count, even at high confidence.
    assert score_job(da, [], [_trait(0.95, TraitStatus.INFERRED)]) == 0.0
    assert score_job(da, [], []) == 0.0


def test_already_recommended_is_never_returned() -> None:
    nurse_facts = [_fact("occupation", "nurse")]
    # Medical is the only specific match for a nurse; excluding it must fall back, never return it.
    chosen = match_jobs_for_user("u", nurse_facts, [], CATALOG, already_recommended={MEDICAL_ID})
    assert chosen is not None
    assert chosen.id != MEDICAL_ID
    assert chosen.available_to_all is True  # fell back to the general job


def test_falls_back_to_available_to_all_when_nothing_specific() -> None:
    chosen = match_jobs_for_user("u", [], [], CATALOG, already_recommended=set())
    assert chosen is not None
    assert chosen.id == GENERAL_ID


def test_specific_match_beats_fallback() -> None:
    chosen = match_jobs_for_user(
        "u", [_fact("occupation", "registered nurse")], [], CATALOG, already_recommended=set()
    )
    assert chosen is not None
    assert chosen.id == MEDICAL_ID
