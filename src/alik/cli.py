"""Terminal REPL — the second I/O adapter over the companion brain."""

from __future__ import annotations

import asyncio
import os
import uuid

from alik.companion import Companion
from alik.config import Settings
from alik.extraction import Extractor
from alik.llm import AnthropicLLM
from alik.memory.graph import GraphMemory
from alik.prompt import load_persona


async def _run() -> None:
    settings = Settings()
    user_id = os.environ.get("ALIK_USER_ID", "local-user")
    session_id = uuid.uuid4().hex

    memory = await GraphMemory.connect(
        database_url=settings.database_url,
        redis_url=settings.redis_url,
        falkordb_url=settings.falkordb_url,
        graph_name=settings.graph_name,
        working_ttl_seconds=settings.working_buffer_ttl_seconds,
        current_facts_limit=settings.current_facts_limit,
    )
    llm = AnthropicLLM(
        api_key=settings.anthropic_api_key.get_secret_value(),
        model=settings.companion_model,
        max_tokens=settings.companion_max_tokens,
    )
    extraction_llm = AnthropicLLM(
        api_key=settings.anthropic_api_key.get_secret_value(),
        model=settings.extraction_model,
        max_tokens=settings.extraction_max_tokens,
    )
    companion = Companion(
        memory=memory,
        llm=llm,
        persona=load_persona(settings.persona_path),
        episode_limit=settings.episode_retrieve_limit,
        extractor=Extractor(llm=extraction_llm, memory=memory),
        reflect_back_min_turn=settings.reflect_back_min_turn,
        reflect_back_min_confidence=settings.reflect_back_min_confidence,
        reflect_back_confidence_bump=settings.reflect_back_confidence_bump,
        corrected_trait_confidence=settings.corrected_trait_confidence,
        reflect_back_cooldown_sessions=settings.reflect_back_cooldown_sessions,
    )

    print(f"alik — session {session_id} (user {user_id}). Type /quit to end.\n")
    try:
        # Phase 5: if a proactive check-in is queued, open with it instead of waiting.
        opener = await companion.open_session(user_id, session_id)
        if opener:
            print(f"alik> {opener}\n")
        while True:
            try:
                line = (await asyncio.to_thread(input, "you> ")).strip()
            except EOFError:
                break
            if not line:
                continue
            if line in ("/quit", "/exit"):
                break
            print("alik> ", end="", flush=True)
            async for delta in companion.respond(user_id, session_id, line):
                print(delta, end="", flush=True)
            print("\n")

        print("…summarizing this session into memory…")
        summary = await companion.end_session(user_id, session_id)
        if summary:
            print(f"remembered: {summary}")
        # end_session fires extraction in the background; the CLI has no long-lived
        # loop, so wait for it before tearing down the connection.
        await companion.drain()
    finally:
        await memory.aclose()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
