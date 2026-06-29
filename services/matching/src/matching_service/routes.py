"""HTTP surface. Every route is service-to-service (token-gated) except /health.

The brain drives this service: it asks for a match or a due follow-up (and delivers the
opener through the companion), reads the open/pending thread to wire up the conversation,
and posts the classified outcome back. Selection + lifecycle live in ``selection``/``store``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel

from matching_service.deps import verify_service_token
from matching_service.models import JobOutcome
from matching_service.selection import followup_outcome_side_effect, select_match
from matching_service.store import Store

router = APIRouter(dependencies=[Depends(verify_service_token)])


class OutcomeBody(BaseModel):
    user_id: str
    outcome: JobOutcome  # pydantic rejects an unknown value with 422


def _job_by_id(request: Request) -> dict:
    return {j.id: j for j in request.app.state.catalog}


@router.get("/match/{user_id}")
async def match(user_id: str, request: Request) -> dict | None:
    """Pick + log the next recommendation for the user (reads the brain Profile API)."""
    store: Store = request.app.state.store
    profile = await request.app.state.brain_client.get_profile(user_id)
    result = await select_match(
        store, profile, request.app.state.catalog, user_id, request.app.state.settings
    )
    if result is None:
        return None
    rec_id, job = result
    return {
        "recommendation_id": rec_id,
        "job": {
            "id": job.id,
            "title": job.title,
            "partner": job.partner,
            "partner_url": job.partner_url,
            "pay_range": job.pay_range,
        },
    }


@router.get("/users/{user_id}/followup-due")
async def followup_due(user_id: str, request: Request) -> dict | None:
    store: Store = request.app.state.store
    rec = await store.due_followup(user_id)
    if rec is None:
        return None
    job = _job_by_id(request).get(rec.job_id)
    return {
        "recommendation_id": rec.id,
        "title": job.title if job is not None else "that opportunity",
        "partner": job.partner if job is not None else "",
    }


@router.get("/users/{user_id}/open-recommendation")
async def open_recommendation(user_id: str, request: Request) -> dict | None:
    """The open, not-yet-delivered recommendation — for the companion's delivery setup."""
    store: Store = request.app.state.store
    rec = await store.open_undelivered(user_id)
    if rec is None:
        return None
    job = _job_by_id(request).get(rec.job_id)
    return {
        "recommendation_id": rec.id,
        "partner_url": job.partner_url if job is not None else None,
    }


@router.get("/users/{user_id}/pending-followup")
async def pending_followup(user_id: str, request: Request) -> dict | None:
    store: Store = request.app.state.store
    rec = await store.pending_followup(user_id)
    return {"recommendation_id": rec.id} if rec is not None else None


@router.post("/recommendations/{rec_id}/delivered", status_code=status.HTTP_204_NO_CONTENT)
async def mark_delivered(rec_id: str, request: Request) -> None:
    await request.app.state.store.mark_delivered(rec_id)


@router.post("/recommendations/{rec_id}/followup-sent", status_code=status.HTTP_204_NO_CONTENT)
async def mark_followup_sent(rec_id: str, request: Request) -> None:
    await request.app.state.store.mark_followup_sent(rec_id)


@router.post("/recommendations/{rec_id}/outcome", status_code=status.HTTP_204_NO_CONTENT)
async def set_outcome(rec_id: str, body: OutcomeBody, request: Request) -> None:
    """Record the outcome; if the user tried + liked it, flip their engagement flag on."""
    store: Store = request.app.state.store
    await store.set_outcome(rec_id, body.outcome)
    if followup_outcome_side_effect(body.outcome):
        await store.set_job_active(body.user_id, True)


@router.get("/users/{user_id}/job-active")
async def job_active(user_id: str, request: Request) -> dict:
    return {"active": await request.app.state.store.get_job_active(user_id)}


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(user_id: str, request: Request) -> None:
    """Erase this service's data for the user (cross-service account deletion)."""
    await request.app.state.store.delete_user(user_id)
