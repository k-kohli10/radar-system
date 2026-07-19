"""E5: the real front door, once — chaos -> Prometheus -> alertmanager -> webhook.

Every other test substitutes the Prometheus process. This one does not. It stands up a
real Prometheus with ``deploy/prometheus/alerting-rules.yml`` mounted and a real
alertmanager, points them at a real platform-sim, and waits for an actual alert to
arrive at an actual HTTP receiver. It is the artifact that says *yes, the declared rules
work against a real evaluator* rather than only against a hand-built payload.

OPT-IN, AND SKIPPED TWO WAYS
----------------------------
It needs Docker and takes ~2 minutes (the rule's ``for: 1m`` has to elapse), so it never
runs by accident:

- the ``infra`` marker is deselected by default (``addopts = -m 'not live and not
  infra'``), so the normal suite, the pre-commit hook and CI skip it;
- and even when opted in it SKIPS without a working Docker, so ``pytest -m infra`` on a
  machine without Docker is a skip, not a red build.

    pytest -m infra -s tests/e2e/test_real_prometheus_alert.py

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It stops at the receiver: it asserts the webhook ARRIVED with the right labels, not that
ingestion then created an incident. The payload-to-incident half is covered
deterministically in ``test_platform_sim_alert_path`` against real Postgres, and
duplicating it here would make a two-minute Docker test load-bearing for a guarantee the
fast suite already holds. Phase 10 owns the production wiring; this proves the rules
themselves are sound.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request

pytestmark = pytest.mark.infra

REPO_ROOT = Path(__file__).resolve().parents[2]
RULES_FILE = REPO_ROOT / "deploy" / "prometheus" / "alerting-rules.yml"

PROMETHEUS_IMAGE = "prom/prometheus:v2.55.0"
ALERTMANAGER_IMAGE = "prom/alertmanager:v0.27.0"

#: OrderProcessingFailureRate: `for: 1m`, evaluated every 15s, plus alertmanager's
#: group_wait. Two and a half minutes leaves comfortable margin over the ~90s it
#: actually takes, without letting a wedged run hang the suite indefinitely.
ALERT_TIMEOUT_SECONDS = 150.0
POLL_INTERVAL_SECONDS = 2.0

SCENARIO_ALERT = "OrderProcessingFailureRate"
SCENARIO_SPIKE = {"rate": 0.15, "duration_seconds": 600}


def _free_port() -> int:
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _docker_works() -> bool:
    if shutil.which("docker") is None:
        return False
    result = subprocess.run(
        ["docker", "info"], capture_output=True, text=True, timeout=30
    )
    return result.returncode == 0


@contextmanager
def _uvicorn_server(app: FastAPI, port: int) -> Iterator[None]:
    """Serve ``app`` on ``port`` in a background thread, on all interfaces.

    Bound to 0.0.0.0, not 127.0.0.1: the containers reach back to the host through
    host.docker.internal, and a loopback-only bind refuses those connections.
    """
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    server = uvicorn.Server(config)
    import threading

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    else:
        raise RuntimeError(f"uvicorn did not start on {port}")
    try:
        yield
    finally:
        server.should_exit = True
        thread.join(timeout=10)


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


def _write_configs(tmp: Path, *, sim_port: int, receiver_port: int) -> None:
    shutil.copy(RULES_FILE, tmp / "alerting-rules.yml")

    (tmp / "prometheus.yml").write_text(
        json.dumps(
            {
                "global": {"scrape_interval": "5s", "evaluation_interval": "5s"},
                "rule_files": ["/etc/prometheus/alerting-rules.yml"],
                "alerting": {
                    "alertmanagers": [
                        {"static_configs": [{"targets": ["alertmanager:9093"]}]}
                    ]
                },
                "scrape_configs": [
                    {
                        "job_name": "platform-sim",
                        "static_configs": [
                            {"targets": [f"host.docker.internal:{sim_port}"]}
                        ],
                    }
                ],
            }
        )
    )

    (tmp / "alertmanager.yml").write_text(
        json.dumps(
            {
                "route": {
                    "receiver": "radar",
                    "group_wait": "1s",
                    "group_interval": "5s",
                    "repeat_interval": "1h",
                },
                "receivers": [
                    {
                        "name": "radar",
                        "webhook_configs": [
                            {
                                "url": (
                                    f"http://host.docker.internal:{receiver_port}"
                                    f"/alerts/prometheus"
                                ),
                                "send_resolved": False,
                            }
                        ],
                    }
                ],
            }
        )
    )


def test_real_prometheus_fires_a_declared_rule_to_a_webhook(tmp_path: Path) -> None:
    if not _docker_works():
        pytest.skip("Docker is not available; the real-Prometheus proof is opt-in")

    from prometheus_client import CollectorRegistry
    from radar_platform_sim.main import create_app as create_sim_app

    sim_port, receiver_port = _free_port(), _free_port()
    _write_configs(tmp_path, sim_port=sim_port, receiver_port=receiver_port)

    received: list[dict[str, Any]] = []
    receiver = FastAPI()

    @receiver.post("/alerts/prometheus")
    async def collect(request: Request) -> dict[str, str]:
        received.append(await request.json())
        return {"status": "ok"}

    network = f"radar-e2e-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["docker", "network", "create", network], check=True, capture_output=True
    )

    host_gateway = ["--add-host", "host.docker.internal:host-gateway"]

    try:
        with (
            _uvicorn_server(
                create_sim_app(metrics_registry=CollectorRegistry()), sim_port
            ),
            _uvicorn_server(receiver, receiver_port),
            _container(
                "radar-e2e-alertmanager",
                [
                    "--network",
                    network,
                    "--network-alias",
                    "alertmanager",
                    *host_gateway,
                    "-v",
                    f"{tmp_path}/alertmanager.yml:/etc/alertmanager/alertmanager.yml:ro",
                    ALERTMANAGER_IMAGE,
                    "--config.file=/etc/alertmanager/alertmanager.yml",
                ],
            ),
            _container(
                "radar-e2e-prometheus",
                [
                    "--network",
                    network,
                    *host_gateway,
                    "-v",
                    f"{tmp_path}/prometheus.yml:/etc/prometheus/prometheus.yml:ro",
                    "-v",
                    f"{tmp_path}/alerting-rules.yml:/etc/prometheus/alerting-rules.yml:ro",
                    PROMETHEUS_IMAGE,
                    "--config.file=/etc/prometheus/prometheus.yml",
                ],
            ),
        ):
            # the spike must be live BEFORE Prometheus starts sampling, so the rule
            # sees a continuously-breaching series rather than a gap
            httpx.post(
                f"http://127.0.0.1:{sim_port}/chaos/order-failures",
                json=SCENARIO_SPIKE,
                timeout=10,
            ).raise_for_status()

            deadline = time.monotonic() + ALERT_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                if received:
                    break
                time.sleep(POLL_INTERVAL_SECONDS)
            else:
                pytest.fail(
                    f"no alert reached the webhook within {ALERT_TIMEOUT_SECONDS}s"
                )

        alerts = [a for body in received for a in body.get("alerts", [])]
        names = {a["labels"]["alertname"] for a in alerts}
        assert SCENARIO_ALERT in names, f"got {names}"

        fired = next(a for a in alerts if a["labels"]["alertname"] == SCENARIO_ALERT)
        assert fired["labels"]["service"] == "order-service"
        assert fired["labels"]["severity"] == "critical"
        assert fired["status"] == "firing"

        elapsed = ALERT_TIMEOUT_SECONDS - (deadline - time.monotonic())
        print(f"\nreal Prometheus fired {SCENARIO_ALERT} in ~{elapsed:.0f}s")
    finally:
        subprocess.run(["docker", "network", "rm", network], capture_output=True)
