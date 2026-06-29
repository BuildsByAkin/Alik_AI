# alik — project context

alik is a voice-first AI companion that learns you by listening day to day,
then (later) introduces you to compatible people in real life. We are building
the BRAIN FIRST IN TEXT; voice is the final phase and just an I/O swap.

## Golden rules
- The brain is modality-independent: text in, text out. Never couple logic to voice.
- All memory access goes through the `Memory` interface (write/retrieve/invalidate/delete).
  Nothing else imports the DB driver directly.
- `Memory.delete(user_id)` must FULLY erase a user. This is a legal requirement, not a feature.
- Build only the current phase. Leave clean seams for later phases; do not pre-build them.

## Runtime model vs coding model
- You (Claude Code) run on Opus 4.8 — that's for writing code.
- The COMPANION calls a cheaper model at runtime. Default `claude-sonnet-4-6`, configurable via env.
  Never hardcode the model string.

## Stack
- Python 3.12+, uv, FastAPI, async everywhere, full type hints.
- Postgres 16 + Redis (via docker-compose). pydantic-settings for config.
- ruff for lint/format, pytest for tests.

## Phase status
- Phase 1 ✓ — companion loop + working/short-term memory (text)
- Phase 2 ✓ — async extraction + temporal graph (FalkorDB)
- Phase 3 ✓ — nightly sleep pass (promote/resolve/decay/reflect)
- Phase 4 ✓ — pattern layer + reflect-back (live verified)
- Phase 5 ✓ — commitment lifecycle + proactivity engine (live verified)
- Phase 7 ✓ — earn / job-matching. EXTRACTED to the `services/matching/` microservice (own
  Postgres on 5433, port 8002); the brain only delivers + reports via `matching_client`.
- Living profile ✓ Step 1 — behavioral-dimension layer + soft-confirm + Profile API +
  cross-service delete (unit-tested; live gate pending).
- Living profile ✓ Step 2 — job-matching microservice (above) consuming the Profile API.
- Phase 6 — voice (half-cascade) (next)

## Open seams
- last_seen on graph Facts: still using valid_from as proxy — NOT addressed (deferred).
  Bump last_seen on any fact confirmed or re-stated in a session for accurate confidence
  decay. Seam: the `before` arg threaded through GraphMemory.decay_stale_facts →
  GraphStore.decay_confidence. NOTE: InferredTraits now DO have a real last_seen
  (`last_detected_at`, Phase 5.2) — the Fact version can follow the same pattern.
- Commitments soft-dedup unresolved duplicates (Phase 5.1 → 5.4). write_commitments merges a
  re-stated OPEN commitment with the same key into the existing node — bumps mention_count,
  refreshes expected_by — instead of inserting. RESOLVED nodes are never merged into (history
  preserved). Phase 5.4 (below) made keys reliable and switched the merge to KEY ALONE
  (dropped the old difflib ≥0.6 content gate). Pile-up is addressed at the source now; the
  remaining residue is genuinely-distinct intents the user never resolves (a staleness/expiry
  sink was scoped OUT — revisit if open counts climb over long horizons).
- Upcoming-commitment heads-up has no cross-day dedup of its own (only the
  one-undelivered-checkin-per-user guard). If a user keeps clearing check-ins, a
  heads-up could re-queue hourly until the commitment goes due. Acceptable for now.

## Conventions
- Small, focused modules. If execution diverges from the approved plan, stop and ask.
- Every memory-touching change ships with a test.

## Key decisions
- delete() is intentionally loud when FalkorDB is unreachable — it erases
  Postgres/Redis then raises rather than silently succeeding. Legal correctness
  over availability. Re-run once FalkorDB is up; ops are idempotent.
- Sleep-pass confidence decay uses a Fact's `valid_from` as a proxy for
  "last mentioned" (we don't track last-mention yet — re-stating a fact is a no-op
  in write_nodes). Known imprecision; a real `last_seen` is Phase 4 scope. The seam
  is the `before` arg threaded through GraphMemory.decay_stale_facts →
  GraphStore.decay_confidence — swap the proxy there when last_seen lands.
- Phase 4 trait temporal-resolution policy lives in GraphMemory.write_traits (NOT
  graph_store.insert_trait), mirroring write_nodes for Facts — keeps it provable
  against the in-memory double. A detect-driven supersede only CLOSES the old window
  (close_node) and keeps its status; correct_trait (status=corrected) is reserved
  for the reflect-back correction path.
