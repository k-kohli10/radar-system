"""Every alert rule must reference a metric the simulator actually exposes.

The rules in ``deploy/prometheus/alerting-rules.yml`` and the metrics in
``metrics.py`` are joined only by a string. Nothing in Python imports the rule
file, and Prometheus does not complain about a rule over a metric that does not
exist — such a rule simply never fires. So a typo or a one-sided rename produces
an alert that is silently unfirable and looks completely healthy: the rule is
present, the metric is present, and no test fails.

This is the join. It reads the rule expressions, extracts the metric names, and
checks each one against a live ``/metrics`` scrape.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry
from radar_platform_sim.main import create_app

RULES_FILE = (
    Path(__file__).resolve().parents[3] / "deploy" / "prometheus" / "alerting-rules.yml"
)

#: PromQL functions that appear in expressions and are not metric names.
_FUNCTIONS = {"rate", "irate", "increase", "histogram_quantile", "sum", "avg", "max"}


def _rules() -> list[dict[str, Any]]:
    parsed = yaml.safe_load(RULES_FILE.read_text())
    return [rule for group in parsed["groups"] for rule in group["rules"]]


def _metric_names(expr: str) -> set[str]:
    """Metric names referenced by a rule expression."""
    return {
        name
        for name in re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\b", expr)
        if name not in _FUNCTIONS and not name.replace(".", "").isdigit()
    }


def _exposed() -> set[str]:
    client = TestClient(create_app(metrics_registry=CollectorRegistry()))
    body = client.get("/metrics").text
    return set(re.findall(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{|\s)", body, re.MULTILINE))


@pytest.mark.parametrize("rule", _rules(), ids=lambda r: str(r["alert"]))
def test_rule_references_a_metric_the_simulator_exposes(rule: dict[str, Any]) -> None:
    exposed = _exposed()

    for metric in _metric_names(rule["expr"]):
        assert metric in exposed, (
            f"{rule['alert']} watches {metric!r}, which the simulator does not "
            f"expose. The rule can never fire. Either the metric was renamed on "
            f"one side only, or the scenario behind it was never built."
        )


def test_every_rule_carries_a_service_and_a_severity_label() -> None:
    """Ingestion reads both off the labels; a rule missing either is a 422 at 3am."""
    for rule in _rules():
        assert "service" in rule["labels"], f"{rule['alert']} has no service label"
        assert "severity" in rule["labels"], f"{rule['alert']} has no severity label"


def test_the_rules_cover_every_chaos_driven_metric() -> None:
    """The reverse direction: a scenario nobody alerts on cannot reach RADAR.

    A chaos endpoint whose metric no rule watches is dead weight — it can be
    spiked, but nothing will ever page on it, so it can never produce an incident.
    """
    watched = {m for rule in _rules() for m in _metric_names(rule["expr"])}

    chaos_driven = {
        "order_processing_failure_rate",
        "checkout_timeout_rate",
        "payment_gateway_error_rate",
        "payment_declines_total",
        "inventory_check_p95_seconds",
        "order_service_memory_bytes",
    }

    unwatched = chaos_driven - watched
    assert not unwatched, (
        f"these chaos-driven metrics have no alert rule, so spiking them can "
        f"never produce an incident: {sorted(unwatched)}"
    )
