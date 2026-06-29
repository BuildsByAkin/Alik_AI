"""Auth route tests — signup happy path, age<25 block (403), login happy path."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import FakeClient


def _session(uid: str = "uid-1") -> SimpleNamespace:
    return SimpleNamespace(
        user=SimpleNamespace(id=uid),
        session=SimpleNamespace(access_token="access-tok", refresh_token="refresh-tok"),
    )


def test_signup_happy_path(client: TestClient, fake_anon: FakeClient) -> None:
    async def fake_sign_up(_creds: dict) -> Any:
        return _session("uid-1")

    fake_anon.auth.sign_up = fake_sign_up

    resp = client.post(
        "/auth/signup",
        json={
            "email": "a@b.com",
            "password": "hunter2hunter2",
            "name": "Avery",
            "age": 31,
            "city": "Minneapolis",
            "state": "MN",
        },
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body == {
        "access_token": "access-tok",
        "refresh_token": "refresh-tok",
        "user_id": "uid-1",
    }


def test_signup_under_25_blocked(client: TestClient) -> None:
    resp = client.post(
        "/auth/signup",
        json={
            "email": "kid@b.com",
            "password": "hunter2hunter2",
            "name": "Kit",
            "age": 24,
            "city": "Minneapolis",
            "state": "MN",
        },
    )

    assert resp.status_code == 403
    assert resp.json()["detail"] == "alik is for people 25 and older"


def test_signup_unsupported_state_blocked(client: TestClient) -> None:
    resp = client.post(
        "/auth/signup",
        json={
            "email": "tx@b.com",
            "password": "hunter2hunter2",
            "name": "Tex",
            "age": 31,
            "city": "Austin",
            "state": "TX",
        },
    )

    assert resp.status_code == 403
    assert resp.json()["detail"] == "alik isn't available in your state yet"


def test_login_happy_path(client: TestClient, fake_anon: FakeClient) -> None:
    async def fake_sign_in(_creds: dict) -> Any:
        return _session("uid-42")

    fake_anon.auth.sign_in_with_password = fake_sign_in

    resp = client.post("/auth/login", json={"email": "a@b.com", "password": "hunter2hunter2"})

    assert resp.status_code == 200
    assert resp.json() == {
        "access_token": "access-tok",
        "refresh_token": "refresh-tok",
        "user_id": "uid-42",
    }