- InferredTraits are never stated as fact: only CONFIRMED traits enter the system
  prompt; INFERRED ones surface only via reflect-back. Provenance is mandatory —
  parse_detection drops any trait with zero cited (and input-verified) episode/signal
  ids. `python -m alik.sleep_pass --explain-trait <id>` prints a trait's provenance.
- A user CORRECTION opens a new CONFIRMED trait (same key) that INHERITS the
  superseded trait's provenance + records source_session_id, so provenance stays
  non-empty and the correction is traceable to the inference it replaced.
- Reflect-back state (surfaced→awaiting-classification) is in-process on the
  Companion, double-guarded by the graph `surfaced_in_session` flag so a trait isn't
  re-surfaced in the same session even across a restart. Conscious carve-out: a
  pending classification is lost if the process restarts mid-session (acceptable this
  phase — matches the Redis-TTL ephemerality of the working buffer).
- detect() feeds current trait keys back into the prompt to prevent slug drift across
  nightly runs. The LLM does not produce stable slugs on its own (Phase 4 live
  acceptance caught a second pass coining `energized_by_sister_trail_running` next to
  `..._trail_runs`, duplicating). build_detection_request passes the user's CURRENT
  traits (key + status + content) and DETECTION_SYSTEM instructs the model to reuse an
  existing key verbatim for the same idea — the same dedup mechanism EXTRACTION uses
  via its canonical key list. (Divergence from the original plan: detect() now also
  reads current traits — made to satisfy the idempotency acceptance criterion.)
- write_traits refuses to supersede a CONFIRMED trait — confirmed traits change only
  via the reflect-back loop, never via re-detection (a re-detect with reworded content
  would otherwise silently undo a confirmation / re-open a correction).
  find_current_trait returns status for this guard. Corrected traits are closed
  (valid_until set) and retained for audit until Memory.delete erases everything.
- Phase 5: Commitments became their own `CommitmentNode` type (not GraphNode) with a
  lifecycle (pending→due→resolved_kept|resolved_dropped). They write via
  `write_commitments`/`insert_commitment` (NOT write_nodes), and every read COALESCEs
  a missing `status` to 'pending' so pre-Phase-5 commitment nodes join the lifecycle.
  Extraction now also captures an optional `expected_by` (only when the user states a
  time). The nightly `tick_commitments` (6th sleep pass) only advances pending→due
  (by expected_by, or a 14-day fallback for null-expected_by); it NEVER resolves —
  only the user resolves, via conversation.
- Phase 5 follow-through → pattern layer (DEVIATION from the literal spec, approved):
  resolving a commitment writes a follow-through EmotionalSignal (key `follow_through`,
  provenance to the commitment) rather than mutating an InferredTrait in real time.
  The nightly detect() folds that signal into the follow-through trait — reuses the
  Phase 4 seam, avoids brittle real-time trait-matching.
