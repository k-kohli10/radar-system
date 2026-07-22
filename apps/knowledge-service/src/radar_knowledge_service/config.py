"""Knowledge-service configuration and its Vault secrets.

The same strict split every RADAR service keeps (docs/adr/0007):

- **Non-secret settings** — Elasticsearch URL, index name, gateway URL, the
  embedding dimension — come from ``RADAR_*`` environment variables via
  :class:`KnowledgeSettings`.
- **Tokens** are secrets and come from Vault-mounted files, never the
  environment: the service's own inbound ``agent_token``, and its TWO outbound
  gateway tokens — ``gateway_token_embed`` and ``gateway_token_reason``. Two
  files because "one token = one mode" is a Locked Decision: query embedding
  spends ``embed``, CRAG grading spends ``reason``, and a leaked embed
  credential must not be spendable on reasoning. There is deliberately no bare
  ``gateway_token`` file for this service — with two grants the bare name has no
  defensible meaning, and the mint script removes it (see dev-mint-tokens.py).

``embedding_dims`` is duplicated from the index's mapping ON PURPOSE: the value
travels from config into the embedding client, which verifies every returned
vector against it, and readiness compares it against what the live index was
actually created with. A mismatch is a model swap without a re-index, and it
should stop the pod from serving rather than degrade every query silently.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import SecretStr
from radar_common import RadarSettings, read_secret

SERVICE_NAME = "knowledge-service"
"""This service's identity: Vault path, metrics label, processed_events actor."""

EMBED_TOKEN_SECRET = "gateway_token_embed"
"""Vault secret filename: the gateway token granting ``embed`` mode."""

REASON_TOKEN_SECRET = "gateway_token_reason"
"""Vault secret filename: the gateway token granting ``reason`` mode (CRAG)."""


class KnowledgeSettings(RadarSettings):
    """Non-secret knowledge-service settings, read from ``RADAR_*`` env vars."""

    service_name: str = SERVICE_NAME
    #: The Elasticsearch cluster holding the runbook chunk index.
    elasticsearch_url: str = "http://localhost:9200"
    #: The chunk index name — must match what the indexer wrote.
    index_name: str = "radar-runbooks"
    #: Must match the index mapping's dense_vector dims AND the gateway's
    #: configured embedding model. See the module docstring.
    embedding_dims: int = 1536
    #: The llm-gateway base URL. The default matches `make gateway`.
    gateway_url: str = "http://127.0.0.1:8081"


def load_gateway_tokens(
    *, directory: Path | None = None
) -> tuple[SecretStr, SecretStr]:
    """Read both outbound gateway tokens: ``(embed, reason)``.

    Both are required: a context API that can embed but not grade would serve
    ungraded context while looking healthy, and one that can grade but not embed
    cannot retrieve at all. Raises ``SecretNotFoundError`` on the first missing
    file, failing startup loudly so ``/readyz`` reports 503.
    """
    embed = read_secret(EMBED_TOKEN_SECRET, directory=directory)
    reason = read_secret(REASON_TOKEN_SECRET, directory=directory)
    assert embed is not None and reason is not None  # required=True raised if absent
    return embed, reason
