"""Gateway configuration: mode routing table and the token→mode map.

Two strictly separated sources, mirroring the platform-wide config/secret split
(docs/adr/0007-vault-init-container.md):

- **Mode config**: which provider/model serves each mode, per-mode token limits
  and timeouts, and optional fallback providers. Non-secret YAML mounted as a
  ConfigMap; path set via ``RADAR_GATEWAY_CONFIG_PATH``.
- **Token map**: the agent-token IAM table (token -> calling service + its one
  allowed mode). Token values are secrets, so the whole map lives in a single
  Vault secret file (``gateway_tokens``) read once at startup, never in the
  ConfigMap and never in the environment.

``gateway_tokens`` secret file shape (YAML)::

    tokens:
      <64-char hex token>:
        service: watcher-agent
        allowed_mode: fast

Nothing in this module logs, and no error raised from it ever embeds a token
value: YAML parse errors quote source lines, so parse failures of the secret
are reported without the underlying exception text.
"""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from radar_common import ConfigurationError, RadarSettings, read_secret
from radar_contracts import LLMMode

GATEWAY_TOKENS_SECRET = "gateway_tokens"
"""Vault secret filename holding the YAML token→mode map."""


class GatewaySettings(RadarSettings):
    """Non-secret llm-gateway settings, read from ``RADAR_*`` env vars."""

    service_name: str = "llm-gateway"
    gateway_config_path: Path = Path("config/gateway.yaml")


class ModeConfig(BaseModel):
    """Provider routing and limits for one gateway mode."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(description="Provider plugin name, e.g. 'openai'.")
    model: str = Field(description="Concrete model id, e.g. 'gpt-4o'.")
    max_input_tokens: int = Field(gt=0, description="Input token ceiling (422 over).")
    max_output_tokens: int | None = Field(
        default=None,
        gt=0,
        description="Output token cap passed to the provider; None for embed.",
    )
    timeout_seconds: float = Field(gt=0, description="Hard per-call timeout.")


class FallbackConfig(BaseModel):
    """Secondary provider/model tried after the primary exhausts its retries."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(description="Fallback provider plugin name.")
    model: str = Field(description="Fallback model id.")


class CircuitBreakerConfig(BaseModel):
    """Per-binding circuit-breaker tuning for the provider failure policy.

    Optional in the YAML; the defaults match the module constants. A binding
    that fails ``failure_threshold`` times in a row opens and fails fast for
    ``reset_timeout_seconds`` before a half-open trial call.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    failure_threshold: int = Field(
        default=5, gt=0, description="Consecutive failures before the circuit opens."
    )
    reset_timeout_seconds: float = Field(
        default=30.0, gt=0, description="Open-circuit cooldown before a trial call."
    )


class GatewayConfig(BaseModel):
    """The full mode-routing table loaded from the gateway YAML config."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    modes: dict[LLMMode, ModeConfig]
    fallback: dict[LLMMode, FallbackConfig] = Field(default_factory=dict)
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)

    @model_validator(mode="after")
    def _require_every_mode(self) -> GatewayConfig:
        # The mode set is a Locked Decision and every mode has a token granting
        # it, so a partial table is a deployment mistake — fail startup loudly.
        missing = [mode.value for mode in LLMMode if mode not in self.modes]
        if missing:
            raise ValueError(f"gateway config missing modes: {missing}")
        return self


class TokenGrant(BaseModel):
    """What one agent token is allowed to do: identify a service, use one mode."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    service: str = Field(description="Calling service name, e.g. 'watcher-agent'.")
    allowed_mode: LLMMode = Field(description="The single mode this token may use.")


class TokenMap:
    """The token→grant IAM table.

    Deliberately not a Pydantic model: keys are secret token values, so the map
    must never be serializable and its ``repr`` shows only service names.
    Lookup compares in constant time against every entry, matching
    :class:`radar_common.AgentTokenAuth`.
    """

    def __init__(self, grants: Mapping[str, TokenGrant]) -> None:
        if not grants:
            raise ConfigurationError("gateway token map has no entries")
        self._grants = dict(grants)

    def lookup(self, token: str | None) -> TokenGrant | None:
        """Return the grant for ``token``, or None if it matches no entry."""
        if not token:
            return None
        candidate = token.encode("utf-8")
        found: TokenGrant | None = None
        # No early return: every stored token is compared so timing does not
        # depend on which (or whether an) entry matched.
        for value, grant in self._grants.items():
            if hmac.compare_digest(candidate, value.encode("utf-8")):
                found = grant
        return found

    def __len__(self) -> int:
        return len(self._grants)

    def __repr__(self) -> str:
        services = sorted(grant.service for grant in self._grants.values())
        return f"TokenMap(services={services})"

    __str__ = __repr__


def load_gateway_config(path: Path) -> GatewayConfig:
    """Load and validate the mode-routing YAML at ``path``.

    Raises :class:`ConfigurationError` (readyz stays 503) on a missing file,
    invalid YAML, or a table that fails validation.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigurationError(f"gateway config file not found: {path}") from None
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"gateway config is not valid YAML: {path}") from exc
    if not isinstance(raw, dict):
        raise ConfigurationError(f"gateway config must be a YAML mapping: {path}")
    try:
        return GatewayConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigurationError(f"gateway config invalid: {path}: {exc}") from exc


def load_token_map(*, directory: Path | None = None) -> TokenMap:
    """Load the token→mode map from the ``gateway_tokens`` Vault secret.

    ``directory`` overrides the secrets directory (tests); production reads the
    init-container mount. Error paths are careful never to include YAML parser
    output or validation input, both of which could quote token values.
    """
    secret = read_secret(GATEWAY_TOKENS_SECRET, directory=directory)
    assert secret is not None  # required=True: read_secret raised if absent
    try:
        raw = yaml.safe_load(secret.get_secret_value())
    except yaml.YAMLError:
        # Parser errors quote source lines; never chain or embed them.
        raise ConfigurationError(
            f"{GATEWAY_TOKENS_SECRET} secret is not valid YAML"
        ) from None
    if not isinstance(raw, dict) or not isinstance(raw.get("tokens"), dict):
        raise ConfigurationError(
            f"{GATEWAY_TOKENS_SECRET} secret must be a mapping with a 'tokens' key"
        )

    grants: dict[str, TokenGrant] = {}
    for position, (token, entry) in enumerate(raw["tokens"].items(), start=1):
        if not isinstance(token, str) or not token:
            raise ConfigurationError(
                f"{GATEWAY_TOKENS_SECRET} entry {position}: token must be a "
                "non-empty string"
            )
        try:
            grants[token] = TokenGrant.model_validate(entry)
        except ValidationError:
            # Entry values (service, allowed_mode) are not secret, but pydantic
            # errors echo their input; identify the entry by position instead.
            raise ConfigurationError(
                f"{GATEWAY_TOKENS_SECRET} entry {position}: expected "
                "{service, allowed_mode}"
            ) from None
    return TokenMap(grants)
