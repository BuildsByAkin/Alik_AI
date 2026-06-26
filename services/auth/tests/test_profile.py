"""Profile route tests — GET /profile/me shape, photo upload file-type validation."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import FakeClient

PROFILE_ROW = {
    "id": "uid-1",
    "name": "Avery",
    "age": 31,
    "city": "Lagos",
    "photo_url": None,
    "created_at": "2026-06-26T10:00:00+00:00",
    "updated_at": "2026-06-26T10:00:00+00:00",
}


def test_get_profile_returns_correct_shape(
    client: TestClient, fake_service: FakeClient, auth_as
) -> None:
    auth_as("uid-1")
    fake_service.set_table_result("profiles", PROFILE_ROW)

    resp = client.get("/profile/me")

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"id", "name", "age", "city", "photo_url", "created_at", "updated_at"}
    assert body["id"] == "uid-1"
    assert body["age"] == 31
    assert body["photo_url"] is None


def test_photo_upload_rejects_bad_file_type(client: TestClient, auth_as) -> None:
    auth_as("uid-1")

    resp = client.post(
        "/profile/me/photo",
        files={"photo": ("evil.txt", b"not an image", "text/plain")},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Photo must be image/jpeg or image/png"
