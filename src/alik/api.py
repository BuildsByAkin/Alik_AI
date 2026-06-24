"""FastAPI surface — one of two I/O adapters over the companion brain."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from alik.companion import Companion
from alik.config import Settings
from alik.extraction import Extractor
from alik.llm import AnthropicLLM
from alik.memory.base import Memory
from alik.memory.graph import GraphMemory
from alik.prompt import load_persona
from alik.scheduler import start_scheduler


class ChatRequest(BaseModel):
    user_id: str
    session_id: str
    message: str


class EndRequest(BaseModel):
    user_id: str


def create_app(*, companion: Companion | None = None, memory: Memory | None = None) -> FastAPI:
    """Build the app. Inject ``companion``/``memory`` for tests; otherwise they are
    constructed from ``Settings`` at startup."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if getattr(app.state, "companion", None) is not None:
            yield  # pre-injected (tests) — nothing to build or tear down here.
            return

        settings = Settings()
        mem = await GraphMemory.connect(
            database_url=settings.database_url,
            redis_url=settings.redis_url,
            falkordb_url=settings.falkordb_url,
            graph_name=settings.graph_name,
            working_ttl_seconds=settings.working_buffer_ttl_seconds,
            current_facts_limit=settings.current_facts_limit,
            reflection_after_days=settings.reflection_after_days,
            confidence_decay_days=settings.confidence_decay_days,
            confidence_decay_factor=settings.confidence_decay_factor,
            confidence_floor=settings.confidence_floor,
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
        app.state.memory = mem
        app.state.companion = Companion(
            memory=mem,
            llm=llm,
            persona=load_persona(settings.persona_path),
            episode_limit=settings.episode_retrieve_limit,
            extractor=Extractor(llm=extraction_llm, memory=mem),
        )
        # Nightly sleep pass — best-effort (skips cleanly if APScheduler absent).
        app.state.scheduler = start_scheduler(mem, extraction_llm, settings)
        try:
            yield
        finally:
            if app.state.scheduler is not None:
                app.state.scheduler.shutdown(wait=False)
            await app.state.companion.drain()  # let in-flight extractions finish
            await mem.aclose()

    app = FastAPI(title="alik", lifespan=lifespan)
    if companion is not None:
        app.state.companion = companion
    if memory is not None:
        app.state.memory = memory

    @app.post("/chat")
    async def chat(req: ChatRequest, request: Request) -> StreamingResponse:
        companion: Companion = request.app.state.companion

        async def stream():
            async for delta in companion.respond(req.user_id, req.session_id, req.message):
                yield delta

        return StreamingResponse(stream(), media_type="text/plain")

    @app.post("/sessions/{session_id}/end")
    async def end_session(session_id: str, req: EndRequest, request: Request) -> dict:
        companion: Companion = request.app.state.companion
        summary = await companion.end_session(req.user_id, session_id)
        return {"summary": summary}

    @app.delete("/users/{user_id}")
    async def delete_user(user_id: str, request: Request) -> dict:
        memory: Memory = request.app.state.memory
        await memory.delete(user_id)
        return {"deleted": user_id}

    return app


app = create_app()
