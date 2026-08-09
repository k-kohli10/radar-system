"""STEP-11 PAIRED: the reasoner's real fallback fires LLMTemplateFallbackActive.

``test_real_prometheus_alert`` proves the *simulated-shop* rules
(``alerting-rules.yml``) fire against a real evaluator. This proves the other rule
file — RADAR's OWN service-health rules (``radar-service-alerts.yml``) — and one rule
in particular: ``LLMTemplateFallbackActive``, the alert that tells an operator the LLM
path is degraded and incidents are getting template RCAs instead of model analysis.

WHY THIS ONE NEEDS ITS OWN PROOF, AND WHY THE MOCK IS A CLEAN 503
-----------------------------------------------------------------
The rule keys on ``radar_recommendations_fallback_total{reason="gateway_unavailable"}``
— a counter the REASONER emits when it gives up on the LLM and templates the RCA. So
the only faithful way to make it move is to make the real reasoner really fall back,
which means the real ``GatewayClient._call -> _classify -> resolve`` path has to run.

The seam that keeps this honest: ``_classify`` maps a 503 to
``GATEWAY_UNAVAILABLE`` on ``response.status_code == 503`` ALONE — it never reads the
body. So a mock gateway that returns a clean HTTP 503 drives the actual production
mapping, not a shortcut. A 200-with-garbage mock would be WRONG here: it would still
fire the alert, but under a different reason (``not_json`` / ``schema_invalid``), and
the assertion below (``reason == "gateway_unavailable"``) would fail. The reason label
is load-bearing precisely because it is what an operator pages on —
``gateway_unavailable`` means wait for the provider, ``rejected`` means fix config NOW.

WHAT IS REAL
------------
- **The reasoner.** The actual ``create_reasoner_app`` on a real uvicorn socket, its
  real lifespan, its real httpx gateway client (``timeout=None``), on real Postgres.
- **The database.** A real incident + plan is seeded per event, as the pipeline leaves
  them; the reasoner loads them and builds the real context bundle.
- **Prometheus.** A real ``prom/prometheus`` container, loading the repo's real
  ``radar-service-alerts.yml``, scraping the reasoner's ``/metrics`` with the same
  ``service: reasoner-agent`` target label the compose ``prometheus.yml`` sets — so the
  ``sum by (service, reason)`` in the rule groups exactly as it will in production.

Only the llm-gateway is a mock, and only so it can be made to fail on command: a real
gateway with every provider down would itself return 503 — the case being proven.

IN THE DEFAULT SUITE, FAIL-LOUD
-------------------------------
``infra`` but NOT ``live``: ``addopts = -m 'not live'`` keeps it in ``make test`` and
CI, and ``make test-quick`` (``-m 'not live and not infra'``) drops it for the fast
inner loop. Without a working Docker it FAILS rather than skips — a done-condition that
silently skips when its dependency is down is a false green.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from socket import socket
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from prometheus_client import CollectorRegistry
from radar_common import AGENT_TOKEN_HEADER
from radar_contracts import ReasoningRequestedPayload
from radar_database import Alert, Database, Incident, InvestigationPlan

from tests.e2e.harness import _build_mock_gateway, success, unavailable

# infra, and DELIBERATELY NOT `live` — see the module docstring. Being
# infra-but-not-live is the ONLY thing that keeps this done-condition proof in the
# default suite and CI. Do not add `live`.
pytestmark = pytest.mark.infra

REPO_ROOT = Path(__file__).resolve().parents[2]
RULES_FILE = REPO_ROOT / "deploy" / "prometheus" / "radar-service-alerts.yml"

PROMETHEUS_IMAGE = "prom/prometheus:v2.55.0"

FALLBACK_ALERT = "LLMTemplateFallbackActive"
EXPECTED_REASON = "gateway_unavailable"
EXPECTED_SERVICE = "reasoner-agent"

#: The reasoner's inbound /events token and its outbound gateway token — two distinct
#: grants (a service must not accept its own gateway token on /events). Values are
#: shape-only; the mock 503s before the gateway token is ever checked.
AGENT_TOKEN = "r" * 64
GATEWAY_TOKEN = "g" * 64

#: `for: 2m` in the rule, plus 5s evaluation + scrape intervals and a couple of scrapes
#: to establish a rising counter. ~2m10s in practice; 240s leaves margin without letting
#: a wedged run hang the suite.
ALERT_TIMEOUT_SECONDS = 240.0
POLL_INTERVAL_SECONDS = 2.0
#: One fresh fallback every this often during the wait, so Prometheus always observes a
#: counter that is still RISING inside its 5m rate() window rather than a flat plateau
#: (a plateau's rate decays to zero and the alert would resolve).
DRIVE_INTERVAL_SECONDS = 10.0


def _free_port() -> int:
    with closing(socket()) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _docker_works() -> bool:
    if shutil.which("docker") is None:
        return False
    result = subprocess.run(
        ["docker", "info"], capture_output=True, text=True, timeout=30
    )
    return result.returncode == 0


@asynccontextmanager
async def _serve(app: FastAPI, *, host: str) -> AsyncIterator[tuple[str, int]]:
    """Serve ``app`` on a real ephemeral port; yield ``http://<host>:<port>``.

    The reasoner is served on ``0.0.0.0`` (not loopback) because the Prometheus
    container reaches back through ``host.docker.internal``, and a loopback-only bind
    refuses those connections. The mock gateway only ever answers the reasoner (a host
    process), so 127.0.0.1 is fine for it.
    """
    config = uvicorn.Config(app, host=host, port=0, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://{host}:{port}", port
    finally:
        server.should_exit = True
        await task


@contextmanager
def _container(name: str, args: list[str]) -> Iterator[None]:
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    subprocess.run(
        ["docker", "run", "-d", "--name", name, *args], check=True, capture_output=True
    )
    try:
        yield
    finally:
        logs = subprocess.run(
            ["docker", "logs", "--tail", "25", name], capture_output=True, text=True
        )
        if logs.stdout or logs.stderr:
            print(f"\n--- {name} logs ---\n{logs.stdout}{logs.stderr}", file=sys.stderr)
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def _write_prometheus_config(tmp: Path, *, reasoner_port: int) -> None:
    """The real rules file, plus a scrape config that mirrors the compose one.

    The ``service: reasoner-agent`` target label is what the rule's
    ``sum by (service, reason)`` groups on — set here exactly as ``deploy/prometheus/
    prometheus.yml`` sets it, so the grouping under test is the production grouping.
    """
    shutil.copy(RULES_FILE, tmp / "radar-service-alerts.yml")
    (tmp / "prometheus.yml").write_text(
        json.dumps(
            {
                "global": {"scrape_interval": "5s", "evaluation_interval": "5s"},
                "rule_files": ["/etc/prometheus/radar-service-alerts.yml"],
                "scrape_configs": [
                    {
                        "job_name": "radar",
                        "static_configs": [
                            {
                                "targets": [f"host.docker.internal:{reasoner_port}"],
                                "labels": {"service": EXPECTED_SERVICE},
                            }
                        ],
                    }
                ],
            }
        )
    )


async def _seed_incident_and_plan(db: Database) -> tuple[UUID, UUID]:
    """One incident + its alert + its plan, as the pipeline leaves them.

    A fresh pair per event: recommendations carry a UNIQUE index on ``incident_id``, so
    reusing one incident would make the second event a no-op (``already_recommended``)
    that never touches the fallback counter. One pair, one fallback.
    """
    incident = Incident(
        id=uuid4(),
        correlation_id=uuid4(),
        fingerprint=uuid4().hex + uuid4().hex,
        service_name="order-service",
        title="order-service OrderProcessingFailureRate",
        severity="high",
        status="open",
        alert_count=1,
    )
    plan = InvestigationPlan(
        id=uuid4(),
        incident_id=incident.id,
        correlation_id=incident.correlation_id,
        steps=[{"order": 1, "description": "Check recent deployments"}],
        template_key="order-service:OrderProcessingFailureRate",
        status="pending",
    )
    async with db.session() as session:
        session.add(incident)
        session.add(
            Alert(
                id=uuid4(),
                source="mock",
                fingerprint=incident.fingerprint,
                service_name="order-service",
                alert_name="OrderProcessingFailureRate",
                severity="high",
                status="firing",
                raw_payload={},
                fired_at=datetime.now(UTC),
                received_at=datetime.now(UTC),
                incident_id=incident.id,
                correlation_id=uuid4(),
            )
        )
        session.add(plan)
        await session.commit()
    return incident.id, plan.id


async def _drive_one_event(
    client: httpx.AsyncClient, reasoner_url: str, db: Database
) -> None:
    """Seed a pair and POST one reasoning_requested event; require a real 200."""
    incident_id, plan_id = await _seed_incident_and_plan(db)
    payload = ReasoningRequestedPayload(incident_id=incident_id, plan_id=plan_id)
    body: dict[str, Any] = {
        "event_id": str(uuid4()),
        "event_type": "incident.reasoning_requested",
        "correlation_id": str(uuid4()),
        "payload": payload.model_dump(mode="json"),
    }
    response = await client.post(
        f"{reasoner_url}/events", json=body, headers={AGENT_TOKEN_HEADER: AGENT_TOKEN}
    )
    response.raise_for_status()
    assert response.json()["status"] == "processed", response.text


_FALLBACK_SERIES = re.compile(
    r'radar_recommendations_fallback_total\{[^}]*reason="'
    + EXPECTED_REASON
    + r'"[^}]*\}\s+([0-9.]+)'
)


async def _fallback_count(client: httpx.AsyncClient, reasoner_url: str) -> float:
    """The reasoner's OWN view of the gateway_unavailable fallback counter.

    Absent from ``/metrics`` entirely until the first such fallback (prometheus_client
    emits no line for an unused label set), which is exactly what the trigger-is-the-
    trigger check relies on.
    """
    text = (await client.get(f"{reasoner_url}/metrics")).text
    match = _FALLBACK_SERIES.search(text)
    return float(match.group(1)) if match else 0.0


def _find_alert(rules_json: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """(rule_state, active_alerts) for the fallback rule, else ("absent", [])."""
    for group in rules_json["data"]["groups"]:
        for rule in group["rules"]:
            if rule.get("name") == FALLBACK_ALERT:
                return rule["state"], rule.get("alerts", [])
    return "absent", []


def _write_secrets(directory: Path, *, dsn: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "postgres_dsn").write_text(dsn)
    (directory / "agent_token").write_text(AGENT_TOKEN)
    (directory / "gateway_token").write_text(GATEWAY_TOKEN)


async def test_real_prometheus_fires_the_llm_fallback_alert(
    db: Database,
    database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _docker_works():
        # Fail-loud, NOT skip: this proof runs in the default suite, and a
        # done-condition that silently skips when its dependency is down is a false
        # green. No Docker here means the fallback -> scrape -> fire path went unproven.
        pytest.fail("Docker unavailable; the fallback->scrape->fire proof cannot run")

    from radar_reasoner_agent.main import create_app as create_reasoner_app

    _write_secrets(tmp_path / "secrets", dsn=database_url)
    monkeypatch.setenv("RADAR_SECRETS_DIR", str(tmp_path / "secrets"))

    async with _serve_gateway() as (gateway_url, control):
        # Read at app-build time, so it must be set before create_app.
        monkeypatch.setenv("RADAR_GATEWAY_URL", gateway_url)
        reasoner = create_reasoner_app(
            metrics_registry=CollectorRegistry(), with_tracing=False
        )
        async with (
            _serve(reasoner, host="0.0.0.0") as (reasoner_url, reasoner_port),
            httpx.AsyncClient(timeout=10) as client,
        ):
            await _await_ready(client, reasoner_url)

            # --- TEETH 1: the 503 is what fires it. A SUCCESS produces no fallback ---
            # counter at all, so a fired alert cannot be an artifact of merely calling
            # the reasoner — it can only be the gateway failure.
            control.respond = success
            await _drive_one_event(client, reasoner_url, db)
            assert await _fallback_count(client, reasoner_url) == 0.0, (
                "a successful RCA must not touch the gateway_unavailable counter"
            )

            # --- now make the gateway fail, and prove the reason locally first ---
            control.respond = unavailable
            await _drive_one_event(client, reasoner_url, db)
            await _drive_one_event(client, reasoner_url, db)
            local = await _fallback_count(client, reasoner_url)
            assert local == 2.0, (
                f"the reasoner should have templated 2 RCAs under gateway_unavailable, "
                f"saw {local}"
            )

            # --- TEETH 2: the real Prometheus, the real rule file, the real fire ---
            _write_prometheus_config(tmp_path, reasoner_port=reasoner_port)
            prom_port = _free_port()
            with _container(
                "radar-reasoner-fallback-prometheus",
                [
                    "-p",
                    f"127.0.0.1:{prom_port}:9090",
                    "--add-host",
                    "host.docker.internal:host-gateway",
                    "-v",
                    f"{tmp_path}/prometheus.yml:/etc/prometheus/prometheus.yml:ro",
                    "-v",
                    f"{tmp_path}/radar-service-alerts.yml:"
                    f"/etc/prometheus/radar-service-alerts.yml:ro",
                    PROMETHEUS_IMAGE,
                    "--config.file=/etc/prometheus/prometheus.yml",
                ],
            ):
                fired = await _poll_until_firing(
                    client, db, reasoner_url, f"http://127.0.0.1:{prom_port}"
                )

    labels = fired["labels"]
    assert labels["reason"] == EXPECTED_REASON, (
        f"fired under the wrong reason: {labels}. A 200-with-garbage mock would fire "
        f"this same alert under not_json/schema_invalid — the reason is the proof."
    )
    assert labels["service"] == EXPECTED_SERVICE, labels
    assert labels["severity"] == "warning"
    assert labels["alert_source"] == "radar"


@asynccontextmanager
async def _serve_gateway() -> AsyncIterator[tuple[str, Any]]:
    """The mock llm-gateway on a real socket; yields (base_url, its GatewayControl)."""
    app, control = _build_mock_gateway()
    async with _serve(app, host="127.0.0.1") as (url, _port):
        yield url, control


async def _await_ready(client: httpx.AsyncClient, reasoner_url: str) -> None:
    """Wait for the reasoner's lifespan to load secrets and reach Postgres."""
    for _ in range(200):
        try:
            if (await client.get(f"{reasoner_url}/readyz")).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.05)
    pytest.fail("the reasoner never became ready")


