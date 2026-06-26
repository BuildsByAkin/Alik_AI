"""Profile business logic: read, update, and photo upload (with pre-upload validation).

Reads/writes use the service client scoped by ``user_id``; the caller is already
authenticated by ``deps.get_current_user`` before any of this runs.
"""

from __future__ import annotations

from fastapi import HTTPException, status

from ..models import PhotoResponse, ProfileResponse, ProfileUpdate
from ..supabase_client import get_service_client

PHOTO_BUCKET = "profile-photos"
ALLOWED_PHOTO_TYPES = {"image/jpeg", "image/png"}
MAX_PHOTO_BYTES = 5 * 1024 * 1024  # 5 MB


async def get_profile(user_id: str) -> ProfileResponse:
    service = await get_service_client()
    try:
        resp = await service.table("profiles").select("*").eq("id", user_id).single().execute()
    except Exception as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Profile not found") from exc

    if not resp.data:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Profile not found")
    return ProfileResponse(**resp.data)


async def update_profile(user_id: str, update: ProfileUpdate) -> ProfileResponse:
    # Only name/city are editable; drop unset fields so we never null a column by accident.
    changes = update.model_dump(exclude_none=True)
    if not changes:
        # Nothing to change — just return the current profile.
        return await get_profile(user_id)

    service = await get_service_client()
    try:
        resp = await service.table("profiles").update(changes).eq("id", user_id).execute()
    except Exception as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Could not update profile") from exc

    if not resp.data:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Profile not found")
    return ProfileResponse(**resp.data[0])


async def upload_photo(user_id: str, content_type: str | None, data: bytes) -> PhotoResponse:
    """Validate type + size BEFORE sending to Supabase, then upsert ``{user_id}.jpg``."""
    if content_type not in ALLOWED_PHOTO_TYPES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Photo must be image/jpeg or image/png",
        )
    if len(data) > MAX_PHOTO_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            "Photo must be 5MB or smaller",
        )

    path = f"{user_id}.jpg"  # one stable file/URL per user; re-upload overwrites.
    service = await get_service_client()
    try:
        await service.storage.from_(PHOTO_BUCKET).upload(
            path,
            data,
            {"content-type": content_type, "upsert": "true"},
        )
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Photo upload failed") from exc

    public_url = service.storage.from_(PHOTO_BUCKET).get_public_url(path)

    try:
        await (
            service.table("profiles").update({"photo_url": public_url}).eq("id", user_id).execute()
        )
    except Exception as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Could not save photo URL on profile"
        ) from exc

    return PhotoResponse(photo_url=public_url)
