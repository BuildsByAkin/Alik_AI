"""Each cron pass emits exactly one greppable ``PASS_SUMMARY`` INFO line when it finishes —
with the spec'd fields, even on zero work, even with failures (InMemoryStore + fakes, no infra).

The five passes' field maps:
  ingest  -> users_processed failures pool_ready duration_s
  score   -> pairs_scored users_processed failures duration_s
  eval    -> pairs_evaluated llm_failures flagged_for_review duration_s
  surface -> checkins_queued brain_failures duration_s
  cluster -> groups_proposed groups_surfaced failures duration_s
"""

from __future__ import annotations

import logging

import pytest

import connections_service.scoring as scoring_mod
from connections_service.cluster import clustering_pass
from connections_service.config import Settings
from connections_service.eval import eval_pass
from connections_service.ingest import run_ingest
from connections_service.models import (
    CandidateScore,
    EvalResult,
    InterestEdge,
    InterestMatch,
    KernelExplanation,
    UserPoolEntry,
)
from connections_service.store import InMemoryStore
from connections_service.surface import surface_pass
from tests.conftest import FakeAuth, FakeBrain, dim, make_profile

S = Settings()
GOOD = '{"would_click": true, "confidence": 0.8, "reason": "both climb", "flag_for_review": false}'
FLAGGED = (
    '{"would_click": false, "confidence": 0.3, "reason": "thin", '
    '"flag_for_review": true, "flag_reason": "only one shared interest"}'
)


# --- helpers ----------------------------------------------------------------------------


def _summary(caplog, pass_name: str) -> dict[str, str]:
    """Find the single PASS_SUMMARY line for ``pass_name`` and parse its key=value fields."""
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("PASS_SUMMARY ")]
    mine = [ln for ln in lines if f" pass={pass_name} " in f"{ln} "]
    assert len(mine) == 1, f"expected one PASS_SUMMARY for {pass_name}, got {lines}"
    parts = mine[0].split()
    assert parts[0] == "PASS_SUMMARY"
    fields = dict(p.split("=", 1) for p in parts[1:])
    assert fields.pop("pass") == pass_name
    float(fields["duration_s"])  # always present + numeric
    return fields


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def brain() -> FakeBrain:
    return FakeBrain()


@pytest.fixture(autouse=True)
def _info_logs(caplog):
    caplog.set_level(logging.INFO)


def _cand(a, b, *, score=0.8, conf=0.7, flag=False) -> CandidateScore:
    exp = KernelExplanation(
        interest_specific=[
            InterestMatch(
                "outdoor_active:rock_climbing", "outdoor_active", "rock_climbing", 1.0, 1.0
            )
        ],
        match_type="specific",
    )
    return CandidateScore(a, b, score, score, 0.0, 0.0, conf, flag, exp)


class FakeLLM:
    def __init__(self, behavior=GOOD) -> None:
        self.behavior = behavior

    async def complete(self, *, system, messages):
        if callable(self.behavior):
            return self.behavior(messages[0]["content"])
        return self.behavior


# --- ingest -----------------------------------------------------------------------------


async def test_ingest_summary_fields(store, brain, caplog):
    brain.set("u1", make_profile(dimensions=[dim("interest_intensity", "intense_specific", 0.9)]))
    await run_ingest(store, brain, FakeAuth({"MN": ["u1"]}), S)
    assert _summary(caplog, "ingest") == {
        "users_processed": "1",
        "failures": "0",
        "pool_ready": "1",
        "duration_s": _summary(caplog, "ingest")["duration_s"],
    }


async def test_ingest_summary_zero_work(store, brain, caplog):
    await run_ingest(store, brain, FakeAuth({}), S)  # empty roster
    f = _summary(caplog, "ingest")
    assert f["users_processed"] == "0" and f["failures"] == "0" and f["pool_ready"] == "0"


async def test_ingest_summary_counts_failures(store, caplog):
    class BoomBrain(FakeBrain):
        async def fetch_profile(self, user_id):
            raise RuntimeError("boom")

    await run_ingest(store, BoomBrain(), FakeAuth({"MN": ["u1"]}), S)
    f = _summary(caplog, "ingest")
    assert f["failures"] == "1" and f["users_processed"] == "1" and f["pool_ready"] == "0"


# --- score ------------------------------------------------------------------------------


async def _seed_scored(store, *uids):
    for uid in uids:
        await store.upsert_user_pool(UserPoolEntry(user_id=uid, state="MN", pool_ready=True))
        await store.upsert_user_interests(uid, [InterestEdge("gaming:dnd", 1.0, "primary_hobby")])


async def test_score_summary_fields(store, caplog):
    await _seed_scored(store, "a", "b")
    await scoring_mod.scoring_pass(store, S)
    f = _summary(caplog, "score")
    assert f["users_processed"] == "2" and f["failures"] == "0"
    assert int(f["pairs_scored"]) >= 1


async def test_score_summary_zero_work(store, caplog):
    await scoring_mod.scoring_pass(store, S)  # empty pool
    f = _summary(caplog, "score")
    assert f["pairs_scored"] == "0" and f["users_processed"] == "0" and f["failures"] == "0"


