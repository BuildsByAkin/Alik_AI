"""Auth routes: signup, login, logout, refresh, and the right-to-erasure account delete."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from ..deps import get_current_token, get_current_user
from ..models import LoginRequest, RefreshRequest, SignupRequest, TokenResponse
from ..services import auth_svc

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/signup", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def signup(req: SignupRequest) -> TokenResponse:
    return await auth_svc.signup(req)


@router.post("/login", response_model=TokenResponse)
async def login(req: LoginRequest) -> TokenResponse:
    return await auth_svc.login(req.email, req.password)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(token: str = Depends(get_current_token)) -> None:
    await auth_svc.logout(token)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(req: RefreshRequest) -> TokenResponse:
    return await auth_svc.refresh(req.refresh_token)


@router.delete("/account", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(user_id: str = Depends(get_current_user)) -> None:
    """Hard-erase the user everywhere (photo, profile row, auth user). Not a soft delete."""
    await auth_svc.delete_account(user_id)
