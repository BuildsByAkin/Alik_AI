"""The ONLY module that imports the ``supabase`` SDK.

Everything else in the service goes through ``get_anon_client`` / ``get_service_client``.
Two clients exist on purpose:

- **anon** — carries the project's anon (public) key; used for user-context auth ops
  (signup, login, refresh, token validation via ``auth.get_user``).
- **service** — carries the service-role key; used for admin ops that must bypass RLS:
  inserting the profile row at signup, hard-deleting the auth user at erasure, and
  writing/removing the storage object.

Both are the async client (``AsyncClient``) so the service stays async throughout.
Clients are created once on first use and cached for the process lifetime.
"""

from __future__ import annotations

from supabase import AsyncClient, acreate_client

from .config import settings

_anon_client: AsyncClient | None = None
_service_client: AsyncClient | None = None


async def get_anon_client() -> AsyncClient:
    """Async Supabase client authed with the anon (public) key."""
    global _anon_client
    if _anon_client is None:
        _anon_client = await acreate_client(settings.url, settings.anon_key.get_secret_value())
    return _anon_client


async def get_service_client() -> AsyncClient:
    """Async Supabase client authed with the service-role key (bypasses RLS — admin only)."""
    global _service_client
    if _service_client is None:
        _service_client = await acreate_client(
            settings.url, settings.service_key.get_secret_value()
        )
    return _service_client


def reset_clients() -> None:
    """Drop cached clients (used by tests to inject fakes / force re-creation)."""
    global _anon_client, _service_client
    _anon_client = None
    _service_client = None
