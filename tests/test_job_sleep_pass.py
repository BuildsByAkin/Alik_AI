"""The match_jobs sleep pass (Phase 7) against the in-memory doubles, infra-free.

Proves: a nurse (occupation fact) gets the Mindrift medical job queued; a user with no known
occupation gets the Outlier general fallback. Uses the real data/jobs.json.
"""

from __future__ import annotations

from datetime import UTC, datetime

from alik.config import Settings
from alik.job_matcher import load_catalog
from alik.memory.graph import GraphMemory
from alik.models import CheckinType, GraphNode, NodeType
from alik.sleep_pass import match_jobs
from tests.conftest import InMemoryGraphStore, InMemoryMemory

CATALOG = load_catalog("data/jobs.json")


def _mem() -> GraphMemory:
    return GraphMemory(base=InMemoryMemory(), graph=InMemoryGraphStore(), current_facts_limit=50)


async def _seed_fact(mem: GraphMemory, user_id: str, key: str, content: str) -> None:
    await mem.write_nodes(
        [
            GraphNode(
                user_id=user_id,
                type=NodeType.FACT,
                key=key,
                content=content,
                valid_from=datetime.now(UTC),
            )
        ]
    )


async def test_nurse_gets_mindrift_job_queued(user_id) -> None:
    mem = _mem()
    await _seed_fact(mem, user_id, "occupation", "ICU nurse")

    queued = await match_jobs(mem, CATALOG, user_id, Settings())
    assert queued == 1

    checkin = await mem.get_pending_checkin(user_id)
    assert checkin is not None
    assert checkin.checkin_type is CheckinType.JOB_RECOMMENDATION
    assert "https://mindrift.ai" in checkin.message_hint

    recs = await mem.get_job_recommendations(user_id)
    assert [r.job_id for r in recs] == ["mindrift-medical-eval-001"]
    assert recs[0].outcome is None  # open thread


async def test_unknown_occupation_gets_outlier_general(user_id) -> None:
    mem = _mem()  # no facts at all

    queued = await match_jobs(mem, CATALOG, user_id, Settings())
    assert queued == 1

    checkin = await mem.get_pending_checkin(user_id)
    assert checkin is not None
    assert checkin.checkin_type is CheckinType.JOB_RECOMMENDATION
    assert "https://outlier.ai/contributors" in checkin.message_hint

    recs = await mem.get_job_recommendations(user_id)
    assert [r.job_id for r in recs] == ["outlier-general-001"]


async def test_open_thread_blocks_second_recommendation(user_id) -> None:
    mem = _mem()
    assert await match_jobs(mem, CATALOG, user_id, Settings()) == 1
    # A pending check-in + an open (unresolved) thread → no second recommendation.
    assert await match_jobs(mem, CATALOG, user_id, Settings()) == 0
