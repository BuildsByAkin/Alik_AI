"""The job catalog: the ``Job`` shape and a strict loader.

Moved verbatim from the brain's Phase 7 ``job_matcher`` — the catalog is the source of
truth (``data/jobs.json``); add a job by adding a JSON object, no code change.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


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
    """Load and validate the catalog. Raises clearly if missing or malformed (so a bad
    catalog fails loudly at startup, not silently at request time)."""
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
