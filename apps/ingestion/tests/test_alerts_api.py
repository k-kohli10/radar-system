"""HTTP contract tests for the ingestion alert API.

The normalizer and persist tests cover the pipeline *internals*; these cover the
*HTTP layer* those never touch — routing, per-source webhook auth, the
401-beats-422 guard ordering, and the health/readiness/metrics endpoints. All of
that behavior (from the webhook-auth commit) lives only at the HTTP boundary, so
it is only testable by driving the real app.

The app is driven in-process with FastAPI's ``TestClient`` (which runs the
lifespan on enter). No live infra beyond Postgres: secrets are faked by pointing
``RADAR_SECRETS_DIR`` at a tmp dir holding a ``postgres_dsn`` file (the same test
DB the rest of the suite uses) and one ``webhook_token_<source>`` file per
source. The module skips when Postgres is unconfigured, like the other DB tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from prometheus_client import CollectorRegistry
from radar_database import Base
from radar_ingestion.main import create_app
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

# Distinct per-source tokens, injected into the fake secrets dir so assertions
# can anchor to known values (right token accepted, others/cross-source 401).
TOKENS = {
    "prometheus": "prometheus-webhook-token-0000000000000000000000000000",
    "kibana": "kibana-webhook-token-11111111111111111111111111111111",
    "mock": "mock-webhook-token-222222222222222222222222222222222222",
}

MOCK_BODY = {
    "service_name": "order-service",
    "alert_name": "OrderFailure",
    "severity": "critical",
}


def _truncate(database_url: str) -> None:
    """Clear every table on ``database_url`` so each test starts empty."""

    async def run() -> None:
        engine = create_async_engine(database_url)
        tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
        try:
            async with engine.begin() as conn:
                await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        finally:
            await engine.dispose()

    asyncio.run(run())


def _write_secrets(directory: Path, *, dsn: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "postgres_dsn").write_text(dsn)
    for source, token in TOKENS.items():
        (directory / f"webhook_token_{source}").write_text(token)


@pytest.fixture
def client(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """A TestClient over the real app: faked secrets, real (clean) test DB."""
    _truncate(database_url)
    secrets = tmp_path / "secrets"
    _write_secrets(secrets, dsn=database_url)
    monkeypatch.setenv("RADAR_SECRETS_DIR", str(secrets))
    # Fresh registry per app so request-metric families never collide with
    # another app's global registry; no tracing so no OTel setup in tests.
    app = create_app(metrics_registry=CollectorRegistry(), with_tracing=False)
    with TestClient(app) as test_client:
        yield test_client


def _post(
    client: TestClient, source: str, *, token: str | None, json: object
) -> Response:
    headers = {"X-Radar-Webhook-Token": token} if token is not None else {}
    response: Response = client.post(f"/alerts/{source}", json=json, headers=headers)
    return response


# --- happy path + dedup -------------------------------------------------------


def test_valid_mock_alert_returns_202_with_incident_id(client: TestClient) -> None:
    response = _post(client, "mock", token=TOKENS["mock"], json=MOCK_BODY)

    assert response.status_code == 202
    body = response.json()
    assert body["deduplicated"] is False
    assert body["incident_id"]  # a real incident id was returned


def test_duplicate_alert_dedups_to_the_same_incident(client: TestClient) -> None:
    first = _post(client, "mock", token=TOKENS["mock"], json=MOCK_BODY)
    second = _post(client, "mock", token=TOKENS["mock"], json=MOCK_BODY)

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["deduplicated"] is False
    assert second.json()["deduplicated"] is True
    # The second alert attaches to the SAME incident opened by the first, within
    # the dedup window — not merely "some incident id".
    assert second.json()["incident_id"] == first.json()["incident_id"]


# --- radar_incidents_total: one tick per incident OPENED ----------------------

INCIDENTS_TOTAL = "radar_incidents_total"


def _incidents_opened(
    client: TestClient, *, service: str, severity: str
) -> float | None:
    """The value of radar_incidents_total for one (service, severity), or None.

    None — not 0.0 — before the first open: prometheus_client emits no line for a label
    set never observed, and the tests assert on that absence.
    """
    prefix = f'{INCIDENTS_TOTAL}{{service="{service}",severity="{severity}"}} '
    for line in client.get("/metrics").text.splitlines():
        if line.startswith(prefix):
            return float(line[len(prefix) :])
    return None


def test_opening_an_incident_increments_incidents_total(client: TestClient) -> None:
    """ingestion produces radar_incidents_total, ticked once per incident OPENED and
    labelled by that incident's service and severity — the counter the plan wants on the
    service that actually opens incidents, not on feedback-service where it sat at zero.
    """
    assert (
        _incidents_opened(client, service="order-service", severity="critical") is None
    )

    response = _post(client, "mock", token=TOKENS["mock"], json=MOCK_BODY)
    assert response.status_code == 202
    assert response.json()["deduplicated"] is False

    assert (
        _incidents_opened(client, service="order-service", severity="critical") == 1.0
    )


def test_a_dedup_attach_does_not_increment_incidents_total(client: TestClient) -> None:
    """The counter counts incidents OPENED, not alerts received.

    A duplicate attaches to the existing incident within the dedup window, opening
    nothing, so the counter must stay at 1 — otherwise a storm of repeats for one
    incident would read on the dashboard as many incidents.

    Mutation that must turn this red: drop the ``if not result.deduplicated`` guard in
    the route, and the dedup below ticks the counter to 2.
    """
    first = _post(client, "mock", token=TOKENS["mock"], json=MOCK_BODY)
    second = _post(client, "mock", token=TOKENS["mock"], json=MOCK_BODY)
    assert first.json()["deduplicated"] is False
    assert second.json()["deduplicated"] is True

    assert (
        _incidents_opened(client, service="order-service", severity="critical") == 1.0
    )


# --- per-source webhook auth --------------------------------------------------


def test_bad_token_returns_401(client: TestClient) -> None:
    response = _post(client, "mock", token="wrong-token", json=MOCK_BODY)
    assert response.status_code == 401


def test_missing_token_returns_401(client: TestClient) -> None:
    response = _post(client, "mock", token=None, json=MOCK_BODY)
    assert response.status_code == 401


def test_cross_source_token_returns_401(client: TestClient) -> None:
    # A valid MOCK token presented to the PROMETHEUS endpoint: auth is per
    # source, so cross-source reuse fails closed.
    response = _post(client, "prometheus", token=TOKENS["mock"], json=MOCK_BODY)
    assert response.status_code == 401


# --- payload validation + guard ordering --------------------------------------


def test_valid_token_malformed_body_returns_422(client: TestClient) -> None:
    bad = {"service_name": "s", "alert_name": "a", "severity": "page"}  # unknown
    response = _post(client, "mock", token=TOKENS["mock"], json=bad)
    assert response.status_code == 422


def test_bad_token_and_malformed_body_returns_401_not_422(client: TestClient) -> None:
    # A non-object body fails FastAPI's body validation *before* the auth
    # dependency runs; the guard must still answer 401 for the bad token, never
    # leak a 422 that reveals the body was inspected pre-auth.
    response = _post(client, "mock", token="wrong-token", json=[1, 2, 3])
    assert response.status_code == 401


# --- health / readiness / metrics ---------------------------------------------


def test_healthz_returns_200(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_returns_200_when_secrets_and_db_are_satisfied(
    client: TestClient,
) -> None:
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_metrics_endpoint_is_present(client: TestClient) -> None:
    client.get("/healthz")  # drive one request so the counter has a sample
    response = client.get("/metrics")
    assert response.status_code == 200
    assert b"radar_requests_total" in response.content


def test_readyz_returns_503_when_secrets_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No postgres_dsn (and no tokens) in the secrets dir: startup fails to load
    # them, readiness stays false, and /readyz reports 503 without crashing.
    # This one needs no Postgres — startup never reaches the database.
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("RADAR_SECRETS_DIR", str(empty))
    app = create_app(metrics_registry=CollectorRegistry(), with_tracing=False)
    with TestClient(app) as test_client:
        response = test_client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
