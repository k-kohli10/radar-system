"""Pull ingestion's secrets from the local dev Vault into ITS OWN directory.

The local stand-in for the Kubernetes init-container (ADR 0007): ingestion only
ever reads secret FILES, and per ADR 0011 each source's webhook token lives in
its OWN file so it can be rotated or revoked independently. After changing a
token in Vault, re-run this and restart ingestion. Usage (normally via
`make ingestion-secrets`):

    uv run python scripts/dev-ingestion-secrets.py

Reads VAULT_ADDR and VAULT_DEV_ROOT_TOKEN from the repo-root .env, and writes
into ``$RADAR_SECRETS_DIR/ingestion/`` (default ``~/.radar-dev/secrets``):

- webhook_token_prometheus, webhook_token_kibana, webhook_token_mock
  from the matching fields at secret/radar/ingestion
- postgres_dsn from secret/radar/postgres

The ``ingestion/`` SUBDIRECTORY, matching ``dev-agent-secrets.py``'s one-dir-per-
service layout — not the flat root. Ingestion holds no ``agent_token`` (its
inbound auth is webhook tokens), so the agent-secrets pull skips it, and this
script is the ONLY thing that assembles its directory. It therefore has to
assemble ALL of it: writing the tokens into the flat root left ingestion's
``RADAR_SECRETS_DIR`` without a ``postgres_dsn``, and the service failed
readiness on first launch — found live-firing the Phase 7 pipeline.

Webhook tokens stay three SEPARATE files, never one blob — matching the
production Vault layout, so rotating one source's token never rewrites
another's. Prints file names only — never secret values.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
INGESTION_SECRET_PATH = "secret/data/radar/ingestion"
POSTGRES_PATH = "secret/data/radar/postgres"
WEBHOOK_TOKEN_FIELDS = (
    "webhook_token_prometheus",
    "webhook_token_kibana",
    "webhook_token_mock",
)


def read_env_file() -> dict[str, str]:
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        sys.exit("ERROR: .env not found — run scripts/bootstrap.sh first.")
    values: dict[str, str] = {}
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def fetch(client: httpx.Client, path: str) -> dict[str, str]:
    response = client.get(f"/v1/{path}")
    if response.status_code == 404:
        sys.exit(
            f"ERROR: nothing stored at {path.replace('/data', '')} — "
            "seed the dev Vault first (make seed && make tokens)."
        )
    response.raise_for_status()
    data: dict[str, str] = response.json()["data"]["data"]
    return data


def write_secret_file(directory: Path, name: str, value: str) -> None:
    path = directory / name
    path.write_text(value if value.endswith("\n") else value + "\n")
    path.chmod(0o600)
    print(f"  wrote {path}")


def main() -> None:
    env = read_env_file()
    vault_addr = env.get("VAULT_ADDR", "http://localhost:8200")
    token = env.get("VAULT_DEV_ROOT_TOKEN")
    if not token:
        sys.exit("ERROR: VAULT_DEV_ROOT_TOKEN not set in .env")

    # Ingestion's OWN directory under the root — the same one-dir-per-service
    # layout as dev-agent-secrets.py, and the directory the service launches
    # with as its RADAR_SECRETS_DIR.
    root_dir = Path(
        os.environ.get("RADAR_SECRETS_DIR", str(Path.home() / ".radar-dev/secrets"))
    )
    secrets_dir = root_dir / "ingestion"
    secrets_dir.mkdir(parents=True, exist_ok=True)

    try:
        with httpx.Client(
            base_url=vault_addr, headers={"X-Vault-Token": token}, timeout=10
        ) as client:
            print(f"pulling from Vault at {vault_addr} -> {secrets_dir}")
            ingestion = fetch(client, INGESTION_SECRET_PATH)
            present = {
                field: ingestion[field]
                for field in WEBHOOK_TOKEN_FIELDS
                if ingestion.get(field)
            }
            if not present:
                sys.exit(
                    "ERROR: no webhook_token_* fields at secret/radar/ingestion — "
                    "write at least one (e.g. webhook_token_mock) first."
                )
            # One file per source, so each rotates independently.
            for name, value in sorted(present.items()):
                write_secret_file(secrets_dir, name, value)

            # The DSN too: nothing else assembles this directory (ingestion has
            # no agent_token, so the agent-secrets pull skips it), and a dir
            # with tokens but no postgres_dsn fails readiness at launch.
            dsn = fetch(client, POSTGRES_PATH).get("postgres_dsn")
            if not dsn:
                sys.exit(
                    "ERROR: no postgres_dsn at secret/radar/postgres — "
                    "run `make seed` first."
                )
            write_secret_file(secrets_dir, "postgres_dsn", dsn)
    except httpx.ConnectError:
        sys.exit(
            f"ERROR: cannot reach Vault at {vault_addr} — is the stack up? "
            "(make dev / make start s=vault)"
        )

    print("done — restart ingestion to pick these up")


if __name__ == "__main__":
    main()
