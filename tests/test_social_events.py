"""Phase 8 brain foundation: the matchmaking write-back (record/get social events + erasure)
and the rendezvous opener directives (what the companion asks, kept anonymous)."""

from __future__ import annotations

from alik.companion import Companion
from alik.models import CheckinType, SocialEvent, SocialEventKind
from tests.conftest import InMemoryMemory


def _event(user_id: str, kind: SocialEventKind, summary: str, **kw) -> SocialEvent:
    return SocialEvent(user_id=user_id, kind=kind, summary=summary, source="connections", **kw)


async def test_record_and_read_back_newest_first():
    mem = InMemoryMemory()
    await mem.record_social_event(
        _event("u1", SocialEventKind.PEOPLE_INTRODUCED, "introduced to someone who loves pottery",
               counterpart_ref="c1")
    )
    await mem.record_social_event(
        _event("u1", SocialEventKind.PEOPLE_ACCEPTED, "was open to meeting the pottery person",
               counterpart_ref="c1")
    )
    events = await mem.get_recent_social_events("u1")
    assert [e.kind for e in events] == [
        SocialEventKind.PEOPLE_ACCEPTED,
        SocialEventKind.PEOPLE_INTRODUCED,
    ]  # newest first
    assert events[0].counterpart_ref == "c1"


async def test_limit_and_user_scoping():
    mem = InMemoryMemory()
    for i in range(5):
        await mem.record_social_event(_event("u1", SocialEventKind.MET, f"met #{i}"))
    await mem.record_social_event(_event("u2", SocialEventKind.MET, "other user's meet"))
    assert len(await mem.get_recent_social_events("u1", limit=3)) == 3
    assert len(await mem.get_recent_social_events("u2")) == 1  # scoped per user


async def test_delete_erases_social_events():
    mem = InMemoryMemory()
    await mem.record_social_event(_event("u1", SocialEventKind.MEET_SET, "a meet is set"))
    await mem.delete("u1")
    assert await mem.get_recent_social_events("u1") == []  # legal erasure requirement


# --- rendezvous opener directives (what the AI asks, anonymized) --------------


def test_rendezvous_pref_directive_asks_rough_where_when_anonymously():
    d = Companion._opening_directive(CheckinType.RENDEZVOUS_PREF, "the pottery person, this week")
    assert "WHERE and WHEN" in d
    assert "NEVER an exact address" in d
    assert "never a name" in d and "anonymous" in d


def test_rendezvous_confirm_directive_relays_a_plan():
    d = Companion._opening_directive(CheckinType.RENDEZVOUS_CONFIRM, "Saturday, Uptown")
    assert "Saturday, Uptown" in d
    assert "never a name" in d


def test_rendezvous_followup_asks_how_it_felt_not_whether():
    d = Companion._opening_directive(CheckinType.RENDEZVOUS_FOLLOWUP, "the pottery meet")
    assert "how it FELT" in d
    assert "never whether they actually went" in d
