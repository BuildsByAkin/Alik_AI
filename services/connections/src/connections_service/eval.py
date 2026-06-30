"""LLM cross-evaluation: after the kernel shortlists, a small model judges whether two people
would genuinely click, in alik's voice. The model sees a PRIVACY-SAFE summary only (no names,
no ages, no raw facts, no sensitive keys).

Pure pieces (label maps, summary builder, shared-signal renderer, response parser) have no
LLM/store/I/O and are unit-tested directly; ``eval_pass`` does the store reads + one LLM call
per directed pair.
"""

from __future__ import annotations

import json
import logging

from connections_service.config import Settings
from connections_service.interests import all_interest_nodes
from connections_service.models import (
    DimensionSnapshot,
    EvalResult,
    InterestEdge,
    KernelExplanation,
    UserPoolEntry,
)

logger = logging.getLogger("connections.eval")

# --- the dimension label table (single editable source; validated loudly at import) ----------
#
# Every taxonomy axis × value MUST have an entry. A missing key raises at import (startup),
# never silently leaks a raw enum into a prompt. Edit phrasings here without touching logic.

DIMENSION_TAXONOMY: dict[str, tuple[str, ...]] = {
    "detail_specificity": ("vague", "moderate", "highly_specific"),
    "topic_focus": ("deep_narrow", "balanced", "broad_shallow"),
    "interest_intensity": ("casual", "engaged", "intense_specific"),
    "structure_preference": ("flexible", "mixed", "needs_structure"),
    "sensory_sensitivity": ("low", "medium", "high"),
    "social_predictability_need": ("low", "medium", "high"),
}

DIMENSION_LABELS: dict[tuple[str, str], str] = {
    ("detail_specificity", "vague"): "talks about interests in broad strokes",
    ("detail_specificity", "moderate"): "shares a fair bit of detail about their interests",
    (
        "detail_specificity",
        "highly_specific",
    ): "talks about their interests in precise, specific detail",
    ("topic_focus", "deep_narrow"): "goes deep on a few things they love",
    ("topic_focus", "balanced"): "balances going deep with exploring widely",
    ("topic_focus", "broad_shallow"): "likes sampling a wide range of things",
    ("interest_intensity", "casual"): "keeps their interests casual and low-key",
    ("interest_intensity", "engaged"): "is genuinely engaged in their interests",
    ("interest_intensity", "intense_specific"): "is intensely, specifically into what they love",
    ("structure_preference", "flexible"): "is happy to be spontaneous",
    ("structure_preference", "mixed"): "likes a loose plan but stays flexible",
    ("structure_preference", "needs_structure"): "feels most at ease when there's a clear plan",
    ("sensory_sensitivity", "low"): "is comfortable in loud, busy settings",
    ("sensory_sensitivity", "medium"): "is fine in most settings",
    ("sensory_sensitivity", "high"): "prefers calmer, lower-key environments",
    ("social_predictability_need", "low"): "is easygoing about who and what's involved",
    ("social_predictability_need", "medium"): "likes a rough sense of the plan",
    ("social_predictability_need", "high"): "likes to know who and what's involved in advance",
}

# Short human names per axis — used to render shared/compatible dimensions in the prompt.
AXIS_HUMAN: dict[str, str] = {
    "detail_specificity": "level of detail",
    "topic_focus": "how they dive into interests",
    "interest_intensity": "intensity about interests",
    "structure_preference": "how much they like a plan",
    "sensory_sensitivity": "environment preferences",
    "social_predictability_need": "needing to know the plan",
}


def _validate_labels() -> None:
    """Loudly fail at startup if any axis×value (or axis) is unlabeled — never leak a raw enum."""
    missing = [
        f"{axis}={value}"
        for axis, values in DIMENSION_TAXONOMY.items()
        for value in values
        if (axis, value) not in DIMENSION_LABELS
    ]
    missing_axes = [a for a in DIMENSION_TAXONOMY if a not in AXIS_HUMAN]
    if missing or missing_axes:
        raise RuntimeError(
            f"connections eval: incomplete dimension labels (values={missing}, axes={missing_axes})"
        )


_validate_labels()

# node_id -> canonical label, from the interest taxonomy (single source of truth).
_INTEREST_LABELS = {n.id: n.canonical_label for n in all_interest_nodes()}


def _interest_label(node_id: str) -> str:
    label = _INTEREST_LABELS.get(node_id)
    if label is not None:
        return label.lower()
    return node_id.split(":", 1)[-1].replace("_", " ")  # graceful fallback, never the raw id


