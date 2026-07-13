"""Seed the local dev Vault with the secrets a human supplies.

The dev Vault runs in-memory: restart the container and every secret is gone.
Until now nothing in the repo could put them back — the scripts only ever *pulled*
from Vault — so a wiped Vault meant hand-repairing it before anything would start.
This is the write half, and with `make tokens` it makes a dev Vault fully
reconstructible from an empty one::

    make seed      # what a human supplies:  DSN, provider API key   (this script)
    make tokens    # what the platform mints: agent + gateway tokens (dev-mint-tokens)

That split is the whole design. This script only ever copies values that already
exist in ``.env`` — it generates nothing, so re-running it cannot invent a
credential or invalidate one. Minted credentials are the other script's business,
and it keeps existing ones rather than clobbering them.

Values are read from the repo-root ``.env`` (gitignored; written by
scripts/bootstrap.sh) and written to the Vault paths the services' init-containers
read in production. Prints paths and field names — never a secret value.

Usage (normally via ``make seed``)::

    uv run python scripts/dev-seed-vault.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Vault path -> {field: .env key}. Exactly the human-supplied secrets: the
#: database DSN (which embeds the password) and the provider API key. Everything
#: else RADAR needs is minted, not supplied.
SEED_MAP: dict[str, dict[str, str]] = {
    "secret/data/radar/postgres": {"postgres_dsn": "POSTGRES_DSN"},
    "secret/data/radar/llm": {"openai_api_key": "OPENAI_API_KEY"},
}


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


def main() -> None:
    env = read_env()
    addr = env.get("VAULT_ADDR", "http://localhost:8200")
    root = env.get("VAULT_DEV_ROOT_TOKEN")
    if not root:
        sys.exit("ERROR: VAULT_DEV_ROOT_TOKEN not set in .env")

    try:
        with httpx.Client(
            base_url=addr, headers={"X-Vault-Token": root}, timeout=10
        ) as client:
            print(f"seeding Vault at {addr} from .env")
            for path, fields in SEED_MAP.items():
                # Merge into whatever is already there: a path may hold minted
                # fields too (secret/radar/llm also carries other providers' keys),
                # and seeding must not delete what it did not write.
                existing = client.get(f"/v1/{path}")
                current: dict[str, str] = (
                    existing.json()["data"]["data"]
                    if existing.status_code == 200
                    else {}
                )

                written: list[str] = []
                for field, env_key in fields.items():
                    value = env.get(env_key)
                    if not value:
                        print(
                            f"  SKIP {path.replace('/data', '')}.{field}: "
                            f"{env_key} is empty in .env"
                        )
                        continue
                    current[field] = value
                    written.append(field)

                if not written:
                    continue
                response = client.post(f"/v1/{path}", json={"data": current})
                response.raise_for_status()
                print(f"  wrote {path.replace('/data', '')}: {written}")
    except httpx.ConnectError:
        sys.exit(
            f"ERROR: cannot reach Vault at {addr} — is the stack up? "
            "(make dev / make start s=vault)"
        )

    print("\ndone — now run `make tokens` to mint the agent and gateway tokens")


if __name__ == "__main__":
    main()
