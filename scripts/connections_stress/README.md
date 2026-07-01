# connections_stress — live end-to-end dry run for the people-matching chain

A quality-review harness (NOT a unit test) that exercises the **whole** connections pipeline on
realistic, LLM-generated data and captures the actual companion openers a user would hear.

## What it does
1. **seed_brain.py** — 8 Minnesota personas (overlapping hobbies) talk with the real companion for
   N days; extraction + sleep pass build real facts/traits/dimensions. Data is left in the brain DB.
2. **fake_auth.py** — a fixture-backed stand-in for the auth service (roster + identity) so the
   chain can run without Supabase. Only the auth *data store* is faked; contracts are identical.
3. Brain runs as a real HTTP server; **run_passes.py** runs the five real connections passes
   (ingest → score → eval → surface → cluster) against real profiles over HTTP.
4. **dump_connections.py** dumps every connections table; **capture_openers.py** opens real
   sessions and records the delivered `people_match` openers (on the real companion model).
5. **build_report.py** assembles `output/connections_stress/REPORT.md`.

`run_e2e.sh` orchestrates all of it (starts/stops the servers, resets the connections DB per run).

## Run
```bash
scripts/connections_stress/run_e2e.sh [DAYS] [TURNS]     # default 5 6
SKIP_SEED=1 scripts/connections_stress/run_e2e.sh 5 6    # reuse seeded brain data; chain only (cheap)
```
Requires docker-compose up (postgres/redis/falkordb/connections-postgres) and
`ALIK_ANTHROPIC_API_KEY` in the repo-root `.env`. Spends real (cheap-model) API calls.

## Outputs — `output/connections_stress/`
- `REPORT.md` — full auto-generated review (regenerated each run)
- `FINDINGS.md` — curated findings + fix validation (hand-written; durable)
- `<user_id>/` — per-user brain dumps (facts, traits, dimensions, transcripts)
- `_connections/*.json` — every connections table
- `_openers.json` — the delivered openers
- `_logs/` — per-stage logs (seed, passes, openers, servers)

## Notes
- The harness deletes + reseeds the `cx-*` synthetic users each seed run, and truncates the
  connections tables at the start of the chain, so runs are clean and repeatable.
- MN is the default launch state; personas live in MN cities so they're eligible.
