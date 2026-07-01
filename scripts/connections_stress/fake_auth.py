"""A stand-in for the auth service, backed by a fixture instead of Supabase.

Supabase is not configured in this environment, so for the end-to-end dry run we replace ONLY
auth's data store — not its contract. This app speaks the exact endpoints the brain and the
connections service call over HTTP, guarded by the same mesh token:

  GET  /internal/users?state=MN     -> ["cx-ava", ...]      (the ingest roster, by state)
  GET  /internal/profiles/{id}      -> {name, age, city, state}   (identity for the profile)
  DELETE /internal/users/{id}       -> 204                   (erasure fan-out target)
  GET  /health                      -> {"status": "ok"}

Everything downstream (brain profile assembly, connections ingest/score/eval/surface/cluster)
runs for real against this. Run it on :8001 with SERVICE_TOKEN set to the shared mesh secret.
"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import Response

sys.path.insert(0, str(Path(__file__).resolve().parent))
from personas import IDENTITIES  # noqa: E402

_TOKEN = os.environ.get("SERVICE_TOKEN", "")

app = FastAPI(title="fake-auth (connections stress test)")


def _guard(x_service_token: str | None) -> None:
    if not _TOKEN:
        return  # tokenless local dev — mirror the brain's optional guard
    if not x_service_token or not secrets.compare_digest(x_service_token, _TOKEN):
        raise HTTPException(status_code=401, detail="bad service token")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/internal/users")
async def list_users(
    state: str = Query(...), x_service_token: str | None = Header(default=None)
) -> list[str]:
    _guard(x_service_token)
    want = state.strip().upper()
    return [uid for uid, ident in IDENTITIES.items() if ident.get("state") == want]


@app.get("/internal/profiles/{user_id}")
async def get_profile(user_id: str, x_service_token: str | None = Header(default=None)) -> dict:
    _guard(x_service_token)
    ident = IDENTITIES.get(user_id)
    if ident is None:
        raise HTTPException(status_code=404, detail="no such profile")
    return {"user_id": user_id, **ident, "photo_url": None}


@app.delete("/internal/users/{user_id}", status_code=204)
async def delete_user(user_id: str, x_service_token: str | None = Header(default=None)) -> Response:
    _guard(x_service_token)
    return Response(status_code=204)
