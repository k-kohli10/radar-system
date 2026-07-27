"""Pull each agent's secrets from the dev Vault into its OWN secret directory.

The local stand-in for the Kubernetes init-container (ADR 0007): a service only
ever reads secret FILES, never the environment. After changing a value in Vault,
re-run this and restart the service.

**One directory per service** — ``$RADAR_SECRETS_DIR/<service>/`` — not one shared
directory. That is forced by the design, not a stylistic choice: every service
reads its token from a file with a fixed name (``agent_token``), so two services
sharing a directory would share one file and therefore one token. Per-service
tokens exist precisely so a leaked one is scoped to a single service, and a flat
directory would quietly collapse that back into a shared secret. Each service
launches with ``RADAR_SECRETS_DIR`` pointed at its own directory, exactly as each
pod gets its own Vault mount in production.

What each service's directory receives:

- ``agent_token``     — its inbound identity (guards its ``POST /events``)
- ``postgres_dsn``    — the shared database DSN, copied per service
- ``gateway_token``   — if it calls the llm-gateway (the reasoner)
- ``dispatch_tokens`` — if it dispatches (the outbox worker): the map of every
  target's token, so it can send the *target's* token rather than its own

Services are discovered from Vault rather than listed here: any ``secret/radar/*``
holding an ``agent_token`` is pulled. So a new agent needs no edit to this file —
mint it, pull it, run it.

Prints file names only — never secret values.

Usage (normally via ``make agent-secrets``)::

    uv run python scripts/dev-agent-secrets.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
POSTGRES_PATH = "secret/data/radar/postgres"

#: Fields copied verbatim from a service's Vault secret into a file of the same
#: name. A service that lacks one simply does not get that file.
#:
#: ``slack_bot_token`` and ``slack_app_token`` joined in Phase 9 for feedback-service:
#: the bot token (``xoxb-``) posts RCA cards, the app-level token (``xapp-``)
#: authenticates the Socket Mode connection that receives button clicks and ``@radar``
#: mentions. Both are now required for the service to go ready (``load_slack_app_token``
#: runs in the lifespan), so both are pulled here — without the app token a clean
#: secrets dir leaves feedback-service stuck at ``/readyz`` 503.
SERVICE_FIELDS = (
    "agent_token",
    "gateway_token",
    "dispatch_tokens",
    "knowledge_token",
    "slack_bot_token",
    "slack_app_token",
)

#: Gateway-token fields are also matched by PREFIX, because a service granted more
#: than one mode holds one token per mode (``gateway_token_embed``,
#: ``gateway_token_reason``) rather than a single ``gateway_token``. Listing them
#: here by hand would mean this file has to be edited every time a grant is added
#: in dev-mint-tokens.py — the drift that script's docstring exists to prevent.
GATEWAY_FIELD_PREFIX = "gateway_token"


def secret_fields(secret: dict[str, Any]) -> list[str]:
    """Which of a secret's fields become files, in a stable order."""
    return sorted(
        {
            field
            for field in secret
            if field in SERVICE_FIELDS or field.startswith(GATEWAY_FIELD_PREFIX)
        }
    )


def prune_stale_gateway_files(directory: Path, keep: set[str]) -> None:
    """Delete gateway-token files Vault no longer has.

    Without this, dropping a grant — or a service going from one grant to
    per-mode tokens — leaves the old file on disk holding a token nothing
    refreshes and the gateway may have already revoked. A service reading it
    fails at call time with a 401 that looks like a gateway problem, when the
    real cause is a file that should not still exist. Writing new files without
    removing dead ones is the same silent-staleness this repo keeps designing
    against.
    """
    for path in sorted(directory.glob(f"{GATEWAY_FIELD_PREFIX}*")):
        if path.name not in keep:
            path.unlink()
            print(f"    removed {path.name} (no longer in Vault)")


def read_env() -> dict[str, str]:
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        sys.exit("ERROR: .env not found — run scripts/bootstrap.sh first.")
    values: dict[str, str] = {}
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def read_secret(client: httpx.Client, path: str) -> dict[str, Any]:
    """Return the secret's data, or ``{}`` if nothing is stored at ``path``."""
    response = client.get(f"/v1/{path}")
    if response.status_code == 404:
        return {}
    response.raise_for_status()
    data: dict[str, Any] = response.json()["data"]["data"]
    return data


def list_services(client: httpx.Client) -> list[str]:
    """Names under ``secret/radar/`` — the candidate services to pull."""
    response = client.request("LIST", "/v1/secret/metadata/radar")
    if response.status_code == 404:
        return []
    response.raise_for_status()
    keys: list[str] = response.json()["data"]["keys"]
    return [k.rstrip("/") for k in keys]


def write_secret_file(directory: Path, name: str, value: str) -> None:
    path = directory / name
    path.write_text(value if value.endswith("\n") else value + "\n")
    path.chmod(0o600)
    print(f"    wrote {path.name}")


def main() -> None:
    env = read_env()
    addr = env.get("VAULT_ADDR", "http://localhost:8200")
    token = env.get("VAULT_DEV_ROOT_TOKEN")
    if not token:
        sys.exit("ERROR: VAULT_DEV_ROOT_TOKEN not set in .env")

    root_dir = Path(
        os.environ.get("RADAR_SECRETS_DIR", str(Path.home() / ".radar-dev/secrets"))
    )

    try:
        with httpx.Client(
            base_url=addr, headers={"X-Vault-Token": token}, timeout=10
        ) as client:
            print(f"pulling from Vault at {addr} -> {root_dir}/<service>/")

            dsn = read_secret(client, POSTGRES_PATH).get("postgres_dsn")
            if not dsn:
                sys.exit(
                    "ERROR: no postgres_dsn at secret/radar/postgres — "
                    "run `make seed` first."
                )

            pulled = 0
            for service in list_services(client):
                secret = read_secret(client, f"secret/data/radar/{service}")
                if "agent_token" not in secret:
                    continue  # not an agent: llm-gateway, ingestion, postgres, …
                directory = root_dir / service
                directory.mkdir(parents=True, exist_ok=True)
                print(f"  {service}/")
                fields = secret_fields(secret)
                for field in fields:
                    value = secret.get(field)
                    if value:
                        write_secret_file(directory, field, value)
                prune_stale_gateway_files(directory, keep=set(fields))
                # Every agent talks to Postgres, and each reads the DSN from its
                # own mount — so it is copied per service, not shared.
                write_secret_file(directory, "postgres_dsn", dsn)
                pulled += 1

            if not pulled:
                sys.exit(
                    "ERROR: no services with an agent_token found in Vault — "
                    "run `make tokens` first."
                )
    except httpx.ConnectError:
        sys.exit(
            f"ERROR: cannot reach Vault at {addr} — is the stack up? "
            "(make dev / make start s=vault)"
        )

    print(f"\ndone — pulled {pulled} service(s); restart them to pick these up")


if __name__ == "__main__":
    main()