async def test_score_summary_counts_failures(store, monkeypatch, caplog):
    await _seed_scored(store, "a", "b")

    async def boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(scoring_mod, "generate_candidates", boom)
    await scoring_mod.scoring_pass(store, S)
    f = _summary(caplog, "score")
    assert f["failures"] == "2" and f["users_processed"] == "2" and f["pairs_scored"] == "0"


# --- eval -------------------------------------------------------------------------------


async def _seed_eval_pair(store, a, b, **cand_kw):
    for uid in (a, b):
        await store.upsert_user_pool(UserPoolEntry(user_id=uid, state="MN", pool_ready=True))
        await store.upsert_user_interests(
            uid, [InterestEdge("outdoor_active:rock_climbing", 1.0, "primary_hobby")]
        )
        await store.upsert_profile_dimensions(uid, [])
    await store.save_candidate_score(_cand(a, b, **cand_kw))


async def test_eval_summary_fields(store, caplog):
    await _seed_eval_pair(store, "a", "b")
    await eval_pass(store, FakeLLM(), S)
    f = _summary(caplog, "eval")
    assert f["pairs_evaluated"] == "1" and f["llm_failures"] == "0"
    assert f["flagged_for_review"] == "0"


async def test_eval_summary_counts_flagged(store, caplog):
    await _seed_eval_pair(store, "a", "b")
    await eval_pass(store, FakeLLM(behavior=FLAGGED), S)
    f = _summary(caplog, "eval")
    assert f["pairs_evaluated"] == "1" and f["flagged_for_review"] == "1"


async def test_eval_summary_zero_work(store, caplog):
    await eval_pass(store, FakeLLM(), S)  # empty pool
    f = _summary(caplog, "eval")
    assert f == {
        "pairs_evaluated": "0",
        "llm_failures": "0",
        "flagged_for_review": "0",
        "duration_s": f["duration_s"],
    }


async def test_eval_summary_counts_llm_failures(store, caplog):
    await _seed_eval_pair(store, "a", "b")

    def boom(_content):
        raise RuntimeError("boom")

    await eval_pass(store, FakeLLM(behavior=boom), S)
    f = _summary(caplog, "eval")
    assert f["llm_failures"] == "1" and f["pairs_evaluated"] == "0"


# --- surface ----------------------------------------------------------------------------


async def _seed_surfaceable(store, a, b):
    for uid in (a, b):
        await store.upsert_user_pool(UserPoolEntry(user_id=uid, state="MN", pool_ready=True))
    await store.save_candidate_score(_cand(a, b))
    await store.save_eval_result(
        EvalResult(a, b, True, 0.8, 0.7, "they both light up about climbing", "test-model")
    )


async def test_surface_summary_fields(store, brain, caplog):
    await _seed_surfaceable(store, "a", "b")
    await surface_pass(store, brain, S)
    f = _summary(caplog, "surface")
    assert f["checkins_queued"] == "1" and f["brain_failures"] == "0"


async def test_surface_summary_zero_work(store, brain, caplog):
    await surface_pass(store, brain, S)  # empty pool
    f = _summary(caplog, "surface")
    assert f["checkins_queued"] == "0" and f["brain_failures"] == "0"


async def test_surface_summary_counts_brain_failures(store, brain, caplog):
    await _seed_surfaceable(store, "a", "b")
    brain.checkin_id = None  # queue_checkin "fails"
    await surface_pass(store, brain, S)
    f = _summary(caplog, "surface")
    assert f["checkins_queued"] == "0" and f["brain_failures"] == "1"


# --- cluster ----------------------------------------------------------------------------

RUN = "outdoor_active:running"


async def _seed_clique(store, *uids):
    import itertools

    for uid in uids:
        await store.upsert_user_pool(UserPoolEntry(user_id=uid, state="MN", pool_ready=True))
        await store.upsert_user_interests(uid, [InterestEdge(RUN, 1.0, "primary_hobby")])
    for a, b in itertools.combinations(uids, 2):
        await store.save_candidate_score(_cand(a, b, score=0.8))
        await store.save_candidate_score(_cand(b, a, score=0.8))


async def test_cluster_summary_fields(store, brain, caplog):
    await _seed_clique(store, "a", "b", "c")
    await clustering_pass(store, brain, S)
    f = _summary(caplog, "cluster")
    assert f["groups_proposed"] == "1" and f["groups_surfaced"] == "1" and f["failures"] == "0"


async def test_cluster_summary_zero_work(store, brain, caplog):
    await clustering_pass(store, brain, S)  # no clusterable nodes
    f = _summary(caplog, "cluster")
    assert f == {
        "groups_proposed": "0",
        "groups_surfaced": "0",
        "failures": "0",
        "duration_s": f["duration_s"],
    }


async def test_cluster_summary_counts_failures(store, brain, monkeypatch, caplog):
    await _seed_clique(store, "a", "b", "c")

    async def boom(_user_ids):
        raise RuntimeError("boom")

    monkeypatch.setattr(store, "get_pairwise_scores", boom)
    await clustering_pass(store, brain, S)
    f = _summary(caplog, "cluster")
    assert int(f["failures"]) >= 1 and f["groups_proposed"] == "0"
