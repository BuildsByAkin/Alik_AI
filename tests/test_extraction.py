"""Extraction: fake the LLM, run on a sample transcript, confirm nodes land.

Infra-free: fake extraction model + in-memory graph double.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime

from alik.extraction import Extractor
from alik.models import CommitmentNode, CommitmentStatus, MemoryRecord, MemoryTier, NodeType
from alik.prompt import parse_extraction


class FakeExtractionLLM:
    """Returns a fixed JSON payload from ``complete`` (the extraction call path) and
    records the messages it was handed, so tests can assert what the model saw."""

    def __init__(self, payload: dict) -> None:
        self._payload = json.dumps(payload)
        self.last_messages: Sequence[dict] = []

    async def stream_reply(
        self, *, system: str, messages: Sequence[dict]
    ) -> AsyncIterator[str]:  # pragma: no cover - unused by extraction
        yield ""

    async def complete(self, *, system: str, messages: Sequence[dict]) -> str:
        self.last_messages = messages
        return self._payload


def _turn(user_id: str, role: str, content: str) -> MemoryRecord:
    return MemoryRecord(
        user_id=user_id, session_id="S", tier=MemoryTier.WORKING, role=role, content=content
    )


async def test_extraction_writes_nodes_to_graph(graph_memory_fake, user_id):
    payload = {
        "facts": [{"key": "pet_dog_name", "content": "has a dog named Rufus", "confidence": 0.95}],
        "emotional_signals": [{"key": "mood", "content": "felt anxious about work"}],
        "commitments": [{"key": "gym", "content": "will go to the gym tomorrow"}],
    }
    extractor = Extractor(llm=FakeExtractionLLM(payload), memory=graph_memory_fake)
    transcript = [
        _turn(user_id, "user", "I got a dog named Rufus. Work's been making me anxious."),
        _turn(user_id, "assistant", "Congrats on Rufus."),
        _turn(user_id, "user", "I'll go to the gym tomorrow."),
    ]

    result = await extractor.run(user_id, "S", transcript)

    # The parsed result reflects all three categories...
    assert [f.content for f in result.facts] == ["has a dog named Rufus"]
    assert result.signals[0].type is NodeType.EMOTIONAL_SIGNAL
    assert result.commitments[0].content == "will go to the gym tomorrow"

    # ...and the fact actually landed in the graph as current truth.
    facts = await graph_memory_fake.get_current_facts(user_id)
    assert [f.content for f in facts] == ["has a dog named Rufus"]
    assert facts[0].confidence == 0.95


async def test_open_commitments_fed_back_and_restatement_merges(graph_memory_fake, user_id):
    """Slug-drift fix: extraction feeds the user's open commitments back to the model so a
    re-stated intent reuses the existing key, and write_commitments' same-key soft-dedup
    then merges it into one node instead of piling up a new one per session."""
    # Seed an already-tracked open commitment.
    await graph_memory_fake.write_commitments(
        [
            CommitmentNode(
                user_id=user_id,
                key="other:start_therapy",
                content="will start therapy this week",
                valid_from=datetime.now(UTC),
                status=CommitmentStatus.PENDING,
            )
        ]
    )

    # The model restates the SAME intent, correctly REUSING the fed-back key.
    payload = {
        "facts": [],
        "emotional_signals": [],
        "commitments": [
            {"key": "other:start_therapy", "content": "will start therapy this week, emailed two"}
        ],
    }
    llm = FakeExtractionLLM(payload)
    extractor = Extractor(llm=llm, memory=graph_memory_fake)
    transcript = [_turn(user_id, "user", "I emailed two therapists, starting this week.")]

    await extractor.run(user_id, "S2", transcript)

    # The existing commitment key was fed into the extraction request...
    rendered = "\n".join(m["content"] for m in llm.last_messages)
    assert "Commitments already tracked" in rendered
    assert "other:start_therapy" in rendered

    # ...so the restatement merged into the SAME node (no pile-up); mention_count bumped.
    openc = await graph_memory_fake.get_open_commitments(user_id)
    assert len(openc) == 1
    assert openc[0].key == "other:start_therapy"
    assert openc[0].mention_count == 2


def test_commitment_expected_by_is_timezone_aware(user_id):
    """A model can return expected_by WITHOUT an offset; it must be normalized to a
    TZ-aware datetime so the commitment tick pass can compare it to now(UTC) without
    'can't compare offset-naive and offset-aware datetimes'."""
    raw = json.dumps(
        {
            "facts": [],
            "emotional_signals": [],
            "commitments": [
                {"key": "race", "content": "run a 10k", "expected_by": "2026-07-01T18:00:00"}
            ],
        }
    )
    result = parse_extraction(raw, user_id=user_id, session_id="S")
    assert len(result.commitments) == 1
    expected_by = result.commitments[0].expected_by
    assert expected_by is not None
    assert expected_by.tzinfo is not None  # naive input -> UTC-aware, no crash on compare