def _cap(s: str) -> str:
    return s[0].upper() + s[1:] if s else s


# --- the privacy-safe person summary (pure) -------------------------------------------------


def build_person_summary(
    entry: UserPoolEntry,
    interests: list[InterestEdge],
    dimensions: list[DimensionSnapshot],
    *,
    max_interests: int,
    dimension_floor: float,
) -> str:
    """A privacy-safe bullet summary: city + specific interest labels + dimension phrasings.
    Excludes name, photo, age, and all raw/sensitive fact content by construction."""
    lines: list[str] = []
    if entry.city:
        lines.append(f"- From {entry.city}")

    specific = sorted(
        (e for e in interests if not e.interest_node_id.endswith(":_general")),
        key=lambda e: e.weight,
        reverse=True,
    )
    labels = [_interest_label(e.interest_node_id) for e in specific[:max_interests]]
    if labels:
        lines.append("- Into: " + ", ".join(labels))

    by_axis = {d.dimension: d for d in dimensions}
    for axis in DIMENSION_TAXONOMY:
        d = by_axis.get(axis)
        if d is None or d.confidence < dimension_floor or d.status == "corrected":
            continue
        label = DIMENSION_LABELS.get((axis, d.value))
        if label:
            lines.append(f"- {_cap(label)}")
        else:
            logger.warning("connections eval: unlabeled %s=%s — omitted", axis, d.value)

    return "\n".join(lines) if lines else "- (not much known yet)"


# --- the shared-signals block from the kernel's explanation (pure) --------------------------


def render_shared_signals(
    explanation: KernelExplanation, *, shared_dimension_threshold: float
) -> str:
    """Turn the kernel's KernelExplanation into the prompt's 'What they share' block."""
    lines: list[str] = []
    if explanation.interest_specific:
        labels = [_interest_label(m.node_id) for m in explanation.interest_specific]
        lines.append("- Both into: " + ", ".join(labels))
    elif explanation.match_type == "broad_only" and explanation.interest_broad:
        pretty = [b.replace("_", " ") for b in explanation.interest_broad]
        lines.append("- Both drawn to: " + ", ".join(pretty))

    for dm in explanation.dimensions:
        if dm.axis_score < shared_dimension_threshold:
            continue
        human = AXIS_HUMAN.get(dm.axis, dm.axis.replace("_", " "))
        if dm.value_a == dm.value_b:
            label = DIMENSION_LABELS.get((dm.axis, dm.value_a))
            lines.append(f"- Aligned on {human}: {label}" if label else f"- Aligned on {human}")
        else:
            lines.append(f"- Compatible on {human}")

    if explanation.values_causes:
        lines.append(
            "- Both care about: " + ", ".join(_interest_label(n) for n in explanation.values_causes)
        )

    return "\n".join(lines) if lines else "- Not much in common on the surface."


# --- the prompt + response parsing ----------------------------------------------------------

EVAL_SYSTEM = (
    "You are alik, an AI that finds people for real-life connection. You have compared notes "
    "on two people. Your job is to judge whether they would genuinely enjoy spending time "
    "together, and why.\n\n"
    "Respond in JSON only, no other text:\n"
    "{\n"
    '  "would_click": true | false,\n'
    '  "confidence": 0.0-1.0,\n'
    '  "reason": "one or two sentences, specific to what they share, written as if alik is '
    'explaining to a friend why these two people should meet — warm, concrete, no jargon",\n'
    '  "flag_for_review": true | false,\n'
    '  "flag_reason": "only if flag_for_review is true — what made you uncertain"\n'
    "}\n\n"
    "Be honest. If the shared signals are thin or generic, say so with low confidence and flag "
    "it. Only say would_click: true if you can point to something specific they share."
)


def build_eval_messages(
    person_a_summary: str, person_b_summary: str, shared_signals: str
) -> list[dict]:
    content = (
        f"Person A:\n{person_a_summary}\n\n"
        f"Person B:\n{person_b_summary}\n\n"
        f"What they share:\n{shared_signals}"
    )
    return [{"role": "user", "content": content}]


