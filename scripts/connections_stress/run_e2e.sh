#!/usr/bin/env bash
# Connections service — live end-to-end dry run orchestrator.
#
# Sequences: seed brain (in-process) -> start fake-auth + brain API (HTTP) -> run the five
# connections passes against real profiles -> capture the companion openers -> build the report.
# Only the auth DATA STORE is faked (no Supabase); every other component runs for real.
#
#   scripts/connections_stress/run_e2e.sh [DAYS] [TURNS]        # default 5 6
#
# Re-runnable: it deletes the cx-* synthetic users from the brain at seed time and dumps
# everything to output/connections_stress/.

set -uo pipefail

DAYS="${1:-5}"
TURNS="${2:-6}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"

MESH="stress-test-mesh-token-not-for-prod"
LOGDIR="output/connections_stress/_logs"
mkdir -p "$LOGDIR"

ANTHROPIC_KEY="$(grep -E '^ALIK_ANTHROPIC_API_KEY=' .env | cut -d= -f2-)"
if [ -z "$ANTHROPIC_KEY" ]; then echo "FATAL: ALIK_ANTHROPIC_API_KEY not found in .env"; exit 1; fi

FAKE_AUTH_PID=""
BRAIN_PID=""
cleanup() {
  echo ""
  echo "== teardown: stopping servers =="
  [ -n "$BRAIN_PID" ] && kill "$BRAIN_PID" 2>/dev/null
  [ -n "$FAKE_AUTH_PID" ] && kill "$FAKE_AUTH_PID" 2>/dev/null
  wait 2>/dev/null
}
trap cleanup EXIT

wait_http() {  # wait_http URL EXPECTED_CODE_REGEX NAME
  local url="$1" want="$2" name="$3" i code
  for i in $(seq 1 60); do
    code="$(curl -s -o /dev/null -w '%{http_code}' "$url" 2>/dev/null)"
    if [[ "$code" =~ $want ]]; then echo "  $name up (HTTP $code)"; return 0; fi
    sleep 1
  done
  echo "FATAL: $name did not come up ($url, last code=$code)"; return 1
}

echo "================================================================"
echo " CONNECTIONS E2E DRY RUN  |  days=$DAYS turns=$TURNS"
echo "================================================================"

echo ""
echo "== 0. infra: postgres/redis/falkordb/connections-postgres =="
docker compose up -d postgres redis falkordb connections-postgres || { echo "docker compose failed"; exit 1; }
# wait for the connections DB specifically (the brain's are already up per docker ps)
for i in $(seq 1 30); do
  docker compose exec -T connections-postgres pg_isready -U alik >/dev/null 2>&1 && break
  sleep 1
done
echo "  infra ready"

echo ""
if [ -n "${SKIP_SEED:-}" ]; then
  echo "== 1. seed the brain — SKIPPED (SKIP_SEED set; reusing existing brain data) =="
else
  echo "== 1. seed the brain (in-process, cheap model) =="
  uv run python scripts/connections_stress/seed_brain.py --days "$DAYS" --turns "$TURNS" \
    2>"$LOGDIR/seed.err" | tee "$LOGDIR/seed.out"
  if [ "${PIPESTATUS[0]}" -ne 0 ]; then
    echo "FATAL: seeding failed (see $LOGDIR/seed.err)"; tail -20 "$LOGDIR/seed.err"; exit 1
  fi
fi

echo ""
echo "== 2. start fake-auth (:8001) =="
SERVICE_TOKEN="$MESH" uv run uvicorn --app-dir scripts/connections_stress fake_auth:app \
  --port 8001 --log-level warning >"$LOGDIR/fake_auth.log" 2>&1 &
FAKE_AUTH_PID=$!
wait_http "http://localhost:8001/health" "200" "fake-auth" || exit 1

echo ""
echo "== 3. start brain API (:8000), auth->fake, matching/connections disabled =="
ALIK_SERVICE_TOKEN="$MESH" \
ALIK_AUTH_SERVICE_URL="http://localhost:8001" \
ALIK_MATCHING_SERVICE_URL="" \
ALIK_CONNECTIONS_SERVICE_URL="" \
  uv run uvicorn alik.api:app --port 8000 --log-level warning >"$LOGDIR/brain_api.log" 2>&1 &
BRAIN_PID=$!
# profile endpoint returns 200 with the token once memory is connected
wait_http "http://localhost:8000/users/cx-ava/profile" "200|401" "brain API" || exit 1
# confirm the profile actually assembles (identity from fake-auth + facts from brain)
echo "  sample profile (cx-ava):"
curl -s -H "X-Service-Token: $MESH" http://localhost:8000/users/cx-ava/profile \
  | python3 -c "import sys,json; p=json.load(sys.stdin); print('    identity:', p.get('identity')); print('    facts:', len(p.get('facts',[])), 'confirmed_traits:', len(p.get('confirmed_traits',[])), 'dimensions:', len(p.get('dimensions',[])))" 2>/dev/null || echo "    (could not parse profile)"

echo ""
echo "== 4. reset connections DB + stale check-ins, write .env, run the five passes =="
# Each run must start from a clean slate so match_state/group_candidates from a previous run
# don't make the surface/cluster dedup skip fresh introductions. users_pool/interests/dims are
# repopulated by ingest anyway; interest_nodes are re-seeded by the passes.
docker compose exec -T connections-postgres psql -U alik -d connections -c \
  "TRUNCATE users_pool, user_interests, profile_dimensions, candidate_scores, eval_results, match_state, group_candidates;" \
  >/dev/null 2>&1 && echo "  connections tables truncated" \
  || echo "  connections tables not present yet (first run) — the passes will create them"
# Drop any people-match check-ins left in the brain from a prior chain run so the openers we
# capture reflect ONLY this run's surfacing (the seed's own commitment check-ins are untouched).
docker compose exec -T postgres psql -U alik -d alik -c \
  "DELETE FROM pending_checkins WHERE user_id LIKE 'cx-%' AND checkin_type IN ('people_match','people_match_group');" \
  >/dev/null 2>&1 && echo "  stale people-match check-ins cleared" || true
cat > services/connections/.env <<EOF
DATABASE_URL=postgresql://alik:alik@localhost:5434/connections
BRAIN_URL=http://localhost:8000
AUTH_URL=http://localhost:8001
SERVICE_TOKEN=$MESH
ANTHROPIC_API_KEY=$ANTHROPIC_KEY
EVAL_MODEL=claude-haiku-4-5-20251001
LAUNCH_STATES=MN
AGE_FILTER_MODE=off
PORT=8003
EOF
uv run --directory services/connections python "$REPO/scripts/connections_stress/run_passes.py" \
  2>&1 | tee "$LOGDIR/passes.log"

echo ""
echo "== 5. dump connections tables =="
CONN_DB_URL="postgresql://alik:alik@localhost:5434/connections" \
  uv run --directory services/connections python "$REPO/scripts/connections_stress/dump_connections.py"

echo ""
echo "== 6. capture the companion openers (real model) =="
ALIK_SERVICE_TOKEN="$MESH" uv run python scripts/connections_stress/capture_openers.py \
  2>"$LOGDIR/openers.err" | tee "$LOGDIR/openers.out"

echo ""
echo "== 7. build the review report =="
uv run python scripts/connections_stress/build_report.py

echo ""
echo "================================================================"
echo " DONE.  Review: output/connections_stress/REPORT.md"
echo "        Logs:   $LOGDIR/"
echo "================================================================"