- Phase 5 proactivity: an hourly engine (`proactivity.run`, second APScheduler job)
  QUEUES at most one `pending_checkins` row per user (priority: due commitment →
  upcoming within 24h → lapsed >3 days → nothing). It never sends; the companion
  delivers it at the next `open_session()` as the opener, then marks it delivered.
  Tone rule is baked into PROACTIVITY_SYSTEM ("how they FEEL, not whether they did
  it"). pending_checkins lives in Postgres (Memory ABC) and is erased by delete().
  Proactive-opener + checkin-resolution state is in-process on the Companion (same
  carve-out as reflect-back). Graceful degradation: graph down → commitment paths read
  empty → falls through to the Postgres-only general check-in (generic warm if no trait).
- get_due_commitments orders explicit, most-overdue deadlines first (expected_by ASC,
  nulls last via COALESCE-to-far-future), THEN fallback-due ones. Found in Phase 5 live
  acceptance: valid_from-ASC ordering wrongly prioritized a vague 15-day-old no-deadline
  commitment over a concrete one that was actually due yesterday. A real deadline you
  missed should be followed up before something you never put a time on.
- INVARIANT: all stored datetimes are TZ-aware (UTC). Model-sourced ISO timestamps (a
  commitment's `expected_by`) can arrive naive, so they are normalized to UTC both on
  parse (prompt._parse_iso) and on graph read (graph_store._parse_dt). Without this the
  tick pass hit "can't compare offset-naive and offset-aware datetimes" (found running
  scripts/synthetic_users.py). Keep new model-sourced datetimes going through these.
- detect() robustness (found via synthetic run: Maya/Sara got 0 traits while James/David
  got many — purely an input-volume artifact, NOT persona). High-signal users produced a
  long detection response that hit max_tokens and was TRUNCATED; the old all-or-nothing
  json.loads then dropped every trait silently. Fixes: (a) parse_detection uses
  _salvage_objects — recovers every complete {...} from a truncated/fenced array; (b) _cited
  strips a leading 'ep:'/'sig:' so prefixed provenance still matches the bare ids and is
  stored bare; (c) DETECTION_SYSTEM caps output (now ≤5) one-sentence patterns so it stays
  under budget. Verified live.
- Pattern/commitment OVER-PRODUCTION fixes (Phase 5.2, found via synthetic run — Sara hit
  52 traits / Maya 34 commitments, all distinct keys, none pruned). Root cause: the system
  over-captured granular/momentary things as permanent memory and nothing pruned the
  unconfirmed layer; the cheap model amplifies it. Four-part fix:
  (1) EXTRACTION_SYSTEM commitments must be DURABLE intentions worth following up days later
      — not momentary actions ('take a break', 'eat lunch');
  (2) DETECTION_SYSTEM caps to ≤5 SIGNIFICANT, DURABLE, CONSOLIDATED patterns (not micro-
      observations);
  (3) write_traits treats same-key SIMILAR content (difflib ≥0.6) as a re-detection — no
      churn, just refresh last_detected_at; only clearly-different content supersedes;
  (4) PRUNE pass (sleep_pass, after detect): close INFERRED traits whose `last_detected_at`
      (the trait last_seen) is older than `trait_stale_days` (14). CONFIRMED traits are NEVER
      pruned — only user action closes those. `touch_trait` bumps last_detected_at on every
      re-detection (incl. of confirmed traits, as corroboration).
- Cross-key trait CONSOLIDATION (Phase 5.3): string-similarity cross-key dedup was tried
  and PROVEN UNSOUND first — calibrated on real dupes, char-similarity couldn't separate
  semantic dupes (the clearest pair scored 0.46) from genuinely-distinct traits (0.39), and
  reworded dupes scored as low as 0.11. So dedup is SEMANTIC via the cheap model: a sleep
  pass step after detect() (CONSOLIDATE_SYSTEM) groups reworded same-meaning INFERRED traits
  under different keys; GraphMemory.consolidate_traits keeps the highest-confidence member
  and CLOSES the rest (close_node — auditable, not delete), bumping the kept one's
  last_detected_at. CONFIRMED traits are filtered out (never merged). Conservative prompt
  ("only true duplicates; when unsure keep separate"). Verified live: Sara 11→4, Maya 11→5,
  dupes land in the closed set. Order: detect → consolidate → prune → tick.
- Reflect-back CADENCE cooldown (Phase 5.2): it was firing every session (felt like an
  interview). After one fires, set `reflect_back_cooldown_sessions` (3) in the Postgres
  `reflect_back_cooldown` table; end_session decrements it (but NOT the firing session, so
  the full next N are skipped); _maybe_reflect_back gates on `reflect_back_ready`. Durable /
  cross-process (each CLI session is a new process). Erased by delete().
- Commitment pile-up fix (Phase 5.4, found via synthetic run — Sara held 19 pending, of which
  ONE intent ('start therapy') had fragmented across 7 keys: seek_therapy, seeking_therapy,
  start_therapy, therapy_intake, therapy_engagement, therapy-engagement,
  seek_professional_support; 'be honest with Jen' across ~6). Root cause was KEY DRIFT:
  EXTRACTION_SYSTEM's canonical-key list is for FACTS, so commitments got a fresh free-form
  slug every session and write_commitments' same-key soft-dedup could never match. Two-part
  fix, mirroring the trait slug-drift guard (detect() feeds current trait keys back):
  (1) PREVENTION — Extractor.run reads get_open_commitments(user_id) and transcript_for_extraction
      appends a 'Commitments already tracked (reuse the EXACT key for the same intent)' block;
      EXTRACTION_SYSTEM instructs reusing a tracked commitment's exact key for the same intent
      (incl. a further step toward the same goal). Verified live: the 7 therapy keys collapsed
      to 1 reused key (start_therapy) on the next run.
  (2) COLLAPSE — once keys are reliably reused, write_commitments merges on KEY ALONE; the old
      difflib ≥0.6 content gate was DROPPED for commitments (it blocked exactly the reworded
      restatements we want to merge — the live start_therapy pair scored 0.067, be_honest 0.522).
      Same char-similarity unsoundness already documented for traits (Phase 5.3). Keys are
      descriptive-per-intent now, so same key ⇒ same intent; a genuinely different commitment
      gets a different key. (_similar/0.6 is RETAINED — still used by the trait supersede path.)
  Tests: test_extraction.test_open_commitments_fed_back_and_restatement_merges,
  test_commitment_dedup.test_same_key_reworded_restatement_merges. SCOPED OUT (not built): a
  staleness/expiry sink for genuinely-distinct commitments the user never resolves — revisit
  only if open counts climb over long horizons. VERIFIED LIVE (7-day synthetic): ZERO same-key
  dup nodes; open counts Sara 20→3, Maya 15→7, David 16→5, James 3→1; mention_count on survivors
  (Maya's text-Carlos=5, glassblowing=5, sleep-checkpoint=4) confirms restatements collapsed
  into one node each, and the survivors are genuinely-distinct intents (no over-merge).
- Living profile (Step 1): a STRUCTURED behavioral layer alongside the free-form InferredTrait
  layer. Fixed taxonomy in `alik.profile.TAXONOMY` (detail_specificity, topic_focus,
  interest_intensity, structure_preference, sensory_sensitivity, social_predictability_need);
  add an axis/value/behavior-directive there and nothing else changes (detection prompt,
  validation, behavior all read the table). ProfileDimension lives in POSTGRES (one row per
  user+axis, behind the Memory ABC — NOT FalkorDB) so the Profile API and matching get reliable
  reads even when the graph is down, and delete() erases it. The nightly `profile_pass` (sleep
  step after tick) mirrors detect(): provenance-grounded, accumulates confidence via the pure
  `apply_observation` (same value → diminishing-returns bump; competing value → switch if more
  confident, else decay). CONFIRMED dimensions are corroborate-only (never clobbered, like
  write_traits' confirmed guard); CORRECTED are left untouched (no nagging). Soft-confirm is
  reflect-back's sibling for dimensions: a confident-enough UNCONFIRMED dimension is gently
  surfaced in conversation (REFLECT_PROFILE_CONFIRM_SYSTEM); the reply → CONFIRMED or CORRECTED.
  It SHARES reflect-back's cadence cooldown + the one-gentle-check-per-session guard (`_rb_done`)
  so it never feels like an interview; reflect-back is tried first, profile-confirm only if it
  doesn't fire. Behavior directives (alik.profile.behavior_directives) quietly adjust HOW the
  companion shows up — injected into the prompt for CONFIRMED dims always + UNCONFIRMED dims ≥
  profile_behavior_min_confidence — never said aloud, never a label.
- Profile API + cross-service: GET /users/{id}/profile assembles identity (fetched from the
  auth service via `auth_client`, graceful → None on failure) + facts + CONFIRMED traits +
  dimensions; this is the seam the matching service consumes (and guarded by the mesh service
  token — see below). DELETE /users/{id} is a cross-service coordinator: brain Memory.delete
  THEN auth `DELETE /internal/users/{id}` THEN matching `DELETE /users/{id}` — loud +
  idempotent. Auth gained service-token-guarded `/internal/profiles/{id}` + `/internal/users/{id}`.
  httpx is now a brain runtime dep.
- Service mesh (one shared secret): brain `ALIK_SERVICE_TOKEN` == auth `SERVICE_TOKEN` ==
  matching `SERVICE_TOKEN`. The brain sends it to auth + matching, and validates it on its own
  /users/{id}/profile (the guard is OPTIONAL — enforced only when a token is configured, so
  injected-test apps and tokenless local dev still work). Ports: brain 8000, auth 8001,
  matching 8002; matching's own Postgres on host 5433.
- Job-matching extraction (Step 2): `services/matching/` is a standalone FastAPI service (own
  Postgres, own venv, NO LLM — scoring is deterministic). It is a pure consumer of the living
  profile: `GET /match/{user_id}` reads the brain Profile API (facts + confirmed_traits), scores
  the catalog (`data/jobs.json`, moved out of the brain), and owns the recommendation lifecycle
  (log + cooldowns + one-open-thread + job_active) in its own store (`Store` ABC: `PgStore` +
  `InMemoryStore`). The brain keeps ONLY delivery glue: `sleep_pass.match_jobs/check_job_followups`
  ask the service and queue a `pending_checkin`; the companion delivers the opener, shares the
  link on "yes", classifies the follow-up reply and POSTs the outcome (the service flips
  job_active for liked/loved). The follow-through EmotionalSignal still lands in the BRAIN's
  pattern layer (engagement state lives in matching). `CheckinType.JOB_*` + `JobOutcome` +
  `JOB_OUTCOME_CLASSIFY_SYSTEM` stay in the brain (delivery/classification); `JobRecommendation`,
  `job_matcher.py`, the job memory methods, the job Postgres tables, and `data/jobs.json` were
  REMOVED from the brain. Matching is disabled cleanly when `matching_service_url` is empty.