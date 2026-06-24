"""Async extraction: mine an ended session's transcript into graph nodes.

Runs as a background job after ``end_session`` returns. Depends only on the
``LLMClient`` protocol (a cheap model) and ``GraphMemory`` — no infra imports —
so it's testable with a fake LLM and an in-memory graph double.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from alik.llm import LLMClient
from alik.memory.graph import GraphMemory
from alik.models import ExtractionResult, MemoryRecord
from alik.prompt import EXTRACTION_SYSTEM, parse_extraction, transcript_for_extraction

logger = logging.getLogger("alik.extraction")


class Extractor:
    def __init__(self, *, llm: LLMClient, memory: GraphMemory) -> None:
        self._llm = llm
        self._memory = memory

    async def run(
        self, user_id: str, session_id: str, transcript: Sequence[MemoryRecord]
    ) -> ExtractionResult:
        """Extract facts/signals/commitments from a transcript and write them.

        Never raises: a failed extraction is logged and yields an empty result so
        it can't crash the fire-and-forget background task.
        """
        empty = ExtractionResult([], [], [])
        if not transcript:
            return empty
        # Feed back currently-open commitments so the model reuses their keys for a
        # re-stated intent (prevents slug drift / per-session commitment pile-up).
        # Degrades to [] when the graph is down — extraction still runs.
        open_commitments = await self._memory.get_open_commitments(user_id)
        try:
            raw = await self._llm.complete(
                system=EXTRACTION_SYSTEM,
                messages=transcript_for_extraction(transcript, open_commitments),
            )
        except Exception:
            logger.exception("extraction LLM call failed (user=%s session=%s)", user_id, session_id)
            return empty

        result = parse_extraction(raw, user_id=user_id, session_id=session_id)
        nodes = [*result.facts, *result.signals]
        if nodes:
            await self._memory.write_nodes(nodes)
        if result.commitments:  # Phase 5: commitments are their own CommitmentNode type
            await self._memory.write_commitments(result.commitments)
        return result
