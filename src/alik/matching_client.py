"""HTTP client to the job-matching microservice.

Job matching lives in its own service now; the brain only DELIVERS recommendations through
the companion and reports outcomes back. This client is that seam.

Failure posture: read/queue calls degrade gracefully (return None / swallow) so a matching
outage never breaks a conversation or the nightly pass — the user just doesn't get a job
nudge that day. ``delete_user`` is LOUD (raises), because it's part of cross-service erasure.
Authenticated with the shared mesh service token.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("alik.matching_client")


class MatchingClient:
    def __init__(self, *, base_url: str, service_token: str, timeout: float = 5.0) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"X-Service-Token": service_token}
        self._client = httpx.AsyncClient(timeout=timeout)

    async def _get(self, path: str) -> dict | None:
        try:
            resp = await self._client.get(f"{self._base}{path}", headers=self._headers)
            resp.raise_for_status()
        except Exception:
            logger.warning("matching GET %s failed", path, exc_info=True)
            return None
        body = resp.json()
        return body if isinstance(body, dict) else None

    async def _post(self, path: str, json: dict | None = None) -> None:
        try:
            resp = await self._client.post(f"{self._base}{path}", headers=self._headers, json=json)
            resp.raise_for_status()
        except Exception:
            logger.warning("matching POST %s failed", path, exc_info=True)

    # --- nightly pass --------------------------------------------------------
    async def match(self, user_id: str) -> dict | None:
        """Pick + log the next recommendation (the service reads the Profile API)."""
        return await self._get(f"/match/{user_id}")

    async def due_followup(self, user_id: str) -> dict | None:
        return await self._get(f"/users/{user_id}/followup-due")

    async def mark_followup_sent(self, rec_id: str) -> None:
        await self._post(f"/recommendations/{rec_id}/followup-sent")

    # --- companion delivery --------------------------------------------------
    async def open_recommendation(self, user_id: str) -> dict | None:
        return await self._get(f"/users/{user_id}/open-recommendation")

    async def mark_delivered(self, rec_id: str) -> None:
        await self._post(f"/recommendations/{rec_id}/delivered")

    async def pending_followup(self, user_id: str) -> dict | None:
        return await self._get(f"/users/{user_id}/pending-followup")

    async def post_outcome(self, user_id: str, rec_id: str, outcome: str) -> None:
        await self._post(
            f"/recommendations/{rec_id}/outcome", json={"user_id": user_id, "outcome": outcome}
        )

    # --- cross-service deletion (loud) ---------------------------------------
    async def delete_user(self, user_id: str) -> None:
        resp = await self._client.delete(f"{self._base}/users/{user_id}", headers=self._headers)
        if resp.status_code not in (200, 204, 404):
            raise RuntimeError(
                f"matching erasure failed for {user_id}: {resp.status_code} {resp.text}"
            )

    async def aclose(self) -> None:
        await self._client.aclose()
