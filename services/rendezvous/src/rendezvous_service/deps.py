"""Shared dependency: gate every route with the mesh service token (fails closed)."""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, status

from rendezvous_service.config import settings


async def verify_service_token(x_service_token: str | None = Header(default=None)) -> None:
    configured = settings.service_token.get_secret_value()
    if (
        not configured
        or not x_service_token
        or not secrets.compare_digest(x_service_token, configured)
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid service token")
