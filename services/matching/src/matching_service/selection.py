"""The match-selection policy (moved from the brain's sleep-pass ``match_jobs``).

One open thread per user: skip while a recommendation is unresolved, or during a
post-outcome cooldown (disliked -> 14d, not_tried -> 7d). Never the same job twice; never a
disliked partner again. Reads the user's profile from the brain, scores the catalog, and —
on a hit — logs the recommendation and returns it. The brain's check-in queue (one opener
per user) is enforced on the brain side, not here.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from matching_service.catalog import Job
from matching_service.config import Settings
from matching_service.models import JobOutcome
from matching_service.scorer import match_jobs_for_user
from matching_service.store import Store


async def select_match(
    store: Store,
    profile: dict,
    catalog: Sequence[Job],
    user_id: str,
    settings: Settings,
) -> tuple[str, Job] | None:
    """Pick + log the next recommendation for the user, or None. Returns (rec_id, job)."""
    recs = await store.get_recommendations(user_id)  # newest first
    if any(r.outcome is None for r in recs):
        return None  # an open thread blocks new recommendations

    now = datetime.now(UTC)
    if recs:
        latest = recs[0]
        anchor = latest.follow_up_sent_at or latest.recommended_at
        if latest.outcome is JobOutcome.TRIED_DISLIKED and now - anchor < timedelta(
            days=settings.job_disliked_cooldown_days
        ):
            return None
        if latest.outcome is JobOutcome.NOT_TRIED and now - anchor < timedelta(
            days=settings.job_not_tried_cooldown_days
        ):
            return None

    disliked_job_ids = {r.job_id for r in recs if r.outcome is JobOutcome.TRIED_DISLIKED}
    by_id = {j.id: j for j in catalog}
    excluded_partners = {by_id[jid].partner for jid in disliked_job_ids if jid in by_id}

    already = await store.get_recommended_job_ids(user_id)
    job = match_jobs_for_user(
        profile.get("facts", []),
        profile.get("confirmed_traits", []),
        catalog,
        already,
        threshold=settings.job_match_threshold,
        excluded_partners=excluded_partners,
    )
    if job is None:
        return None

    rec_id = await store.log_recommendation(
        user_id, job.id, follow_up_after_days=settings.job_followup_after_days
    )
    return rec_id, job


def followup_outcome_side_effect(outcome: JobOutcome) -> bool:
    """Whether an outcome means the user engaged with paid work (sets job_active)."""
    return outcome in (JobOutcome.TRIED_LIKED, JobOutcome.LOVED_IT)
