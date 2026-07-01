"""FastAPI app for the rendezvous (meeting-coordination) microservice (port 8004).

Standalone: its own Postgres (all meet state), its own deps. It reads/writes the brain only
over HTTP (queue check-ins + record social events) and never touches the brain's databases.
``create_app`` accepts injected ``store``/``brain_client`` for tests; otherwise they're built
from Settings at startup, along with an optional APScheduler job that runs the advance pass.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from rendezvous_service.brain_client import BrainClient
from rendezvous_service.config import settings
from rendezvous_service.lifecycle import advance_pass
from rendezvous_service.models import HealthResponse
from rendezvous_service.routes import router
from rendezvous_service.store import PgStore, Store

logger = logging.getLogger("rendezvous.main")


def _start_scheduler(app: FastAPI):
    """Start the advance-pass cron, or None if APScheduler isn't installed."""
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.warning("apscheduler not installed — scheduler disabled")
        return None

    async def _advance_job() -> None:
        await advance_pass(app.state.store, app.state.brain_client)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(_advance_job, CronTrigger.from_crontab(settings.advance_cron), id="advance")
    scheduler.start()
    return scheduler


def create_app(*, store: Store | None = None, brain_client: BrainClient | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if getattr(app.state, "store", None) is not None:
            yield  # pre-injected (tests) — nothing to build or tear down.
            return
        token = settings.service_token.get_secret_value()
        app.state.store = await PgStore.connect(settings.database_url)
        app.state.brain_client = BrainClient(base_url=settings.brain_url, service_token=token)
        app.state.scheduler = _start_scheduler(app)
        try:
            yield
        finally:
            if app.state.scheduler is not None:
                app.state.scheduler.shutdown(wait=False)
            await app.state.brain_client.aclose()
            await app.state.store.aclose()

    app = FastAPI(title="alik rendezvous service", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    if store is not None:
        app.state.store = store
    if brain_client is not None:
        app.state.brain_client = brain_client

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
