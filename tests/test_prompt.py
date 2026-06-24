"""Pure prompt-building tests. No infrastructure required."""

from __future__ import annotations

from alik.models import MemoryRecord, MemoryTier
from alik.prompt import build_system_prompt, to_messages, transcript_for_summary


def _episode(content: str) -> MemoryRecord:
    return MemoryRecord(user_id="u", session_id="s", tier=MemoryTier.EPISODIC, content=content)


def _turn(role: str, content: str) -> MemoryRecord:
    return MemoryRecord(
        user_id="u", session_id="s", tier=MemoryTier.WORKING, role=role, content=content
    )


def test_system_prompt_without_episodes_is_just_persona():
    assert build_system_prompt("PERSONA", []) == "PERSONA"


def test_system_prompt_injects_episodes():
    prompt = build_system_prompt("PERSONA", [_episode("User has a dog named Rufus.")])
    assert "PERSONA" in prompt
    assert "Rufus" in prompt


def test_to_messages_maps_roles_in_order():
    messages = to_messages([_turn("user", "hi"), _turn("assistant", "hello")])
    assert messages == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]


def test_transcript_for_summary_is_single_user_turn():
    transcript = transcript_for_summary([_turn("user", "my dog is Rufus")])
    assert len(transcript) == 1
    assert transcript[0]["role"] == "user"
    assert "Rufus" in transcript[0]["content"]
