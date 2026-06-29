"""Auth business logic: signup (+rollback), login, logout, refresh, account erasure.

All Supabase access goes through ``supabase_client`` (the only SDK importer). The anon
client runs user-context auth ops; the service client runs admin ops (profile insert,
hard delete of the auth user, storage removal).
"""

from __future__ import annotations

from fastapi import HTTPException, status

from ..models import SignupRequest, TokenResponse
from ..supabase_client import get_anon_client, get_service_client

MIN_AGE = 25
AGE_BLOCK_MESSAGE = "alik is for people 25 and older"
PHOTO_BUCKET = "profile-photos"

# alik launches one state at a time. The frontend only offers launched states, but we gate
# here too (defense in depth): a state outside this set is rejected. Add a state to go live
# there — no other code change needed. The mobile app separately verifies the user's live
# device location is in the state they picked before calling signup; we store only the state.
LAUNCH_STATES = {"MN"}
STATE_BLOCK_MESSAGE = "alik isn't available in your state yet"


async def signup(req: SignupRequest) -> TokenResponse:
    """Create the auth user + profile row atomically, returning a live session.

    Age is gated here (→ 403). If the profile insert fails after the auth user is created,
    the auth user is rolled back so no orphan auth record survives.
    """
    if req.age < MIN_AGE:
        raise HTTPException(status.HTTP_403_FORBIDDEN, AGE_BLOCK_MESSAGE)

    state = req.state.strip().upper()
    if state not in LAUNCH_STATES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, STATE_BLOCK_MESSAGE)

    anon = await get_anon_client()
    try:
        auth_resp = await anon.auth.sign_up({"email": req.email, "password": req.password})
    except Exception as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _clean(exc)) from exc

    if auth_resp.user is None or auth_resp.session is None:
        # With email confirmation OFF this should not happen; surface clearly if it does.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Signup did not return a session — check that email confirmation is disabled.",
        )

    user_id = auth_resp.user.id
    service = await get_service_client()
    try:
        await (
            service.table("profiles")
            .insert(
                {
                    "id": user_id,
                    "name": req.name,
                    "age": req.age,
                    "city": req.city,
                    "state": state,
                }
            )
            .execute()
        )
    except Exception as exc:
        # Roll back the just-created auth user so we never leave an orphan.
        try:
            await service.auth.admin.delete_user(user_id)
        except Exception:  # noqa: BLE001 — rollback is best-effort; surface the original error.
            pass
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Could not create profile: {_clean(exc)}"
        ) from exc

    return TokenResponse(
        access_token=auth_resp.session.access_token,
        refresh_token=auth_resp.session.refresh_token,
        user_id=user_id,
    )


async def login(email: str, password: str) -> TokenResponse:
    anon = await get_anon_client()
    try:
        resp = await anon.auth.sign_in_with_password({"email": email, "password": password})
    except Exception as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password") from exc

    if resp.session is None or resp.user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    return TokenResponse(
        access_token=resp.session.access_token,
        refresh_token=resp.session.refresh_token,
        user_id=resp.user.id,
    )


async def logout(token: str) -> None:
    """Invalidate the session backing ``token`` (admin sign-out, global scope)."""
    service = await get_service_client()
    try:
        await service.auth.admin.sign_out(token)
    except Exception as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Could not log out") from exc


async def refresh(refresh_token: str) -> TokenResponse:
    anon = await get_anon_client()
    try:
        resp = await anon.auth.refresh_session(refresh_token)
    except Exception as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token") from exc

    if resp.session is None or resp.user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token")
    return TokenResponse(
        access_token=resp.session.access_token,
        refresh_token=resp.session.refresh_token,
        user_id=resp.user.id,
    )


async def delete_account(user_id: str) -> None:
    """Hard-erase everything for the user: photo → profile row → auth user.

    Mirrors the companion brain's ``Memory.delete`` principle — loud, not soft. If a step's
    backing service is unreachable we raise rather than silently half-erase; the ops are
    idempotent, so re-running after the service recovers completes the erasure.
    """
    service = await get_service_client()

    # 1) Storage photo (idempotent: removing a missing object is a no-op).
    try:
        await service.storage.from_(PHOTO_BUCKET).remove([f"{user_id}.jpg"])
    except Exception as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "Could not delete profile photo; erasure aborted"
        ) from exc

    # 2) Profile row.
    try:
        await service.table("profiles").delete().eq("id", user_id).execute()
    except Exception as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "Could not delete profile row; erasure aborted"
        ) from exc

    # 3) Auth user — the legally definitive step.
    try:
        await service.auth.admin.delete_user(user_id)
    except Exception as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "Could not delete auth user; erasure aborted"
        ) from exc


def _clean(exc: Exception) -> str:
    """Best-effort human message from a Supabase/GoTrue error."""
    msg = getattr(exc, "message", None) or str(exc)
    return msg.strip() or "Request failed"
