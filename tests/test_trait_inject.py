"""build_system_prompt trait injection: confirmed traits in, inferred traits out.

Inferred traits are never stated as fact — they surface only via reflect-back.
Only CONFIRMED traits belong in the system prompt.
"""

from __future__ import annotations

from datetime import UTC, datetime

from alik.models import InferredTrait, ProvenanceRecord, TraitStatus
from alik.prompt import build_system_prompt


def _trait(content: str, status: TraitStatus) -> InferredTrait:
    now = datetime.now(UTC)
    return InferredTrait(
        user_id="u",
        key=content.replace(" ", "_"),
        content=content,
        confidence=0.9,
        valid_from=now,
        status_updated_at=now,
        status=status,
        provenance=ProvenanceRecord(episode_ids=["ep-1"]),
    )


def test_confirmed_trait_appears_inferred_does_not():
    traits = [
        _trait("is most positive on Monday mornings", TraitStatus.CONFIRMED),
        _trait("gets anxious before big decisions", TraitStatus.INFERRED),
    ]
    prompt = build_system_prompt("persona", episodes=[], traits=traits)

    assert "What you know deeply about this person" in prompt
    assert "most positive on Monday mornings" in prompt
    assert "gets anxious before big decisions" not in prompt


def test_no_section_without_confirmed_traits():
    prompt = build_system_prompt("persona", episodes=[], traits=[_trait("x", TraitStatus.INFERRED)])
    assert "What you know deeply about this person" not in prompt
