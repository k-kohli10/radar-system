"""The query assembly is a string join, which is why it is tested.

A join that drops a component does not raise, does not log, and does not fail a
type check. It just makes retrieval quietly worse — the exact failure mode this
service has had to design against repeatedly.
"""

from __future__ import annotations

import pytest
from radar_contracts import PlanStep
from radar_knowledge_service.query import build_query


def test_every_component_reaches_the_query() -> None:
    """The whole point: nothing is silently dropped.

    Asserting the exact string rather than substrings, because `in` checks pass
    for a query that also contains a duplicated or mangled component.
    """
    query = build_query(
        service_name="payment-gateway",
        alert_name="PaymentGatewayErrorRate",
        investigation_steps=[
            PlanStep(order=1, description="Check recent deployments"),
            PlanStep(order=2, description="Inspect upstream latency"),
        ],
    )

    assert query == (
        "payment-gateway PaymentGatewayErrorRate "
        "Check recent deployments Inspect upstream latency"
    )


def test_identifiers_come_first() -> None:
    """They must survive truncation against a model's input budget.

    Losing the tail of the investigation steps degrades the query; losing the
    service or alert changes what is being asked.
    """
    query = build_query(
        service_name="order-service",
        alert_name="OrderServiceHighMemory",
        investigation_steps=[PlanStep(order=1, description="Check heap usage")],
    )

    assert query.startswith("order-service OrderServiceHighMemory")


def test_steps_are_sorted_by_order_not_by_arrival() -> None:
    """Steps cross a service boundary as JSON.

    A query whose meaning depends on incidental list order is not reproducible,
    and two identical incidents would embed to different vectors.
    """
    steps = [
        PlanStep(order=3, description="third"),
        PlanStep(order=1, description="first"),
        PlanStep(order=2, description="second"),
    ]

    query = build_query(
        service_name="svc", alert_name="Alert", investigation_steps=steps
    )

    assert query == "svc Alert first second third"


def test_retrieval_works_before_a_plan_exists() -> None:
    """Steps are optional: retrieval must not depend on the planner having run."""
    assert build_query(service_name="svc", alert_name="Alert") == "svc Alert"


def test_blank_step_descriptions_do_not_leave_double_spaces() -> None:
    """A blank step must vanish, not become whitespace.

    Double spaces change the BM25 token stream's offsets and make the assembled
    string differ from the visually identical one, which is enough to embed
    differently.
    """
    steps = [
        PlanStep(order=1, description="   "),
        PlanStep(order=2, description="real step"),
    ]

    assert (
        build_query(service_name="svc", alert_name="Alert", investigation_steps=steps)
        == "svc Alert real step"
    )


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_service_name_is_refused(blank: str) -> None:
    """The pre-filter is built around this term.

    Without it the query retrieves plausible chunks for the wrong service — a
    wrong answer rather than a visible failure.
    """
    with pytest.raises(ValueError, match="service_name is required"):
        build_query(service_name=blank, alert_name="Alert")


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_alert_name_is_refused(blank: str) -> None:
    with pytest.raises(ValueError, match="alert_name is required"):
        build_query(service_name="svc", alert_name=blank)
