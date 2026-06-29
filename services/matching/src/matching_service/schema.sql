-- One row per job recommendation delivered to a user. Lifecycle is one OPEN thread per
-- user — a row with outcome IS NULL blocks new recommendations until it resolves.
-- follow_up_after schedules the 3-day check-back; follow_up_sent_at marks the follow-up as
-- queued; outcome is set from the user's follow-up reply.
CREATE TABLE IF NOT EXISTS job_recommendations_log (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           text        NOT NULL,
    job_id            text        NOT NULL,
    recommended_at    timestamptz NOT NULL DEFAULT now(),
    delivered_at      timestamptz,
    follow_up_after   timestamptz,
    follow_up_sent_at timestamptz,
    outcome           text
);

CREATE INDEX IF NOT EXISTS idx_jobrec_user_job ON job_recommendations_log (user_id, job_id);
CREATE INDEX IF NOT EXISTS idx_jobrec_user_recommended
    ON job_recommendations_log (user_id, recommended_at DESC);

-- Per-user engagement flag: true once a user tries a recommendation and likes it.
CREATE TABLE IF NOT EXISTS user_job_state (
    user_id    text PRIMARY KEY,
    job_active boolean NOT NULL DEFAULT false
);
