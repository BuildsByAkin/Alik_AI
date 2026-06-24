"""The companion brain. Modality-independent: text in, text out.

Depends only on the ``Memory`` interface and the ``LLMClient`` protocol, both
injected — the seam for swapping infrastructure, model, or (later) voice I/O.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from alik.extraction import Extractor
from alik.llm import LLMClient
from alik.memory.base import Memory
from alik.models import (
    CommitmentNode,
    GraphNode,
    InferredTrait,
    MemoryRecord,
    MemoryTier,
    NodeType,
    ProvenanceRecord,
    TraitStatus,
)
from alik.prompt import (
    COMMITMENT_RESOLVE_SYSTEM,
    REFLECT_BACK_SYSTEM,
    RESPONSE_CLASSIFY_SYSTEM,
    SUMMARY_SYSTEM,
    build_classify_request,
    build_reflect_back_request,
    build_resolve_request,
    build_system_prompt,
    parse_classification,
    parse_resolution,
    to_messages,
    transcript_for_summary,
)

logger = logging.getLogger("alik.companion")


class Companion:
    def __init__(
        self,
        *,
        memory: Memory,
        llm: LLMClient,
        persona: str,
        episode_limit: int,
        extractor: Extractor | None = None,
        reflect_back_min_turn: int = 3,
        reflect_back_min_confidence: float = 0.65,
        reflect_back_confidence_bump: float = 0.1,
        corrected_trait_confidence: float = 0.7,
        reflect_back_cooldown_sessions: int = 3,
    ) -> None:
        self._memory = memory
        self._llm = llm
        self._persona = persona
        self._episode_limit = episode_limit  # sourced from Settings.episode_retrieve_limit
        self._extractor = extractor
        self._tasks: set[asyncio.Task] = set()  # in-flight background extractions
        # Phase 4 reflect-back tuning (never hardcoded; from Settings).
        self._rb_min_turn = reflect_back_min_turn
        self._rb_min_confidence = reflect_back_min_confidence
        self._rb_bump = reflect_back_confidence_bump
        self._corrected_confidence = corrected_trait_confidence
        self._rb_cooldown_sessions = reflect_back_cooldown_sessions
        # Per-session reflect-back state (in-process; conscious carve-out — see CLAUDE.md).
        # session_id -> trait_id we surfaced and are awaiting a classification for.
        self._rb_pending: dict[str, str] = {}
        # sessions where reflect-back already fired (surface at most once per session).
        self._rb_done: set[str] = set()
        # Phase 5 proactive-opener state (same in-process carve-out as reflect-back).
        self._checkin_opened: set[str] = set()  # sessions that already delivered an opener
        self._checkin_commitment: dict[str, CommitmentNode] = {}  # session -> commitment to resolve
        self._checkin_grace: dict[str, int] = {}  # session -> remaining "unclear" turns

    async def respond(self, user_id: str, session_id: str, user_message: str) -> AsyncIterator[str]:
        """Stream a reply, appending both turns to the live buffer."""
        await self._memory.write(
            MemoryRecord(
                user_id=user_id,
                session_id=session_id,
                tier=MemoryTier.WORKING,
                role="user",
                content=user_message,
                created_at=datetime.now(UTC),
            )
        )

        # If a proactive check-in is open, this message may resolve the commitment.
        commitment = self._checkin_commitment.get(session_id)
        if commitment is not None:
            await self._handle_checkin_response(user_id, session_id, commitment, user_message)

        # If we surfaced a trait last turn, this user message is their answer to it.
        pending = self._rb_pending.pop(session_id, None)
        if pending is not None:
            await self._handle_reflect_back_response(user_id, session_id, pending, user_message)

        ctx = await self._memory.retrieve(user_id, session_id, episode_limit=self._episode_limit)
        system = build_system_prompt(
            self._persona,
            ctx.episodes,
            ctx.facts,
            ctx.commitments,
            reflection=ctx.reflection,
            traits=ctx.traits,
        )

        # Reflect-back: at most once per session, never in the first 3 completed turns.
        question = await self._maybe_reflect_back(user_id, session_id, ctx.working)
        if question:
            system += (
                "\n\nSomewhere natural in your reply, gently weave in this one question "
                f'and nothing more pointed: "{question}" Ask it once, softly — never '
                "interrogate, never make it feel like an interview."
            )

        messages = to_messages(ctx.working)

        chunks: list[str] = []
        async for delta in self._llm.stream_reply(system=system, messages=messages):
            chunks.append(delta)
            yield delta

        await self._memory.write(
            MemoryRecord(
                user_id=user_id,
                session_id=session_id,
                tier=MemoryTier.WORKING,
                role="assistant",
                content="".join(chunks),
                created_at=datetime.now(UTC),
            )
        )

    # --- Phase 4: reflect-back ------------------------------------------------
    #
    # Trait methods live on GraphMemory (a Memory), not the base ABC — traits are
    # graph-only. We duck-type the capability so a plain base Memory still works
    # (reflect-back simply never fires), mirroring the optional extractor.

    def _trait_memory(self):
        return self._memory if hasattr(self._memory, "get_trait_for_reflect") else None

    async def _maybe_reflect_back(
        self, user_id: str, session_id: str, working: list[MemoryRecord]
    ) -> str | None:
        """Pick and phrase one gentle reflect-back question, or return None.

        Gated: trait-capable memory, not already fired this session, and at least
        ``reflect_back_min_turn`` completed (assistant) turns in the buffer.
        """
        mem = self._trait_memory()
        if mem is None or session_id in self._rb_done:
            return None
        completed_turns = sum(1 for t in working if t.role == "assistant")
        if completed_turns < self._rb_min_turn:
            return None
        # Cadence cooldown: don't ask a reflect-back every session (feels like an
        # interview). After one fires, skip the next N sessions (durable, cross-process).
        if not await self._memory.reflect_back_ready(user_id):
            return None
        try:
            trait = await mem.get_trait_for_reflect(
                user_id, session_id, min_confidence=self._rb_min_confidence
            )
        except Exception:
            logger.exception("reflect-back: get_trait_for_reflect failed")
            return None
        if trait is None:
            return None

        question = (
            await self._llm.complete(
                system=REFLECT_BACK_SYSTEM, messages=build_reflect_back_request(trait)
            )
        ).strip()
        if not question:
            return None

        # Mark surfaced (graph + in-process) so we never repeat it this session, even
        # across a process restart, and await the user's answer next turn.
        await mem.mark_trait_surfaced(trait.id, session_id)
        self._rb_pending[session_id] = trait.id
        self._rb_done.add(session_id)
        # Start the cadence cooldown: skip the next N sessions.
        await self._memory.set_reflect_back_cooldown(user_id, self._rb_cooldown_sessions)
        return question

    async def _handle_reflect_back_response(
        self, user_id: str, session_id: str, trait_id: str, user_message: str
    ) -> None:
        """Classify the user's answer to a surfaced trait and apply confirm/correct/deflect."""
        mem = self._trait_memory()
        if mem is None:
            return
        try:
            raw = await self._llm.complete(
                system=RESPONSE_CLASSIFY_SYSTEM, messages=build_classify_request(user_message)
            )
        except Exception:
            logger.exception("reflect-back: classification call failed")
            return
        classification, correction_text = parse_classification(raw)

        if classification == "confirm":
            await mem.confirm_trait(trait_id, confidence_bump=self._rb_bump)
        elif classification == "correct":
            await mem.correct_trait(trait_id)
            await self._open_corrected_trait(mem, trait_id, session_id, correction_text)
        # deflect: leave the trait unchanged.

    async def _open_corrected_trait(
        self, mem, trait_id: str, session_id: str, correction_text: str | None
    ) -> None:
        """Open a new CONFIRMED trait from the user's correction, inheriting provenance."""
        if not correction_text:
            return
        old = await mem.get_trait_by_id(trait_id)
        if old is None:
            return
        now = datetime.now(UTC)
        new = InferredTrait(
            user_id=old.user_id,
            key=old.key,  # same pattern, corrected → supersede semantics by key
            content=correction_text,
            confidence=self._corrected_confidence,
            valid_from=now,
            status_updated_at=now,
            status=TraitStatus.CONFIRMED,
            provenance=ProvenanceRecord(
                episode_ids=list(old.provenance.episode_ids),
                signal_ids=list(old.provenance.signal_ids),
            ),
            source_session_id=session_id,
        )
        await mem.write_traits([new])

    # --- Phase 5: proactive opener + commitment resolution --------------------
    #
    # Check-in queue methods (get_pending_checkin/mark_checkin_delivered) are on the
    # base Memory ABC (Postgres). Commitment resolution + the follow-through signal are
    # graph-only, so they are duck-typed like the trait methods.

    def _commitment_memory(self):
        return self._memory if hasattr(self._memory, "resolve_commitment") else None

    async def open_session(self, user_id: str, session_id: str) -> str | None:
        """If a proactive check-in is queued, deliver it as the session's opener.

        Returns the opener text (also written as the first assistant turn so the
        session has context), or None for an ordinary session. Fires at most once per
        session. The user-facing opener is generated by the companion model from the
        queued one-line hint.
        """
        if session_id in self._checkin_opened:
            return None
        checkin = await self._memory.get_pending_checkin(user_id)
        if checkin is None:
            return None
        await self._memory.mark_checkin_delivered(checkin.id)
        self._checkin_opened.add(session_id)

        ctx = await self._memory.retrieve(user_id, session_id, episode_limit=self._episode_limit)
        directive = (
            "Open this conversation yourself — warm and brief — in the spirit of this "
            f'private note to yourself: "{checkin.message_hint}". Do not mention that '
            "this was scheduled or automated, and ask how they are FEELING about it, "
            "never whether they did it."
        )
        system = build_system_prompt(
            self._persona,
            ctx.episodes,
            ctx.facts,
            ctx.commitments,
            reflection=ctx.reflection,
            traits=ctx.traits,
            opening_directive=directive,
        )
        opener = (
            await self._llm.complete(
                system=system, messages=[{"role": "user", "content": "(Begin the conversation.)"}]
            )
        ).strip() or checkin.message_hint

        await self._memory.write(
            MemoryRecord(
                user_id=user_id,
                session_id=session_id,
                tier=MemoryTier.WORKING,
                role="assistant",
                content=opener,
                created_at=datetime.now(UTC),
            )
        )
        # Track the linked commitment so the user's next turn can resolve it.
        if checkin.commitment_id is not None:
            match = next((c for c in ctx.commitments if c.id == checkin.commitment_id), None)
            if match is not None:
                self._checkin_commitment[session_id] = match
                self._checkin_grace[session_id] = 1  # first turn + one "unclear" grace turn
        return opener

    def _clear_checkin(self, session_id: str) -> None:
        self._checkin_commitment.pop(session_id, None)
        self._checkin_grace.pop(session_id, None)

    async def _handle_checkin_response(
        self, user_id: str, session_id: str, commitment: CommitmentNode, user_message: str
    ) -> None:
        """Classify the user's reply to a proactive check-in and resolve if it's clear."""
        mem = self._commitment_memory()
        if mem is None:
            self._clear_checkin(session_id)
            return
        try:
            raw = await self._llm.complete(
                system=COMMITMENT_RESOLVE_SYSTEM,
                messages=build_resolve_request(user_message, commitment),
            )
        except Exception:
            logger.exception("check-in: resolution classify failed")
            return  # keep state; try again next turn within grace
        resolution, user_words = parse_resolution(raw)

        if resolution in ("kept", "dropped"):
            kept = resolution == "kept"
            await mem.resolve_commitment(commitment.id, kept=kept)
            await self._write_follow_through_signal(
                user_id, session_id, commitment, kept, user_words
            )
            self._clear_checkin(session_id)
        elif self._checkin_grace.get(session_id, 0) > 0:
            self._checkin_grace[session_id] -= 1  # unclear — grant one more turn, no nagging
        else:
            self._clear_checkin(session_id)  # still unclear — let it go

    async def _write_follow_through_signal(
        self,
        user_id: str,
        session_id: str,
        commitment: CommitmentNode,
        kept: bool,
        user_words: str | None,
    ) -> None:
        """Feed the resolution back to the pattern layer as an EmotionalSignal.

        Decision (CLAUDE.md): rather than mutate a follow-through InferredTrait in real
        time (brittle), we write a signal that the nightly detect() folds into the
        follow-through trait — provenance to the commitment that produced it.
        """
        verb = "Followed through on" if kept else "Did not follow through on"
        content = f"{verb} a commitment they made: {commitment.content}"
        if user_words:
            content += f" — they said: {user_words}"
        await self._memory.write_nodes(
            [
                GraphNode(
                    user_id=user_id,
                    type=NodeType.EMOTIONAL_SIGNAL,
                    key="follow_through",
                    content=content,
                    valid_from=datetime.now(UTC),
                    source_session_id=session_id,
                )
            ]
        )

    async def end_session(self, user_id: str, session_id: str) -> str | None:
        """Summarize the session into episodic memory, then clear the buffer."""
        ctx = await self._memory.retrieve(user_id, session_id, episode_limit=self._episode_limit)
        if not ctx.working:
            return None

        summary = (
            await self._llm.complete(
                system=SUMMARY_SYSTEM, messages=transcript_for_summary(ctx.working)
            )
        ).strip()

        if summary:
            await self._memory.write(
                MemoryRecord(
                    user_id=user_id,
                    session_id=session_id,
                    tier=MemoryTier.EPISODIC,
                    content=summary,
                    created_at=datetime.now(UTC),
                )
            )

        # Count this session toward clearing the reflect-back cooldown — but NOT the
        # session in which it fired (so a fired reflect-back skips the full next N).
        if session_id not in self._rb_done:
            await self._memory.decrement_reflect_back_cooldown(user_id)

        # Capture the transcript BEFORE invalidating; extraction runs on this copy
        # so clearing the buffer can't race it.
        transcript = list(ctx.working)
        await self._memory.invalidate(user_id, session_id)
        self._schedule_extraction(user_id, session_id, transcript)
        return summary or None

    def _schedule_extraction(
        self, user_id: str, session_id: str, transcript: list[MemoryRecord]
    ) -> None:
        """Fire-and-forget the extraction job so end_session returns immediately."""
        if self._extractor is None:
            return
        task = asyncio.create_task(self._extractor.run(user_id, session_id, transcript))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Await all in-flight extraction tasks (callers without a long-lived loop)."""
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
