"""Pass-run digest + alerting: aggregation, error rates, alert thresholds, store round-trip,
and best-effort persistence (observability must never fail a pass)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from connections_service.models import PassRun
from connections_service.monitoring import (
    alerts,
    build_digest,
    eval_error_rate,
    format_digest,
)
from connections_service.passlog import emit_pass_summary
from connections_service.store import InMemoryStore

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def _runs() -> list[PassRun]:
    return [
        PassRun("ingest", {"users_processed": 10, "failures": 2, "pool_ready": 8}, 2, NOW),
        PassRun("ingest", {"users_processed": 10, "failures": 0, "pool_ready": 9}, 0, NOW),
        PassRun("eval", {"pairs_evaluated": 8, "llm_failures": 2, "flagged_for_review": 3}, 2, NOW),
        PassRun("cluster", {"groups_proposed": 1, "groups_surfaced": 1, "failures": 0}, 0, NOW),
    ]


def test_build_digest_aggregates_per_pass():
    d = build_digest(_runs(), since_hours=24)
    ingest = d["passes"]["ingest"]
    assert ingest["runs"] == 2
    assert ingest["failures"] == 2
    assert ingest["work"] == 20  # summed users_processed
    assert ingest["error_rate"] == pytest.approx(0.1)  # 2 failures / 20 attempts (incl. failures)


def test_eval_rate_excludes_from_denominator_then_adds_failures():
    d = build_digest(_runs(), since_hours=24)
    # eval work excludes failures: attempts = 8 evaluated + 2 failures = 10 -> 0.2
    assert eval_error_rate(d) == pytest.approx(0.2)


def test_cluster_has_no_rate_only_a_count():
    d = build_digest(_runs(), since_hours=24)
    assert d["passes"]["cluster"]["error_rate"] is None
    assert d["passes"]["cluster"]["work"] is None
    assert d["passes"]["cluster"]["failures"] == 0


def test_alert_fires_on_eval_rate_and_on_missing_passes():
    d = build_digest(_runs(), since_hours=24)
    fired = alerts(d, eval_threshold=0.2)
    assert any("eval LLM-failure rate" in a for a in fired)  # 0.2 >= 0.2
    # score + surface never ran in the window
    assert any("'score' did not run" in a for a in fired)
    assert any("'surface' did not run" in a for a in fired)


def test_no_alert_when_eval_rate_below_threshold_and_all_ran():
    runs = [
        PassRun(name, {"users_processed": 5, "pairs_evaluated": 5, "checkins_queued": 5}, 0, NOW)
        for name in ("ingest", "score", "eval", "surface", "cluster")
    ]
    d = build_digest(runs, since_hours=24)
    assert alerts(d, eval_threshold=0.2) == []


def test_format_digest_is_readable():
    text = format_digest(build_digest(_runs(), since_hours=24), eval_threshold=0.2)
    assert "CONNECTIONS DIGEST" in text
    assert "eval" in text and "ALERTS:" in text


async def test_store_records_and_reads_recent_runs_window_and_order():
    store = InMemoryStore()
    old = PassRun("ingest", {"users_processed": 3}, 0, NOW - timedelta(hours=48))
    fresh1 = PassRun("eval", {"pairs_evaluated": 4}, 0, NOW - timedelta(hours=1))
    fresh2 = PassRun("score", {"users_processed": 4}, 0, NOW - timedelta(minutes=5))
    for r in (old, fresh1, fresh2):
        await store.record_pass_run(r)
    got = await store.get_recent_pass_runs(NOW - timedelta(hours=24))
    assert [r.pass_name for r in got] == ["score", "eval"]  # newest first, old one excluded


async def test_emit_pass_summary_persists_and_hoists_failures():
    store = InMemoryStore()
    await emit_pass_summary(store, "eval", pairs_evaluated=5, llm_failures=3, flagged_for_review=1)
    runs = await store.get_recent_pass_runs(datetime.now(UTC) - timedelta(hours=1))
    assert len(runs) == 1
    assert runs[0].pass_name == "eval"
    assert runs[0].failures == 3  # llm_failures hoisted into the failure count
    assert runs[0].fields["pairs_evaluated"] == 5


async def test_emit_pass_summary_never_raises_on_store_error():
    class BrokenStore:
        async def record_pass_run(self, run):
            raise RuntimeError("db down")

    # Must swallow the error — a monitoring write can never fail the pass.
    await emit_pass_summary(BrokenStore(), "ingest", users_processed=1, failures=0)


async def test_digest_endpoint_returns_aggregate_and_alerts(client, store):
    await store.record_pass_run(PassRun("eval", {"pairs_evaluated": 4, "llm_failures": 0}, 0))
    resp = client.get("/digest")
    assert resp.status_code == 200
    body = resp.json()
    assert body["passes"]["eval"]["runs"] == 1
    assert "alerts" in body  # missing passes (ingest/score/...) will be flagged
