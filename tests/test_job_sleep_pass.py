"""The job sleep-pass glue: it delegates selection to the matching service and only queues
the companion check-in. Selection/cooldown/dedup are the matching service's own tests."""

from __future__ import annotations

from alik.memory.graph import GraphMemory
from alik.models import CheckinType
from alik.sleep_pass import check_job_followups, match_jobs
from tests.conftest import FakeMatching, InMemoryGraphStore, InMemoryMemory

JOB = {
    "id": "mindrift-medical-eval-001",
    "title": "Evaluate AI medical answers",
    "partner": "Mindrift",
    "partner_url": "https://mindrift.ai",
    "pay_range": "$40-60/hr",
}


def _mem() -> GraphMemory:
    return GraphMemory(base=InMemoryMemory(), graph=InMemoryGraphStore(), current_facts_limit=50)


async def test_match_jobs_queues_checkin_from_service_result(user_id) -> None:
    mem = _mem()
    matching = FakeMatching(match_result={"recommendation_id": "r1", "job": JOB})

    assert await match_jobs(mem, matching, user_id) == 1
    checkin = await mem.get_pending_checkin(user_id)
    assert checkin is not None
    assert checkin.checkin_type is CheckinType.JOB_RECOMMENDATION
    assert "https://mindrift.ai" in checkin.message_hint


async def test_match_jobs_noop_when_service_returns_none(user_id) -> None:
    mem = _mem()
    assert await match_jobs(mem, FakeMatching(match_result=None), user_id) == 0
    assert await mem.get_pending_checkin(user_id) is None


async def test_match_jobs_skips_when_checkin_already_pending(user_id) -> None:
    mem = _mem()
    matching = FakeMatching(match_result={"recommendation_id": "r1", "job": JOB})
    assert await match_jobs(mem, matching, user_id) == 1
    # One undelivered check-in already queued → no second recommendation this run.
    assert await match_jobs(mem, matching, user_id) == 0


async def test_match_jobs_disabled_without_client(user_id) -> None:
    assert await match_jobs(_mem(), None, user_id) == 0


async def test_check_followups_queues_and_marks_sent(user_id) -> None:
    mem = _mem()
    matching = FakeMatching(
        due={
            "recommendation_id": "r9",
            "title": "Evaluate AI medical answers",
            "partner": "Mindrift",
        }
    )

    assert await check_job_followups(mem, matching, user_id) == 1
    checkin = await mem.get_pending_checkin(user_id)
    assert checkin is not None
    assert checkin.checkin_type is CheckinType.JOB_FOLLOWUP
    assert "Evaluate AI medical answers" in checkin.message_hint
    assert matching.followup_sent == ["r9"]


async def test_check_followups_noop_when_none_due(user_id) -> None:
    mem = _mem()
    assert await check_job_followups(mem, FakeMatching(due=None), user_id) == 0
