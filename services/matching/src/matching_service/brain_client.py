"""Client for the companion brain's Profile API — the input to scoring.

The matching service is a pure consumer of the assembled living profile: given a user_id,
it reads ``{facts, confirmed_traits}`` from the brain and scores the catalog against them.
Authenticated with the shared service token. Returns empty lists on any failure so a brain
hiccup degrades to "no specific match / fallback only" rather than erroring the request.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("matching.brain_client")


class BrainClient:
    def __init__(self, *, base_url: str, service_token: str, timeout: float = 5.0) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"X-Service-Token": service_token}
        self._client = httpx.AsyncClient(timeout=timeout)

    async def get_profile(self, user_id: str) -> dict:
        """Return ``{"facts": [...], "confirmed_traits": [...]}`` for scoring.

        Degrades to empty lists on any error (the user simply gets the fallback job).
        """
        url = f"{self._base}/users/{user_id}/profile"
        try:
            resp = await self._client.get(url, headers=self._headers)
            resp.raise_for_status()
        except Exception:
            logger.warning("profile fetch failed for %s", user_id, exc_info=True)
            return {"facts": [], "confirmed_traits": []}
        body = resp.json()
        return {
            "facts": body.get("facts", []),
            "confirmed_traits": body.get("confirmed_traits", []),
        }

    async def aclose(self) -> None:
        await self._client.aclose()
