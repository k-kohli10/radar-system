"""RADAR's own history as reasoning signal: a base-rate prior and a feedback loop.

Lever 3. Until now the reasoner looked at one incident in isolation — the runbooks
plus the firing alert — and never at what happened the last time THIS alert fired,
even though every past recommendation and every 👍/👎 is sitting in Postgres. Two
signals close that gap, both keyed on the incident's fingerprint (service + alert +
severity), both folded into the prompt-facing :class:`ContextBundle`:

- **Historical-cause prior** — of the prior real recommendations for this
  fingerprint, how did their root causes break down by category ("last N: deploy
  ×3, dependency ×1")? A model told "this alert has been a deploy three of the last
  four times" reasons with a real base rate instead of guessing, and can earn
  confidence it otherwise could not.

- **Past feedback** — the root causes engineers marked 👍 (confirmed), the
  corrections they wrote (📝), and how many they marked 👎 (down-weight). This is
  the roadmap's correction-gated re-reason: a cause a human already accepted for
  this exact alert is worth more than a fresh guess, and one they rejected is worth
  less.

Fallback recommendations are excluded from both — their root_cause is "AI analysis
unavailable", which is the absence of a cause, not a cause to count or confirm.

THE CAUSE CLASSIFIER IS A HEURISTIC, AND SAYS SO
------------------------------------------------
Root causes are free text; the base rate needs categories. There is no cause-type
column and no LLM call here (the reasoner must not make a second paid call to
summarize its own history), so causes are bucketed by keyword. It is deliberately
coarse and deterministic: the prior is a HINT the model weighs, not a verdict it
obeys, so a miscategorized cause costs a nudge, not a wrong RCA. First matching
category wins, in the fixed priority order below; anything unmatched is ``other``.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from radar_database import Feedback, Incident, Recommendation
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

#: How many prior recommendations feed the base rate, most recent first. Bounded so
#: a long-lived fingerprint does not grow the prompt without limit.
PRIOR_LIMIT = 20

#: How many confirmed causes / corrections are surfaced. A handful is grounding; a
#: wall of them is noise the model has to wade through.
FEEDBACK_LIMIT = 5

SENTIMENT_HELPFUL = "helpful"
SENTIMENT_NOT_HELPFUL = "not_helpful"

OTHER_CATEGORY = "other"

#: Cause categories and the substrings that assign a root cause to one, in priority
#: order (first match wins). Coarse on purpose — see the module docstring. Ordered so
#: the more specific, more actionable causes win a tie: a "deploy that broke a
#: database dependency" is filed as a deploy, the thing to roll back.
CAUSE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "deployment",
        (
            "deploy",
            "rollout",
            "roll out",
            "rollback",
            "release",
            "version ",
            "canary",
            "regression",
            "new build",
            "bad push",
        ),
    ),
    (
        "dependency",
        (
            "dependency",
            "upstream",
            "downstream",
            "third-party",
            "third party",
            "external service",
            "payment gateway",
            "provider outage",
            "api call",
        ),
    ),
    (
        "database",
        (
            "database",
            "connection pool",
            "deadlock",
            "replication",
            "slow query",
            "postgres",
            "sql",
            "db ",
        ),
    ),
    (
        "resource",
        (
            "memory",
            "cpu",
            "oom",
            "out of memory",
            "disk",
            "saturat",
            "exhaust",
            "leak",
            "capacity",
            "throttl",
            "resource",
        ),
    ),
    (
        "configuration",
        (
            "config",
            "misconfig",
            "feature flag",
            "env var",
            "environment variable",
            "certificate",
            "expired",
            "secret",
        ),
    ),
    (
        "network",
        ("network", "dns", "connectivity", "packet loss", "tls handshake"),
    ),
    (
        "traffic",
        ("traffic", "load spike", "surge", "spike in", "rate limit", "overload"),
    ),
)


def classify_cause(root_cause: str) -> str:
    """Bucket one root-cause string into a cause category. First match wins.

    A heuristic — see the module docstring. Returns ``other`` when nothing matches,
    which is a real category (an unclassifiable cause), not an error.
    """
    text = root_cause.lower()
    for category, keywords in CAUSE_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return category
    return OTHER_CATEGORY


class HistoricalPrior(BaseModel):
    """The base rate of prior root causes for incidents with this fingerprint.

    Always present on the bundle (``total=0`` when this fingerprint is new), because
    "this has never fired before" is itself information the model should reason with.
    """

    model_config = ConfigDict(extra="forbid")

    #: How many prior real (non-fallback) recommendations the counts summarize.
    total: int = 0
    #: Cause category -> occurrences, e.g. ``{"deployment": 3, "dependency": 1}``.
    #: Only categories that actually occurred appear; empty when ``total`` is 0.
    category_counts: dict[str, int] = Field(default_factory=dict)


class PastFeedback(BaseModel):
    """What engineers said about prior RCAs for this fingerprint.

    The feedback loop: confirmed causes are worth more, rejected ones less. Always
    present (empty when there is no feedback yet)."""

    model_config = ConfigDict(extra="forbid")

    #: Root causes of prior recommendations an engineer marked 👍 helpful.
    confirmed_causes: list[str] = Field(default_factory=list)
    #: Free-text corrections engineers supplied (📝). Dormant until the correction
    #: modal ships (Phase 9 deferral), but read here so it lights up when it does.
    corrections: list[str] = Field(default_factory=list)
    #: How many prior recommendations for this fingerprint were marked 👎.
    unhelpful_count: int = 0


async def build_history(
    session: AsyncSession, *, fingerprint: str, exclude_incident_id: UUID
) -> tuple[HistoricalPrior, PastFeedback]:
    """Summarize prior recommendations and feedback for ``fingerprint``.

    Excludes the current incident (``exclude_incident_id``) and all fallback
    recommendations. Read-only; runs on the caller's session inside the reasoner's
    first transaction, before the LLM call.
    """
    prior = await _historical_prior(
        session, fingerprint=fingerprint, exclude_incident_id=exclude_incident_id
    )
    feedback = await _past_feedback(
        session, fingerprint=fingerprint, exclude_incident_id=exclude_incident_id
    )
    return prior, feedback


async def _historical_prior(
    session: AsyncSession, *, fingerprint: str, exclude_incident_id: UUID
) -> HistoricalPrior:
    root_causes = (
        (
            await session.execute(
                select(Recommendation.root_cause)
                .join(Incident, Recommendation.incident_id == Incident.id)
                .where(
                    Incident.fingerprint == fingerprint,
                    Incident.id != exclude_incident_id,
                    Recommendation.is_fallback.is_(False),
                )
                .order_by(Recommendation.created_at.desc())
                .limit(PRIOR_LIMIT)
            )
        )
        .scalars()
        .all()
    )

    counts: dict[str, int] = {}
    for root_cause in root_causes:
        category = classify_cause(root_cause)
        counts[category] = counts.get(category, 0) + 1
    return HistoricalPrior(total=len(root_causes), category_counts=counts)


async def _past_feedback(
    session: AsyncSession, *, fingerprint: str, exclude_incident_id: UUID
) -> PastFeedback:
    rows = (
        await session.execute(
            select(
                Feedback.sentiment,
                Feedback.correction_text,
                Recommendation.root_cause,
            )
            .join(Recommendation, Feedback.recommendation_id == Recommendation.id)
            .join(Incident, Recommendation.incident_id == Incident.id)
            .where(
                Incident.fingerprint == fingerprint,
                Incident.id != exclude_incident_id,
                Recommendation.is_fallback.is_(False),
            )
            .order_by(Feedback.created_at.desc())
        )
    ).all()

    confirmed: list[str] = []
    corrections: list[str] = []
    unhelpful = 0
    for sentiment, correction_text, root_cause in rows:
        if sentiment == SENTIMENT_HELPFUL and root_cause not in confirmed:
            confirmed.append(root_cause)
        elif sentiment == SENTIMENT_NOT_HELPFUL:
            unhelpful += 1
        if correction_text and correction_text not in corrections:
            corrections.append(correction_text)
    return PastFeedback(
        confirmed_causes=confirmed[:FEEDBACK_LIMIT],
        corrections=corrections[:FEEDBACK_LIMIT],
        unhelpful_count=unhelpful,
    )
