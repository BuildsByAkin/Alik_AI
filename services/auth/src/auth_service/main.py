"""FastAPI app for the Auth + User Profile microservice (port 8001).

Standalone from the companion brain — its own deps, its own Supabase backend.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import server_settings
from .models import HealthResponse
from .routes import auth, profile
from .supabase_client import get_anon_client, get_service_client


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Warm both Supabase clients on startup so the first request isn't slowed by client
    # creation (and config problems surface immediately, not on first call).
    await get_anon_client()
    await get_service_client()
    yield


app = FastAPI(title="alik Auth + Profile Service", version="0.1.0", lifespan=lifespan)

app.include_router(auth.router)
app.include_router(profile.router)


@app.get("/health", response_model=HealthResponse, tags=["health"])
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=server_settings.port)


if __name__ == "__main__":
    main()