async def _poll_until_firing(
    client: httpx.AsyncClient,
    db: Database,
    reasoner_url: str,
    prom_url: str,
) -> dict[str, Any]:
    """Drive a steady fallback trickle and poll the rule until it FIRES.

    Prints the state transcript (absent -> pending -> firing) so the fire is shown, not
    just asserted. Returns the firing alert instance (reason=gateway_unavailable).
    """
    deadline = time.monotonic() + ALERT_TIMEOUT_SECONDS
    next_drive = 0.0
    seen_states: list[str] = []
    while time.monotonic() < deadline:
        now = time.monotonic()
        if now >= next_drive:
            # Keep the counter rising so rate()[5m] stays > 0 across the `for: 2m`.
            await _drive_one_event(client, reasoner_url, db)
            next_drive = now + DRIVE_INTERVAL_SECONDS

        try:
            rules = (await client.get(f"{prom_url}/api/v1/rules")).json()
        except httpx.HTTPError:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            continue

        state, alerts = _find_alert(rules)
        if not seen_states or seen_states[-1] != state:
            seen_states.append(state)
            elapsed = ALERT_TIMEOUT_SECONDS - (deadline - time.monotonic())
            print(f"[{elapsed:5.0f}s] {FALLBACK_ALERT}: {state}")

        if state == "firing":
            firing = [
                a
                for a in alerts
                if a.get("state") == "firing"
                and a["labels"].get("reason") == EXPECTED_REASON
            ]
            if firing:
                print(f"\ntranscript: {' -> '.join(seen_states)}")
                print(f"fired alert labels: {firing[0]['labels']}")
                return firing[0]

        await asyncio.sleep(POLL_INTERVAL_SECONDS)

    pytest.fail(
        f"{FALLBACK_ALERT} did not fire with reason={EXPECTED_REASON} within "
        f"{ALERT_TIMEOUT_SECONDS}s; states seen: {seen_states}"
    )
