"""The proactive session opener (Companion.open_session), infra-free.

Proves: when a check-in is queued the session opens with it (and it's marked
delivered + written as the first assistant turn, and the linked commitment is tracked
for resolution); a second open in the same session does not repeat; no check-in -> None.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime

from alik.companion import Companion
from alik.memory.graph import GraphMemory
from alik.models import CheckinType, CommitmentNode, CommitmentStatus, PendingCheckin
from alik.prompt import load_persona
from tests.conftest import InMemoryGraphStore, InMemoryMemory


class FakeOpenerLLM:
    async def stream_reply(
        self, *, system: str, messages: Sequence[dict]
    ) -> AsyncIterator[str]:  # pragma: no cover
        yield ""

    async def complete(self, *, system: str, messages: Sequence[dict]) -> str:
        return "Hey — how are you feeling about the half marathon these days?"


def _mem() -> GraphMemory:
    return GraphMemory(base=InMemoryMemory(), graph=InMemoryGraphStore(), current_facts_limit=50)


def _companion(mem: GraphMemory) -> Companion:
    return Companion(memory=mem, llm=FakeOpenerLLM(), persona=load_persona(), episode_limit=10)


async def test_opens_with_checkin_and_marks_delivered(user_id):
    mem = _mem()
    c = CommitmentNode(
        user_id=user_id,
        key="other:race",
        content="sign up for the half marathon",
        valid_from=datetime.now(UTC),
        status=CommitmentStatus.DUE,
    )
    await mem.write_commitments([c])
    await mem.queue_checkin(
        PendingCheckin(
            user_id=user_id,
            checkin_type=CheckinType.DUE_COMMITMENT,
            message_hint="check in warmly about the half marathon",
            commitment_id=c.id,
        )
    )
    companion = _companion(mem)

    opener = await companion.open_session(user_id, "S")
    assert opener and "half marathon" in opener
    # Delivered exactly once: no longer pending.
    assert await mem.get_pending_checkin(user_id) is None
    # Written as the opening assistant turn so the session has context.
    ctx = await mem.retrieve(user_id, "S")
    assert ctx.working and ctx.working[-1].role == "assistant"
    assert ctx.working[-1].content == opener
    # The linked commitment is tracked for resolution on the next user turn.
    assert companion._checkin_commitment["S"].id == c.id


async def test_second_open_same_session_does_not_repeat(user_id):
    mem = _mem()
    await mem.queue_checkin(
        PendingCheckin(
            user_id=user_id,
            checkin_type=CheckinType.GENERAL_CHECKIN,
            message_hint="how are things?",
        )
    )
    companion = _companion(mem)

    assert await companion.open_session(user_id, "S") is not None
    assert await companion.open_session(user_id, "S") is None  # already opened this session


async def test_no_checkin_means_no_opener(user_id):
    mem = _mem()
    companion = _companion(mem)
    assert await companion.open_session(user_id, "S") is None
