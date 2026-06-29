"""Shared FastAPI dependency: validate the Bearer token and resolve the current user.

Token validation is delegated to Supabase (``auth.get_user(jwt)``) — we never verify or
decode the JWT ourselves. A missing/invalid token yields 401.
"""

from __future__ import annotations

import secrets

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import server_settings
from .supabase_client import get_anon_client

_bearer = HTTPBearer(auto_error=False)


async def verify_service_token(x_service_token: str | None = Header(default=None)) -> None:
    """Gate the service-to-service /internal endpoints with the shared secret.

    Fails closed: if no token is configured, every request is rejected. Constant-time
    compare to avoid leaking the secret via timing.
    """
    configured = server_settings.service_token.get_secret_value()
    if (
        not configured
        or not x_service_token
        or not secrets.compare_digest(x_service_token, configured)
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid service token")


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    """Return the authenticated user's id, or raise 401.

    Returns the raw ``user_id`` (uuid str). Routes that also need the token itself read it
    from the request directly; most only need the id.
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")

    token = credentials.credentials
    client = await get_anon_client()
    try:
        resp = await client.auth.get_user(token)
    except Exception as exc:  # supabase raises on an invalid/expired token
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token") from exc

    if resp is None or resp.user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    return resp.user.id


async def get_current_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    """Return the raw bearer token (for ops that must act in the user's session, e.g. logout)."""
    if credentials is None or not credentials.credentials:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    return credentials.credentials
