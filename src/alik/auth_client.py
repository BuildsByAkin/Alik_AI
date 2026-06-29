"""HTTP client to the auth microservice's internal (service-to-service) endpoints.

The brain uses this for two things: pulling a user's identity into the assembled
living profile, and coordinating cross-service account erasure. The two have different
failure postures on purpose:

- ``get_profile`` degrades gracefully — any failure returns ``None`` and the profile is
  simply assembled without identity (the auth service being down must not break a read).
- ``delete_user`` is LOUD — it raises on failure, mirroring ``Memory.delete``: erasure is
  a legal requirement and must never silently half-complete. The ops are idempotent, so
  re-running after the service recovers finishes the job.

Auth is reached with a shared ``X-Service-Token`` (the /internal endpoints are not
user-facing). This is the only module in the brain that talks to the auth service.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("alik.auth_client")


class AuthClient:
    def __init__(self, *, base_url: str, service_token: str, timeout: float = 5.0) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"X-Service-Token": service_token}
        self._client = httpx.AsyncClient(timeout=timeout)

    async def get_profile(self, user_id: str) -> dict | None:
        """The user's identity row from auth (name, age, city, photo_url), or None.

        None on a missing profile (404) or any error — identity is best-effort context.
        """
        url = f"{self._base}/internal/profiles/{user_id}"
        try:
            resp = await self._client.get(url, headers=self._headers)
        except Exception:
            logger.warning("auth identity fetch failed for %s", user_id, exc_info=True)
            return None
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code != 404:
            logger.warning("auth identity fetch for %s returned %s", user_id, resp.status_code)
        return None

    async def delete_user(self, user_id: str) -> None:
        """Hard-erase the user in the auth service. Raises on failure (loud erasure).

        404 is treated as success — the user is already gone (idempotent).
        """
        url = f"{self._base}/internal/users/{user_id}"
        resp = await self._client.delete(url, headers=self._headers)
        if resp.status_code not in (200, 204, 404):
            raise RuntimeError(f"auth erasure failed for {user_id}: {resp.status_code} {resp.text}")

    async def aclose(self) -> None:
        await self._client.aclose()
