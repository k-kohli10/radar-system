"""App assembly and lifecycle: readiness, shutdown, and startup failures."""

from __future__ import annotations

from typing import Any

import pytest
import radar_llm_gateway.main as main
from fastapi.testclient import TestClient
from gateway_harness import GatewayEnv
from prometheus_client import CollectorRegistry
from radar_plugin_sdk import PluginRegistry


def _app(**kwargs: Any) -> Any:
    return main.create_app(
        metrics_registry=CollectorRegistry(), with_tracing=False, **kwargs
    )


def test_startup_marks_ready_and_serves_auth(gateway_env: GatewayEnv) -> None:
    app = _app()
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 200
        # the auth layer is live on the fully assembled app
        body = {"mode": "fast", "messages": [{"role": "user", "content": "hi"}]}
        assert client.post("/v1/complete", json=body).status_code == 401
        wrong_mode = client.post(
            "/v1/complete",
            json={**body, "mode": "reason"},
            headers={"X-Radar-Agent-Token": gateway_env.fast_token},
        )
        assert wrong_mode.status_code == 403


def test_shutdown_marks_not_ready_before_draining(gateway_env: GatewayEnv) -> None:
    app = _app()
    with TestClient(app) as client:
        assert client.get("/readyz").status_code == 200
    # lifespan exited: a probe now must see 503, not 200
    probe = TestClient(app).get("/readyz")
    assert probe.status_code == 503
    assert probe.json()["reason"] == "shutting down"


def test_missing_api_key_keeps_readyz_503(gateway_env: GatewayEnv) -> None:
    (gateway_env.secrets_dir / "openai_api_key").unlink()
    app = _app()
    with TestClient(app) as client:
        response = client.get("/readyz")
        assert response.status_code == 503
        assert "openai_api_key" in response.json()["reason"]
        assert client.get("/healthz").status_code == 200


def test_missing_token_map_keeps_readyz_503(gateway_env: GatewayEnv) -> None:
    (gateway_env.secrets_dir / "gateway_tokens").unlink()
    app = _app()
    with TestClient(app) as client:
        response = client.get("/readyz")
        assert response.status_code == 503
        assert "gateway_tokens" in response.json()["reason"]


def test_invalid_config_keeps_readyz_503(gateway_env: GatewayEnv) -> None:
    gateway_env.config_path.write_text("modes: {fast: {provider: openai}}\n")
    app = _app()
    with TestClient(app) as client:
        assert client.get("/readyz").status_code == 503


def test_plugin_registration_failure_is_caught_by_readiness(
    gateway_env: GatewayEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Registration runs inside lifespan startup, so a failure keeps readiness
    false (503) instead of crashing the import — and unexpected exception
    text is reduced to the class name."""

    def broken(registry: PluginRegistry) -> None:
        raise RuntimeError("possibly-sensitive vendor text")

    monkeypatch.setattr(main, "register_plugins", broken)
    app = _app()
    with TestClient(app) as client:
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json()["reason"] == "startup failed: RuntimeError"
        assert "possibly-sensitive" not in response.text


def test_ready_pod_recovers_reason_free_readyz(gateway_env: GatewayEnv) -> None:
    app = _app()
    with TestClient(app) as client:
        assert client.get("/readyz").json() == {"status": "ready"}
