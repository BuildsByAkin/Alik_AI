"""Companion delivery of a rendezvous coordination check-in + routing the reply back to the
rendezvous service, infra-free. Proves each stage (pref/confirm/followup) captures the reply,
posts it back once, and clears the per-session state."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

from alik.companion import Companion
from alik.memory.graph import GraphMemory
from alik.models import CheckinType, PendingCheckin
from alik.prompt import load_persona
from tests.conftest import InMemoryGraphStore, InMemoryMemory

MEET = "meet-1"


class FakeRendezvous:
    def __init__(self) -> None:
        self.prefs: list[tuple[str, str, str]] = []
        self.confirms: list[tuple[str, str, bool]] = []
        self.followups: list[tuple[str, str, bool]] = []

    async def post_pref(self, meet_id, user_id, text) -> None:
        self.prefs.append((meet_id, user_id, text))

    async def post_confirm(self, meet_id, user_id, accepted) -> None:
        self.confirms.append((meet_id, user_id, accepted))

    async def post_followup(self, meet_id, user_id, felt_positive) -> None:
        self.followups.append((meet_id, user_id, felt_positive))

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
        return "sounds good"


def _mem() -> GraphMemory:
    return GraphMemory(base=InMemoryMemory(), graph=InMemoryGraphStore(), current_facts_limit=50)


def _companion(mem, llm, rv) -> Companion:
    return Companion(
        memory=mem, llm=llm, persona=load_persona(), episode_limit=10, rendezvous_client=rv
    )


async def _consume(agen: AsyncIterator[str]) -> str:
    return "".join([chunk async for chunk in agen])


async def _queue(mem, user_id, checkin_type, reason):
    await mem.queue_checkin(
        PendingCheckin(
            user_id=user_id,
            checkin_type=checkin_type,
            message_hint=reason,
            payload={"type": str(checkin_type), "reason": reason, "meet_id": MEET},
        )
    )


async def test_pref_reply_is_relayed_verbatim(user_id):
    mem, rv = _mem(), FakeRendezvous()
    await _queue(mem, user_id, CheckinType.RENDEZVOUS_PREF, "settle a rough where/when")
    companion = _companion(mem, FakeLLM(), rv)

    opener = await companion.open_session(user_id, "S")
    assert opener is not None
    assert companion._rendezvous_checkin["S"] == ("pref", MEET)

    await _consume(companion.respond(user_id, "S", "weekends, somewhere in Uptown"))
    assert rv.prefs == [(MEET, user_id, "weekends, somewhere in Uptown")]
    assert "S" not in companion._rendezvous_checkin  # single-shot, cleared


async def test_confirm_yes_posts_accepted(user_id):
    mem, rv = _mem(), FakeRendezvous()
    await _queue(mem, user_id, CheckinType.RENDEZVOUS_CONFIRM, "a rough plan: Uptown / Saturday")
    companion = _companion(mem, FakeLLM(), rv)
    await companion.open_session(user_id, "S")

    await _consume(companion.respond(user_id, "S", "yeah that works!"))
    assert rv.confirms == [(MEET, user_id, True)]


async def test_confirm_no_posts_declined(user_id):
    mem, rv = _mem(), FakeRendezvous()
    await _queue(mem, user_id, CheckinType.RENDEZVOUS_CONFIRM, "a rough plan")
    companion = _companion(mem, FakeLLM(), rv)
    await companion.open_session(user_id, "S")

    await _consume(companion.respond(user_id, "S", "no, not this weekend"))
    assert rv.confirms == [(MEET, user_id, False)]


async def test_followup_reads_feeling(user_id):
    mem, rv = _mem(), FakeRendezvous()
    await _queue(mem, user_id, CheckinType.RENDEZVOUS_FOLLOWUP, "ask how it went")
    companion = _companion(mem, FakeLLM(), rv)
    await companion.open_session(user_id, "S")

    await _consume(companion.respond(user_id, "S", "it was great, we really clicked"))
    assert rv.followups == [(MEET, user_id, True)]


async def test_no_rendezvous_client_means_no_capture(user_id):
    mem = _mem()
    await _queue(mem, user_id, CheckinType.RENDEZVOUS_PREF, "where/when")
    companion = Companion(memory=mem, llm=FakeLLM(), persona=load_persona(), episode_limit=10)

    opener = await companion.open_session(user_id, "S")
    assert opener is not None  # opener still delivered
    assert "S" not in companion._rendezvous_checkin  # ...but no capture armed (graceful)
