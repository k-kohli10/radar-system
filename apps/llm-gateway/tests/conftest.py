"""Fixtures for the llm-gateway test suite."""

from __future__ import annotations

from pathlib import Path

import pytest
from gateway_harness import GatewayEnv, GatewayHarness, build_harness, write_gateway_env


@pytest.fixture
def gateway_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GatewayEnv:
    """A temp Vault dir + gateway config, exported via the RADAR env vars."""
    env = write_gateway_env(tmp_path)
    monkeypatch.setenv("RADAR_SECRETS_DIR", str(env.secrets_dir))
    monkeypatch.setenv("RADAR_GATEWAY_CONFIG_PATH", str(env.config_path))
    return env


@pytest.fixture
def gw(gateway_env: GatewayEnv) -> GatewayHarness:
    """A wired gateway app over fake providers; no network, no real sleeps."""
    return build_harness(gateway_env)
