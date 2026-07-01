"""HTTP client to the rendezvous (meeting-coordination) microservice.

The companion uses this to close the rendezvous loop — when a user replies to a coordination
check-in (their rough where/when, a yes/no to a plan, or how a meet felt), it posts that back
so the rendezvous service can advance the meet. The brain's account-deletion also fans out
here. Authenticated with the shared mesh service token.

Failure posture mirrors ConnectionsClient: the reply posts are best-effort (a lost reply just
means the meet doesn't advance this turn and is retried), while ``delete_user`` is LOUD
(raises) because it is part of right-to-erasure.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("alik.rendezvous_client")


class RendezvousClient:
    def __init__(self, *, base_url: str, service_token: str, timeout: float = 5.0) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"X-Service-Token": service_token}
        self._client = httpx.AsyncClient(timeout=timeout)

    async def post_pref(self, meet_id: str, user_id: str, text: str) -> None:
        """The user's rough where/when for a meet (best-effort)."""
        await self._post("/meets/pref", {"meet_id": meet_id, "user_id": user_id, "text": text})

    async def post_confirm(self, meet_id: str, user_id: str, accepted: bool) -> None:
        """The user's yes/no to a proposed rough plan (best-effort)."""
        await self._post(
            "/meets/confirm", {"meet_id": meet_id, "user_id": user_id, "accepted": accepted}
        )

    async def post_followup(self, meet_id: str, user_id: str, felt_positive: bool) -> None:
        """How the meet felt for the user — positive or not (best-effort)."""
        await self._post(
            "/meets/followup",
            {"meet_id": meet_id, "user_id": user_id, "felt_positive": felt_positive},
        )

    async def _post(self, path: str, body: dict) -> None:
        try:
            resp = await self._client.post(f"{self._base}{path}", headers=self._headers, json=body)
            resp.raise_for_status()
        except Exception:
            logger.warning("rendezvous %s failed for %s", path, body.get("user_id"), exc_info=True)

    async def delete_user(self, user_id: str) -> None:
        """Hard-erase the user's rendezvous data. Raises on failure (loud erasure). 404 = gone."""
        resp = await self._client.delete(f"{self._base}/users/{user_id}", headers=self._headers)
        if resp.status_code not in (200, 204, 404):
            raise RuntimeError(
                f"rendezvous erasure failed for {user_id}: {resp.status_code} {resp.text}"
            )

    async def aclose(self) -> None:
        await self._client.aclose()
