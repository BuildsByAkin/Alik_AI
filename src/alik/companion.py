"""The companion brain. Modality-independent: text in, text out.

Depends only on the ``Memory`` interface and the ``LLMClient`` protocol, both
injected — the seam for swapping infrastructure, model, or (later) voice I/O.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from alik import profile
from alik.extraction import Extractor
from alik.llm import LLMClient
from alik.memory.base import Memory
from alik.models import (
    CheckinType,
    CommitmentNode,
    GraphNode,
    InferredTrait,
    JobOutcome,
    MemoryRecord,
    MemoryTier,
    NodeType,
    ProvenanceRecord,
    TraitStatus,
)
from alik.prompt import (
    COMMITMENT_RESOLVE_SYSTEM,
    JOB_OUTCOME_CLASSIFY_SYSTEM,
    REFLECT_BACK_SYSTEM,
    REFLECT_PROFILE_CONFIRM_SYSTEM,
    RESPONSE_CLASSIFY_SYSTEM,
    SUMMARY_SYSTEM,
    build_classify_request,
    build_job_outcome_request,
    build_profile_confirm_request,
    build_reflect_back_request,
    build_resolve_request,
    build_system_prompt,
    parse_classification,
    parse_job_outcome,
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
        profile_confirm_min_confidence: float = 0.6,
        profile_confirm_min_observations: int = 2,
        profile_behavior_min_confidence: float = 0.75,
        profile_confirm_confidence_bump: float = 0.1,
        matching_client=None,
        connections_client=None,
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
        # Living-profile soft-confirm tuning (from Settings).
        self._pd_confirm_min_confidence = profile_confirm_min_confidence
        self._pd_confirm_min_observations = profile_confirm_min_observations
        self._pd_behavior_min_confidence = profile_behavior_min_confidence
        self._pd_confirm_bump = profile_confirm_confidence_bump
        # Per-session reflect-back state (in-process; conscious carve-out — see CLAUDE.md).
        # session_id -> trait_id we surfaced and are awaiting a classification for.
        self._rb_pending: dict[str, str] = {}
        # session_id -> dimension name we soft-confirmed and are awaiting a reply for.
        self._pd_pending: dict[str, str] = {}
        # sessions where a gentle check (reflect-back OR profile-confirm) already fired —
        # at most one per session so it never feels like an interview.
        self._rb_done: set[str] = set()
        # Phase 5 proactive-opener state (same in-process carve-out as reflect-back).
        self._checkin_opened: set[str] = set()  # sessions that already delivered an opener
        self._checkin_commitment: dict[str, CommitmentNode] = {}  # session -> commitment to resolve
        self._checkin_grace: dict[str, int] = {}  # session -> remaining "unclear" turns
        # Job matching lives in its own microservice now; this client is the delivery seam
        # (None → job nudges are simply never delivered, graceful).
        self._matching = matching_client
        # Job-checkin state (same in-process carve-out). session -> dict with
        # {"type": "recommendation"|"followup", "url": str|None, "rec_id": str|None}.
        self._job_checkin: dict[str, dict] = {}
        # People-matching delivery (Part 5): introduce a candidate the connections service
        # surfaced; capture the yes/no and post it back. None → never delivered (graceful).
        self._connections = connections_client
        self._match_checkin: dict[str, str] = {}  # session_id -> candidate_id awaiting a reply

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

        # If we gently checked a profile dimension last turn, this is their answer to it.
        pd_pending = self._pd_pending.pop(session_id, None)
        if pd_pending is not None:
            await self._handle_profile_confirm_response(
                user_id, session_id, pd_pending, user_message
            )

        # If a job recommendation/follow-up is open, this message is their reply to it.
        job_directive: str | None = None
        if session_id in self._job_checkin:
            job_directive = await self._handle_job_response(user_id, session_id, user_message)

        # If a people-match introduction is open, this message is their yes/no to it.
        match_directive: str | None = None
        if session_id in self._match_checkin:
            match_directive = await self._handle_match_response(user_id, session_id, user_message)

        ctx = await self._memory.retrieve(user_id, session_id, episode_limit=self._episode_limit)
        system = build_system_prompt(
            self._persona,
            ctx.episodes,
            ctx.facts,
            ctx.commitments,
            reflection=ctx.reflection,
            traits=ctx.traits,
            behavior_directives=self._behavior_directives(ctx.dimensions),
        )
        if job_directive:
            system += f"\n\n{job_directive}"
        if match_directive:
            system += f"\n\n{match_directive}"

        # One gentle check per session: prefer a reflect-back question, else a profile
        # soft-confirm. Both share the cadence cooldown so it never feels like an interview.
        question = await self._maybe_reflect_back(user_id, session_id, ctx.working)
        if not question:
            question = await self._maybe_profile_confirm(user_id, session_id, ctx.working)
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

    # --- Living profile: behavior adjustment + soft-confirm -------------------
    #
    # Behavior directives quietly shape HOW the companion shows up (structure, sensory,
    # predictability) — never said aloud, never a label. Soft-confirm is the trait
    # reflect-back's sibling for behavioral DIMENSIONS: when one is confident enough we
    # gently check it in conversation; the reply confirms or corrects it. Profile methods
    # are on the base Memory ABC (Postgres), so unlike traits they need no duck-typing.

    def _behavior_directives(self, dimensions) -> list[str]:
        return profile.behavior_directives(
            dimensions, behavior_min_confidence=self._pd_behavior_min_confidence
        )

    async def _maybe_profile_confirm(
        self, user_id: str, session_id: str, working: list[MemoryRecord]
    ) -> str | None:
        """Pick and phrase one gentle profile soft-confirm question, or return None.

        Same gates as reflect-back: not already used this session, enough completed
        turns, and the shared cadence cooldown is clear. Targets a confident-enough
        UNCONFIRMED dimension not yet surfaced this session.
        """
        if session_id in self._rb_done:
            return None
        completed_turns = sum(1 for t in working if t.role == "assistant")
        if completed_turns < self._rb_min_turn:
            return None
        if not await self._memory.reflect_back_ready(user_id):
            return None
        try:
            dim = await self._memory.get_dimension_to_confirm(
                user_id,
                session_id,
                min_confidence=self._pd_confirm_min_confidence,
                min_observations=self._pd_confirm_min_observations,
            )
        except Exception:
            logger.exception("profile soft-confirm: get_dimension_to_confirm failed")
            return None
        if dim is None:
            return None

        question = (
            await self._llm.complete(
                system=REFLECT_PROFILE_CONFIRM_SYSTEM,
                messages=build_profile_confirm_request(dim),
            )
        ).strip()
        if not question:
            return None

        await self._memory.mark_dimension_surfaced(user_id, dim.dimension, session_id)
        self._pd_pending[session_id] = dim.dimension
        self._rb_done.add(session_id)  # one gentle check per session, shared with reflect-back
        await self._memory.set_reflect_back_cooldown(user_id, self._rb_cooldown_sessions)
        return question

    async def _handle_profile_confirm_response(
        self, user_id: str, session_id: str, dimension: str, user_message: str
    ) -> None:
        """Classify the user's reply to a soft-confirmed dimension: confirm/correct/deflect."""
        try:
            raw = await self._llm.complete(
                system=RESPONSE_CLASSIFY_SYSTEM, messages=build_classify_request(user_message)
            )
        except Exception:
            logger.exception("profile soft-confirm: classification call failed")
            return
        classification, _ = parse_classification(raw)
        if classification == "confirm":
            await self._memory.confirm_dimension(
                user_id, dimension, confidence_bump=self._pd_confirm_bump, session_id=session_id
            )
        elif classification == "correct":
            await self._memory.correct_dimension(user_id, dimension, session_id=session_id)
        # deflect: leave it unconfirmed (the cadence cooldown spaces out any re-ask).

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
        directive = self._opening_directive(checkin.checkin_type, checkin.message_hint)
        system = build_system_prompt(
            self._persona,
            ctx.episodes,
            ctx.facts,
            ctx.commitments,
            reflection=ctx.reflection,
            traits=ctx.traits,
            behavior_directives=self._behavior_directives(ctx.dimensions),
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
        # Set up job-checkin state so the user's next turn is handled (matching service only).
        if self._matching is not None:
            if checkin.checkin_type is CheckinType.JOB_RECOMMENDATION:
                await self._setup_job_recommendation(user_id, session_id, checkin.message_hint)
            elif checkin.checkin_type is CheckinType.JOB_FOLLOWUP:
                await self._setup_job_followup(user_id, session_id)
        # Arm the people-match reply capture so the next turn's yes/no is posted back.
        if checkin.checkin_type is CheckinType.PEOPLE_MATCH and self._connections is not None:
            candidate_id = (checkin.payload or {}).get("candidate_id")
            if candidate_id:
                self._match_checkin[session_id] = candidate_id
        return opener

    # --- Job recommendation delivery + lifecycle (via the matching microservice) ----
    # Selection/logging/outcomes live in the matching service; the companion only delivers
    # the opener, shares the link on a "yes", and classifies the follow-up reply.

    @staticmethod
    def _opening_directive(checkin_type: CheckinType, hint: str) -> str:
        """Choose the opener brief for a queued check-in — warmer for job/people types."""
        if checkin_type is CheckinType.PEOPLE_MATCH:
            return (
                "Open this conversation yourself — warm and natural, like a good friend "
                "casually mentioning someone you think they'd genuinely enjoy meeting. This is "
                "NOT a dating app and NOT a 'match': never use the word 'match', and never "
                "mention an app, a system, an algorithm, or that anything was computed. In one "
                "or two sentences, bring the other person up grounded in what they share, using "
                f'this private note as the heart of it: "{hint}". End by gently seeing whether '
                "they'd be open to meeting them — light and no-pressure, not a formal question."
            )
        if checkin_type is CheckinType.JOB_RECOMMENDATION:
            return (
                "Open this conversation yourself, warmly, like a friend who just spotted an "
                "opportunity for them — never like an ad. In one or two natural sentences, "
                "mention it based on what you know about them, include the pay range, and then "
                'offer to share the link by ending with: "Want me to send you the link?" Work '
                f'from this private note to yourself: "{hint}". Never mention that this was '
                "scheduled or automated."
            )
        if checkin_type is CheckinType.JOB_FOLLOWUP:
            return (
                "Open this conversation yourself — warm and brief — and ask how that opportunity "
                "went for them: how they FEEL about it, not just whether they did it. Work from "
                f'this private note to yourself: "{hint}". Never mention that this was scheduled '
                "or automated."
            )
        return (
            "Open this conversation yourself — warm and brief — in the spirit of this "
            f'private note to yourself: "{hint}". Do not mention that this was scheduled '
            "or automated, and ask how they are FEELING about it, never whether they did it."
        )

    async def _setup_job_recommendation(self, user_id: str, session_id: str, hint: str) -> None:
        """Mark the open recommendation delivered (so the 3-day follow-up can fire) and stash
        the link so a 'yes' next turn can share it. URL comes from the matching service (the
        hint is the fallback source)."""
        open_rec = await self._matching.open_recommendation(user_id)
        if open_rec is not None:
            await self._matching.mark_delivered(open_rec["recommendation_id"])
        self._job_checkin[session_id] = {
            "type": "recommendation",
            "url": (open_rec or {}).get("partner_url") or self._extract_url(hint),
            "rec_id": open_rec["recommendation_id"] if open_rec is not None else None,
        }

    async def _setup_job_followup(self, user_id: str, session_id: str) -> None:
        rec = await self._matching.pending_followup(user_id)
        self._job_checkin[session_id] = {
            "type": "followup",
            "url": None,
            "rec_id": rec["recommendation_id"] if rec is not None else None,
        }

    @staticmethod
    def _extract_url(text: str) -> str | None:
        match = re.search(r"https?://\S+", text)
        return match.group(0).rstrip('.,;:")') if match else None

    @staticmethod
    def _is_affirmative(message: str) -> bool:
        """Lightweight yes/no read for the link offer (a binary — no model call needed)."""
        m = message.lower()
        negatives = ("no", "nope", "nah", "not now", "later", "skip", "don't", "do not")
        if any(n in m for n in negatives):
            return False
        positives = ("yes", "yeah", "yep", "sure", "please", "ok", "okay", "send", "link", "go")
        return any(p in m for p in positives)

    async def _handle_job_response(
        self, user_id: str, session_id: str, user_message: str
    ) -> str | None:
        """Handle the user's reply to an open job recommendation or follow-up.

        Returns an optional one-turn system directive (share the link / ask why), or None.
        Always clears the per-session job state — these are single-shot exchanges.
        """
        state = self._job_checkin.pop(session_id, None)
        if state is None:
            return None

        if state["type"] == "recommendation":
            # 'yes' → share the link; 'no'/ignore → drop quietly (already logged).
            if state.get("url") and self._is_affirmative(user_message):
                return (
                    "The person said yes to the link. Share it warmly and naturally in your "
                    f"reply: {state['url']}"
                )
            return None

        # follow-up: classify the outcome and apply side effects.
        try:
            raw = await self._llm.complete(
                system=JOB_OUTCOME_CLASSIFY_SYSTEM,
                messages=build_job_outcome_request(user_message),
            )
        except Exception:
            logger.exception("job follow-up: outcome classify failed")
            return None
        outcome = parse_job_outcome(raw)
        if outcome is None:
            return None  # leave the thread open; don't guess
        rec_id = state.get("rec_id")
        if rec_id is not None:
            # The matching service records the outcome and flips job_active for liked/loved.
            await self._matching.post_outcome(user_id, rec_id, str(outcome))
        await self._apply_job_outcome(user_id, session_id, outcome)
        if outcome is JobOutcome.NOT_TRIED:
            return (
                "They haven't tried it yet. In one warm, natural sentence, gently ask what's "
                "been holding them back — no pressure."
            )
        return None

    async def _apply_job_outcome(self, user_id: str, session_id: str, outcome: JobOutcome) -> None:
        """Feed the classified outcome back to the BRAIN's pattern layer as an EmotionalSignal
        (engagement state itself lives in the matching service)."""
        if outcome in (JobOutcome.TRIED_LIKED, JobOutcome.LOVED_IT):
            await self._write_job_signal(
                user_id,
                session_id,
                "follow_through",
                "Followed through on paid work alik suggested and it went well.",
            )
        elif outcome is JobOutcome.TRIED_DISLIKED:
            await self._write_job_signal(
                user_id,
                session_id,
                "job_category_disliked",
                "Tried paid work alik suggested and did not enjoy that kind of work.",
            )

    async def _write_job_signal(
        self, user_id: str, session_id: str, key: str, content: str
    ) -> None:
        """Append an EmotionalSignal the nightly detect() can fold in (graph-only; duck-typed)."""
        if not hasattr(self._memory, "write_nodes"):
            return
        await self._memory.write_nodes(
            [
                GraphNode(
                    user_id=user_id,
                    type=NodeType.EMOTIONAL_SIGNAL,
                    key=key,
                    content=content,
                    valid_from=datetime.now(UTC),
                    source_session_id=session_id,
                )
            ]
        )

    # --- People-match introduction reply (via the connections microservice) ----------

    async def _handle_match_response(
        self, user_id: str, session_id: str, user_message: str
    ) -> str | None:
        """A light yes/no read of the user's reply to an introduction, posted back to
        connections. Single-shot — always clears the per-session state."""
        candidate_id = self._match_checkin.pop(session_id, None)
        if candidate_id is None or self._connections is None:
            return None
        accepted = self._is_affirmative(user_message)
        await self._connections.post_match_response(user_id, candidate_id, accepted)
        if accepted:
            return (
                "They're open to meeting them. Warmly let them know you'll help make the "
                "introduction happen — no pressure, no logistics yet."
            )
        return None

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
