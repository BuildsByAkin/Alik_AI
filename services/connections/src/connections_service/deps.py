"""Shared dependency: gate every endpoint with the mesh service token.

The connections service is entirely service-to-service (the brain calls it); there are no
user-facing routes, so every request must carry the shared ``X-Service-Token``. Fails
closed — if no token is configured, all requests are rejected.
"""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, status

from connections_service.config import settings


async def verify_service_token(x_service_token: str | None = Header(default=None)) -> None:
    configured = settings.service_token.get_secret_value()
    if (
        not configured
        or not x_service_token
        or not secrets.compare_digest(x_service_token, configured)
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid service token")
