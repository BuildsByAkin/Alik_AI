"""Companion job-recommendation delivery + follow-up lifecycle, infra-free.

The brain still delivers the opener, shares the link on "yes", and classifies the follow-up
reply — but recommendation state now lives in the matching service (faked here). Proves the
brain calls the service correctly (mark delivered, post outcome) and drives the conversation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

from alik.companion import Companion
from alik.memory.graph import GraphMemory
from alik.models import CheckinType, PendingCheckin
from alik.prompt import JOB_OUTCOME_CLASSIFY_SYSTEM, load_persona
from tests.conftest import FakeMatching, InMemoryGraphStore, InMemoryMemory

MINDRIFT_URL = "https://mindrift.ai"
REC_HINT = (
    'You spotted some paid work that might suit them: "Evaluate AI medical answers" with '
    f"Mindrift, paying $40-60/hr. Mention it warmly and offer the link: {MINDRIFT_URL}"
)


class FakeJobLLM:
    """Returns a scripted outcome for the follow-up classify call; warm opener otherwise."""

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


def _companion(mem: GraphMemory, llm, matching) -> Companion:
    return Companion(
        memory=mem, llm=llm, persona=load_persona(), episode_limit=10, matching_client=matching
    )


async def _consume(agen: AsyncIterator[str]) -> str:
    return "".join([chunk async for chunk in agen])


async def _queue_rec(mem: GraphMemory, user_id: str) -> None:
    await mem.queue_checkin(
        PendingCheckin(
            user_id=user_id, checkin_type=CheckinType.JOB_RECOMMENDATION, message_hint=REC_HINT
        )
    )


async def test_recommendation_opener_is_warm_and_marks_delivered(user_id) -> None:
    mem = _mem()
    await _queue_rec(mem, user_id)
    matching = FakeMatching(open_rec={"recommendation_id": "r1", "partner_url": MINDRIFT_URL})
    llm = FakeJobLLM()
    companion = _companion(mem, llm, matching)

    opener = await companion.open_session(user_id, "S")
    assert opener is not None
    assert "friend who just spotted an opportunity" in llm.last_system
    assert "Want me to send you the link?" in llm.last_system
    assert matching.delivered == ["r1"]  # delivery reported to the service
    assert companion._job_checkin["S"]["url"] == MINDRIFT_URL
    assert companion._job_checkin["S"]["rec_id"] == "r1"


async def test_yes_shares_the_link(user_id) -> None:
    mem = _mem()
    await _queue_rec(mem, user_id)
    matching = FakeMatching(open_rec={"recommendation_id": "r1", "partner_url": MINDRIFT_URL})
    llm = FakeJobLLM()
    companion = _companion(mem, llm, matching)
    await companion.open_session(user_id, "S")

    await _consume(companion.respond(user_id, "S", "yes please, send it"))
    assert MINDRIFT_URL in llm.last_system
    assert "S" not in companion._job_checkin  # single-shot, cleared


async def test_no_drops_quietly(user_id) -> None:
    mem = _mem()
    await _queue_rec(mem, user_id)
    matching = FakeMatching(open_rec={"recommendation_id": "r1", "partner_url": MINDRIFT_URL})
    llm = FakeJobLLM()
    companion = _companion(mem, llm, matching)
    await companion.open_session(user_id, "S")

    await _consume(companion.respond(user_id, "S", "no thanks, not interested"))
    assert "S" not in companion._job_checkin
    assert MINDRIFT_URL not in (llm.last_system or "")
    assert matching.outcomes == []  # nothing resolved at the recommendation stage


async def test_followup_loved_it_posts_outcome(user_id) -> None:
    mem = _mem()
    await mem.queue_checkin(
        PendingCheckin(
            user_id=user_id,
            checkin_type=CheckinType.JOB_FOLLOWUP,
            message_hint="Check in on how the Mindrift work went.",
        )
    )
    matching = FakeMatching(pending={"recommendation_id": "r2"})
    llm = FakeJobLLM(outcome="loved_it")
    companion = _companion(mem, llm, matching)

    await companion.open_session(user_id, "S")
    assert companion._job_checkin["S"]["rec_id"] == "r2"

    await _consume(companion.respond(user_id, "S", "honestly I loved it, it's going great"))
    assert matching.outcomes == [(user_id, "r2", "loved_it")]
