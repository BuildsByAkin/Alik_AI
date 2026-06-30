"""Companion delivery of a people-match introduction + the yes/no callback, infra-free.

Proves: the opener is framed as a friend mentioning someone (never "match"/algorithm/app),
grounded in the EvalResult reason; a "yes" posts accepted, a "no" posts skipped — both
single-shot. Tone mirrors the job-recommendation delivery.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

from alik.companion import Companion
from alik.memory.graph import GraphMemory
from alik.models import CheckinType, PendingCheckin
from alik.prompt import load_persona
from tests.conftest import InMemoryGraphStore, InMemoryMemory

REASON = "they both light up about rock climbing and quiet weekends"


class FakeConnections:
    def __init__(self) -> None:
        self.responses: list[tuple[str, str, bool]] = []

    async def post_match_response(self, user_id: str, candidate_id: str, accepted: bool) -> None:
        self.responses.append((user_id, candidate_id, accepted))

    async def aclose(self) -> None:
        pass


class FakeLLM:
    def __init__(self) -> None:
        self.last_system: str | None = None

    async def stream_reply(self, *, system: str, messages: Sequence[dict]) -> AsyncIterator[str]:
        self.last_system = system
        yield "ok "

    async def complete(self, *, system: str, messages: Sequence[dict]) -> str:
        self.last_system = system
        return "Hey — there's someone I really think you'd enjoy."


def _mem() -> GraphMemory:
    return GraphMemory(base=InMemoryMemory(), graph=InMemoryGraphStore(), current_facts_limit=50)


def _companion(mem, llm, conn) -> Companion:
    return Companion(
        memory=mem, llm=llm, persona=load_persona(), episode_limit=10, connections_client=conn
    )


async def _consume(agen: AsyncIterator[str]) -> str:
    return "".join([chunk async for chunk in agen])


async def _queue_match(mem, user_id, candidate_id="cand-1"):
    await mem.queue_checkin(
        PendingCheckin(
            user_id=user_id,
            checkin_type=CheckinType.PEOPLE_MATCH,
            message_hint=REASON,
            payload={
                "type": "people_match",
                "reason": REASON,
                "candidate_id": candidate_id,
                "shared_interests": ["rock climbing"],
                "match_confidence": 0.74,
            },
        )
    )


async def test_people_match_opener_is_warm_not_clinical(user_id):
    mem = _mem()
    await _queue_match(mem, user_id)
    llm = FakeLLM()
    companion = _companion(mem, llm, FakeConnections())

    opener = await companion.open_session(user_id, "S")
    assert opener is not None
    # The opener brief frames it as a friend mentioning someone, grounded in the reason,
    # with the no-"match"/no-algorithm guardrail present.
    assert "good friend casually mentioning someone" in llm.last_system
    assert REASON in llm.last_system
    assert "never use the word 'match'" in llm.last_system
    assert companion._match_checkin["S"] == "cand-1"


async def test_yes_posts_accepted(user_id):
    mem = _mem()
    await _queue_match(mem, user_id)
    conn = FakeConnections()
    companion = _companion(mem, FakeLLM(), conn)
    await companion.open_session(user_id, "S")

    await _consume(companion.respond(user_id, "S", "yeah I'd love to meet them"))
    assert conn.responses == [(user_id, "cand-1", True)]
    assert "S" not in companion._match_checkin  # single-shot, cleared


async def test_no_posts_skipped(user_id):
    mem = _mem()
    await _queue_match(mem, user_id)
    conn = FakeConnections()
    companion = _companion(mem, FakeLLM(), conn)
    await companion.open_session(user_id, "S")

    await _consume(companion.respond(user_id, "S", "no thanks, not right now"))
    assert conn.responses == [(user_id, "cand-1", False)]
