"""HTTP surface: token gating, the create/reply endpoints wired to the lifecycle, and the
right-to-erasure delete (a meet erased for one side is gone entirely)."""

from __future__ import annotations


def test_health_is_open(client) -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_routes_require_service_token(client) -> None:
    assert client.post("/meets", json={}).status_code == 401
    assert client.delete("/users/u1").status_code == 401


def _create(client, headers) -> str:
    resp = client.post(
        "/meets",
        headers=headers,
        json={"user_a": "A", "user_b": "B", "desc_a": "a potter", "desc_b": "a runner"},
    )
    assert resp.status_code == 201
    return resp.json()["meet_id"]


def test_full_loop_over_http(client, headers, brain, store) -> None:
    meet_id = _create(client, headers)
    for uid, text in (("A", "Uptown"), ("B", "Saturday")):
        r = client.post(
            "/meets/pref",
            headers=headers,
            json={"meet_id": meet_id, "user_id": uid, "text": text},
        )
        assert r.status_code == 204
    for uid in ("A", "B"):
        r = client.post(
            "/meets/confirm",
            headers=headers,
            json={"meet_id": meet_id, "user_id": uid, "accepted": True},
        )
        assert r.status_code == 204
    # both confirmed -> a meet_set memory was written for each side
    assert sum(1 for (_u, k, _s, _r) in brain.social_events if k == "meet_set") == 2
    for uid, felt in (("A", True), ("B", True)):
        r = client.post(
            "/meets/followup",
            headers=headers,
            json={"meet_id": meet_id, "user_id": uid, "felt_positive": felt},
        )
        assert r.status_code == 204
    assert sum(1 for (_u, k, _s, _r) in brain.social_events if k == "met") == 2


def test_reply_to_unknown_meet_is_404(client, headers) -> None:
    r = client.post(
        "/meets/pref",
        headers=headers,
        json={"meet_id": "nope", "user_id": "A", "text": "x"},
    )
    assert r.status_code == 404


def test_delete_user_erases_the_meet(client, headers) -> None:
    meet_id = _create(client, headers)
    assert client.delete("/users/A", headers=headers).status_code == 204
    # the meet (which referenced both A and B) is gone entirely — nothing about A survives
    r = client.post(
        "/meets/pref",
        headers=headers,
        json={"meet_id": meet_id, "user_id": "B", "text": "x"},
    )
    assert r.status_code == 404
