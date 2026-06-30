"""FastAPI surface — one of two I/O adapters over the companion brain."""

from __future__ import annotations

import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from alik.auth_client import AuthClient
from alik.companion import Companion
from alik.config import Settings
from alik.connections_client import ConnectionsClient
from alik.extraction import Extractor
from alik.llm import AnthropicLLM
from alik.matching_client import MatchingClient
from alik.memory.base import Memory
from alik.memory.graph import GraphMemory
from alik.models import CheckinType, PendingCheckin, TraitStatus
from alik.prompt import load_persona
from alik.scheduler import start_scheduler


def _check_service_token(request: Request, x_service_token: str | None) -> None:
    """Guard service-to-service reads (e.g. matching pulling the profile).

    Enforced only when a service token is configured (so injected-test apps and local dev
    without a token still work); when set, a mismatching/absent header is rejected.
    """
    settings = getattr(request.app.state, "settings", None)
    configured = settings.service_token.get_secret_value() if settings is not None else ""
    if not configured:
        return
    if not x_service_token or not secrets.compare_digest(x_service_token, configured):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid service token")


class ChatRequest(BaseModel):
    user_id: str
    session_id: str
    message: str


class EndRequest(BaseModel):
    user_id: str


class CheckinRequest(BaseModel):
    """A people-match check-in queued by the connections service. One shape for both the 1:1
    (people_match) and group (people_match_group) payloads — ``type`` distinguishes them, and
    the extra fields each carries pass through to the stored payload."""

    model_config = ConfigDict(extra="allow")

    type: str
    reason: str


def _profile_payload(user_id: str, identity: dict | None, facts, traits, dimensions) -> dict:
    """Serialize the assembled living profile for the Profile API (matching's input)."""
    return {
        "user_id": user_id,
        "identity": identity,  # name/age/city/photo_url from auth, or None if unavailable
        "facts": [{"key": f.key, "content": f.content} for f in facts],
        "confirmed_traits": [
            {"key": t.key, "content": t.content, "confidence": t.confidence} for t in traits
        ],
        "dimensions": [
            {
                "dimension": d.dimension,
                "value": d.value,
                "content": d.content,
                "confidence": d.confidence,
                "status": str(d.status),
                "observation_count": d.observation_count,
            }
            for d in dimensions
        ],
    }


def create_app(
    *,
    companion: Companion | None = None,
    memory: Memory | None = None,
    auth_client: AuthClient | None = None,
    matching_client: MatchingClient | None = None,
    connections_client: ConnectionsClient | None = None,
) -> FastAPI:
    """Build the app. Inject ``companion``/``memory``/``auth_client``/``matching_client``/
    ``connections_client`` for tests; otherwise they are constructed from ``Settings``."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if getattr(app.state, "companion", None) is not None:
            yield  # pre-injected (tests) — nothing to build or tear down here.
            return

        settings = Settings()
        app.state.settings = settings
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
        token = settings.service_token.get_secret_value()
        app.state.memory = mem
        app.state.auth_client = AuthClient(base_url=settings.auth_service_url, service_token=token)
        app.state.matching_client = (
            MatchingClient(base_url=settings.matching_service_url, service_token=token)
            if settings.matching_service_url
            else None
        )
        app.state.connections_client = (
            ConnectionsClient(base_url=settings.connections_service_url, service_token=token)
            if settings.connections_service_url
            else None
        )
        app.state.companion = Companion(
            memory=mem,
            llm=llm,
            persona=load_persona(settings.persona_path),
            episode_limit=settings.episode_retrieve_limit,
            extractor=Extractor(llm=extraction_llm, memory=mem),
            matching_client=app.state.matching_client,
            connections_client=app.state.connections_client,
        )
        # Nightly sleep pass — best-effort (skips cleanly if APScheduler absent).
        app.state.scheduler = start_scheduler(mem, extraction_llm, settings)
        try:
            yield
        finally:
            if app.state.scheduler is not None:
                app.state.scheduler.shutdown(wait=False)
            await app.state.companion.drain()  # let in-flight extractions finish
            await app.state.auth_client.aclose()
            if app.state.matching_client is not None:
                await app.state.matching_client.aclose()
            if app.state.connections_client is not None:
                await app.state.connections_client.aclose()
            await mem.aclose()

    app = FastAPI(title="alik", lifespan=lifespan)
    if companion is not None:
        app.state.companion = companion
    if memory is not None:
        app.state.memory = memory
    if auth_client is not None:
        app.state.auth_client = auth_client
    if matching_client is not None:
        app.state.matching_client = matching_client
    if connections_client is not None:
        app.state.connections_client = connections_client

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

    @app.get("/users/{user_id}/profile")
    async def get_profile(
        user_id: str, request: Request, x_service_token: str | None = Header(default=None)
    ) -> dict:
        """The assembled living profile — identity (auth) + facts + confirmed traits +
        behavioral dimensions. The single rich picture the matching service consumes."""
        _check_service_token(request, x_service_token)
        memory: Memory = request.app.state.memory
        auth: AuthClient | None = getattr(request.app.state, "auth_client", None)
        facts = (
            await memory.get_current_facts(user_id) if hasattr(memory, "get_current_facts") else []
        )
        all_traits = (
            await memory.get_current_traits(user_id)
            if hasattr(memory, "get_current_traits")
            else []
        )
        traits = [t for t in all_traits if t.status is TraitStatus.CONFIRMED]
        dimensions = await memory.get_profile_dimensions(user_id)
        identity = await auth.get_profile(user_id) if auth is not None else None
        return _profile_payload(user_id, identity, facts, traits, dimensions)

    @app.post("/users/{user_id}/checkins")
    async def queue_checkin(
        user_id: str,
        req: CheckinRequest,
        request: Request,
        x_service_token: str | None = Header(default=None),
    ) -> dict:
        """Service-to-service: the connections service queues a people-match opener. The
        companion delivers it (warmly, never as a 'match') at the next session."""
        _check_service_token(request, x_service_token)
        memory: Memory = request.app.state.memory
        checkin_type = (
            CheckinType.PEOPLE_MATCH_GROUP
            if req.type == "people_match_group"
            else CheckinType.PEOPLE_MATCH
        )
        checkin_id = await memory.queue_checkin(
            PendingCheckin(
                user_id=user_id,
                checkin_type=checkin_type,
                message_hint=req.reason,  # the warm reason — the core of the opener
                payload=req.model_dump(),
            )
        )
        return {"checkin_id": checkin_id}

    @app.delete("/users/{user_id}")
    async def delete_user(user_id: str, request: Request) -> dict:
        """Cross-service account erasure: the brain's own memory, then the auth, matching, and
        connections services. Loud and idempotent — if any backend can't erase, we raise rather
        than report a partial success; re-running after recovery completes the erasure."""
        memory: Memory = request.app.state.memory
        auth: AuthClient | None = getattr(request.app.state, "auth_client", None)
        matching: MatchingClient | None = getattr(request.app.state, "matching_client", None)
        connections: ConnectionsClient | None = getattr(
            request.app.state, "connections_client", None
        )
        await memory.delete(user_id)
        if auth is not None:
            await auth.delete_user(user_id)
        if matching is not None:
            await matching.delete_user(user_id)
        if connections is not None:
            await connections.delete_user(user_id)
        return {"deleted": user_id}

    return app


app = create_app()
