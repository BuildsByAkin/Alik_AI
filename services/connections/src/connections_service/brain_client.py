"""Client for the companion brain's Profile API — the ONLY way this service reads a user.

Two failure postures on purpose:
- ``fetch_profile`` returns **None on transport/5xx failure** (distinct from a successful but
  empty profile). Ingestion uses this so a brain outage never destroys/destales a snapshot.
- ``get_profile`` degrades to an all-empty profile (used by scoring reads in Parts 3-4, where
  "nothing to match on" is the right fallback).
"""

from __future__ import annotations

import logging

import httpx

from connections_service.models import GroupCheckin, MatchCheckin

logger = logging.getLogger("connections.brain_client")

_EMPTY = {"identity": None, "facts": [], "confirmed_traits": [], "dimensions": []}


class BrainClient:
    def __init__(self, *, base_url: str, service_token: str, timeout: float = 5.0) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"X-Service-Token": service_token}
        self._client = httpx.AsyncClient(timeout=timeout)

    async def fetch_profile(self, user_id: str) -> dict | None:
        """The assembled profile, or **None on failure** (so ingestion can keep the last
        snapshot instead of overwriting it with an empty one)."""
        url = f"{self._base}/users/{user_id}/profile"
        try:
            resp = await self._client.get(url, headers=self._headers)
            resp.raise_for_status()
        except Exception:
            logger.warning("profile fetch failed for %s", user_id, exc_info=True)
            return None
        body = resp.json()
        return {
            "identity": body.get("identity"),
            "facts": body.get("facts", []),
            "confirmed_traits": body.get("confirmed_traits", []),
            "dimensions": body.get("dimensions", []),
        }

    async def get_profile(self, user_id: str) -> dict:
        """Like ``fetch_profile`` but degrades to an all-empty profile on failure."""
        return await self.fetch_profile(user_id) or dict(_EMPTY)

    async def queue_checkin(self, user_id: str, checkin: MatchCheckin | GroupCheckin) -> str | None:
        """Queue a people-match (1:1 or group) opener in the brain. Returns the PendingCheckin id,
        or None on failure (the caller then keeps no state and retries next pass)."""
        url = f"{self._base}/users/{user_id}/checkins"
        try:
            resp = await self._client.post(url, headers=self._headers, json=checkin.to_payload())
            resp.raise_for_status()
        except Exception:
            logger.warning("queue_checkin failed for %s", user_id, exc_info=True)
            return None
        body = resp.json()
        return body.get("checkin_id") if isinstance(body, dict) else None

    async def aclose(self) -> None:
        await self._client.aclose()
