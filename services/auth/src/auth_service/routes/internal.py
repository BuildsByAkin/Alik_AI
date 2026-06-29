"""Internal (service-to-service) routes — NOT user-facing.

Guarded by the shared ``X-Service-Token`` rather than a user's Bearer token, so the
companion brain can read identity by ``user_id`` (for the living profile) and erase a
user by id (cross-service account deletion). These mirror the user-facing ``/profile/me``
and ``/auth/account`` endpoints, keyed by id instead of the caller's session.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from ..deps import verify_service_token
from ..models import ProfileResponse
from ..services import auth_svc, profile_svc

router = APIRouter(
    prefix="/internal", tags=["internal"], dependencies=[Depends(verify_service_token)]
)


@router.get("/profiles/{user_id}", response_model=ProfileResponse)
async def internal_get_profile(user_id: str) -> ProfileResponse:
    """Identity row for the brain's assembled living profile."""
    return await profile_svc.get_profile(user_id)


@router.get("/users")
async def internal_list_users(state: str) -> list[str]:
    """User ids whose profile is in ``state`` (2-letter code). The roster the connections
    (people-matching) service ingests from — auth owns who exists and where."""
    return await profile_svc.list_user_ids_by_state(state.strip().upper())


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def internal_delete_user(user_id: str) -> None:
    """Hard-erase the user (photo + profile row + auth user) — same loud erasure as
    ``DELETE /auth/account``, invoked by the brain's cross-service delete coordinator."""
    await auth_svc.delete_account(user_id)
