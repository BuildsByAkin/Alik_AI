"""Phase 7 job matching — pure, infra-free.

Reads what the companion already knows (current Facts + CONFIRMED InferredTraits) and
scores a static, hand-curated catalog (``data/jobs.json``). Nothing here touches a DB,
the graph, or the model: matching is deterministic so it is cheap and fully testable.

MATCHING RULES
--------------
- ``available_to_all`` jobs always score ``1.0`` — they are the fallback for everyone.
- ``required_fact_values`` is OR-semantics: satisfied when ANY listed key has a current
  fact whose ``content`` contains ANY listed substring (case-insensitive).
- ``required_facts`` is key-presence: the user must have a current fact with that key
  (value ignored). Honored for forward-compat; unused by the seed catalog.
- ``requires_any_confirmed_trait`` + ``min_confidence``: the user must have at least one
  CONFIRMED trait at/above that confidence.
- Every specified requirement group is a HARD GATE — if a job specifies a group and it
  fails, the job scores ``0.0``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from alik.models import GraphNode, InferredTrait, TraitStatus


@dataclass(frozen=True, slots=True)
class Job:
    """One catalog entry. Mirrors a ``data/jobs.json`` object."""

    id: str
    title: str
    description: str
    partner: str
    partner_url: str
    pay_range: str
    required_facts: dict[str, list[str]] = field(default_factory=dict)
    required_fact_values: dict[str, list[str]] = field(default_factory=dict)
    required_traits: list[str] = field(default_factory=list)
    requires_any_confirmed_trait: bool = False
    min_confidence: float = 0.0
    available_to_all: bool = False


def load_catalog(path: str | Path) -> list[Job]:
    """Load and validate the catalog. Raises clearly if missing or malformed.

    Called once on sleep-pass startup — a bad catalog should fail loudly there, not
    silently yield zero matches at runtime.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"job catalog not found: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"job catalog is not valid JSON ({p}): {exc}") from exc

    raw_jobs = data.get("jobs") if isinstance(data, dict) else data
    if not isinstance(raw_jobs, list):
        raise ValueError(f"job catalog must have a 'jobs' array ({p})")

    jobs: list[Job] = []
    seen: set[str] = set()
    for i, obj in enumerate(raw_jobs):
        if not isinstance(obj, dict):
            raise ValueError(f"job catalog entry {i} is not an object ({p})")
        try:
            job = Job(
                id=obj["id"],
                title=obj["title"],
                description=obj["description"],
                partner=obj["partner"],
                partner_url=obj["partner_url"],
                pay_range=obj["pay_range"],
                required_facts=obj.get("required_facts", {}) or {},
                required_fact_values=obj.get("required_fact_values", {}) or {},
                required_traits=obj.get("required_traits", []) or [],
                requires_any_confirmed_trait=bool(obj.get("requires_any_confirmed_trait", False)),
                min_confidence=float(obj.get("min_confidence", 0.0)),
                available_to_all=bool(obj.get("available_to_all", False)),
            )
        except KeyError as exc:
            raise ValueError(f"job catalog entry {i} missing required field {exc} ({p})") from exc
        if job.id in seen:
            raise ValueError(f"duplicate job id in catalog: {job.id} ({p})")
        seen.add(job.id)
        jobs.append(job)
    return jobs


def _fact_values_match(required: dict[str, list[str]], facts: list[GraphNode]) -> bool:
    """OR-semantics: any required key whose current fact content contains any substring."""
    by_key: dict[str, list[str]] = {}
    for f in facts:
        by_key.setdefault(f.key.lower(), []).append(f.content.lower())
    for key, substrings in required.items():
        contents = by_key.get(key.lower(), [])
        for sub in substrings:
            s = sub.lower()
            if any(s in content for content in contents):
                return True
    return False


def _facts_present(required: dict[str, list[str]], facts: list[GraphNode]) -> bool:
    """Key-presence gate: the user has a current fact for every required key."""
    present = {f.key.lower() for f in facts}
    return all(key.lower() in present for key in required)


def _has_confirmed_trait(traits: list[InferredTrait], min_confidence: float) -> bool:
    return any(t.status is TraitStatus.CONFIRMED and t.confidence >= min_confidence for t in traits)


def score_job(job: Job, facts: list[GraphNode], traits: list[InferredTrait]) -> float:
    """Score a job in [0, 1]. ``available_to_all`` → 1.0; any failed hard gate → 0.0."""
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
        # A non-available_to_all job with no requirements: nothing to match it on.
        return 0.0
    return matched / specified


def match_jobs_for_user(
    user_id: str,
    facts: list[GraphNode],
    traits: list[InferredTrait],
    catalog: list[Job],
    already_recommended: set[str] | list[str],
    *,
    threshold: float = 0.5,
    excluded_partners: set[str] | None = None,
) -> Job | None:
    """Pick the single best job to surface, or None.

    Priority: the highest-scoring SPECIFIC job at/above ``threshold``; if none clears it,
    the ``available_to_all`` fallback. Never returns a job already recommended to this
    user, nor one from an excluded (disliked) partner.
    """
    seen = set(already_recommended)
    excluded = excluded_partners or set()

    def eligible(job: Job) -> bool:
        return job.id not in seen and job.partner not in excluded

    specific = [j for j in catalog if not j.available_to_all and eligible(j)]
    scored = [(j, score_job(j, facts, traits)) for j in specific]
    qualifying = [(j, s) for j, s in scored if s >= threshold]
    if qualifying:
        # Highest score first; stable tie-break by catalog order.
        qualifying.sort(key=lambda pair: pair[1], reverse=True)
        return qualifying[0][0]

    for job in catalog:
        if job.available_to_all and eligible(job):
            return job
    return None
