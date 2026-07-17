"""Reasoner configuration and its two secrets.

The same strict split every RADAR service keeps (ADR 0007): non-secret settings from
``RADAR_*`` env vars, secrets from Vault-mounted files, never the environment.

The reasoner is the first agent with an OUTBOUND credential, so it holds two tokens
that must not be confused:

- ``agent_token`` — INBOUND. What the outbox worker presents to *this* service on
  ``POST /events``. Loaded by ``main`` and handed to the shared ``EventsAuth``.
- ``gateway_token`` — OUTBOUND. What *this* service presents to the llm-gateway on
  ``POST /v1/complete``. A different value, with its own grant (``allowed_mode:
  extended``), rotatable independently — the reasoner's authority to spend an
  expensive model is a different thing from its identity on the event bus.

Both travel in the same header name (``X-Radar-Agent-Token``), which is precisely
why they are named apart here: sending the wrong one is a 401 that looks like a
platform bug rather than a config mistake.

``service_name`` is ``reasoner-agent``, and that string is load-bearing in three
places that must agree: the ``target_service`` the planner writes on its outbox
events, the ``processed_by`` column this service writes in ``processed_events``, and
the Vault path its secrets come from.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import SecretStr
from radar_common import RadarSettings, read_secret

POSTGRES_DSN_SECRET = "postgres_dsn"
"""Vault secret filename holding the full Postgres DSN (with password)."""

GATEWAY_TOKEN_SECRET = "gateway_token"
"""Vault secret filename holding this service's OUTBOUND llm-gateway token."""

SERVICE_NAME = "reasoner-agent"
"""This service's identity: outbox target, processed_events actor, Vault path."""


class ReasonerSettings(RadarSettings):
    """Non-secret reasoner settings, read from ``RADAR_*`` env vars."""

    service_name: str = SERVICE_NAME

    #: The llm-gateway base URL. The k8s in-cluster address by default; dev points it
    #: at localhost through the same setting, never a test-only branch in code.
    gateway_url: str = "http://llm-gateway.radar.svc.cluster.local:8080"


def load_postgres_dsn(*, directory: Path | None = None) -> str:
    """Read the Postgres DSN from the ``postgres_dsn`` Vault secret.

    Raises ``SecretNotFoundError`` when the file is absent, failing startup loudly so
    ``/readyz`` reports 503.
    """
    secret = read_secret(POSTGRES_DSN_SECRET, directory=directory)
    assert secret is not None  # required=True: read_secret raised if absent
    return secret.get_secret_value()


def load_gateway_token(*, directory: Path | None = None) -> SecretStr:
    """Read this service's OUTBOUND llm-gateway token from Vault.

    Kept as a :class:`SecretStr` all the way to the request header, so it cannot be
    logged or serialized by accident.

    A missing token is a 503, not a silent degradation. Without it every call to the
    gateway would be rejected 401 and every incident would take the template-fallback
    path — the reasoner would look perfectly healthy while never once using the LLM
    it exists to use. Failing readiness makes that a deploy-time error instead of a
    quality problem nobody notices.
    """
    secret = read_secret(GATEWAY_TOKEN_SECRET, directory=directory)
    assert secret is not None  # required=True: read_secret raised if absent
    return secret
