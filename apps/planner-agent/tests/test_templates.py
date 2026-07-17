"""Template matching: the right plan, not merely a plan.

The bug this suite exists to catch is not "the planner crashed". It is **every
alert silently hitting ``_default``** — because the match is exact, a key with the
wrong casing or a trailing space never matches, and ``_default`` produces a
perfectly plausible investigation. Nothing breaks. Nobody notices. Every incident
just gets a slightly-too-generic plan, forever.

A test that asserts "a plan was produced" passes happily while that is happening.
So these assert on the STEP CONTENT — the specific checklist that specific alert
should have got — and separately assert that an *unknown* alert really does fall
back. Both halves are needed: the first alone would pass if everything matched
nothing and defaulted; the second alone would pass if everything matched
everything.

The other half of the defence is that the key the planner builds from the EVENT is
pinned against the key format the YAML declares. ``alert_name`` travels on the
event (the watcher puts it there precisely because ``incidents`` has no such
column), so a mismatch between how the watcher emits it and how the templates are
written would be exactly the silent failure above. The pin is
``test_the_key_built_from_a_real_event_matches_the_shipped_yaml``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from radar_common import ConfigurationError
from radar_contracts import PlanRequestedPayload
from radar_planner_agent.config import PlannerSettings
from radar_planner_agent.templates import (
    DEFAULT_KEY,
    PlanTemplates,
    load_plan_templates,
    template_key,
)

SHIPPED = Path("apps/planner-agent/config/plan-templates.yaml")

# The specific templates the shipped config declares. Asserted against their
# CONTENT below, so "it fell back to _default" cannot masquerade as a match.
ORDER_FAILURE = ("order-service", "OrderProcessingFailureRate")
CHECKOUT_TIMEOUT = ("checkout-service", "CheckoutTimeoutRate")
HIGH_MEMORY = ("order-service", "OrderServiceHighMemory")


def _write(tmp_path: Path, document: Any) -> Path:
    path = tmp_path / "plan-templates.yaml"
    path.write_text(yaml.safe_dump(document))
    return path


def _doc(templates: dict[str, Any]) -> dict[str, Any]:
    return {"templates": templates}


def _steps(*descriptions: str) -> dict[str, Any]:
    return {
        "steps": [
            {"order": i, "description": d} for i, d in enumerate(descriptions, start=1)
        ]
    }


# --- the file that actually ships ----------------------------------------------


def test_the_shipped_templates_file_loads() -> None:
    """A config that does not load is a pod that does not start."""
    templates = load_plan_templates(SHIPPED)

    assert DEFAULT_KEY in templates.templates
    assert templates.keys == [
        "_default",
        "checkout-service:CheckoutTimeoutRate",
        "order-service:OrderProcessingFailureRate",
        "order-service:OrderServiceHighMemory",
    ]


def test_the_settings_default_points_at_the_shipped_file() -> None:
    """The default path must be the file that exists, or a fresh box 503s."""
    assert PlannerSettings().plan_templates_path == SHIPPED
    assert SHIPPED.is_file()


# --- THE RIGHT plan, asserted on its content ------------------------------------


@pytest.mark.parametrize(
    ("service_name", "alert_name", "first_step_contains", "step_count"),
    [
        pytest.param(
            *ORDER_FAILURE,
            "kubectl rollout history deployment/order-service",
            5,
            id="order-failure",
        ),
        pytest.param(
            *CHECKOUT_TIMEOUT,
            "checkout-service pod resource usage",
            5,
            id="checkout-timeout",
        ),
        pytest.param(
            *HIGH_MEMORY,
            "order-service memory trend",
            4,
            id="high-memory",
        ),
    ],
)
def test_a_known_alert_gets_its_own_specific_template(
    service_name: str,
    alert_name: str,
    first_step_contains: str,
    step_count: int,
) -> None:
    """Asserted on STEP CONTENT, because "a plan exists" is true of _default too.

    If matching silently broke and everything fell through to the fallback, a test
    that only checked "we got a plan with some steps" would still pass — and the
    engineer would get a generic checklist for a specific, well-understood failure.
    """
    templates = load_plan_templates(SHIPPED)

    match = templates.match(service_name, alert_name)

    assert match.is_default is False, "this alert has its OWN template"
    assert match.key == f"{service_name}:{alert_name}"
    steps = match.template.ordered_steps
    assert len(steps) == step_count
    assert first_step_contains in steps[0].description
    assert [s.order for s in steps] == list(range(1, step_count + 1))


def test_the_three_templates_are_genuinely_different() -> None:
    """Guards the degenerate pass: three "matches" that are all the same plan.

    Every assertion above would still hold if match() returned the same template
    for everything. This is the test that says they are actually distinct.
    """
    templates = load_plan_templates(SHIPPED)

    plans = {
        templates.match(*ORDER_FAILURE).template.ordered_steps[0].description,
        templates.match(*CHECKOUT_TIMEOUT).template.ordered_steps[0].description,
        templates.match(*HIGH_MEMORY).template.ordered_steps[0].description,
        templates.match("unknown-service", "UnknownAlert")
        .template.ordered_steps[0]
        .description,
    }
    assert len(plans) == 4, "the templates must not all be the same plan"


# --- the _default fallback -------------------------------------------------------


def test_an_unknown_alert_falls_back_to_default() -> None:
    templates = load_plan_templates(SHIPPED)

    match = templates.match("payment-gateway", "SomethingNobodyPlannedFor")

    assert match.is_default is True
    assert match.key == DEFAULT_KEY
    assert "Check recent deployments for the affected service" in (
        match.template.ordered_steps[0].description
    )


@pytest.mark.parametrize(
    ("service_name", "alert_name"),
    [
        pytest.param("Order-Service", "OrderProcessingFailureRate", id="wrong-case"),
        pytest.param("order-service", "orderprocessingfailurerate", id="lower-alert"),
        pytest.param("order-service ", "OrderProcessingFailureRate", id="trailing-ws"),
        pytest.param("order-service", "OrderProcessingFailure", id="prefix-only"),
    ],
)
def test_matching_is_exact_and_never_guesses(
    service_name: str, alert_name: str
) -> None:
    """A near-miss falls back. It does NOT quietly serve the specific template.

    This is the refusal, stated as a test: no case folding, no stripping, no prefix
    matching. A loose match that served the wrong specific template would give an
    engineer a confident, plausible, irrelevant checklist — worse than a generic
    one that is honest about being generic.
    """
    templates = load_plan_templates(SHIPPED)

    match = templates.match(service_name, alert_name)

    assert match.is_default is True


# --- the key the PLANNER builds must match the key the YAML declares --------------


def test_the_key_built_from_a_real_event_matches_the_shipped_yaml() -> None:
    """THE pin: the event's fields, run through template_key, hit a real template.

    ``alert_name`` travels on incident.plan_requested (the watcher puts it there
    because ``incidents`` has no such column). If the watcher's spelling and the
    YAML's spelling ever diverge, EVERY alert falls silently through to _default
    and produces a plausible plan nobody questions.

    So this builds the key from a real ``PlanRequestedPayload`` — the exact contract
    the watcher emits — and asserts it lands on the specific template. It is the
    test that fails when someone renames an alert in one place and not the other.
    """
    payload = PlanRequestedPayload(
        incident_id=uuid4(),
        service_name="order-service",
        alert_name="OrderProcessingFailureRate",
    )
    templates = load_plan_templates(SHIPPED)

    key = template_key(payload.service_name, payload.alert_name)
    match = templates.match(payload.service_name, payload.alert_name)

    assert key == "order-service:OrderProcessingFailureRate"
    assert key in templates.templates, "the event's key must exist in the YAML"
    assert match.is_default is False
    assert match.key == key


def test_every_shipped_key_is_reachable_from_an_event() -> None:
    """No template in the file is unreachable dead config.

    Every non-default key must split back into a (service_name, alert_name) pair
    that, fed through the planner's own key builder, returns that same key. A key
    that fails this could never be produced by any alert — it would sit in the file
    looking like coverage while contributing nothing.
    """
    templates = load_plan_templates(SHIPPED)

    for key in templates.keys:
        if key == DEFAULT_KEY:
            continue
        service_name, alert_name = key.split(":")
        assert template_key(service_name, alert_name) == key
        assert templates.match(service_name, alert_name).is_default is False


# --- refusing bad config ----------------------------------------------------------


def test_a_missing_default_template_is_refused(tmp_path: Path) -> None:
    """Boot failure, not a 3am surprise.

    Without _default, the first alert nobody wrote a template for has no
    investigation at all. Better to fail at deploy than to stall an incident later.
    """
    path = _write(
        tmp_path,
        _doc({"order-service:OrderProcessingFailureRate": _steps("check things")}),
    )

    with pytest.raises(ConfigurationError, match=DEFAULT_KEY):
        load_plan_templates(path)


@pytest.mark.parametrize(
    ("key", "why"),
    [
        pytest.param("order-service", "no-colon", id="no-colon"),
        pytest.param("a:b:c", "two-colons", id="two-colons"),
        pytest.param(":OrderFailure", "empty-service", id="empty-service"),
        pytest.param("order-service:", "empty-alert", id="empty-alert"),
        pytest.param("order-service: OrderFailure", "inner-space", id="inner-space"),
    ],
)
def test_a_key_that_could_never_match_is_refused(
    tmp_path: Path, key: str, why: str
) -> None:
    """Dead config, refused at startup.

    A key no alert can produce is not a harmless typo: the alert it was written for
    falls through to _default, and the file goes on *looking* like that alert is
    covered. That is the silent failure this whole module is built around, and this
    is where it gets caught.
    """
    path = _write(tmp_path, _doc({key: _steps("x"), DEFAULT_KEY: _steps("y")}))

    with pytest.raises(ConfigurationError):
        load_plan_templates(path)


def test_a_key_with_trailing_whitespace_is_refused(tmp_path: Path) -> None:
    """The one that would otherwise cost a day: invisible in a diff, silent at run."""
    path = _write(
        tmp_path,
        _doc(
            {
                "order-service:OrderProcessingFailureRate ": _steps("x"),
                DEFAULT_KEY: _steps("y"),
            }
        ),
    )

    with pytest.raises(ConfigurationError, match="whitespace"):
        load_plan_templates(path)


def test_duplicate_step_orders_are_refused(tmp_path: Path) -> None:
    """Two steps claiming order 3 make the investigation sequence ambiguous."""
    path = _write(
        tmp_path,
        _doc(
            {
                DEFAULT_KEY: {
                    "steps": [
                        {"order": 1, "description": "first"},
                        {"order": 1, "description": "also first?"},
                    ]
                }
            }
        ),
    )

    with pytest.raises(ConfigurationError):
        load_plan_templates(path)


def test_a_template_with_no_steps_is_refused(tmp_path: Path) -> None:
    """An empty investigation is not an investigation."""
    path = _write(tmp_path, _doc({DEFAULT_KEY: {"steps": []}}))

    with pytest.raises(ConfigurationError):
        load_plan_templates(path)


def test_an_unknown_field_is_refused(tmp_path: Path) -> None:
    """A typo'd field must not be silently dropped."""
    path = _write(
        tmp_path,
        _doc(
            {
                DEFAULT_KEY: {
                    "steps": [{"order": 1, "description": "x", "sevrity": "high"}]
                }
            }
        ),
    )

    with pytest.raises(ConfigurationError):
        load_plan_templates(path)


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="not found"):
        load_plan_templates(tmp_path / "nope.yaml")


def test_invalid_yaml_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "plan-templates.yaml"
    path.write_text("templates: [unclosed\n")

    with pytest.raises(ConfigurationError, match="not valid YAML"):
        load_plan_templates(path)


def test_steps_are_returned_in_order_regardless_of_file_order(
    tmp_path: Path,
) -> None:
    """The YAML may list steps out of order; the plan must not be."""
    path = _write(
        tmp_path,
        _doc(
            {
                DEFAULT_KEY: {
                    "steps": [
                        {"order": 3, "description": "third"},
                        {"order": 1, "description": "first"},
                        {"order": 2, "description": "second"},
                    ]
                }
            }
        ),
    )

    templates: PlanTemplates = load_plan_templates(path)

    steps = templates.match("any", "thing").template.ordered_steps
    assert [s.description for s in steps] == ["first", "second", "third"]
