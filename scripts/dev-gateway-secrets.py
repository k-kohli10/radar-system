"""Pull llm-gateway secrets from the local dev Vault into secret files.

The local stand-in for the Kubernetes init-container (ADR 0007): the gateway
only ever reads secret FILES, so after changing a value in Vault you re-run
this and restart the gateway. Usage (normally via `make gateway-secrets`):

    uv run python scripts/dev-gateway-secrets.py

Reads VAULT_ADDR and VAULT_DEV_ROOT_TOKEN from the repo-root .env, and writes
into RADAR_SECRETS_DIR (default ~/.radar-dev/secrets):

- every ``*_api_key`` field found at   secret/radar/llm
- the ``gateway_tokens`` field from    secret/radar/llm-gateway

Prints file names only — never secret values.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
LLM_SECRET_PATH = "secret/data/radar/llm"
GATEWAY_SECRET_PATH = "secret/data/radar/llm-gateway"


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
            "write the secret in Vault first."
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

    secrets_dir = Path(
        os.environ.get("RADAR_SECRETS_DIR", str(Path.home() / ".radar-dev/secrets"))
    )
    secrets_dir.mkdir(parents=True, exist_ok=True)

    try:
        with httpx.Client(
            base_url=vault_addr, headers={"X-Vault-Token": token}, timeout=10
        ) as client:
            print(f"pulling from Vault at {vault_addr} -> {secrets_dir}")

            llm = fetch(client, LLM_SECRET_PATH)
            api_keys = {k: v for k, v in llm.items() if k.endswith("_api_key")}
            if not api_keys:
                sys.exit(
                    "ERROR: no *_api_key fields at secret/radar/llm — "
                    "add e.g. openai_api_key in Vault first."
                )
            for name, value in sorted(api_keys.items()):
                write_secret_file(secrets_dir, name, value)

            gateway = fetch(client, GATEWAY_SECRET_PATH)
            tokens_yaml = gateway.get("gateway_tokens")
            if not tokens_yaml:
                sys.exit("ERROR: no gateway_tokens field at secret/radar/llm-gateway.")
            # gateway_tokens is a YAML document, not a single line — write as-is.
            (secrets_dir / "gateway_tokens").write_text(tokens_yaml)
            (secrets_dir / "gateway_tokens").chmod(0o600)
            print(f"  wrote {secrets_dir / 'gateway_tokens'}")
    except httpx.ConnectError:
        sys.exit(
            f"ERROR: cannot reach Vault at {vault_addr} — is the stack up? "
            "(make dev / make start s=vault)"
        )

    print("done — restart the gateway to pick these up (make gateway)")


if __name__ == "__main__":
    main()
