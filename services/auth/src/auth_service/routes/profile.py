"""Profile routes: read, update (name/city only), and photo upload."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, UploadFile

from ..deps import get_current_user
from ..models import PhotoResponse, ProfileResponse, ProfileUpdate
from ..services import profile_svc

router = APIRouter(prefix="/profile", tags=["profile"])


@router.get("/me", response_model=ProfileResponse)
async def get_me(user_id: str = Depends(get_current_user)) -> ProfileResponse:
    return await profile_svc.get_profile(user_id)


@router.patch("/me", response_model=ProfileResponse)
async def update_me(
    update: ProfileUpdate, user_id: str = Depends(get_current_user)
) -> ProfileResponse:
    return await profile_svc.update_profile(user_id, update)


@router.post("/me/photo", response_model=PhotoResponse)
async def upload_photo(
    photo: UploadFile = File(...), user_id: str = Depends(get_current_user)
) -> PhotoResponse:
    data = await photo.read()
    return await profile_svc.upload_photo(user_id, photo.content_type, data)
