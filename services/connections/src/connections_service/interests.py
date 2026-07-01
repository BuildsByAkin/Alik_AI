"""The interest graph: the two-level taxonomy (single source of truth) + deterministic
fact→interest extraction. Pure — no I/O, no LLM, fully testable.

Structure: ``broad_category → specific_interest``. Every broad category also has a
``_general`` catch-all node, so a categorizable-but-unrecognized fact still yields a BROAD
edge — that is what makes the Part-3 cold-start "relax to broader categories" fallback work.

Privacy: only interest-flavored signals are derived here. ``relationship_goal`` and
``health_concern`` are never read; ``values_core`` contributes only a derived ``social_causes``
node when a cause keyword matches — never raw content.
"""

from __future__ import annotations

import logging
import re

from connections_service.models import InterestEdge, InterestNode

logger = logging.getLogger("connections.interests")

GENERAL = "_general"

# broad_category -> {specific_interest: canonical_label}. Extend freely; node id = "{broad}:{spec}".
TAXONOMY: dict[str, dict[str, str]] = {
    "outdoor_active": {
        "running": "Running",
        "hiking": "Hiking",
        "rock_climbing": "Rock climbing",
        "cycling": "Cycling",
        "swimming": "Swimming",
        "skiing": "Skiing / snowboarding",
        "kayaking": "Kayaking / canoeing",
        "camping": "Camping",
    },
    "creative": {
        "photography": "Photography",
        "writing": "Writing",
        "painting": "Painting",
        "drawing": "Drawing",
        "music_making": "Making music",
        "pottery": "Pottery",
        "crafting": "Crafting",
    },
    "gaming": {
        "video_games": "Video games",
        "board_games": "Board games",
        "tabletop_wargaming": "Tabletop wargaming",
        "dnd": "D&D / TTRPGs",
        "card_games": "Card games",
        "puzzles": "Puzzles",
    },
    "food_drink": {
        "cooking": "Cooking",
        "baking": "Baking",
        "wine": "Wine",
        "coffee": "Coffee",
        "craft_beer": "Craft beer",
    },
    "intellectual": {
        "reading_fiction": "Reading fiction",
        "reading_nonfiction": "Reading nonfiction",
        "chess": "Chess",
        "debate": "Debate",
        "languages": "Languages",
        "philosophy": "Philosophy",
    },
    "sports_watching": {
        "football": "Football",
        "basketball": "Basketball",
        "hockey": "Hockey",
        "baseball": "Baseball",
        "soccer": "Soccer",
    },
    "wellness": {
        "yoga": "Yoga",
        "meditation": "Meditation",
        "strength_training": "Strength training",
        "pilates": "Pilates",
        "running_wellness": "Running (fitness)",
    },
    "music_listening": {
        "indie": "Indie",
        "hip_hop": "Hip-hop",
        "classical": "Classical",
        "electronic": "Electronic",
        "jazz": "Jazz",
        "metal": "Metal",
        "folk": "Folk",
    },
    "tech": {
        "programming": "Programming",
        "hardware": "Hardware",
        "ai": "AI / ML",
        "electronics": "Electronics",
    },
    "social_causes": {
        "volunteering": "Volunteering",
        "environment": "Environment / climate",
        "animal_welfare": "Animal welfare",
        "community_organizing": "Community organizing",
        "faith_community": "Faith community",
    },
}

# Weight = intensity factor (per-user, from the interest_intensity dimension) × fact-rank factor.
# Both tunable here (single source of truth).
INTENSITY_FACTORS = {"intense_specific": 1.0, "engaged": 0.7, "casual": 0.4}
INTENSITY_MISSING = 0.5
RANK_FACTORS = {"primary_hobby": 1.0, "secondary_hobby": 0.8, "tertiary_hobby": 0.6}

