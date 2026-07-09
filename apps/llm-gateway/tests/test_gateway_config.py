"""Config and token-map loading: validation, and secret-free error paths."""

from __future__ import annotations

import secrets
from pathlib import Path

import pytest
from gateway_harness import GATEWAY_YAML
from radar_common import ConfigurationError
from radar_contracts import LLMMode
from radar_llm_gateway.core.config import (
    load_gateway_config,
    load_token_map,
)
from radar_llm_gateway.core.security import estimate_tokens


def test_gateway_config_loads_all_modes_and_fallbacks(tmp_path: Path) -> None:
    path = tmp_path / "gateway.yaml"
    path.write_text(GATEWAY_YAML)
    config = load_gateway_config(path)
    assert set(config.modes) == set(LLMMode)
    fast = config.modes[LLMMode.FAST]
    assert (fast.provider, fast.model) == ("openai", "gpt-4o-mini")
    assert fast.max_input_tokens == 4096
    assert fast.timeout_seconds == 5
    assert config.modes[LLMMode.EMBED].max_output_tokens is None
    assert set(config.fallback) == {LLMMode.REASON, LLMMode.EXTENDED}
    assert config.fallback[LLMMode.EXTENDED].model == "gpt-4o-mini"


def test_gateway_config_missing_mode_fails_startup(tmp_path: Path) -> None:
    path = tmp_path / "gateway.yaml"
    path.write_text(
        "modes:\n"
        "  fast: {provider: openai, model: m, max_input_tokens: 1, "
        "timeout_seconds: 1}\n"
    )
    with pytest.raises(ConfigurationError, match="missing modes"):
        load_gateway_config(path)


def test_gateway_config_missing_file_fails_startup(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="not found"):
        load_gateway_config(tmp_path / "nope.yaml")


def test_token_map_loads_grants(tmp_path: Path) -> None:
    token = secrets.token_hex(32)
    (tmp_path / "gateway_tokens").write_text(
        f"tokens:\n  {token}:\n    service: watcher-agent\n    allowed_mode: fast\n"
    )
    token_map = load_token_map(directory=tmp_path)
    grant = token_map.lookup(token)
    assert grant is not None
    assert grant.service == "watcher-agent"
    assert grant.allowed_mode is LLMMode.FAST
    assert token_map.lookup("nope") is None
    assert token_map.lookup(None) is None


def test_token_map_invalid_yaml_error_never_quotes_the_secret(
    tmp_path: Path,
) -> None:
    token = secrets.token_hex(32)
    (tmp_path / "gateway_tokens").write_text(f"tokens: [\n  {token} broken")
    with pytest.raises(ConfigurationError) as excinfo:
        load_token_map(directory=tmp_path)
    assert token not in str(excinfo.value)
    rendered = repr(excinfo.getrepr(style="long"))
    assert token not in rendered


def test_token_map_invalid_entry_identified_by_position_not_content(
    tmp_path: Path,
) -> None:
    token = secrets.token_hex(32)
    (tmp_path / "gateway_tokens").write_text(
        f"tokens:\n  {token}: {{service: 1, allowed_mode: warp}}\n"
    )
    with pytest.raises(ConfigurationError) as excinfo:
        load_token_map(directory=tmp_path)
    message = str(excinfo.value)
    assert "entry 1" in message
    assert token not in message


def test_token_map_missing_secret_fails_startup(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="gateway_tokens"):
        load_token_map(directory=tmp_path)


def test_token_map_empty_fails_startup(tmp_path: Path) -> None:
    (tmp_path / "gateway_tokens").write_text("tokens: {}\n")
    with pytest.raises(ConfigurationError):
        load_token_map(directory=tmp_path)


@pytest.mark.parametrize(
    ("texts", "expected"),
    [
        ([], 0),
        ([""], 0),
        (["abcd"], 1),
        (["abcde"], 2),  # ceil(5/4)
        (["ab", "cd"], 1),  # summed before dividing
        (["x" * 4096 * 4], 4096),
    ],
)
def test_estimate_tokens_is_ceil_of_chars_over_four(
    texts: list[str], expected: int
) -> None:
    assert estimate_tokens(texts) == expected
