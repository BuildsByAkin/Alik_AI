"""Pure prompt-building. No DB, no network — text in, text out, fully testable."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from importlib import resources

from alik.models import (
    CommitmentNode,
    CommitmentStatus,
    ExtractionResult,
    GraphNode,
    InferredTrait,
    JobOutcome,
    MemoryRecord,
    NodeType,
    ProvenanceRecord,
    TraitStatus,
)

SUMMARY_SYSTEM = (
    "You summarize a chat conversation between a user and their AI companion. "
    "Write a concise third-person summary capturing durable facts the user shared "
    "about themselves — their life, relationships, preferences, plans — anything "
    "worth remembering for future conversations. Omit pleasantries and small talk. "
    "Output only the summary."
)

EXTRACTION_SYSTEM = (
    "You read a conversation between a user and their AI companion and extract "
    "structured knowledge about the USER. Return ONLY a JSON object with three "
    'arrays: "facts", "emotional_signals", and "commitments". Each item is an '
    'object with "key", "content", and "confidence" (0.0-1.0).\n\n'
    "- facts: durable truths about the user (preferences, relationships, habits, "
    'life details). The "key" names what the fact is ABOUT, so a later, '
    "contradicting fact about the same thing reuses the same key.\n"
    "- emotional_signals: point-in-time feelings or mood the user expressed.\n"
    "- commitments: ONLY durable intentions the person means to follow through on over "
    "time and that would be worth gently asking about days later (e.g. 'sign up for the "
    "half marathon', 'call mom this weekend', 'start therapy'). Do NOT extract momentary "
    "in-conversation actions ('take a break now', 'eat lunch', 'go for a walk', 'check "
    "back later'), vague aspirations, or things already done. When unsure, leave it out — "
    "a commitment is something you could follow up on next week. A commitment item MAY "
    'also include "expected_by": an ISO 8601 datetime for WHEN they said they\'d do it, '
    "but ONLY if the user actually stated or clearly implied a time. Omit it otherwise — "
    "never guess a deadline. Commitment keys are FREE-FORM (the canonical-key list below "
    "is for facts, NOT commitments): use a short descriptive other:<slug>. CRITICAL: if "
    "the input lists 'Commitments already tracked' for this person and a commitment you "
    "find is the SAME underlying intent as one of them — even if reworded, or describing a "
    "further step toward the same goal (e.g. 'looked up therapists' then 'emailed two' then "
    "'waiting to hear back' are ALL the one 'start therapy' commitment) — REUSE that tracked "
    "commitment's EXACT key verbatim instead of coining a new one. Only mint a new key for a "
    "genuinely new intent; consistent keys are what collapse restatements.\n\n"
    "key MUST be one of the canonical keys below, or other:<slug> if none "
    "fit. Consistency is what drives deduplication — never invent new keys.\n\n"
    "primary_exercise = physical fitness activity (running, gym, cycling, "
    "swimming, yoga). primary_hobby = leisure interest (photography, cooking, "
    "gaming, reading, music). A person can have both — do not collapse them.\n\n"
    "CANONICAL KEYS:\n\n"
    "Lifestyle & habits:\n"
    "primary_hobby, secondary_hobby, tertiary_hobby, primary_exercise,\n"
    "fitness_level, sleep_pattern, diet_preference, alcohol_preference,\n"
    "gaming_habit, travel_frequency, morning_evening_person, music_taste,\n"
    "movie_tv_taste, book_genre, food_cuisine_preference, sports_team\n\n"
    "Work & ambition:\n"
    "occupation, company, career_stage, work_style, income_level,\n"
    "education_level, ambition_level, side_project, skill_learning,\n"
    "financial_situation\n\n"
    "Life situation:\n"
    "location_city, living_situation, relationship_status, relationship_goal,\n"
    "family_situation, children_status, wants_children, pet, health_concern\n\n"
    "Personality & values:\n"
    "personality_trait, communication_style, social_preference, humor_style,\n"
    "introvert_extrovert, creativity_level, anxiety_level, love_language,\n"
    "political_leaning, religious_belief, values_core, life_goal,\n"
    "current_challenge, stress_source, energy_source, close_friends_count\n\n"
    "Only include what the USER actually conveyed; do not invent. If a category is "
    "empty, return an empty array. Output JSON only, no prose."
)


def load_persona(path: str | None = None) -> str:
    if path:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    return resources.files("alik").joinpath("persona.txt").read_text(encoding="utf-8").strip()


def build_system_prompt(
    persona: str,
    episodes: Sequence[MemoryRecord],
    facts: Sequence[GraphNode] = (),
    commitments: Sequence[CommitmentNode] = (),
    reflection: str | None = None,
    traits: Sequence[InferredTrait] = (),
    opening_directive: str | None = None,
) -> str:
    """Persona plus injected memory of who this person is.

    For established users a Phase 3 reflection replaces the episodic list (leaner
    prompt); new users get episodic summaries. Current graph facts and open
    commitments are always injected alongside whichever narrative source is used.

    Phase 4: only CONFIRMED traits are injected (defensively filtered here). INFERRED
    traits are never stated as fact — they surface only via reflect-back.
    Phase 5: commitments are status-aware (due ones flagged); a proactive
    opening_directive, when present, tells the companion how to open the session.
    """
    sections = [persona]
    if reflection:
        sections.append(f"Your current understanding of this person:\n{reflection}")
    confirmed = [t for t in traits if t.status is TraitStatus.CONFIRMED]
    if confirmed:
        lines = "\n".join(f"- {t.content}" for t in confirmed)
        sections.append(f"What you know deeply about this person:\n{lines}")
    if facts:
        lines = "\n".join(f"- {f.content}" for f in facts)
        sections.append(f"What is currently true about this person:\n{lines}")
    if commitments:
        lines = "\n".join(
            f"- {c.content}" + (" (now due)" if c.status is CommitmentStatus.DUE else "")
            for c in commitments
        )
        sections.append(f"Open commitments this person made:\n{lines}")
    if episodes and not reflection:
        lines = "\n".join(f"- {e.content}" for e in episodes)
        sections.append(f"What you remember from previous conversations:\n{lines}")
    if opening_directive:
        sections.append(opening_directive)
    return "\n\n".join(sections)


def to_messages(working: Sequence[MemoryRecord]) -> list[dict]:
    """Render the live buffer into the Anthropic messages format."""
    return [{"role": turn.role or "user", "content": turn.content} for turn in working]


def transcript_for_summary(working: Sequence[MemoryRecord]) -> list[dict]:
    """Render the session as a single user turn asking for a summary."""
    convo = "\n".join(f"{turn.role}: {turn.content}" for turn in working)
    return [{"role": "user", "content": f"Conversation to summarize:\n\n{convo}"}]


def transcript_for_extraction(
    working: Sequence[MemoryRecord],
    open_commitments: Sequence[CommitmentNode] = (),
) -> list[dict]:
    """Render the session as a single user turn asking for structured extraction.

    The user's currently-open commitments are fed back so the model REUSES their exact
    keys for the same intent rather than coining a fresh slug each session — the same
    slug-drift guard detect() uses for traits. Without it, one intent ('start therapy')
    piled up a new commitment node per session under drifting keys (seek_therapy,
    seeking_therapy, start_therapy, …), defeating write_commitments' same-key soft-dedup.
    """
    convo = "\n".join(f"{turn.role}: {turn.content}" for turn in working)
    content = f"Conversation to analyze:\n\n{convo}"
    if open_commitments:
        tracked = "\n".join(f"[{c.key}] {c.content}" for c in open_commitments)
        content += (
            f"\n\nCommitments already tracked (reuse the EXACT key for the same intent):\n{tracked}"
        )
    return [{"role": "user", "content": content}]


def _coerce_nodes(
    items: object,
    *,
    node_type: NodeType,
    user_id: str,
    session_id: str | None,
    valid_from: datetime,
) -> list[GraphNode]:
    nodes: list[GraphNode] = []
    if not isinstance(items, list):
        return nodes
    for item in items:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content", "")).strip()
        if not content:
            continue
        key = str(item.get("key") or content).strip()
        try:
            confidence = float(item.get("confidence", 1.0))
        except (TypeError, ValueError):
            confidence = 1.0
        nodes.append(
            GraphNode(
                user_id=user_id,
                type=node_type,
                key=key,
                content=content,
                valid_from=valid_from,
                valid_until=None,
                confidence=confidence,
                source_session_id=session_id,
            )
        )
    return nodes


def _coerce_commitments(
    items: object,
    *,
    user_id: str,
    session_id: str | None,
    valid_from: datetime,
) -> list[CommitmentNode]:
    """Build CommitmentNodes (status pending) with an optional parsed expected_by."""
    commitments: list[CommitmentNode] = []
    if not isinstance(items, list):
        return commitments
    for item in items:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content", "")).strip()
        if not content:
            continue
        key = str(item.get("key") or content).strip()
        try:
            confidence = float(item.get("confidence", 1.0))
        except (TypeError, ValueError):
            confidence = 1.0
        expected_by = _parse_iso(item.get("expected_by"))
        commitments.append(
            CommitmentNode(
                user_id=user_id,
                key=key,
                content=content,
                valid_from=valid_from,
                status=CommitmentStatus.PENDING,
                expected_by=expected_by,
                confidence=confidence,
                source_session_id=session_id,
            )
        )
    return commitments


def _parse_iso(value: object) -> datetime | None:
    """Parse a model-supplied ISO datetime, forcing TZ-awareness (naive -> UTC) so
    downstream comparisons against ``datetime.now(UTC)`` never mix naive and aware."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def parse_extraction(
    raw: str,
    *,
    user_id: str,
    session_id: str | None = None,
    valid_from: datetime | None = None,
) -> ExtractionResult:
    """Parse the extraction model's JSON into graph nodes + commitment nodes.

    Tolerant of prose around the JSON object; on any parse failure returns an
    empty result so a malformed extraction never crashes the background job.
    """
    valid_from = valid_from or datetime.now(UTC)
    empty = ExtractionResult([], [], [])
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return empty
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return empty
    if not isinstance(data, dict):
        return empty

    def coerce(field_key: str, node_type: NodeType) -> list[GraphNode]:
        return _coerce_nodes(
            data.get(field_key),
            node_type=node_type,
            user_id=user_id,
            session_id=session_id,
            valid_from=valid_from,
        )

    return ExtractionResult(
        facts=coerce("facts", NodeType.FACT),
        signals=coerce("emotional_signals", NodeType.EMOTIONAL_SIGNAL),
        commitments=_coerce_commitments(
            data.get("commitments"),
            user_id=user_id,
            session_id=session_id,
            valid_from=valid_from,
        ),
    )


