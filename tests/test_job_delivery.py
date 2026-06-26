"""Companion job-recommendation delivery + follow-up lifecycle (Phase 7), infra-free.

Proves: a job_recommendation opener uses the warm framing and offers the link; "yes" shares
the partner URL; "no" drops it (rec stays logged, outcome still open); a job_followup classified
as loved_it writes the outcome and activates the thread.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

from alik.companion import Companion
from alik.memory.graph import GraphMemory
from alik.models import CheckinType, JobOutcome, PendingCheckin
from alik.prompt import JOB_OUTCOME_CLASSIFY_SYSTEM, load_persona
from tests.conftest import InMemoryGraphStore, InMemoryMemory

MINDRIFT_URL = "https://mindrift.ai"
REC_HINT = (
    'You spotted some paid work that might suit them: "Evaluate AI medical answers" with '
    f"Mindrift, paying $40-60/hr. Mention it warmly and offer the link: {MINDRIFT_URL}"
)


class FakeJobLLM:
    """Returns a scripted outcome for the follow-up classify call; echoes otherwise."""

    def __init__(self, outcome: str | None = None) -> None:
        self.outcome = outcome
        self.last_system: str | None = None

    async def stream_reply(self, *, system: str, messages: Sequence[dict]) -> AsyncIterator[str]:
        self.last_system = system
        yield "ok "

    async def complete(self, *, system: str, messages: Sequence[dict]) -> str:
        self.last_system = system
        if system == JOB_OUTCOME_CLASSIFY_SYSTEM and self.outcome is not None:
            return f'{{"outcome": "{self.outcome}"}}'
        return "Warm opener."


def _mem() -> GraphMemory:
    return GraphMemory(base=InMemoryMemory(), graph=InMemoryGraphStore(), current_facts_limit=50)


def _companion(mem: GraphMemory, llm) -> Companion:
    return Companion(memory=mem, llm=llm, persona=load_persona(), episode_limit=10)


async def _consume(agen: AsyncIterator[str]) -> str:
    return "".join([chunk async for chunk in agen])


async def _queue_recommendation(mem: GraphMemory, user_id: str) -> None:
    await mem.queue_checkin(
        PendingCheckin(
            user_id=user_id,
            checkin_type=CheckinType.JOB_RECOMMENDATION,
            message_hint=REC_HINT,
        )
    )
    await mem.log_job_recommendation(user_id, "mindrift-medical-eval-001", follow_up_after_days=3)


async def test_recommendation_opener_is_warm_and_offers_link(user_id) -> None:
    mem = _mem()
    await _queue_recommendation(mem, user_id)
    llm = FakeJobLLM()
    companion = _companion(mem, llm)

    opener = await companion.open_session(user_id, "S")
    assert opener is not None
    # Warm, friend-who-spotted-an-opportunity framing + link offer in the opener brief.
    assert "friend who just spotted an opportunity" in llm.last_system
    assert "Want me to send you the link?" in llm.last_system
    # Delivery stamped so the 3-day follow-up becomes eligible; session state armed.
    recs = await mem.get_job_recommendations(user_id)
    assert recs[0].delivered_at is not None
    assert companion._job_checkin["S"]["url"] == MINDRIFT_URL


async def test_yes_shares_the_link(user_id) -> None:
    mem = _mem()
    await _queue_recommendation(mem, user_id)
    llm = FakeJobLLM()
    companion = _companion(mem, llm)
    await companion.open_session(user_id, "S")

    await _consume(companion.respond(user_id, "S", "yes please, send it"))
    # The reply's system prompt carries the link-sharing directive with the URL.
    assert MINDRIFT_URL in llm.last_system
    assert "S" not in companion._job_checkin  # single-shot, cleared


async def test_no_drops_quietly_and_keeps_log(user_id) -> None:
    mem = _mem()
    await _queue_recommendation(mem, user_id)
    llm = FakeJobLLM()
    companion = _companion(mem, llm)
    await companion.open_session(user_id, "S")

    await _consume(companion.respond(user_id, "S", "no thanks, not interested"))
    assert "S" not in companion._job_checkin
    recs = await mem.get_job_recommendations(user_id)
    assert recs[0].outcome is None  # still logged, thread still open (resolved at follow-up)
    assert MINDRIFT_URL not in (llm.last_system or "")


async def test_followup_loved_it_records_outcome_and_activates(user_id) -> None:
    mem = _mem()
    rec_id = await mem.log_job_recommendation(
        user_id, "mindrift-medical-eval-001", follow_up_after_days=3
    )
    await mem.mark_job_recommendation_delivered(rec_id)
    await mem.mark_job_followup_sent(rec_id)
    await mem.queue_checkin(
        PendingCheckin(
            user_id=user_id,
            checkin_type=CheckinType.JOB_FOLLOWUP,
            message_hint="Check in on how the Mindrift work went.",
        )
    )
    llm = FakeJobLLM(outcome="loved_it")
    companion = _companion(mem, llm)

    await companion.open_session(user_id, "S")
    assert companion._job_checkin["S"]["rec_id"] == rec_id

    await _consume(companion.respond(user_id, "S", "honestly I loved it, it's going great"))
    recs = await mem.get_job_recommendations(user_id)
    assert recs[0].outcome is JobOutcome.LOVED_IT
    assert await mem.get_job_active(user_id) is True
