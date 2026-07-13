"""The worker's two sets of credentials — the one it accepts, and the ones it sends.

**Inbound** (``AdminAuth``): the dead-letter admin endpoints touch the database, so
they are guarded by the worker's *own* ``X-Radar-Agent-Token``. The worker loads
that token from Vault during startup, so the auth dependency is built at app-build
time over an accessor and late-binds the token: until it loads, the dependency
answers 503; once loaded, a bad or missing token is 401.

**Outbound** (``DispatchTokenMap``): every agent has its OWN agent token, so the
worker cannot authenticate to them by presenting its own — it must present *the
target's*. It therefore holds a map of them, loaded from the ``dispatch_tokens``
Vault secret (a YAML ``{service: token}`` document written by ``make tokens``).

That the worker holds every target's token is not a weakening of the per-service
model — it is forced by it. The worker is the only caller of any ``/events``
endpoint, so someone has to hold the keys, and it is already the one component that
can forge any event it likes. Per-service tokens still buy what they are meant to:
a token leaked from the *watcher* opens the watcher and nothing else.

A target with no token in the map fails **closed** — the dispatch is refused before
any request is made, rather than sending nothing and collecting a confusing 401
from the far end (see the dispatcher's ``no_dispatch_token`` outcome).

Constant-time comparison is reused from :class:`radar_common.AgentTokenAuth`.
Nothing here logs or embeds a token value, and the map's ``repr`` shows only
service names.
"""

# NOTE: no ``from __future__ import annotations`` here. The dependency's
# ``Header()`` annotation must stay a real object for FastAPI to resolve it into a
# header parameter (stringized annotations would leave it unresolved), matching
# radar_common.AgentTokenAuth and ingestion's security module.

from collections.abc import Callable, Coroutine, Mapping
from pathlib import Path
from typing import Annotated, Any

import yaml
from fastapi import Header, HTTPException, status
from pydantic import SecretStr
from radar_common import AgentTokenAuth, ConfigurationError, read_secret

DISPATCH_TOKENS_SECRET = "dispatch_tokens"
"""Vault secret filename holding the YAML ``{target_service: token}`` map."""


class DispatchTokenMap:
    """The tokens the worker presents to each dispatch target.

    Deliberately not a Pydantic model: the values are secret token strings, so the
    map must never serialize and its ``repr`` shows only target names — the same
    treatment the gateway's ``TokenMap`` and ingestion's ``WebhookTokenMap`` get.
    """

    def __init__(self, tokens: Mapping[str, str]) -> None:
        if not tokens:
            raise ConfigurationError("dispatch token map has no entries")
        empty = sorted(name for name, value in tokens.items() if not value)
        if empty:
            raise ConfigurationError(
                f"dispatch token map has empty tokens for: {', '.join(empty)}"
            )
        self._tokens = {name: SecretStr(value) for name, value in tokens.items()}

    def get(self, target_service: str) -> SecretStr | None:
        """Return the token ``target_service`` accepts, or ``None`` if unknown.

        ``None`` means fail closed: the caller refuses the dispatch rather than
        sending an empty or borrowed token.
        """
        return self._tokens.get(target_service)

    def __len__(self) -> int:
        return len(self._tokens)

    @property
    def targets(self) -> list[str]:
        """Sorted names of targets that have a token (safe to log)."""
        return sorted(self._tokens)

    def __repr__(self) -> str:
        return f"DispatchTokenMap(targets={self.targets})"

    __str__ = __repr__


def load_dispatch_tokens(*, directory: Path | None = None) -> DispatchTokenMap:
    """Read the ``dispatch_tokens`` map from its Vault secret file.

    The file is a YAML mapping of target service to that service's agent token,
    written by ``make tokens`` (which derives it from the per-service secrets, so
    it cannot drift from them). ``directory`` overrides the secrets directory in
    tests; production reads the init-container mount.

    Raises :class:`~radar_common.ConfigurationError` on a malformed document — the
    parse error is never included, because a YAML error quotes the offending source
    line, which here would be a token.
    """
    secret = read_secret(DISPATCH_TOKENS_SECRET, directory=directory)
    assert secret is not None  # required=True: read_secret raised if absent
    try:
        document = yaml.safe_load(secret.get_secret_value())
    except yaml.YAMLError:
        raise ConfigurationError(
            f"secret '{DISPATCH_TOKENS_SECRET}' is not valid YAML"
        ) from None
    if not isinstance(document, dict):
        raise ConfigurationError(
            f"secret '{DISPATCH_TOKENS_SECRET}' must be a mapping of "
            "target service to token"
        )
    return DispatchTokenMap({str(k): str(v) for k, v in document.items()})


class AdminAuth:
    """Late-binding ``X-Radar-Agent-Token`` dependency for the admin endpoints.

    Constructed with an accessor rather than the token itself: the worker's
    agent token loads during startup (a missing secret keeps ``/readyz`` at 503
    instead of crashing import), and each request reads the current value.
    """

    def __init__(self, get_auth: Callable[[], AgentTokenAuth | None]) -> None:
        self._get_auth = get_auth

    def require(self) -> Callable[..., Coroutine[Any, Any, None]]:
        """Return the FastAPI dependency enforcing the agent token."""
        get_auth = self._get_auth

        async def dependency(
            x_radar_agent_token: Annotated[str | None, Header()] = None,
        ) -> None:
            auth = get_auth()
            if auth is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="outbox-worker is not ready",
                )
            if not auth.verify(x_radar_agent_token):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid or missing agent token",
                )

        return dependency
