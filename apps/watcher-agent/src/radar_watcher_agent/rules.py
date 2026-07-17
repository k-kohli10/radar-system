"""The correlation rules: a typed, validated model of the YAML ConfigMap.

Correlation policy is configuration, not code — a window or a cooldown changes with
a ConfigMap edit and a restart, never a deploy. What this module guarantees is that
a *wrong* edit is loud: the file is validated on startup, and an invalid one leaves
``/readyz`` at 503 rather than silently falling back to defaults. ``extra="forbid"``
throughout is the load-bearing part of that — ``suppresion:`` (one 's') would
otherwise be silently ignored, and suppression would appear to be configured while
doing nothing at all. That is precisely the class of bug that config-driven behaviour
invites, and the only defence is to refuse unknown keys.

Unlike the gateway's token map, this file holds NO secrets, so error messages here
quote the offending path and the validation detail freely. That is deliberate and
worth contrasting: the gateway's loader must never echo its parse errors, because a
YAML error quotes the source line and the source line is a credential.

WHAT IS LIVE, AND WHAT IS INERT
-------------------------------
Live, and enforced by later commits in this phase:

- ``suppression`` — a new incident too soon after the previous one of the same
  (service, alert) gets no investigation plan.
- ``escalation`` — a burst of alerts on one incident raises its severity.

Parsed and validated, but applied by nothing (docs/adr/0013):

- ``default_window_minutes`` / ``window_overrides``
- ``service_groups``
- ``fingerprint_fields``

They are inert because **ingestion decides which incident an alert lands on** — it
dedups on the fingerprint inside its own window, and by the time the watcher sees the
event the incident already exists. The watcher cannot un-create it, so a window it
disagreed with would be a claim it cannot honour. Rather than delete the config (and
lose the plan's intent) or pretend it works, the fields are kept, validated, and
pinned inert by a test.

``fingerprint_fields`` gets stronger treatment than "ignored": it is enforced as a
DECLARATION. The field list lives once, in :data:`radar_contracts.FINGERPRINT_FIELDS`
— ingestion *builds* its digest from it, and this loader refuses to start if the YAML
no longer declares the same list in the same order. So there is no second copy to go
stale, and the one field most likely to be edited in the belief that it changes
behaviour cannot silently diverge from what the system actually hashes: reorder the
contract and ingestion's fingerprints change *and* this config stops loading, in the
same commit.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from radar_common import ConfigurationError
from radar_contracts import FINGERPRINT_FIELDS, Severity


class WindowOverride(BaseModel):
    """A per-alert correlation window. DEFERRED — applied by nothing (ADR 0013)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    alert_name: str = Field(min_length=1)
    window_minutes: int = Field(gt=0)


class ServiceGroup(BaseModel):
    """A stack correlated as one incident. DEFERRED — applied by nothing (ADR 0013)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    services: list[str] = Field(min_length=1)
    group_as_single_incident: bool = True


class SuppressionRule(BaseModel):
    """LIVE. Withhold the investigation plan for a too-soon repeat of an alert.

    ``suppress_follow_on_minutes`` is measured from the *previous* incident's
    ``opened_at`` for the same ``(service_name, alert_name)``. A cooldown shorter
    than ingestion's 5-minute dedup window can never fire: an alert arriving that
    soon attaches to the still-open incident rather than opening a new one, and a
    suppression rule only ever sees new incidents. That is a configuration mistake
    worth catching, but it is not an error — a 5-minute cooldown is exactly the
    boundary — so it is left to the operator and documented in the YAML.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    alert_name: str = Field(min_length=1)
    suppress_follow_on_minutes: int = Field(gt=0)


class EscalationRule(BaseModel):
    """LIVE. Raise an incident's severity when alerts arrive fast enough.

    ``alert_count_threshold`` alerts attached to the incident within
    ``within_minutes`` raises it to ``escalate_to``. The count is over alerts by
    arrival time, NOT the incident's running ``alert_count`` — an untimed counter
    would make ``within_minutes`` decorative, and 3 alerts over three hours would
    escalate exactly like 3 in ten seconds.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    alert_count_threshold: int = Field(gt=1)
    within_minutes: int = Field(gt=0)
    escalate_to: Severity


class CorrelationRules(BaseModel):
    """The whole ``correlation:`` block, validated."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    default_window_minutes: int = Field(gt=0)
    window_overrides: list[WindowOverride] = Field(default_factory=list)
    service_groups: list[ServiceGroup] = Field(default_factory=list)
    suppression: list[SuppressionRule] = Field(default_factory=list)
    escalation: list[EscalationRule] = Field(default_factory=list)
    fingerprint_fields: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate(self) -> CorrelationRules:
        # A second rule for the same alert would be silently unreachable — the
        # lookup returns the first match. Refuse, rather than let an operator edit a
        # rule that never applies and conclude the feature is broken.
        self._reject_duplicates([r.alert_name for r in self.suppression], "suppression")
        self._reject_duplicates(
            [w.alert_name for w in self.window_overrides], "window_overrides"
        )

        # fingerprint_fields is a declaration of what ingestion hashes, not a knob.
        # If it no longer says what ingestion actually does, the two have drifted and
        # every correlation decision downstream is built on a false premise.
        if tuple(self.fingerprint_fields) != FINGERPRINT_FIELDS:
            expected = ", ".join(FINGERPRINT_FIELDS)
            got = ", ".join(self.fingerprint_fields) or "(empty)"
            raise ValueError(
                f"fingerprint_fields must be exactly [{expected}] in that order — "
                f"it declares what ingestion hashes, and the watcher trusts that "
                f"fingerprint rather than recomputing it. Got: [{got}]. Changing "
                f"this list does not change the fingerprint; changing ingestion's "
                f"compute_fingerprint does."
            )
        return self

    @staticmethod
    def _reject_duplicates(names: list[str], where: str) -> None:
        duplicates = sorted(n for n, count in Counter(names).items() if count > 1)
        if duplicates:
            raise ValueError(
                f"{where} has more than one rule for: {', '.join(duplicates)} — "
                "only the first would ever apply"
            )

    def suppression_for(self, alert_name: str) -> SuppressionRule | None:
        """The suppression rule for ``alert_name``, or ``None`` if it has none."""
        for rule in self.suppression:
            if rule.alert_name == alert_name:
                return rule
        return None


class RulesDocument(BaseModel):
    """The YAML file's top level: a single ``correlation:`` mapping."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    correlation: CorrelationRules


def load_correlation_rules(path: Path) -> CorrelationRules:
    """Load and validate the correlation rules at ``path``.

    Raises :class:`~radar_common.ConfigurationError` — which keeps ``/readyz`` at
    503 — on a missing file, invalid YAML, or a document that fails validation.
    Startup does not proceed on a bad config: a watcher running with defaults it was
    never configured with is worse than a watcher that will not start, because the
    first one looks fine.

    The messages quote the path and the validation detail. Safe here, unlike the
    gateway's token map: this file contains no secrets.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigurationError(f"correlation rules file not found: {path}") from None
    except yaml.YAMLError as exc:
        raise ConfigurationError(
            f"correlation rules are not valid YAML: {path}: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise ConfigurationError(f"correlation rules must be a YAML mapping: {path}")
    try:
        return RulesDocument.model_validate(raw).correlation
    except ValidationError as exc:
        raise ConfigurationError(f"correlation rules invalid: {path}: {exc}") from exc
