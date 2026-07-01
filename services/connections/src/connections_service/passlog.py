"""One greppable INFO summary line per cron pass — plus best-effort persistence for the digest.

Each of the five passes (ingest, score, eval, surface, cluster) emits exactly one
``PASS_SUMMARY`` line when it finishes — succeeded or partially failed — so a full cron
cycle yields five lines and ``grep PASS_SUMMARY`` returns nothing else. Fields are flat
``key=value`` pairs (space-separated) so the line stays human-readable AND feeds the digest.

``emit_pass_summary`` also records the same fields to the store's ``pass_runs`` table so the
monitoring digest reads structured history instead of scraping logs. Persistence is
best-effort: observability must never fail the matching pipeline, so a store error is logged
and swallowed.
"""

from __future__ import annotations

import logging

from connections_service.models import PassRun

logger = logging.getLogger("connections.passlog")

PREFIX = "PASS_SUMMARY"

# Field names that count as hard failures across the passes (eval/surface rename theirs).
_FAILURE_KEYS = ("failures", "llm_failures", "brain_failures")


def format_pass_summary(pass_name: str, **fields: object) -> str:
    body = " ".join(f"{key}={value}" for key, value in fields.items())
    return f"{PREFIX} pass={pass_name} {body}"


def _failure_count(fields: dict) -> int:
    total = 0
    for key in _FAILURE_KEYS:
        try:
            total += int(fields.get(key, 0) or 0)
        except (TypeError, ValueError):
            continue
    return total


async def emit_pass_summary(store, pass_name: str, **fields: object) -> None:
    """Log the one PASS_SUMMARY line and record the run for the digest. Never raises."""
    logger.info(format_pass_summary(pass_name, **fields))
    try:
        await store.record_pass_run(
            PassRun(pass_name=pass_name, fields=dict(fields), failures=_failure_count(fields))
        )
    except Exception:  # observability must not break the pass
        logger.warning("pass_runs persistence failed for pass=%s", pass_name, exc_info=True)
