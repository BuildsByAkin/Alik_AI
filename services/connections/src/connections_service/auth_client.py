"""Client for the auth service's internal roster endpoint.

Auth owns who exists and where (the ``profiles`` table + ``state``), so the ingest job pulls
the per-state roster of ``user_id``s from here, then fetches each rich profile from the brain.
A small bounded retry guards the whole-run gate; on persistent failure it returns ``[]`` so a
cycle simply ingests nobody (and tries again next schedule) rather than crashing.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

logger = logging.getLogger("connections.auth_client")


class AuthClient:
    def __init__(self, *, base_url: str, service_token: str, timeout: float = 5.0) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"X-Service-Token": service_token}
        self._client = httpx.AsyncClient(timeout=timeout)

    async def list_user_ids(self, state: str, *, retries: int = 2) -> list[str]:
        """User ids whose profile is in ``state``. ``[]`` on persistent failure (never raises)."""
        url = f"{self._base}/internal/users"
        for attempt in range(retries + 1):
            try:
                resp = await self._client.get(url, params={"state": state}, headers=self._headers)
                resp.raise_for_status()
                body = resp.json()
                return [str(uid) for uid in body] if isinstance(body, list) else []
            except Exception:
                if attempt == retries:
                    logger.warning("auth roster fetch failed for state=%s", state, exc_info=True)
                    return []
                await asyncio.sleep(0.5 * (attempt + 1))
        return []

    async def aclose(self) -> None:
        await self._client.aclose()
