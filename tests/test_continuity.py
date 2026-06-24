"""The headline test: a fact stated in session A is recalled in session B.

LLM is faked (deterministic); Memory is real, so this exercises the genuine
cross-session path: working buffer -> summarize -> episodic -> injected context.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from alik.companion import Companion
from alik.prompt import load_persona
from tests.conftest import requires_infra


async def _drain(stream: AsyncIterator[str]) -> None:
    async for _ in stream:
        pass


@requires_infra
async def test_fact_persists_across_sessions(memory, user_id, fake_llm):
    companion = Companion(memory=memory, llm=fake_llm, persona=load_persona(), episode_limit=10)
    try:
        # Session A: the user states a fact, then the session ends.
        await _drain(companion.respond(user_id, "A", "Remember: my dog is named Rufus."))
        summary = await companion.end_session(user_id, "A")
        assert summary is not None
        assert "Rufus" in summary

        # Session B: a fresh session asks about it.
        await _drain(companion.respond(user_id, "B", "What's my dog's name?"))

        # The companion's injected context for session B carried the fact across.
        assert fake_llm.last_system is not None
        assert "Rufus" in fake_llm.last_system
    finally:
        await memory.delete(user_id)
