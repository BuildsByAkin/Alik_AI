"""One greppable INFO summary line per cron pass.

Each of the five passes (ingest, score, eval, surface, cluster) emits exactly one
``PASS_SUMMARY`` line when it finishes — succeeded or partially failed — so a full cron
cycle yields five lines and ``grep PASS_SUMMARY`` returns nothing else. Fields are flat
``key=value`` pairs (space-separated) so the line stays human-readable AND could later feed
a log aggregator without changing the emitters. Purely observational: no infra, no log
shipping (deliberately deferred).
"""

from __future__ import annotations

PREFIX = "PASS_SUMMARY"


def format_pass_summary(pass_name: str, **fields: object) -> str:
    body = " ".join(f"{key}={value}" for key, value in fields.items())
    return f"{PREFIX} pass={pass_name} {body}"
