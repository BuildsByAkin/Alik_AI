"""HTTP surface — all service-to-service (token-gated). ``/health`` lives in ``main``.

- POST /meets            : create a meet (connections calls this when both sides accepted).
- POST /meets/pref       : the companion posts a user's rough where/when.
- POST /meets/confirm    : the companion posts a user's yes/no to the plan.
- POST /meets/followup   : the companion posts how a meet felt.
- DELETE /users/{id}     : right-to-erasure (called by the brain's delete fan-out).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from rendezvous_service.deps import verify_service_token
from rendezvous_service.lifecycle import apply_confirm, apply_followup, apply_pref
from rendezvous_service.models import ConfirmReply, CreateMeet, FollowupReply, Meet, PrefReply
from rendezvous_service.store import Store

router = APIRouter(dependencies=[Depends(verify_service_token)])


async def _load(request: Request, meet_id: str) -> tuple[Store, Meet]:
    store: Store = request.app.state.store
    meet = await store.get_meet(meet_id)
    if meet is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such meet")
    return store, meet


@router.post("/meets", status_code=status.HTTP_201_CREATED)
async def create_meet(body: CreateMeet, request: Request) -> dict:
    store: Store = request.app.state.store
    meet = Meet(user_a=body.user_a, user_b=body.user_b, desc_a=body.desc_a, desc_b=body.desc_b)
    await store.save_meet(meet)
    return {"meet_id": meet.id}


@router.post("/meets/pref", status_code=status.HTTP_204_NO_CONTENT)
async def meet_pref(body: PrefReply, request: Request) -> None:
    store, meet = await _load(request, body.meet_id)
    await apply_pref(store, meet, body.user_id, body.text)


@router.post("/meets/confirm", status_code=status.HTTP_204_NO_CONTENT)
async def meet_confirm(body: ConfirmReply, request: Request) -> None:
    store, meet = await _load(request, body.meet_id)
    await apply_confirm(store, request.app.state.brain_client, meet, body.user_id, body.accepted)


@router.post("/meets/followup", status_code=status.HTTP_204_NO_CONTENT)
async def meet_followup(body: FollowupReply, request: Request) -> None:
    store, meet = await _load(request, body.meet_id)
    await apply_followup(
        store, request.app.state.brain_client, meet, body.user_id, body.felt_positive
    )


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(user_id: str, request: Request) -> None:
    store: Store = request.app.state.store
    await store.delete_user(user_id)
