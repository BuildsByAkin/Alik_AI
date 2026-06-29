"""Pure scoring against the profile-dict shape. Loads the real catalog (doubles as
catalog validation)."""

from __future__ import annotations

from matching_service.catalog import load_catalog
from matching_service.scorer import match_jobs_for_user, score_job

CATALOG = load_catalog("data/jobs.json")
GENERAL_ID = "outlier-general-001"
MEDICAL_ID = "mindrift-medical-eval-001"
DATAANNO_ID = "dataannotation-general-ranking-001"


def _job(job_id: str):
    return next(j for j in CATALOG if j.id == job_id)


def _fact(key: str, content: str) -> dict:
    return {"key": key, "content": content}


def _trait(confidence: float) -> dict:
    return {
        "key": "detail_oriented",
        "content": "pays attention to detail",
        "confidence": confidence,
    }


def test_available_to_all_always_scores_one() -> None:
    assert score_job(_job(GENERAL_ID), [], []) == 1.0


def test_occupation_fact_match_scores_positive_else_zero() -> None:
    medical = _job(MEDICAL_ID)
    assert score_job(medical, [_fact("occupation", "ICU Nurse")], []) > 0
    assert score_job(medical, [_fact("primary_hobby", "gardening")], []) == 0.0


def test_confirmed_trait_gate() -> None:
    da = _job(DATAANNO_ID)
    assert score_job(da, [], [_trait(0.85)]) > 0  # confirmed >= 0.8
    assert score_job(da, [], [_trait(0.7)]) == 0.0  # below min_confidence
    assert score_job(da, [], []) == 0.0


def test_already_recommended_is_never_returned() -> None:
    chosen = match_jobs_for_user(
        [_fact("occupation", "nurse")], [], CATALOG, already_recommended={MEDICAL_ID}
    )
    assert chosen is not None
    assert chosen.id != MEDICAL_ID
    assert chosen.available_to_all is True


def test_falls_back_to_available_to_all_when_nothing_specific() -> None:
    chosen = match_jobs_for_user([], [], CATALOG, already_recommended=set())
    assert chosen is not None
    assert chosen.id == GENERAL_ID


def test_specific_match_beats_fallback() -> None:
    chosen = match_jobs_for_user(
        [_fact("occupation", "registered nurse")], [], CATALOG, already_recommended=set()
    )
    assert chosen is not None
    assert chosen.id == MEDICAL_ID
