"""HTTP client to the connections (people-matching) microservice.

The companion uses this to close the match loop — when a user responds to an introduction,
it posts the yes/no back so the connections service records accepted/skipped. The brain's
account-deletion also fans out here. Authenticated with the shared mesh service token.

Failure posture: ``post_match_response`` is best-effort (a failure leaves the match_state as
``shown``; it won't be re-surfaced, and the response is simply lost — logged). ``delete_user``
is LOUD (raises), because it's part of right-to-erasure.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("alik.connections_client")


class ConnectionsClient:
    def __init__(self, *, base_url: str, service_token: str, timeout: float = 5.0) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"X-Service-Token": service_token}
        self._client = httpx.AsyncClient(timeout=timeout)

    async def post_match_response(self, user_id: str, candidate_id: str, accepted: bool) -> None:
        """Report the user's yes/no to an introduction (best-effort)."""
        try:
            resp = await self._client.post(
                f"{self._base}/matches/response",
                headers=self._headers,
                json={"user_id": user_id, "candidate_id": candidate_id, "accepted": accepted},
            )
            resp.raise_for_status()
        except Exception:
            logger.warning(
                "match response post failed for %s->%s", user_id, candidate_id, exc_info=True
            )

    async def delete_user(self, user_id: str) -> None:
        """Hard-erase the user's connections data. Raises on failure (loud erasure). 404 = gone."""
        resp = await self._client.delete(f"{self._base}/users/{user_id}", headers=self._headers)
        if resp.status_code not in (200, 204, 404):
            raise RuntimeError(
                f"connections erasure failed for {user_id}: {resp.status_code} {resp.text}"
            )

    async def aclose(self) -> None:
        await self._client.aclose()
