"""Commitment resolution during a proactive check-in (Companion logic), infra-free.

Proves: a 'kept' reply resolves the commitment and writes a follow-through signal;
'dropped' does the same with follow_through=False; 'unclear' resolves nothing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime

from alik.companion import Companion
from alik.memory.graph import GraphMemory
from alik.models import CommitmentNode, CommitmentStatus
from alik.prompt import COMMITMENT_RESOLVE_SYSTEM, load_persona
from tests.conftest import InMemoryGraphStore, InMemoryMemory


class FakeResolveLLM:
    def __init__(self, resolution: str, user_words: str = "we'll see") -> None:
        self.resolution = resolution
        self.user_words = user_words

    async def stream_reply(
        self, *, system: str, messages: Sequence[dict]
    ) -> AsyncIterator[str]:  # pragma: no cover
        yield ""

    async def complete(self, *, system: str, messages: Sequence[dict]) -> str:
        assert system == COMMITMENT_RESOLVE_SYSTEM
        return f'{{"resolution": "{self.resolution}", "user_words": "{self.user_words}"}}'


def _mem() -> GraphMemory:
    return GraphMemory(base=InMemoryMemory(), graph=InMemoryGraphStore(), current_facts_limit=50)


async def _seed_commitment(mem: GraphMemory, user_id: str) -> CommitmentNode:
    c = CommitmentNode(
        user_id=user_id,
        key="other:race",
        content="sign up for the half marathon",
        valid_from=datetime.now(UTC),
        status=CommitmentStatus.DUE,
    )
    await mem.write_commitments([c])
    return c


def _companion(mem: GraphMemory, llm) -> Companion:
    return Companion(memory=mem, llm=llm, persona=load_persona(), episode_limit=10)


async def test_kept_resolves_and_writes_follow_through(user_id):
    mem = _mem()
    c = await _seed_commitment(mem, user_id)
    companion = _companion(mem, FakeResolveLLM("kept", "yeah I signed up!"))
    companion._checkin_commitment["S"] = c
    companion._checkin_grace["S"] = 1

    await companion._handle_checkin_response(user_id, "S", c, "yeah I signed up this morning")

    stored = mem._graph._commitments[c.id]
    assert stored.status is CommitmentStatus.RESOLVED_KEPT
    assert stored.follow_through is True
    # Follow-through signal written for the nightly detect() to fold into the trait.
    signals = await mem.get_emotional_signals(user_id)
    ft = [s for s in signals if s.key == "follow_through"]
    assert len(ft) == 1
    assert "Followed through" in ft[0].content
    assert "S" not in companion._checkin_commitment  # state cleared


async def test_dropped_resolves_with_false_follow_through(user_id):
    mem = _mem()
    c = await _seed_commitment(mem, user_id)
    companion = _companion(mem, FakeResolveLLM("dropped", "nah I let it slide"))
    companion._checkin_commitment["S"] = c
    companion._checkin_grace["S"] = 1

    await companion._handle_checkin_response(user_id, "S", c, "honestly I didn't get to it")

    stored = mem._graph._commitments[c.id]
    assert stored.status is CommitmentStatus.RESOLVED_DROPPED
    assert stored.follow_through is False
    ft = [s for s in await mem.get_emotional_signals(user_id) if s.key == "follow_through"]
    assert len(ft) == 1
    assert "Did not follow through" in ft[0].content


async def test_unclear_resolves_nothing(user_id):
    mem = _mem()
    c = await _seed_commitment(mem, user_id)
    companion = _companion(mem, FakeResolveLLM("unclear"))
    companion._checkin_commitment["S"] = c
    companion._checkin_grace["S"] = 1

    await companion._handle_checkin_response(user_id, "S", c, "anyway, how are you?")

    assert mem._graph._commitments[c.id].status is CommitmentStatus.DUE  # untouched
    assert await mem.get_emotional_signals(user_id) == []  # no signal
    assert "S" in companion._checkin_commitment  # still active (one grace turn granted)
    assert companion._checkin_grace["S"] == 0
