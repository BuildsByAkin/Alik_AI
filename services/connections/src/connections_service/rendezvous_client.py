"""Client to the rendezvous service — connections calls it once (create a meet) when two people
have MUTUALLY accepted an introduction. Best-effort: a failure just means the meet isn't created
this time (the accept is still recorded); it can be retried. Mesh-token authed.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("connections.rendezvous_client")


class RendezvousClient:
    def __init__(self, *, base_url: str, service_token: str, timeout: float = 5.0) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"X-Service-Token": service_token}
        self._client = httpx.AsyncClient(timeout=timeout)

    async def create_meet(self, user_a: str, user_b: str, desc_a: str, desc_b: str) -> str | None:
        """Create a meet for a mutually-accepted pair. Returns the meet id, or None on failure."""
        try:
            resp = await self._client.post(
                f"{self._base}/meets",
                headers=self._headers,
                json={"user_a": user_a, "user_b": user_b, "desc_a": desc_a, "desc_b": desc_b},
            )
            resp.raise_for_status()
        except Exception:
            logger.warning("create_meet failed for %s+%s", user_a, user_b, exc_info=True)
            return None
        body = resp.json()
        return body.get("meet_id") if isinstance(body, dict) else None

    async def aclose(self) -> None:
        await self._client.aclose()
