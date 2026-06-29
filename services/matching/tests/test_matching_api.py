"""The matching API end-to-end against the in-memory store + fake brain."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from matching_service.models import Recommendation

MEDICAL_ID = "mindrift-medical-eval-001"
GENERAL_ID = "outlier-general-001"


def test_requires_service_token(client) -> None:
    assert client.get("/match/u1").status_code == 401  # no header


def test_match_picks_specific_job_and_logs_it(client, store, brain, headers) -> None:
    brain.profile = {
        "facts": [{"key": "occupation", "content": "ICU nurse"}],
        "confirmed_traits": [],
    }

    body = client.get("/match/u1", headers=headers).json()

    assert body["job"]["id"] == MEDICAL_ID
    assert body["job"]["partner_url"] == "https://mindrift.ai"
    recs = store._user("u1")
    assert [r.job_id for r in recs] == [MEDICAL_ID]
    assert recs[0].outcome is None  # open thread


def test_match_falls_back_when_unknown(client, headers) -> None:
    body = client.get("/match/u2", headers=headers).json()
    assert body["job"]["id"] == GENERAL_ID


def test_open_thread_blocks_second_match(client, headers) -> None:
    assert client.get("/match/u3", headers=headers).json() is not None
    assert client.get("/match/u3", headers=headers).json() is None  # open thread blocks


def test_delivery_and_open_recommendation_flow(client, store, headers) -> None:
    client.get("/match/u4", headers=headers)
    rec_id = store._user("u4")[0].id

    open_rec = client.get("/users/u4/open-recommendation", headers=headers).json()
    assert open_rec["recommendation_id"] == rec_id
    assert open_rec["partner_url"]  # the fallback job has a URL

    assert client.post(f"/recommendations/{rec_id}/delivered", headers=headers).status_code == 204
    assert store._user("u4")[0].delivered_at is not None
    # Once delivered, it's no longer the "open undelivered" recommendation.
    assert client.get("/users/u4/open-recommendation", headers=headers).json() is None


def test_followup_due_and_sent(client, store, headers) -> None:
    # Seed a delivered recommendation whose follow-up window has already passed.
    past = datetime.now(UTC) - timedelta(days=1)
    store._recs.append(
        Recommendation(
            user_id="u5",
            job_id=MEDICAL_ID,
            recommended_at=past - timedelta(days=3),
            delivered_at=past - timedelta(days=3),
            follow_up_after=past,
        )
    )
    rec_id = store._user("u5")[0].id

    due = client.get("/users/u5/followup-due", headers=headers).json()
    assert due["recommendation_id"] == rec_id
    assert due["title"] == "Evaluate AI medical answers"

    assert (
        client.post(f"/recommendations/{rec_id}/followup-sent", headers=headers).status_code == 204
    )
    # No longer due once the follow-up has been sent.
    assert client.get("/users/u5/followup-due", headers=headers).json() is None


def test_outcome_loved_it_sets_job_active(client, store, headers) -> None:
    client.get("/match/u6", headers=headers)
    rec_id = store._user("u6")[0].id

    resp = client.post(
        f"/recommendations/{rec_id}/outcome",
        headers=headers,
        json={"user_id": "u6", "outcome": "loved_it"},
    )
    assert resp.status_code == 204
    assert store._user("u6")[0].outcome.value == "loved_it"
    assert client.get("/users/u6/job-active", headers=headers).json() == {"active": True}


def test_outcome_rejects_unknown_value(client, store, headers) -> None:
    client.get("/match/u7", headers=headers)
    rec_id = store._user("u7")[0].id
    resp = client.post(
        f"/recommendations/{rec_id}/outcome",
        headers=headers,
        json={"user_id": "u7", "outcome": "banana"},
    )
    assert resp.status_code == 422


def test_delete_user_erases(client, store, headers) -> None:
    client.get("/match/u8", headers=headers)
    assert store._user("u8")
    assert client.delete("/users/u8", headers=headers).status_code == 204
    assert store._user("u8") == []
