"""Pydantic request/response models for the auth + profile API.

Note: ``age`` is a plain ``int`` here on purpose — the ``>= 25`` rule is enforced in the
service layer so it returns **403** (a ``Field(ge=25)`` would yield a 422). The DB
``CHECK (age >= 25)`` is a backstop.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr


class SignupRequest(BaseModel):
    email: EmailStr
    password: str
    name: str
    age: int
    city: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    user_id: str


class ProfileUpdate(BaseModel):
    # age and email are not editable after signup, so they are intentionally absent.
    name: str | None = None
    city: str | None = None


class ProfileResponse(BaseModel):
    id: str
    name: str
    age: int
    city: str
    photo_url: str | None = None
    created_at: datetime
    updated_at: datetime


class PhotoResponse(BaseModel):
    photo_url: str


class HealthResponse(BaseModel):
    status: str
