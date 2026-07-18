"""E4: a chaos spike drives a real alert to a real incident.

The done-condition for the platform-sim series, and the path Phase 8 will trigger
retrieval on. Everything here is real except the Prometheus process itself:

- **real** platform-sim app: the chaos endpoint is POSTed and ``/metrics`` scraped;
- **real** alert rules: the threshold and labels are read out of
  ``deploy/prometheus/alerting-rules.yml``, never hardcoded here;
- **real** ingestion: the Prometheus normalizer, the webhook token check, the
  fingerprint, the 5-minute dedup window, and the incident INSERT;
- **real** Postgres, because dedup is a database guarantee.

WHY NOT /alerts/mock
--------------------
The mock endpoint takes an already-normalized body, so it would prove nothing about
whether an alertmanager-shaped payload — nested ``labels``/``annotations``,
``startsAt``, ``status`` — normalizes into the right incident. That translation IS the
under test, so this goes through ``/alerts/prometheus`` with a Prometheus-shaped body
and its own per-source token.

WHY THE PROMETHEUS PROCESS IS SUBSTITUTED
-----------------------------------------
Standing up Prometheus would add ~90s and a Docker dependency to the default suite for
one link in the chain: comparing a scraped sample against a threshold. That link is
evaluated here directly, from the rule's own expression, and the real scrape -> rule ->
alertmanager -> webhook path is proven ONCE in the opt-in
``test_real_prometheus_alert_path`` (``pytest -m infra``). The default suite must not
depend on Docker; Phase 10 owns the production wiring.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from prometheus_client import CollectorRegistry
from radar_database import Database
from radar_ingestion.normalizer import compute_fingerprint
from radar_platform_sim.main import create_app as create_sim_app
from sqlalchemy import func, select
from starlette.testclient import TestClient

from tests.e2e.harness import MOCK_ALERT, Pipeline

RULES_PATH = Path(__file__).resolve().parents[2] / "deploy" / "prometheus"
RULES_FILE = RULES_PATH / "alerting-rules.yml"

#: The scenario driven end to end. Chosen because its fingerprint is the one the rest of
#: the e2e suite already uses (see the identity-agreement assertion below), so this test
#: proves the crafted path lands on the SAME incident identity the mock path does.
SCENARIO_ALERT = "OrderProcessingFailureRate"
SCENARIO_ENDPOINT = "/chaos/order-failures"
SCENARIO_SPIKE = {"rate": 0.15, "duration_seconds": 120}


def _rules() -> list[dict[str, Any]]:
    parsed = yaml.safe_load(RULES_FILE.read_text())
    return [rule for group in parsed["groups"] for rule in group["rules"]]


def _rule(alertname: str) -> dict[str, Any]:
    for rule in _rules():
        if rule["alert"] == alertname:
            return rule
    raise AssertionError(f"{alertname} is not declared in {RULES_FILE}")


def _threshold(expr: str) -> tuple[str, float]:
    """Split an instant-vector rule expression into its metric and its threshold."""
    match = re.fullmatch(r"\s*(\w+)\s*>\s*([0-9.e+-]+)\s*", expr)
    assert match is not None, f"not a simple instant-vector expression: {expr!r}"
    return match.group(1), float(match.group(2))


def _scrape(client: TestClient, metric: str) -> float:
    body = client.get("/metrics").text
    match = re.search(rf"^{re.escape(metric)} (\S+)$", body, re.MULTILINE)
    assert match is not None, f"{metric} absent from /metrics"
    return float(match.group(1))


def _alertmanager_body(rule: dict[str, Any]) -> dict[str, Any]:
    """The webhook body alertmanager posts for ``rule``, fanned out to one alert.

    Built from the rule's own labels and annotations, so a change to the rule file
    changes what this test sends — the rule stays the single source of truth.
    """
    return {
        "status": "firing",
        "startsAt": "2026-07-18T10:00:00.000Z",
        "generatorURL": "http://prometheus:9090/graph",
        "fingerprint": "f" * 16,
        "labels": {"alertname": rule["alert"], **rule["labels"]},
        "annotations": rule["annotations"],
    }


async def _incident_count(db: Database, fingerprint: str) -> int:
    from radar_database import Incident

    async with db.session() as session:
        result = await session.execute(
            select(func.count())
            .select_from(Incident)
            .where(Incident.fingerprint == fingerprint)
        )
        return int(result.scalar_one())


async def test_chaos_spike_drives_a_prometheus_alert_to_an_incident(
    pipeline: Pipeline, db: Database
) -> None:
    """The full chain: chaos -> metric breaches the real rule -> incident."""
    rule = _rule(SCENARIO_ALERT)
    metric, threshold = _threshold(rule["expr"])

    sim = TestClient(create_sim_app(metrics_registry=CollectorRegistry()))

    # 1. at rest the metric sits below its threshold, so nothing would fire
    assert _scrape(sim, metric) <= threshold

    # 2. chaos spikes it
    sim.post(SCENARIO_ENDPOINT, json=SCENARIO_SPIKE)
    spiked = _scrape(sim, metric)

    # 3. the rule's own expression now breaches — this is the link a running
    #    Prometheus would evaluate, checked against the rule as written on disk
    assert spiked > threshold, (
        f"{metric} spiked to {spiked}, which does not breach {rule['expr']!r}; "
        f"the chaos value and the rule threshold have drifted apart"
    )

    # 4. the alert alertmanager would then POST, through ingestion's real front door
    body = _alertmanager_body(rule)
    response = await pipeline.post_prometheus_alert(body)
    assert response.status_code == 202, response.text

    # 5. it landed as an incident under the fingerprint the contract computes
    expected = compute_fingerprint(
        rule["labels"]["service"], rule["alert"], rule["labels"]["severity"]
    )
    assert await _incident_count(db, expected) == 1


async def test_the_crafted_alert_has_the_same_identity_as_the_mock_fixture(
    pipeline: Pipeline, db: Database
) -> None:
    """The Prometheus path and the existing e2e fixture agree on one incident.

    Same identity-agreement check as the rules file's own validation: if the rule and
    the fixture disagreed, Phase 8 retrieval would be exercised against an incident no
    other test ever produces.
    """
    rule = _rule(SCENARIO_ALERT)

    assert rule["labels"]["service"] == MOCK_ALERT["service_name"]
    assert rule["alert"] == MOCK_ALERT["alert_name"]
    assert rule["labels"]["severity"] == MOCK_ALERT["severity"]

    fingerprint = compute_fingerprint(
        MOCK_ALERT["service_name"], MOCK_ALERT["alert_name"], MOCK_ALERT["severity"]
    )

    await pipeline.post_prometheus_alert(_alertmanager_body(rule))
    assert await _incident_count(db, fingerprint) == 1

    # the mock path, same identity: dedup attaches it rather than opening a second
    await pipeline.post_alert()
    assert await _incident_count(db, fingerprint) == 1, (
        "the mock fixture opened a SECOND incident, so the Prometheus rule and the "
        "e2e fixture no longer describe the same thing"
    )


async def test_a_repeated_prometheus_alert_dedups_within_the_window(
    pipeline: Pipeline, db: Database
) -> None:
    """Phase 5's dedup guarantee, over the Prometheus body rather than the mock one."""
    rule = _rule(SCENARIO_ALERT)
    body = _alertmanager_body(rule)
    fingerprint = compute_fingerprint(
        rule["labels"]["service"], rule["alert"], rule["labels"]["severity"]
    )

    assert (await pipeline.post_prometheus_alert(body)).status_code == 202
    assert (await pipeline.post_prometheus_alert(body)).status_code == 202

    assert await _incident_count(db, fingerprint) == 1


@pytest.mark.parametrize("rule", _rules(), ids=lambda r: str(r["alert"]))
async def test_every_declared_rule_normalizes_into_an_incident(
    rule: dict[str, Any], pipeline: Pipeline, db: Database
) -> None:
    """Every one of the six rules, not just the one driven end to end.

    A rule whose labels ingestion rejects (an unknown severity, a missing service) is
    unusable no matter how well its metric behaves, and it would fail at 3am rather
    than here.
    """
    response = await pipeline.post_prometheus_alert(_alertmanager_body(rule))
    assert response.status_code == 202, f"{rule['alert']}: {response.text}"

    fingerprint = compute_fingerprint(
        rule["labels"]["service"], rule["alert"], rule["labels"]["severity"]
    )
    assert await _incident_count(db, fingerprint) == 1
