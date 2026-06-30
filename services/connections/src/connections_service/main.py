"""FastAPI app for the people-matching ('connections') microservice (port 8003).

Standalone from the companion brain: its own Postgres (all match state), its own deps. It is
a PURE CONSUMER — it reads the assembled living profile from the brain's Profile API and never
touches the brain's databases. ``create_app`` accepts injected ``store``/``brain_client``/
``auth_client`` for tests; otherwise they are built from Settings at startup.

Part 2 adds ingestion: at startup we seed the interest taxonomy and (best-effort) start an
APScheduler job that refreshes profiles on ``INGEST_CRON``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from connections_service.auth_client import AuthClient
from connections_service.brain_client import BrainClient
from connections_service.cluster import clustering_pass
from connections_service.config import settings
from connections_service.eval import eval_pass
from connections_service.ingest import run_ingest
from connections_service.interests import all_interest_nodes
from connections_service.llm import AnthropicLLM, LLMClient
from connections_service.models import HealthResponse
from connections_service.routes import router
from connections_service.scoring import scoring_pass
from connections_service.store import PgStore, Store
from connections_service.surface import surface_pass

logger = logging.getLogger("connections.main")


def _start_scheduler(app: FastAPI):
    """Start the ingest + scoring + eval crons, or None if APScheduler isn't installed."""
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.warning("apscheduler not installed — schedulers disabled")
        return None

    async def _ingest_job() -> None:
        await run_ingest(app.state.store, app.state.brain_client, app.state.auth_client, settings)

    async def _score_job() -> None:
        await scoring_pass(app.state.store, settings)

    async def _eval_job() -> None:
        await eval_pass(app.state.store, app.state.llm, settings)

    async def _surface_job() -> None:
        await surface_pass(app.state.store, app.state.brain_client, settings)

    async def _cluster_job() -> None:
        await clustering_pass(app.state.store, app.state.brain_client, settings)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(_ingest_job, CronTrigger.from_crontab(settings.ingest_cron), id="ingest")
    scheduler.add_job(_score_job, CronTrigger.from_crontab(settings.score_cron), id="score")
    scheduler.add_job(_eval_job, CronTrigger.from_crontab(settings.eval_cron), id="eval")
    scheduler.add_job(_surface_job, CronTrigger.from_crontab(settings.surface_cron), id="surface")
    scheduler.add_job(_cluster_job, CronTrigger.from_crontab(settings.cluster_cron), id="cluster")
    scheduler.start()
    return scheduler


def create_app(
    *,
    store: Store | None = None,
    brain_client: BrainClient | None = None,
    auth_client: AuthClient | None = None,
    llm: LLMClient | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if getattr(app.state, "store", None) is not None:
            yield  # pre-injected (tests) — nothing to build or tear down.
            return
        token = settings.service_token.get_secret_value()
        app.state.settings = settings
        app.state.store = await PgStore.connect(settings.database_url)
        await app.state.store.ensure_interest_nodes(all_interest_nodes())
        app.state.brain_client = BrainClient(base_url=settings.brain_url, service_token=token)
        app.state.auth_client = AuthClient(base_url=settings.auth_url, service_token=token)
        app.state.llm = AnthropicLLM(
            api_key=settings.anthropic_api_key.get_secret_value(),
            model=settings.eval_model,
            max_tokens=settings.eval_max_tokens,
        )
        app.state.scheduler = _start_scheduler(app)
        try:
            yield
        finally:
            if app.state.scheduler is not None:
                app.state.scheduler.shutdown(wait=False)
            await app.state.auth_client.aclose()
            await app.state.brain_client.aclose()
            await app.state.store.aclose()

    app = FastAPI(title="alik connections service", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    if store is not None:
        app.state.store = store
    if brain_client is not None:
        app.state.brain_client = brain_client
    if auth_client is not None:
        app.state.auth_client = auth_client
    if llm is not None:
        app.state.llm = llm

    @app.get("/health", response_model=HealthResponse, tags=["health"])
    async def health() -> HealthResponse:
        return HealthResponse(status="ok")

    app.include_router(router)
    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=settings.port)


if __name__ == "__main__":
    main()