# --- Phase 3: sleep pass prompts ---------------------------------------------

SALIENCE_SYSTEM = (
    "You score how SALIENT each episode summary is for long-term memory. An episode "
    "is salient if it is emotionally significant, names something the person mentions "
    "repeatedly, or ties to a commitment or goal. Routine small talk is NOT salient. "
    "You are given a numbered list. Return ONLY a JSON array of objects "
    '{"index": <int>, "score": <0.0-1.0>}, one per episode. No prose.'
)

REFLECTION_SYSTEM = (
    "You write a short reflection (3-5 sentences) capturing what you currently know "
    "about this person: who they are, their emotional patterns, and what they are "
    "working toward. Write in the third person, warm but factual. Use only what you "
    "are given. Output only the reflection."
)


def build_salience_request(episodes: Sequence[MemoryRecord]) -> list[dict]:
    """Render recent episodes as a single numbered scoring request."""
    listing = "\n".join(f"[{i}] {e.content}" for i, e in enumerate(episodes))
    return [{"role": "user", "content": f"Episodes to score:\n\n{listing}"}]


def parse_salience(raw: str, count: int) -> list[float]:
    """Parse the salience model's JSON array into a score list aligned to input order.

    Tolerant of prose and missing entries; anything unparseable scores 0.0 (so a bad
    response simply promotes nothing rather than crashing the sleep pass).
    """
    scores = [0.0] * count
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end <= start:
        return scores
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return scores
    if not isinstance(data, list):
        return scores
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item["index"])
            score = float(item["score"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= idx < count:
            scores[idx] = score
    return scores


def build_reflection_request(
    facts: Sequence[GraphNode],
    commitments: Sequence[GraphNode],
    signals: Sequence[GraphNode],
    promoted_episodes: Sequence[MemoryRecord],
) -> list[dict]:
    """Assemble the current picture of a user for the reflection model."""

    def block(title: str, items: Sequence[str]) -> str:
        if not items:
            return f"{title}: (none)"
        body = "\n".join(f"- {x}" for x in items)
        return f"{title}:\n{body}"

    parts = [
        block("Current facts", [f.content for f in facts]),
        block("Emotional signals", [s.content for s in signals]),
        block("Open commitments", [c.content for c in commitments]),
        block("Notable past episodes", [e.content for e in promoted_episodes]),
    ]
    return [{"role": "user", "content": "\n\n".join(parts)}]


# --- Phase 4: pattern-layer prompts ------------------------------------------

DETECTION_SYSTEM = (
    "You infer durable PATTERNS about a person from their notable past episodes and "
    "emotional signals. Look for: recurring emotional themes, topics that energize or "
    "drain them, follow-through tendencies, social patterns, and timing patterns.\n\n"
    "You are given episodes and signals, each tagged with an id like [ep:<id>] or "
    "[sig:<id>]. Return ONLY a JSON array of objects, each:\n"
    '{"key": <canonical slug>, "content": <one human-readable sentence>, '
    '"confidence": <0.0-1.0>, "provenance_episode_ids": [<ids>], '
    '"provenance_signal_ids": [<ids>]}.\n\n'
    "PROVENANCE IS MANDATORY: every pattern MUST cite the specific episode and/or "
    "signal ids it was inferred from, copying the id EXACTLY as shown (the bare value "
    "inside the tag, without the 'ep:'/'sig:' prefix), using ONLY ids that appear in "
    "the input. At least one id per pattern. Drop any pattern you cannot ground.\n\n"
    "Return AT MOST 5 patterns — only the most SIGNIFICANT and DURABLE ones that define "
    "this person, NOT one-off micro-observations or different phrasings of a single "
    "moment. CONSOLIDATE related observations into one broader pattern rather than "
    "emitting many granular ones (e.g. prefer 'anxious before big decisions' over three "
    "separate traits for three separate decisions). Keep each content to ONE concise "
    "sentence. Brevity matters: a long response gets truncated and lost.\n\n"
    "key is a stable slug for the pattern (e.g. anxiety_before_decisions, "
    "energized_by_sister, drained_by_money_talk, drops_social_commitments, "
    "lonely_sunday_nights, lowest_on_sundays) so a later, refined pattern about the "
    "same thing reuses the same key.\n\n"
    "KEY STABILITY IS CRITICAL — duplicate patterns under different keys are the main "
    "failure mode. You are shown the patterns ALREADY TRACKED, each as [key] its "
    "description. BEFORE emitting any pattern, read those descriptions and check "
    "whether your observation MEANS THE SAME THING as one already tracked, even if you "
    "would word it completely differently. If it does, you MUST output that pattern's "
    "EXACT key (copy it verbatim) — never coin a new slug for an idea that already has "
    "one. Only mint a new key for a genuinely NEW pattern not already covered. When in "
    "doubt, REUSE an existing key rather than risk a near-duplicate. Do NOT re-emit a "
    "pattern already marked (confirmed) or (corrected) unless new evidence truly "
    "refines it; if you do, reuse that pattern's exact key.\n\n"
    "Infer only what the evidence supports; do not invent. Output JSON only, no prose."
)

REFLECT_BACK_SYSTEM = (
    "You are the person's AI companion. You have noticed a possible pattern about "
    "them. Write ONE gentle, natural question that checks whether it's true — phrased "
    "as a question, never a declaration, and never like an interview. It should feel "
    "like something a close friend would softly wonder aloud. Output ONLY the question."
)

RESPONSE_CLASSIFY_SYSTEM = (
    "You just gently asked the person whether an observed pattern about them is true. "
    "Classify their reply as exactly one of: confirm (they agree), correct (they "
    "disagree and say how it actually is), or deflect (they dodge, joke, or change "
    'the subject). Return ONLY JSON: {"classification": "confirm|correct|deflect", '
    '"correction_text": <the corrected pattern as one sentence, or null>}. '
    "correction_text is non-null ONLY for correct."
)


def build_detection_request(
    promoted_episodes: Sequence[MemoryRecord],
    signals: Sequence[GraphNode],
    current_traits: Sequence[InferredTrait] = (),
) -> list[dict]:
    """Render evidence (id-tagged for provenance) plus the patterns already tracked.

    Feeding back the current traits lets the model REUSE existing keys for recurring
    patterns instead of coining a fresh slug each run — that's what makes a repeat
    sleep pass idempotent (the LLM doesn't produce stable keys on its own).
    """

    def block(title: str, lines: Sequence[str]) -> str:
        if not lines:
            return f"{title}: (none)"
        return f"{title}:\n" + "\n".join(lines)

    ep_lines = [f"[ep:{e.id}] {e.content}" for e in promoted_episodes if e.id]
    sig_lines = [f"[sig:{s.id}] {s.content}" for s in signals]
    trait_lines = [f"[{t.key}] ({t.status}) {t.content}" for t in current_traits]
    parts = [
        block("Notable past episodes", ep_lines),
        block("Emotional signals", sig_lines),
        block("Patterns already tracked (reuse these exact keys for the same idea)", trait_lines),
    ]
    return [{"role": "user", "content": "\n\n".join(parts)}]


def parse_detection(
    raw: str,
    *,
    user_id: str,
    known_episode_ids: set[str],
    known_signal_ids: set[str],
    valid_from: datetime | None = None,
) -> list[InferredTrait]:
    """Parse the detection model's JSON array into InferredTraits.

    Provenance is enforced: cited ids are filtered to the sets actually passed to the
    model (traceability honesty), and any trait left with zero provenance is dropped.
    Tolerant of prose around the array; a malformed response yields [] so detect()
    never crashes the sleep pass.
    """
    valid_from = valid_from or datetime.now(UTC)
    data = _salvage_objects(raw)  # tolerant: recovers complete objects even if truncated
    if not data:
        return []

    traits: list[InferredTrait] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content", "")).strip()
        key = str(item.get("key") or "").strip()
        if not content or not key:
            continue
        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        episode_ids = _cited(item.get("provenance_episode_ids"), known_episode_ids)
        signal_ids = _cited(item.get("provenance_signal_ids"), known_signal_ids)
        if not episode_ids and not signal_ids:
            continue  # provenance is mandatory — drop ungrounded inferences
        traits.append(
            InferredTrait(
                user_id=user_id,
                key=key,
                content=content,
                confidence=confidence,
                valid_from=valid_from,
                status_updated_at=valid_from,
                status=TraitStatus.INFERRED,
                provenance=ProvenanceRecord(episode_ids=episode_ids, signal_ids=signal_ids),
            )
        )
    return traits


def _salvage_objects(raw: str) -> list[dict]:
    """Pull every COMPLETE top-level JSON object out of a (possibly truncated or
    code-fenced) array. The detection model can hit max_tokens and return a valid
    prefix cut off mid-object; a single strict json.loads would then throw the whole
    batch away. Scanning for balanced ``{...}`` blocks salvages all the traits that
    completed before the cutoff."""
    start = raw.find("[")
    if start == -1:
        return []
    # Fast path: a well-formed array parses directly.
    end = raw.rfind("]")
    if end > start:
        try:
            data = json.loads(raw[start : end + 1])
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict)]
        except json.JSONDecodeError:
            pass
    # Salvage path: walk the string, collecting balanced top-level objects.
    objs: list[dict] = []
    depth = 0
    obj_start: int | None = None
    in_str = False
    esc = False
    for i in range(start + 1, len(raw)):
        ch = raw[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and obj_start is not None:
                try:
                    objs.append(json.loads(raw[obj_start : i + 1]))
                except json.JSONDecodeError:
                    pass
                obj_start = None
    return [o for o in objs if isinstance(o, dict)]


def _cited(value: object, known: set[str]) -> list[str]:
    """Provenance ids the model cited, normalized to bare ids and filtered to those in
    the input set. The model sometimes echoes the tag prefix ('ep:'/'sig:'); strip it
    so a prefixed citation still matches (and is stored as the bare id)."""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for x in value:
        s = str(x)
        if s.startswith("ep:") or s.startswith("sig:"):
            s = s.split(":", 1)[1]
        if s in known:
            out.append(s)
    return out


# --- Phase 5.3: cross-key trait consolidation (semantic dedup) ----------------

CONSOLIDATE_SYSTEM = (
    "You are given a list of inferred personality patterns about ONE person, each shown "
    "as [key] its description. Some may be DUPLICATES: different keys describing the SAME "
    "underlying pattern in different words. Identify groups of true duplicates.\n\n"
    "Return ONLY a JSON array of groups, where each group is a JSON array of the keys "
    'that mean the same thing, e.g. [["key_a","key_c"],["key_d","key_e"]]. Include a '
    "group ONLY for genuine duplicates (2+ keys describing one pattern). Do NOT group "
    "patterns that are merely related, adjacent, or about the same topic but genuinely "
    "distinct. When in doubt, leave them separate (omit). If there are no duplicates, "
    "return []. Output JSON only, no prose."
)


def build_consolidation_request(traits: Sequence[InferredTrait]) -> list[dict]:
    """Render the current inferred traits as a [key] description list for grouping."""
    listing = "\n".join(f"[{t.key}] {t.content}" for t in traits)
    return [{"role": "user", "content": f"Patterns:\n{listing}"}]


def parse_consolidation(raw: str, known_keys: set[str]) -> list[list[str]]:
    """Parse the consolidator's JSON into groups of duplicate keys.

    Each group is filtered to keys actually present (known_keys) and de-duplicated;
    only groups with 2+ real keys survive. Tolerant of prose/fences; a bad response
    yields [] so the pass simply consolidates nothing rather than crashing."""
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    groups: list[list[str]] = []
    for group in data:
        if not isinstance(group, list):
            continue
        keys: list[str] = []
        for k in group:
            ks = str(k).strip()
            if ks in known_keys and ks not in keys:
                keys.append(ks)
        if len(keys) >= 2:
            groups.append(keys)
    return groups


COMMITMENT_CONSOLIDATE_SYSTEM = (
    "You are given a numbered list of OPEN commitments one person has made, each as [N] "
    "description. Some may be DUPLICATES: the SAME underlying commitment restated in "
    "different words across conversations. Identify groups of duplicates.\n\n"
    "Return ONLY a JSON array of groups, where each group is a JSON array of the NUMBERS "
    "that refer to the same commitment, e.g. [[0,3,5],[1,4]]. Group ONLY genuine "
    "duplicates (the same intended action). Do NOT group commitments that are merely "
    "related, about the same topic, or sequential-but-different actions. When in doubt, "
    "keep them separate (omit). If there are no duplicates, return []. Output JSON only."
)


def build_commitment_consolidation_request(commitments: Sequence[CommitmentNode]) -> list[dict]:
    """Render open commitments as a numbered list for duplicate grouping."""
    listing = "\n".join(f"[{i}] {c.content}" for i, c in enumerate(commitments))
    return [{"role": "user", "content": f"Open commitments:\n{listing}"}]


def parse_index_groups(raw: str, count: int) -> list[list[int]]:
    """Parse a JSON array of index-groups (e.g. [[0,3],[1,4]]) into validated int groups.

    Indices out of range are dropped; only groups with 2+ distinct valid indices survive.
    Tolerant of prose/fences; a bad response yields [] (consolidate nothing)."""
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    groups: list[list[int]] = []
    for group in data:
        if not isinstance(group, list):
            continue
        idxs: list[int] = []
        for x in group:
            try:
                i = int(x)
            except (TypeError, ValueError):
                continue
            if 0 <= i < count and i not in idxs:
                idxs.append(i)
        if len(idxs) >= 2:
            groups.append(idxs)
    return groups


def build_reflect_back_request(trait: InferredTrait) -> list[dict]:
    """Hand the single eligible trait to the model to phrase as a gentle question."""
    return [{"role": "user", "content": f"Pattern you noticed: {trait.content}"}]


def build_classify_request(user_message: str) -> list[dict]:
    """Wrap the user's reply for confirm/correct/deflect classification."""
    return [{"role": "user", "content": f"Their reply: {user_message}"}]


def parse_classification(raw: str) -> tuple[str, str | None]:
    """Parse the classifier's JSON into ``(classification, correction_text)``.

    Defaults to ("deflect", None) on any parse failure — the safe no-op: we leave the
    trait unchanged rather than wrongly confirming or corrupting it.
    """
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return "deflect", None
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return "deflect", None
    if not isinstance(data, dict):
        return "deflect", None
    classification = str(data.get("classification", "")).strip().lower()
    if classification not in {"confirm", "correct", "deflect"}:
        return "deflect", None
    correction = data.get("correction_text")
    correction_text = str(correction).strip() if correction else None
    if classification != "correct":
        correction_text = None
    return classification, correction_text


# --- Phase 7: job recommendation follow-up classification --------------------

JOB_OUTCOME_CLASSIFY_SYSTEM = (
    "You earlier suggested a paid work opportunity to the person and are now following up. "
    "Classify their reply into exactly one of: tried_liked (they tried it and liked it), "
    "loved_it (they tried it and loved it / it's going great), tried_disliked (they tried "
    "it and didn't like it), not_tried (they haven't tried it). If it's ambiguous, choose "
    'not_tried. Return ONLY JSON: {"outcome": "tried_liked|loved_it|tried_disliked|not_tried"}.'
)


def build_job_outcome_request(user_message: str) -> list[dict]:
    """Wrap the user's follow-up reply for outcome classification."""
    return [{"role": "user", "content": f"Their reply: {user_message}"}]


def parse_job_outcome(raw: str) -> JobOutcome | None:
    """Parse the classifier's JSON into a ``JobOutcome``, or None on any failure.

    None is the safe no-op: we leave the thread open rather than guess an outcome.
    """
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    value = str(data.get("outcome", "")).strip().lower()
    try:
        return JobOutcome(value)
    except ValueError:
        return None


# --- Phase 5: proactivity prompts --------------------------------------------

# TONE RULE (baked in, not just a code comment): care, not accountability.
_TONE_RULE = (
    "TONE RULE: Ask how they are FEELING about it, not WHETHER they did it. Care, not "
    'accountability. Say "How are you feeling about the half marathon signup?" — never '
    '"Did you sign up for the half marathon?" Warm, brief, never naggy.'
)

PROACTIVITY_SYSTEM = (
    "You are the person's AI companion, reaching out about a commitment they made. "
    "Write ONE warm opening sentence to start the next conversation, gently touching "
    "on it. Use their current facts only for tone/context. " + _TONE_RULE + " Output "
    "ONLY the sentence."
)

GENERAL_CHECKIN_SYSTEM = (
    "You are the person's AI companion. It has been a while since you talked. Write ONE "
    "warm 'how are things?' opening sentence. If you are given something you know "
    "deeply about them, reference it specifically so it feels personal, not generic. "
    "Never mention that time has passed in a guilt-tripping way. Output ONLY the sentence."
)

COMMITMENT_RESOLVE_SYSTEM = (
    "You gently checked in with the person about a commitment they had made. From their "
    "reply, decide whether they FOLLOWED THROUGH. Classify as exactly one of: kept "
    "(they did it / are clearly on track), dropped (they did not / let it go), or "
    "unclear (their reply doesn't say either way). Return ONLY JSON: "
    '{"resolution": "kept|dropped|unclear", "user_words": <short quote of what they '
    "said, or null>}."
)


def build_proactivity_request(commitment: CommitmentNode, facts: Sequence[GraphNode]) -> list[dict]:
    """Give the model the commitment + light context to phrase a warm opener."""
    ctx = "\n".join(f"- {f.content}" for f in facts) or "(none)"
    return [
        {
            "role": "user",
            "content": f"Commitment they made: {commitment.content}\n\nWhat you know:\n{ctx}",
        }
    ]


def build_general_checkin_request(traits: Sequence[InferredTrait]) -> list[dict]:
    """Give the model a confirmed trait to anchor a personal (not generic) check-in."""
    confirmed = [t for t in traits if t.status is TraitStatus.CONFIRMED]
    anchor = confirmed[0].content if confirmed else "(nothing specific — keep it warm and open)"
    return [{"role": "user", "content": f"Something you know deeply about them: {anchor}"}]


def build_resolve_request(user_message: str, commitment: CommitmentNode) -> list[dict]:
    """Wrap the user's reply + the commitment for kept/dropped/unclear classification."""
    return [
        {
            "role": "user",
            "content": f"The commitment: {commitment.content}\n\nTheir reply: {user_message}",
        }
    ]


def parse_resolution(raw: str) -> tuple[str, str | None]:
    """Parse the resolver's JSON into ``(resolution, user_words)``.

    Defaults to ("unclear", None) on any parse failure — the safe no-op: we don't
    resolve a commitment we couldn't read, so we never wrongly mark it kept/dropped.
    """
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return "unclear", None
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return "unclear", None
    if not isinstance(data, dict):
        return "unclear", None
    resolution = str(data.get("resolution", "")).strip().lower()
    if resolution not in {"kept", "dropped", "unclear"}:
        return "unclear", None
    words = data.get("user_words")
    return resolution, (str(words).strip() if words else None)
