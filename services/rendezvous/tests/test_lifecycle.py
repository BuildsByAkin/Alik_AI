"""The meet state machine: advance-pass queuing (idempotent, brain-outage safe) and the reply
handlers (pref -> confirm -> followup) advancing state + writing anonymized social memories."""

from __future__ import annotations

from rendezvous_service.lifecycle import advance_pass, apply_confirm, apply_followup, apply_pref
from rendezvous_service.models import Meet, MeetStatus
from rendezvous_service.store import InMemoryStore
from tests.conftest import FakeBrain


def _meet() -> Meet:
    return Meet(user_a="A", user_b="B", desc_a="someone who loves pottery", desc_b="a trail runner")


async def _saved(store, meet):
    await store.save_meet(meet)
    return await store.get_meet(meet.id)


async def test_advance_asks_both_sides_for_prefs_then_stops():
    store, brain = InMemoryStore(), FakeBrain()
    await store.save_meet(_meet())
    await advance_pass(store, brain)
    assert {c[0] for c in brain.checkins} == {"A", "B"}
    assert all(c[1] == "rendezvous_pref" for c in brain.checkins)
    # idempotent: a second pass asks nobody again (asked-flags set)
    brain.checkins.clear()
    await advance_pass(store, brain)
    assert brain.checkins == []


async def test_brain_outage_does_not_set_asked_flag():
    store, brain = InMemoryStore(), FakeBrain()
    brain.checkin_id = None  # queue fails
    await store.save_meet(_meet())
    await advance_pass(store, brain)
    meet = (await store.get_active_meets())[0]
    assert meet.pref_asked_a is False and meet.pref_asked_b is False  # will retry next pass


async def test_both_prefs_move_to_confirming_and_build_plan():
    store = InMemoryStore()
    meet = await _saved(store, _meet())
    meet = await apply_pref(store, meet, "A", "weekends, Uptown")
    assert meet.status is MeetStatus.COORDINATING  # only one side in
    meet = await apply_pref(store, meet, "B", "Saturday mornings")
    assert meet.status is MeetStatus.CONFIRMING
    assert "Uptown" in meet.plan and "Saturday" in meet.plan


async def test_confirm_both_yes_sets_meet_and_writes_memory_to_both():
    store, brain = InMemoryStore(), FakeBrain()
    meet = await _saved(store, _meet())
    meet = await apply_pref(store, meet, "A", "Uptown")
    meet = await apply_pref(store, meet, "B", "Saturday")
    meet = await apply_confirm(store, brain, meet, "A", True)
    assert meet.status is MeetStatus.CONFIRMING  # waiting on B
    meet = await apply_confirm(store, brain, meet, "B", True)
    assert meet.status is MeetStatus.CONFIRMED
    # a 'meet_set' memory landed for BOTH, anonymized, with the counterpart ref
    kinds = [(u, kind, ref) for (u, kind, _s, ref) in brain.social_events]
    assert ("A", "meet_set", "B") in kinds and ("B", "meet_set", "A") in kinds
    assert all(
        "someone who loves pottery" in s or "a trail runner" in s
        for (_u, k, s, _r) in brain.social_events
        if k == "meet_set"
    )


async def test_a_decline_cancels_the_meet():
    store, brain = InMemoryStore(), FakeBrain()
    meet = await _saved(store, _meet())
    meet = await apply_pref(store, meet, "A", "Uptown")
    meet = await apply_pref(store, meet, "B", "Saturday")
    meet = await apply_confirm(store, brain, meet, "A", False)
    assert meet.status is MeetStatus.CANCELLED
    assert not any(k == "meet_set" for (_u, k, _s, _r) in brain.social_events)


async def test_followup_writes_met_memory_and_completes_when_both_in():
    store, brain = InMemoryStore(), FakeBrain()
    meet = Meet(
        user_a="A", user_b="B", desc_a="a potter", desc_b="a runner", status=MeetStatus.CONFIRMED
    )
    meet = await _saved(store, meet)
    meet = await apply_followup(store, brain, meet, "A", True)
    assert meet.status is MeetStatus.CONFIRMED  # waiting on B
    meet = await apply_followup(store, brain, meet, "B", False)
    assert meet.status is MeetStatus.FOLLOWED_UP
    met = [(u, s) for (u, k, s, _r) in brain.social_events if k == "met"]
    assert any(u == "A" and "went well" in s for (u, s) in met)
    assert any(u == "B" and "okay" in s for (u, s) in met)


async def test_stranger_reply_is_ignored():
    store = InMemoryStore()
    meet = await _saved(store, _meet())
    meet = await apply_pref(store, meet, "STRANGER", "whenever")
    assert meet.pref_a is None and meet.pref_b is None