# Free-text keyword -> node id. Order matters (first hit wins). Keep keywords specific.
SYNONYMS: dict[str, str] = {
    # outdoor_active
    "bould": "outdoor_active:rock_climbing",
    "climb": "outdoor_active:rock_climbing",
    "hik": "outdoor_active:hiking",
    "trail": "outdoor_active:hiking",
    "jog": "outdoor_active:running",
    "run": "outdoor_active:running",
    "cycl": "outdoor_active:cycling",
    "bike": "outdoor_active:cycling",
    "biking": "outdoor_active:cycling",
    "swim": "outdoor_active:swimming",
    "snowboard": "outdoor_active:skiing",
    "ski": "outdoor_active:skiing",
    "kayak": "outdoor_active:kayaking",
    "canoe": "outdoor_active:kayaking",
    "camp": "outdoor_active:camping",
    # creative
    "photo": "creative:photography",
    "writ": "creative:writing",
    "poetry": "creative:writing",
    "paint": "creative:painting",
    "draw": "creative:drawing",
    "sketch": "creative:drawing",
    "guitar": "creative:music_making",
    "piano": "creative:music_making",
    "in a band": "creative:music_making",
    "produc": "creative:music_making",
    "potter": "creative:pottery",
    "ceramic": "creative:pottery",
    "knit": "creative:crafting",
    "sew": "creative:crafting",
    "craft": "creative:crafting",
    # gaming
    "warhammer": "gaming:tabletop_wargaming",
    "wargam": "gaming:tabletop_wargaming",
    "miniatur": "gaming:tabletop_wargaming",
    "d&d": "gaming:dnd",
    "dungeons": "gaming:dnd",
    "ttrpg": "gaming:dnd",
    "tabletop rpg": "gaming:dnd",
    "board game": "gaming:board_games",
    "magic the gathering": "gaming:card_games",
    "poker": "gaming:card_games",
    "card game": "gaming:card_games",
    "crossword": "gaming:puzzles",
    "jigsaw": "gaming:puzzles",
    "puzzle": "gaming:puzzles",
    "video game": "gaming:video_games",
    "playstation": "gaming:video_games",
    "xbox": "gaming:video_games",
    "gaming": "gaming:video_games",
    # food_drink
    "cook": "food_drink:cooking",
    "bak": "food_drink:baking",
    "wine": "food_drink:wine",
    "espresso": "food_drink:coffee",
    "coffee": "food_drink:coffee",
    "craft beer": "food_drink:craft_beer",
    "homebrew": "food_drink:craft_beer",
    # intellectual / books
    "chess": "intellectual:chess",
    "debat": "intellectual:debate",
    "philosoph": "intellectual:philosophy",
    "learning spanish": "intellectual:languages",
    "language": "intellectual:languages",
    "nonfiction": "intellectual:reading_nonfiction",
    "non-fiction": "intellectual:reading_nonfiction",
    "history book": "intellectual:reading_nonfiction",
    "biograph": "intellectual:reading_nonfiction",
    "sci-fi": "intellectual:reading_fiction",
    "fantasy": "intellectual:reading_fiction",
    "novel": "intellectual:reading_fiction",
    "fiction": "intellectual:reading_fiction",
    # sports_watching
    "nfl": "sports_watching:football",
    "vikings": "sports_watching:football",
    "football": "sports_watching:football",
    "nba": "sports_watching:basketball",
    "timberwolves": "sports_watching:basketball",
    "basketball": "sports_watching:basketball",
    "nhl": "sports_watching:hockey",
    "hockey": "sports_watching:hockey",
    "mlb": "sports_watching:baseball",
    "twins": "sports_watching:baseball",
    "baseball": "sports_watching:baseball",
    "soccer": "sports_watching:soccer",
    "premier league": "sports_watching:soccer",
    # wellness
    "yoga": "wellness:yoga",
    "medita": "wellness:meditation",
    "mindful": "wellness:meditation",
    "weightlift": "wellness:strength_training",
    "lifting": "wellness:strength_training",
    "strength": "wellness:strength_training",
    "the gym": "wellness:strength_training",
    "pilates": "wellness:pilates",
    # music_listening (genres)
    "indie": "music_listening:indie",
    "hip hop": "music_listening:hip_hop",
    "hip-hop": "music_listening:hip_hop",
    "rap": "music_listening:hip_hop",
    "classical": "music_listening:classical",
    "techno": "music_listening:electronic",
    "edm": "music_listening:electronic",
    "electronic": "music_listening:electronic",
    "jazz": "music_listening:jazz",
    "metal": "music_listening:metal",
    "folk": "music_listening:folk",
    # tech
    "machine learning": "tech:ai",
    "artificial intelligence": "tech:ai",
    "program": "tech:programming",
    "coding": "tech:programming",
    "software": "tech:programming",
    "developer": "tech:programming",
    "arduino": "tech:electronics",
    "raspberry pi": "tech:electronics",
    "soldering": "tech:electronics",
    "hardware": "tech:hardware",
    # social_causes
    "volunteer": "social_causes:volunteering",
    "climate": "social_causes:environment",
    "sustainab": "social_causes:environment",
    "environment": "social_causes:environment",
    "animal rescue": "social_causes:animal_welfare",
    "animal welfare": "social_causes:animal_welfare",
    "mutual aid": "social_causes:community_organizing",
    "activism": "social_causes:community_organizing",
    "organizing": "social_causes:community_organizing",
    "church": "social_causes:faith_community",
    "faith": "social_causes:faith_community",
    "mosque": "social_causes:faith_community",
    "temple": "social_causes:faith_community",
}


