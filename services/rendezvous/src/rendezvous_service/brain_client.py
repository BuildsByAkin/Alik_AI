"""Client for the companion brain — the only thing rendezvous talks to besides its own DB.

Two write paths, both mesh-token authed:
- ``queue_checkin``: queue a coordination opener (RENDEZVOUS_PREF/CONFIRM/FOLLOWUP) the companion
  delivers next session. Returns the check-in id, or None on failure (retry next advance pass).
- ``record_social_event``: record a durable matchmaking memory ("a meet is set", "met") so the
  companion stays coherent. Best-effort (a lost write is re-attempted or simply skipped).
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("rendezvous.brain_client")


class BrainClient:
    def __init__(self, *, base_url: str, service_token: str, timeout: float = 5.0) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"X-Service-Token": service_token}
        self._client = httpx.AsyncClient(timeout=timeout)

    async def queue_checkin(
        self, user_id: str, checkin_type: str, reason: str, *, meet_id: str
    ) -> str | None:
        url = f"{self._base}/users/{user_id}/checkins"
        try:
            resp = await self._client.post(
                url,
                headers=self._headers,
                json={"type": checkin_type, "reason": reason, "meet_id": meet_id},
            )
            resp.raise_for_status()
        except Exception:
            logger.warning("queue_checkin %s failed for %s", checkin_type, user_id, exc_info=True)
            return None
        body = resp.json()
        return body.get("checkin_id") if isinstance(body, dict) else None

    async def record_social_event(
        self, user_id: str, kind: str, summary: str, *, counterpart_ref: str | None = None
    ) -> bool:
        url = f"{self._base}/users/{user_id}/social-events"
        try:
            resp = await self._client.post(
                url,
                headers=self._headers,
                json={
                    "kind": kind,
                    "summary": summary,
                    "source": "rendezvous",
                    "counterpart_ref": counterpart_ref,
                },
            )
            resp.raise_for_status()
        except Exception:
            logger.warning("record_social_event %s failed for %s", kind, user_id, exc_info=True)
            return False
        return True

    async def aclose(self) -> None:
        await self._client.aclose()