def parse_eval_response(raw: str) -> dict | None:
    """Parse the model's JSON into a normalized dict, or None on any malformation."""
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "would_click" not in data or "confidence" not in data:
        return None
    reason = str(data.get("reason", "")).strip()
    if not reason:
        return None
    try:
        confidence = max(0.0, min(1.0, float(data["confidence"])))
    except (TypeError, ValueError):
        return None
    flag = bool(data.get("flag_for_review", False))
    flag_reason = str(data["flag_reason"]).strip() if flag and data.get("flag_reason") else None
    return {
        "would_click": bool(data["would_click"]),
        "confidence": confidence,
        "reason": reason,
        "flag_for_review": flag,
        "flag_reason": flag_reason,
    }


def compute_final_confidence(
    kernel_confidence: float, llm_confidence: float, settings: Settings
) -> float:
    return round(
        settings.kernel_conf_weight * kernel_confidence + settings.llm_conf_weight * llm_confidence,
        4,
    )


# --- the eval pass --------------------------------------------------------------------------


async def eval_pass(store, llm, settings: Settings) -> dict[str, int]:
    """Cross-evaluate each pool_ready user's shortlist. Per-pair isolated; never raises."""
    counts = {"users": 0, "evaluated": 0, "skipped": 0}
    for state in sorted(settings.launch_states_set):
        pool = await store.get_pool_users(state)
        by_id = {e.user_id: e for e in pool}
        for entry in pool:
            counts["users"] += 1
            try:
                candidates = await store.get_candidate_scores(entry.user_id)
            except Exception:
                logger.exception("connections eval: candidate read failed for %s", entry.user_id)
                continue
            shortlist = [
                c
                for c in candidates
                if c.score >= settings.min_kernel_score and not c.human_review_flag
            ][: settings.eval_top_n]
            if not shortlist:
                continue
            a_summary = await _summary(store, entry, settings)
            for cand in shortlist:
                b_entry = by_id.get(cand.user_id_b)
                if b_entry is None:  # candidate left the pool since scoring
                    counts["skipped"] += 1
                    continue
                try:
                    await _eval_pair(store, llm, settings, entry.user_id, a_summary, b_entry, cand)
                    counts["evaluated"] += 1
                except _SkipPair:
                    counts["skipped"] += 1
                except Exception:
                    logger.exception(
                        "connections eval failed for %s->%s", entry.user_id, cand.user_id_b
                    )
                    counts["skipped"] += 1
    logger.info("connections eval complete: %s", counts)
    return counts


class _SkipPair(Exception):
    """A pair we cleanly skip (e.g. malformed LLM JSON) — counted as skipped, not an error."""


async def _summary(store, entry: UserPoolEntry, settings: Settings) -> str:
    return build_person_summary(
        entry,
        await store.get_user_interests(entry.user_id),
        await store.get_profile_dimensions(entry.user_id),
        max_interests=settings.summary_max_interests,
        dimension_floor=settings.dimension_confidence_floor,
    )


async def _eval_pair(store, llm, settings, a_id, a_summary, b_entry, cand) -> None:
    b_summary = await _summary(store, b_entry, settings)
    shared = render_shared_signals(
        cand.explanation, shared_dimension_threshold=settings.shared_dimension_threshold
    )
    raw = await llm.complete(
        system=EVAL_SYSTEM, messages=build_eval_messages(a_summary, b_summary, shared)
    )
    parsed = parse_eval_response(raw)
    if parsed is None:
        logger.warning("connections eval: malformed response for %s->%s", a_id, b_entry.user_id)
        raise _SkipPair
    await store.save_eval_result(
        EvalResult(
            user_id_a=a_id,
            user_id_b=b_entry.user_id,
            would_click=parsed["would_click"],
            llm_confidence=parsed["confidence"],
            final_confidence=compute_final_confidence(
                cand.confidence, parsed["confidence"], settings
            ),
            reason=parsed["reason"],
            eval_model=settings.eval_model,
            flag_for_review=parsed["flag_for_review"],
            flag_reason=parsed["flag_reason"],
        )
    )


def main() -> None:
    """One-shot cross-eval from the CLI (the `connections-eval` console script)."""
    import asyncio

    from connections_service.config import settings
    from connections_service.llm import AnthropicLLM
    from connections_service.store import PgStore

    async def _run() -> None:
        store = await PgStore.connect(settings.database_url)
        llm = AnthropicLLM(
            api_key=settings.anthropic_api_key.get_secret_value(),
            model=settings.eval_model,
            max_tokens=settings.eval_max_tokens,
        )
        try:
            await eval_pass(store, llm, settings)
        finally:
            await store.aclose()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