def all_interest_nodes() -> list[InterestNode]:
    """The full node set to seed the DB with — every specific plus a ``_general`` per category."""
    nodes: list[InterestNode] = []
    for broad, specifics in TAXONOMY.items():
        for spec, label in specifics.items():
            nodes.append(InterestNode(f"{broad}:{spec}", broad, spec, label))
        pretty = broad.replace("_", " ").title()
        nodes.append(InterestNode(f"{broad}:{GENERAL}", broad, GENERAL, f"{pretty} (general)"))
    return nodes


# --- keyword matching -----------------------------------------------------------------------
#
# Keywords are STEMS matched at a word boundary (``\bhik`` catches "hiking"/"hikes"), NOT raw
# substrings — plain ``in`` matched a stem inside an unrelated word ("camp" in "campaign",
# "run" in "runs a campaign"), inventing false interests. Two more safeguards:
#   * FALSE-FRIEND GUARDS: a negative lookahead blocks a stem's known collisions
#     ("camp" but not "campaign"; "ski" but not "skill/skin/skip/skirt").
#   * SPECIFICITY over POSITION: when several keywords hit, an unambiguous MULTIWORD phrase
#     ("tabletop rpg", "board game") wins over a short verb-stem ("run"), so "Runs tabletop RPG
#     campaigns" maps to D&D, not running; among equals the earliest mention wins.

_FALSE_FRIEND_GUARD: dict[str, str] = {
    "camp": r"(?!aign)",  # camping/campsite — never "campaign" (e.g. a D&D campaign)
    "ski": r"(?!ll|n|p|rt)",  # skiing/ski — never skill/skin/skip/skirt
}

# Multiword keywords are normally treated as UNAMBIGUOUS and win over a short verb-stem. A few
# are weak/incidental context ("at the gym" is not a specific interest) — demote them so they
# don't override a clear hobby stem like "bouldering".
_WEAK_MULTIWORD: frozenset[str] = frozenset({"the gym"})


def _compile(keyword: str) -> re.Pattern[str]:
    return re.compile(r"\b" + re.escape(keyword) + _FALSE_FRIEND_GUARD.get(keyword, ""))


_COMPILED_SYNONYMS: list[tuple[str, str, re.Pattern[str]]] = [
    (kw, node_id, _compile(kw)) for kw, node_id in SYNONYMS.items()
]


