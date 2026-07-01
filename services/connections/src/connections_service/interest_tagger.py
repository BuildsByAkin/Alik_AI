"""Semantic (LLM) interest tagging — the smart replacement for keyword substring matching.

The deterministic ``interests.extract_interests`` maps fact text onto the taxonomy by keyword,
so it only catches interests phrased with a known stem and is blind to paraphrase/context
("I spend weekends scrambling up rock faces" never mentions "climb"). This module instead asks
a small model to CLASSIFY a person's described interests into the SAME canonical taxonomy —
understanding meaning, rejecting false signals (a verb like "runs a D&D campaign" is gaming,
not running), and falling back to a broad ``<category>:_general`` node when something fits a
category but no specific node.

Why classify into the fixed taxonomy rather than free-form tags: matching/clustering need a
SHARED vocabulary — two people only "overlap" if their interests resolve to the same node id.
Free-form tags would drift ("hiking" vs "trail hikes") and break overlap, the same slug-drift
problem the brain solved for traits. So the model gets intelligence over MEANING while the node
ids stay canonical. (Coining brand-new specific nodes is a deliberate future extension.)

Privacy: only interest-bearing signals are sent (an allowlist of hobby/taste fact keys +
confirmed-trait text); sensitive keys (relationship_goal, health_concern, …) are never read,
and the OUTPUT is only catalog node ids — no raw content is stored. Pure pieces (prompt build,
parse) are unit-tested directly; ``tag_interests`` does the one LLM call and returns None on
any failure so the caller can fall back to the keyword path (graceful degradation).
"""

from __future__ import annotations

import json
import logging

from connections_service.interests import INTENSITY_FACTORS, TAXONOMY, all_interest_nodes
from connections_service.models import InterestEdge

logger = logging.getLogger("connections.interest_tagger")

# Fact keys that carry an interest/taste (safe to send). Sensitive keys are deliberately absent.
_INTEREST_FACT_KEYS = (
    "primary_hobby",
    "secondary_hobby",
    "tertiary_hobby",
    "primary_exercise",
    "music_taste",
    "book_genre",
    "sports_team",
    "food_cuisine_preference",
    "gaming_habit",
    "movie_tv_taste",
    "values_core",
)

# LLM intensity word -> stored weight (reuses the keyword path's scale so kernels are consistent).
_INTENSITY_WEIGHT = {
    "intense": INTENSITY_FACTORS["intense_specific"],
    "engaged": INTENSITY_FACTORS["engaged"],
    "casual": INTENSITY_FACTORS["casual"],
}
_DEFAULT_WEIGHT = INTENSITY_FACTORS["engaged"]

_VALID_NODES = frozenset(n.id for n in all_interest_nodes())


def _taxonomy_catalog() -> str:
    """The catalog the model must classify into: broad category → specific nodes + a _general."""
    lines: list[str] = []
    for broad, specifics in TAXONOMY.items():
        opts = ", ".join(f"{broad}:{spec} ({label})" for spec, label in specifics.items())
        lines.append(f"- {broad}: {opts}, {broad}:_general (fits the category, no specific node)")
    return "\n".join(lines)


TAG_SYSTEM = (
    "You map a person's real interests onto a FIXED catalog of interest nodes so they can be "
    "matched with compatible people. You understand meaning and paraphrase — you do NOT rely on "
    "keywords.\n\n"
    "Catalog (node_id (label)):\n"
    f"{_taxonomy_catalog()}\n\n"
    "Rules:\n"
    "- Return ONLY node_ids from the catalog above. Never invent a node_id.\n"
    "- Understand context: map what they clearly MEAN, even if the exact word isn't used "
    "(e.g. 'scrambling up rock faces' -> outdoor_active:rock_climbing).\n"
    "- Reject false signals: a word used as a verb or in another sense is NOT that interest "
    "(e.g. 'runs a D&D campaign' -> gaming:dnd, NOT outdoor_active:running; 'a marketing "
    "campaign' is not camping).\n"
    "- If something is clearly in a category but no specific node fits, use <category>:_general.\n"
    "- Only DURABLE interests worth matching on — ignore one-off mentions, chores, moods, and "
    "anything about health, relationships, or work stress.\n"
    "- Rate each: intensity = casual | engaged | intense.\n\n"
    "Respond in JSON only:\n"
    '{"interests": [{"node": "<node_id>", "intensity": "casual|engaged|intense"}]}\n'
    "Return an empty list if nothing in the catalog genuinely applies."
)


def build_tag_request(profile: dict, *, trait_confidence_floor: float) -> list[dict] | None:
    """The user message: their interest-bearing signals. None if there's nothing to tag."""
    facts = {f.get("key"): (f.get("content") or "") for f in profile.get("facts", [])}
    lines: list[str] = []
    for key in _INTEREST_FACT_KEYS:
        content = facts.get(key)
        if content:
            lines.append(f"- {key}: {content}")
    for trait in profile.get("confirmed_traits", []):
        try:
            conf = float(trait.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        content = (trait.get("content") or "").strip()
        if conf >= trait_confidence_floor and content:
            lines.append(f"- trait: {content}")
    if not lines:
        return None
    return [{"role": "user", "content": "Their described interests:\n" + "\n".join(lines)}]


def parse_tags(raw: str) -> list[InterestEdge] | None:
    """Parse the model's JSON into canonical InterestEdges. None on malformation (-> fall back).
    Unknown/hallucinated node ids are dropped; duplicates keep the max weight."""
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    items = data.get("interests") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    edges: dict[str, InterestEdge] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        node = str(item.get("node", "")).strip()
        if node not in _VALID_NODES:
            if node:
                logger.warning("interest_tagger: dropped unknown node %r", node)
            continue
        weight = _INTENSITY_WEIGHT.get(str(item.get("intensity", "")).strip(), _DEFAULT_WEIGHT)
        existing = edges.get(node)
        if existing is None or weight > existing.weight:
            edges[node] = InterestEdge(node, round(weight, 4), "llm")
    return list(edges.values())


async def tag_interests(
    llm, profile: dict, *, trait_confidence_floor: float
) -> list[InterestEdge] | None:
    """Tag a profile's interests via the model. Returns edges, or **None on any failure/empty
    input** so the caller falls back to the deterministic keyword path."""
    messages = build_tag_request(profile, trait_confidence_floor=trait_confidence_floor)
    if messages is None:
        return None
    try:
        raw = await llm.complete(system=TAG_SYSTEM, messages=messages)
    except Exception:
        logger.warning(
            "interest_tagger: LLM call failed — falling back to keyword tagging", exc_info=True
        )
        return None
    edges = parse_tags(raw)
    if edges is None:
        logger.warning("interest_tagger: unparseable response — falling back to keyword tagging")
    return edges
