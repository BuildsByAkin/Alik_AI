"""The pass-run digest + alerting — the observability half of the connections cron pipeline.

Every pass records a row in ``pass_runs`` (see passlog.emit_pass_summary). The five passes
never raise and swallow per-user/per-pair errors, so a silent degradation is otherwise
invisible. This module reads that history and answers two questions:

  * DIGEST  — over the last N hours, how many times did each pass run, how much work did it do,
              how many hard failures, and what's its failure rate?
  * ALERT   — is the eval pass's LLM-failure rate at/above the threshold? (Usually an Anthropic
              outage/rate-limit, not a data problem — worth paging on.)

The aggregation is PURE (list of PassRun in → dict out) so it unit-tests with no infra;
``run_digest`` does the store read + logging around it. Runnable via the ``connections-digest``
console script or the optional in-process scheduler.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from connections_service.models import PassRun

logger = logging.getLogger("connections.monitoring")

PASS_ORDER = ("ingest", "score", "eval", "surface", "cluster")

# Per pass: (throughput field, whether that field already includes the failures). The failure
# rate is failures / attempts, where attempts = work (+ failures when work excludes them).
# cluster has no clean throughput denominator, so it reports a failure COUNT only (rate=None).
_WORK: dict[str, tuple[str | None, bool]] = {
    "ingest": ("users_processed", True),
    "score": ("users_processed", True),
    "eval": ("pairs_evaluated", False),
    "surface": ("checkins_queued", False),
    "cluster": (None, False),
}


def _int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def build_digest(runs: list[PassRun], *, since_hours: int) -> dict:
    """Aggregate pass runs per pass. Pure — pass in the runs already filtered to the window."""
    per_pass: dict[str, dict] = {}
    for name in PASS_ORDER:
        rows = [r for r in runs if r.pass_name == name]
        failures = sum(r.failures for r in rows)
        work_field, includes_failures = _WORK[name]
        work = sum(_int(r.fields.get(work_field)) for r in rows) if work_field else 0
        attempts = work if includes_failures else work + failures
        rate = round(failures / attempts, 4) if (work_field and attempts > 0) else None
        last = max((r.ran_at for r in rows if r.ran_at is not None), default=None)
        per_pass[name] = {
            "runs": len(rows),
            "failures": failures,
            "work": work if work_field else None,
            "error_rate": rate,
            "last_ran_at": last,
        }
    return {"since_hours": since_hours, "passes": per_pass}


def eval_error_rate(digest: dict) -> float | None:
    return digest["passes"]["eval"]["error_rate"]


def alerts(digest: dict, *, eval_threshold: float) -> list[str]:
    """Actionable alerts from a digest. Currently: eval LLM-failure rate over threshold, and any
    pass that produced zero runs in the window (a cron that isn't firing)."""
    out: list[str] = []
    rate = eval_error_rate(digest)
    if rate is not None and rate >= eval_threshold:
        out.append(
            f"eval LLM-failure rate {rate:.0%} >= threshold {eval_threshold:.0%} "
            "(likely an Anthropic outage/rate-limit — check the API, not the data)"
        )
    for name in PASS_ORDER:
        if digest["passes"][name]["runs"] == 0:
            out.append(f"pass '{name}' did not run in the last {digest['since_hours']}h")
    return out


def format_digest(digest: dict, *, eval_threshold: float) -> str:
    """A human-readable digest block (for logs / a daily heads-up)."""
    lines = [f"CONNECTIONS DIGEST (last {digest['since_hours']}h)"]
    for name in PASS_ORDER:
        p = digest["passes"][name]
        rate = "n/a" if p["error_rate"] is None else f"{p['error_rate']:.0%}"
        work = "—" if p["work"] is None else p["work"]
        last = p["last_ran_at"].isoformat() if p["last_ran_at"] else "never"
        lines.append(
            f"  {name:8} runs={p['runs']} work={work} failures={p['failures']} "
            f"fail_rate={rate} last={last}"
        )
    fired = alerts(digest, eval_threshold=eval_threshold)
    lines.append("  ALERTS: " + ("; ".join(fired) if fired else "none"))
    return "\n".join(lines)


async def run_digest(store, settings) -> dict:
    """Read the window from the store, build + log the digest, and log any alerts. Returns the
    digest dict (so a caller/endpoint can serve it). Never raises on the alert path."""
    since = datetime.now(UTC) - timedelta(hours=settings.digest_window_hours)
    runs = await store.get_recent_pass_runs(since)
    digest = build_digest(runs, since_hours=settings.digest_window_hours)
    logger.info(format_digest(digest, eval_threshold=settings.eval_error_rate_threshold))
    for alert in alerts(digest, eval_threshold=settings.eval_error_rate_threshold):
        logger.warning("CONNECTIONS ALERT: %s", alert)
    return digest


def main() -> None:
    """One-shot digest from the CLI (the `connections-digest` console script)."""
    import asyncio

    from connections_service.config import settings
    from connections_service.store import PgStore

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    async def _run() -> None:
        store = await PgStore.connect(settings.database_url)
        try:
            await run_digest(store, settings)
        finally:
            await store.aclose()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
