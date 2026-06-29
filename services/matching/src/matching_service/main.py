"""FastAPI app for the job-matching microservice (port 8002).

Standalone from the companion brain: its own Postgres (the recommendation log), its own
deps. It reads the assembled living profile from the brain to score a curated catalog, and
the brain delivers any recommendation through the companion. ``create_app`` accepts injected
``store``/``brain_client``/``catalog`` for tests; otherwise they are built from Settings.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from matching_service.brain_client import BrainClient
from matching_service.catalog import load_catalog
from matching_service.config import settings
from matching_service.routes import router
from matching_service.store import PgStore, Store


def create_app(
    *,
    store: Store | None = None,
    brain_client: BrainClient | None = None,
    catalog: list | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if getattr(app.state, "store", None) is not None:
            yield  # pre-injected (tests) — nothing to build or tear down.
            return
        app.state.settings = settings
        app.state.catalog = load_catalog(settings.catalog_path)
        app.state.store = await PgStore.connect(settings.database_url)
        app.state.brain_client = BrainClient(
            base_url=settings.brain_url,
            service_token=settings.service_token.get_secret_value(),
        )
        try:
            yield
        finally:
            await app.state.brain_client.aclose()
            await app.state.store.aclose()

    app = FastAPI(title="alik matching service", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    if store is not None:
        app.state.store = store
    if brain_client is not None:
        app.state.brain_client = brain_client
    if catalog is not None:
        app.state.catalog = catalog

    @app.get("/health", tags=["health"])
    async def health() -> dict:
        return {"status": "ok"}

    app.include_router(router)
    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=settings.port)


if __name__ == "__main__":
    main()