def _best_match(content: str, items: list[tuple[str, str, re.Pattern[str]]]) -> str | None:
    """The best synonym hit among ``items``: prefer a multiword (unambiguous) keyword, then the
    earliest mention, then the longest keyword. None if nothing matches."""
    c = content.lower()
    best_key: tuple[int, int, int] | None = None
    best_node: str | None = None
    for keyword, node_id, pattern in items:
        m = pattern.search(c)
        if m is None:
            continue
        strong = " " in keyword and keyword not in _WEAK_MULTIWORD
        key = (0 if strong else 1, m.start(), -len(keyword))
        if best_key is None or key < best_key:
            best_key, best_node = key, node_id
    return best_node


def map_content_to_node(content: str) -> str | None:
    """Best synonym hit anywhere in the taxonomy, or None (caller decides warn vs skip)."""
    return _best_match(content, _COMPILED_SYNONYMS)


def map_in_category(content: str, broad: str) -> str:
    """Map within a known category: a specific synonym if one hits, else the ``_general`` node.
    Used where the fact key already implies the category (music_taste, book_genre, sports_team)."""
    scoped = [t for t in _COMPILED_SYNONYMS if t[1].startswith(f"{broad}:")]
    return _best_match(content, scoped) or f"{broad}:{GENERAL}"


def map_cause(content: str) -> str | None:
    """A social_causes node only if a cause keyword matches — never a generic fallback."""
    scoped = [t for t in _COMPILED_SYNONYMS if t[1].startswith("social_causes:")]
    return _best_match(content, scoped)


def _intensity_factor(dims: dict[str, dict]) -> float:
    value = (dims.get("interest_intensity") or {}).get("value")
    return INTENSITY_FACTORS.get(value, INTENSITY_MISSING)


def extract_interests(profile: dict, *, trait_confidence_floor: float = 0.7) -> list[InterestEdge]:
    """Derive person→interest edges from an assembled Profile API response. Never raises;
    unmappable interest facts are logged and skipped (the user is kept, just without that edge)."""
    facts = {f.get("key"): (f.get("content") or "") for f in profile.get("facts", [])}
    dims = {d.get("dimension"): d for d in profile.get("dimensions", [])}
    intensity = _intensity_factor(dims)

    edges: dict[str, InterestEdge] = {}

    def add(node_id: str, weight: float, source: str) -> None:
        existing = edges.get(node_id)
        if existing is None or weight > existing.weight:
            edges[node_id] = InterestEdge(node_id, round(weight, 4), source)

    # Hobbies — generic mapping; unmappable → warn + skip (keep the user).
    for key, rank in RANK_FACTORS.items():
        content = facts.get(key)
        if not content:
            continue
        node = map_content_to_node(content)
        if node:
            add(node, intensity * rank, key)
        else:
            logger.warning("connections: unmappable %s content=%r", key, content)

    # Exercise — always an activity, so it falls back to wellness:_general.
    if facts.get("primary_exercise"):
        node = map_content_to_node(facts["primary_exercise"]) or "wellness:_general"
        add(node, intensity * RANK_FACTORS["primary_hobby"], "primary_exercise")

    # Category-targeted facts (the key implies the broad category).
    if facts.get("music_taste"):
        add(map_in_category(facts["music_taste"], "music_listening"), 0.6, "music_taste")
    if facts.get("book_genre"):
        add(map_in_category(facts["book_genre"], "intellectual"), 0.6, "book_genre")
    if facts.get("sports_team"):
        add(map_in_category(facts["sports_team"], "sports_watching"), 0.5, "sports_team")
    if facts.get("values_core"):
        cause = map_cause(facts["values_core"])
        if cause:
            add(cause, 0.5, "values_core")

    # Activity-flavored confirmed traits, only when confident.
    for trait in profile.get("confirmed_traits", []):
        try:
            confidence = float(trait.get("confidence", 0.0))
        except (TypeError, ValueError):
            continue
        if confidence < trait_confidence_floor:
            continue
        node = map_content_to_node(trait.get("content", ""))
        if node:
            add(node, 0.6 * confidence, f"trait:{trait.get('key', '')}")

    return list(edges.values())
