"""Companion brain logic, proven infra-free against the in-memory Memory double.

This is the deterministic, always-runnable counterpart to test_continuity.py
(which runs the same flow against real Postgres + Redis when infra is available).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from alik.companion import Companion
from alik.prompt import load_persona
from tests.conftest import InMemoryMemory


async def _drain(stream: AsyncIterator[str]) -> None:
    async for _ in stream:
        pass


def _companion(memory: InMemoryMemory, llm) -> Companion:
    return Companion(memory=memory, llm=llm, persona=load_persona(), episode_limit=10)


async def test_fact_persists_across_sessions(inmemory, fake_llm, user_id):
    companion = _companion(inmemory, fake_llm)

    # Session A: the user states a fact, then the session ends.
    await _drain(companion.respond(user_id, "A", "Remember: my dog is named Rufus."))
    summary = await companion.end_session(user_id, "A")
    assert summary is not None
    assert "Rufus" in summary

    # Session B: a fresh session asks about it.
    await _drain(companion.respond(user_id, "B", "What's my dog's name?"))

    # The injected context for session B carried the fact across sessions.
    assert fake_llm.last_system is not None
    assert "Rufus" in fake_llm.last_system


async def test_response_appends_both_turns_to_buffer(inmemory, fake_llm, user_id):
    companion = _companion(inmemory, fake_llm)
    await _drain(companion.respond(user_id, "S", "hello"))

    ctx = await inmemory.retrieve(user_id, "S")
    roles = [t.role for t in ctx.working]
    assert roles == ["user", "assistant"]
    assert ctx.working[0].content == "hello"


async def test_messages_sent_to_llm_start_with_user_turn(inmemory, fake_llm, user_id):
    companion = _companion(inmemory, fake_llm)
    await _drain(companion.respond(user_id, "S", "first message"))

    assert fake_llm.last_messages is not None
    assert fake_llm.last_messages[0]["role"] == "user"
    assert fake_llm.last_messages[-1]["content"] == "first message"


async def test_end_session_with_empty_buffer_returns_none(inmemory, fake_llm, user_id):
    companion = _companion(inmemory, fake_llm)
    assert await companion.end_session(user_id, "never-used") is None


async def test_end_session_clears_working_buffer(inmemory, fake_llm, user_id):
    companion = _companion(inmemory, fake_llm)
    await _drain(companion.respond(user_id, "S", "remember this"))
    await companion.end_session(user_id, "S")

    ctx = await inmemory.retrieve(user_id, "S")
    assert ctx.working == []
    assert len(ctx.episodes) == 1
