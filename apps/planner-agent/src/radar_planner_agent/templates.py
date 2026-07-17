"""Investigation templates: a typed, validated model of the YAML ConfigMap.

The planner's one job. Given ``service_name`` and ``alert_name`` from the event,
find the investigation an engineer would run — or fall back to a generic one.

THE MATCH IS EXACT
------------------
The key is ``f"{service_name}:{alert_name}"``. No case folding, no prefix match,
no fuzzy fallback, no stripping. That is a deliberate refusal, not an omission: a
loose match that served the *wrong specific* template would be worse than no match
at all, because the engineer would follow a confident, plausible, and irrelevant
checklist. Better a generic plan that is honest about being generic.

THE FAILURE THIS MODULE IS BUILT AROUND
---------------------------------------
Because the match is exact, a mismatch is **silent**. Write a key with the wrong
casing or a trailing space and that alert quietly falls through to ``_default`` —
which produces a perfectly plausible plan. Nothing breaks. Nobody notices. Every
incident just gets a slightly-too-generic investigation forever.

Three guards, because one is not enough for a bug that hides this well:

1. **Keys are validated at startup.** Exactly one colon, no surrounding
   whitespace, both halves non-empty. A key that could never match anything is
   dead config, and the planner refuses to start rather than carry it. This is the
   one that catches the trailing space.
2. **Every fallback logs the key that missed** (in ``routes``, at WARNING), so the
   miss is greppable rather than invisible.
3. **A counter distinguishes matched from default** (in ``routes``), so "every
   alert is hitting ``_default``" is a line on a dashboard rather than something
   somebody has to notice.

``_default`` IS REQUIRED
------------------------
An alert nobody wrote a template for still gets a generic investigation. No
incident is ever left unplanned merely because the template library has a gap. A
missing ``_default`` is a boot failure — loud, at deploy time — rather than a
surprise at 3am when an unknown alert arrives and the planner has nothing to say.

``extra="forbid"`` throughout: a typo'd field is a startup failure, not a silently
ignored one. Unlike the gateway's token map this file holds no secrets, so errors
quote the path and the validation detail freely.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from radar_common import ConfigurationError

DEFAULT_KEY = "_default"
"""The template used when nothing matches. Required — see the module docstring."""

KEY_SEPARATOR = ":"
"""``service_name:alert_name``. Exactly one, and no whitespace around it."""


def template_key(service_name: str, alert_name: str) -> str:
    """Build the lookup key for an alert. EXACT — nothing is normalized here.

    This is the ONE place the key is constructed, and it is deliberately dumb: no
    strip, no lower, no substitution. If the watcher emits ``alert_name`` with a
    different casing than the YAML declares, the correct outcome is a visible
    fallback to ``_default`` — not a silent coercion that appears to work and
    serves an unrelated template on some other alert.
    """
    return f"{service_name}{KEY_SEPARATOR}{alert_name}"


class PlanStep(BaseModel):
    """One step of an investigation: what to check, and in what order."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    order: int = Field(gt=0)
    description: str = Field(min_length=1)


class InvestigationTemplate(BaseModel):
    """The steps to run for one kind of failure."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    steps: list[PlanStep] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_orders(self) -> InvestigationTemplate:
        # Two steps claiming order 3 make the plan's sequence ambiguous, and the
        # engineer reading the card cannot tell which comes first. Cheap to catch
        # here; impossible to notice in production.
        duplicates = sorted(
            order
            for order, count in Counter(s.order for s in self.steps).items()
            if count > 1
        )
        if duplicates:
            raise ValueError(
                f"steps have duplicate order values: {duplicates} — the "
                "investigation sequence would be ambiguous"
            )
        return self

    @property
    def ordered_steps(self) -> list[PlanStep]:
        """The steps, sorted by ``order``, as the plan will store them."""
        return sorted(self.steps, key=lambda s: s.order)


@dataclass(frozen=True, slots=True)
class TemplateMatch:
    """Which template served an alert, and whether it was the fallback."""

    key: str
    template: InvestigationTemplate
    is_default: bool


class PlanTemplates(BaseModel):
    """The whole ``templates:`` block, validated."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    templates: dict[str, InvestigationTemplate]

    @model_validator(mode="after")
    def _validate_keys(self) -> PlanTemplates:
        if DEFAULT_KEY not in self.templates:
            raise ValueError(
                f"no '{DEFAULT_KEY}' template — an alert with no matching template "
                "would have no investigation at all. It is required, so a gap in "
                "the template library is a generic plan rather than a stalled "
                "incident."
            )

        for key in self.templates:
            if key == DEFAULT_KEY:
                continue
            reason = _key_problem(key)
            if reason is not None:
                raise ValueError(
                    f"template key {key!r} {reason}. Keys are matched EXACTLY "
                    f"against '<service_name>{KEY_SEPARATOR}<alert_name>', so a key "
                    "that cannot be produced by any alert is dead config: it would "
                    "never match, and every alert it was meant for would silently "
                    f"fall through to '{DEFAULT_KEY}'."
                )
        return self

    def match(self, service_name: str, alert_name: str) -> TemplateMatch:
        """Find this alert's template, falling back to ``_default``.

        Never returns ``None``: ``_default`` is required at load, so there is
        always an investigation to run.
        """
        key = template_key(service_name, alert_name)
        found = self.templates.get(key)
        if found is not None:
            return TemplateMatch(key=key, template=found, is_default=False)
        return TemplateMatch(
            key=DEFAULT_KEY,
            template=self.templates[DEFAULT_KEY],
            is_default=True,
        )

    @property
    def keys(self) -> list[str]:
        """Sorted template keys — safe to log at startup (no secrets here)."""
        return sorted(self.templates)


def _key_problem(key: str) -> str | None:
    """Why ``key`` could never match a real alert, or ``None`` if it could."""
    if key != key.strip():
        # The one that would otherwise cost a day: a trailing space is invisible in
        # a diff, and the alert it was written for falls through to _default.
        return "has leading or trailing whitespace"
    parts = key.split(KEY_SEPARATOR)
    if len(parts) != 2:
        return f"must contain exactly one {KEY_SEPARATOR!r} (it has {len(parts) - 1})"
    service_name, alert_name = parts
    if not service_name or not alert_name:
        return "has an empty service_name or alert_name"
    if service_name != service_name.strip() or alert_name != alert_name.strip():
        return "has whitespace around the service name or alert name"
    return None


def load_plan_templates(path: Path) -> PlanTemplates:
    """Load and validate the investigation templates at ``path``.

    Raises :class:`~radar_common.ConfigurationError` — which keeps ``/readyz`` at
    503 — on a missing file, invalid YAML, or a document that fails validation.
    Startup does not proceed on a bad config: a planner running with templates
    nobody wrote is worse than a planner that will not start, because the first one
    looks fine and quietly hands every incident the same generic checklist.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigurationError(f"plan templates file not found: {path}") from None
    except yaml.YAMLError as exc:
        raise ConfigurationError(
            f"plan templates are not valid YAML: {path}: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise ConfigurationError(f"plan templates must be a YAML mapping: {path}")
    try:
        return PlanTemplates.model_validate(raw)
    except ValidationError as exc:
        raise ConfigurationError(f"plan templates invalid: {path}: {exc}") from exc
