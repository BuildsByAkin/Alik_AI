"""Deterministic catalog scoring against a user's living profile.

Adapted from the brain's Phase 7 ``job_matcher``: it now reads the assembled-profile JSON
shape (the brain's Profile API) instead of graph objects. ``facts`` are ``{"key","content"}``
dicts; ``confirmed_traits`` are ``{"key","content","confidence"}`` dicts (the Profile API
returns ONLY confirmed traits, so every entry is already confirmed).

MATCHING RULES (unchanged):
- ``available_to_all`` jobs always score 1.0 — the fallback for everyone.
- ``required_fact_values`` is OR-semantics: satisfied when ANY listed key has a fact whose
  content contains ANY listed substring (case-insensitive).
- ``required_facts`` is key-presence.
- ``requires_any_confirmed_trait`` + ``min_confidence``: needs a confirmed trait at/above it.
- Every specified requirement group is a HARD GATE — a specified-and-failing group scores 0.0.
"""

from __future__ import annotations

from collections.abc import Sequence

from matching_service.catalog import Job

Fact = dict  # {"key": str, "content": str}
Trait = dict  # {"key": str, "content": str, "confidence": float}


def _fact_values_match(required: dict[str, list[str]], facts: Sequence[Fact]) -> bool:
    by_key: dict[str, list[str]] = {}
    for f in facts:
        by_key.setdefault(str(f.get("key", "")).lower(), []).append(
            str(f.get("content", "")).lower()
        )
    for key, substrings in required.items():
        contents = by_key.get(key.lower(), [])
        for sub in substrings:
            s = sub.lower()
            if any(s in content for content in contents):
                return True
    return False


def _facts_present(required: dict[str, list[str]], facts: Sequence[Fact]) -> bool:
    present = {str(f.get("key", "")).lower() for f in facts}
    return all(key.lower() in present for key in required)


def _has_confirmed_trait(traits: Sequence[Trait], min_confidence: float) -> bool:
    return any(float(t.get("confidence", 0.0)) >= min_confidence for t in traits)


def score_job(job: Job, facts: Sequence[Fact], traits: Sequence[Trait]) -> float:
    """Score a job in [0, 1]. ``available_to_all`` -> 1.0; any failed hard gate -> 0.0."""
    if job.available_to_all:
        return 1.0

    specified = 0
    matched = 0

    if job.required_fact_values:
        specified += 1
        if _fact_values_match(job.required_fact_values, facts):
            matched += 1
        else:
            return 0.0

    if job.required_facts:
        specified += 1
        if _facts_present(job.required_facts, facts):
            matched += 1
        else:
            return 0.0

    if job.requires_any_confirmed_trait:
        specified += 1
        if _has_confirmed_trait(traits, job.min_confidence):
            matched += 1
        else:
            return 0.0

    if specified == 0:
        return 0.0
    return matched / specified


def match_jobs_for_user(
    facts: Sequence[Fact],
    traits: Sequence[Trait],
    catalog: Sequence[Job],
    already_recommended: set[str] | Sequence[str],
    *,
    threshold: float = 0.5,
    excluded_partners: set[str] | None = None,
) -> Job | None:
    """Pick the single best job to surface, or None.

    Priority: the highest-scoring SPECIFIC job at/above ``threshold``; else the
    ``available_to_all`` fallback. Never a job already recommended, nor an excluded partner.
    """
    seen = set(already_recommended)
    excluded = excluded_partners or set()

    def eligible(job: Job) -> bool:
        return job.id not in seen and job.partner not in excluded

    specific = [j for j in catalog if not j.available_to_all and eligible(j)]
    scored = [(j, score_job(j, facts, traits)) for j in specific]
    qualifying = [(j, s) for j, s in scored if s >= threshold]
    if qualifying:
        qualifying.sort(key=lambda pair: pair[1], reverse=True)
        return qualifying[0][0]

    for job in catalog:
        if job.available_to_all and eligible(job):
            return job
    return None
