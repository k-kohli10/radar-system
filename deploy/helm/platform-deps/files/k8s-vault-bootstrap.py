"""One-time bootstrap of the dev Vault for the k8s platform-deps stack.

Run as a Helm post-install/upgrade Job against the in-cluster dev Vault. It makes
the vault-init containers' `vault login -method=kubernetes role=radar-<service>`
work, and seeds every secret those containers read.

It does NOT re-implement token minting: it imports the SAME module the local/compose
flow uses (dev_mint_tokens, a byte-identical chart copy) and calls its functions, so
the token model (agent tokens, gateway grants, dispatch map, webhook sources) has a
single source. This script adds only what is k8s-specific:

  1. enable + configure the kubernetes auth method (token reviewer = a long-lived
     ServiceAccount JWT with system:auth-delegator),
  2. a policy + role per service (role radar-<svc> bound to SA <svc> in ns radar),
  3. the human-supplied / derived secrets the mint script does not own:
     the provider API key (secret/radar/llm) and postgres_dsn (secret/radar/postgres).

Idempotent: safe to re-run (Helm re-runs it on every upgrade). All config from env.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import dev_mint_tokens as dmt
import httpx

# Per-service Vault read paths, mirroring deploy/compose/vault-init/fetch-secrets.sh.
# kv-v2 policy paths use the `secret/data/...` prefix.
_POSTGRES = "secret/data/radar/postgres"
SERVICE_READ_PATHS: dict[str, list[str]] = {
    "llm-gateway": ["secret/data/radar/llm", "secret/data/radar/llm-gateway"],
    "ingestion": ["secret/data/radar/ingestion", _POSTGRES],
    "watcher-agent": ["secret/data/radar/watcher-agent", _POSTGRES],
    "planner-agent": ["secret/data/radar/planner-agent", _POSTGRES],
    "reasoner-agent": ["secret/data/radar/reasoner-agent", _POSTGRES],
    "outbox-worker": ["secret/data/radar/outbox-worker", _POSTGRES],
    "knowledge-service": ["secret/data/radar/knowledge-service", _POSTGRES],
    "feedback-service": ["secret/data/radar/feedback-service", _POSTGRES],
}


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        sys.exit(f"ERROR: required env {name} is not set")
    return value


def wait_for_vault(client: httpx.Client) -> None:
    for _ in range(60):
        try:
            # 200 (unsealed active), 429 (standby), 501 (uninit) all mean "reachable".
            client.get("/v1/sys/health", timeout=5)
            return
        except httpx.HTTPError:
            time.sleep(2)
    sys.exit("ERROR: Vault never became reachable")


def enable_kubernetes_auth(client: httpx.Client, reviewer_jwt: str, ca_cert: str) -> None:
    mounts = client.get("/v1/sys/auth").json()
    if "kubernetes/" not in mounts.get("data", mounts):
        client.post("/v1/sys/auth/kubernetes", json={"type": "kubernetes"}).raise_for_status()
        print("enabled kubernetes auth method")
    host = f"https://{_env('KUBERNETES_SERVICE_HOST')}:{_env('KUBERNETES_SERVICE_PORT')}"
    client.post(
        "/v1/auth/kubernetes/config",
        json={
            "kubernetes_host": host,
            "kubernetes_ca_cert": ca_cert,
            "token_reviewer_jwt": reviewer_jwt,
        },
    ).raise_for_status()
    print(f"configured kubernetes auth (host {host})")


def write_roles_and_policies(client: httpx.Client, radar_ns: str) -> None:
    for service, paths in SERVICE_READ_PATHS.items():
        policy = "\n".join(
            f'path "{p}" {{ capabilities = ["read"] }}' for p in paths
        )
        client.put(
            f"/v1/sys/policies/acl/radar-{service}", json={"policy": policy}
        ).raise_for_status()
        client.post(
            f"/v1/auth/kubernetes/role/radar-{service}",
            json={
                "bound_service_account_names": [service],
                "bound_service_account_namespaces": [radar_ns],
                "policies": [f"radar-{service}"],
                "ttl": "1h",
            },
        ).raise_for_status()
        print(f"role radar-{service} -> SA {service}/{radar_ns} [{', '.join(paths)}]")


def pin_alertmanager_webhook_token(vault: dmt.Vault) -> None:
    """Pin ingestion's prometheus webhook token to the Alertmanager token.

    Alertmanager sends a fixed token (alertmanager.webhookToken); dev_mint_tokens
    would otherwise mint a RANDOM webhook_token_prometheus, so the authenticated
    webhook would 401. Written BEFORE mint_webhook_tokens so that run keeps it.
    """
    token = os.environ.get("RADAR_ALERTMANAGER_WEBHOOK_TOKEN", "").strip()
    if not token:
        return
    secret = vault.read(dmt.INGESTION_PATH)
    secret["webhook_token_prometheus"] = token
    vault.write(dmt.INGESTION_PATH, secret)
    print("pinned ingestion webhook_token_prometheus to the Alertmanager token")


def seed_supplied_secrets(vault: dmt.Vault) -> None:
    """Seed the secrets dev_mint_tokens does not own: provider key + postgres_dsn."""
    dsn = (
        f"postgresql://{_env('POSTGRES_USER')}:{_env('POSTGRES_PASSWORD')}"
        f"@{_env('RADAR_POSTGRES_HOST')}:5432/{_env('POSTGRES_DB')}"
    )
    pg = vault.read("secret/data/radar/postgres")
    pg["postgres_dsn"] = dsn
    vault.write("secret/data/radar/postgres", pg)
    print("seeded postgres_dsn")

    api_key = os.environ.get("RADAR_OPENAI_API_KEY", "").strip()
    if api_key:
        llm = vault.read("secret/data/radar/llm")
        llm["openai_api_key"] = api_key
        vault.write("secret/data/radar/llm", llm)
        print("seeded openai_api_key")
    else:
        print(
            "WARNING: RADAR_OPENAI_API_KEY empty — provider key NOT seeded. The "
            "gateway will 401 upstream until you add it (create the llm-keys secret)."
        )


def read_reviewer_jwt(path: str) -> str:
    """The token-reviewer SA-token Secret is populated asynchronously; wait for it."""
    for _ in range(30):
        jwt = Path(path).read_text().strip() if Path(path).exists() else ""
        if jwt:
            return jwt
        time.sleep(2)
    sys.exit(f"ERROR: reviewer JWT at {path} never populated")


def main() -> None:
    addr = _env("VAULT_ADDR")
    root = _env("VAULT_TOKEN")
    radar_ns = os.environ.get("RADAR_NAMESPACE", "radar")
    reviewer_jwt = read_reviewer_jwt(_env("REVIEWER_JWT_PATH"))
    ca_cert = Path(
        os.environ.get("KUBE_CA_PATH", "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    ).read_text()

    with httpx.Client(base_url=addr, headers={"X-Vault-Token": root}, timeout=15) as client:
        wait_for_vault(client)
        print(f"bootstrapping Vault at {addr}")
        enable_kubernetes_auth(client, reviewer_jwt, ca_cert)
        write_roles_and_policies(client, radar_ns)

        vault = dmt.Vault(client)
        # Reused, single-source token minting (idempotent, convergent).
        agent_tokens = dmt.mint_agent_tokens(vault, rotate=None)
        dmt.rebuild_dispatch_map(vault, agent_tokens)
        dmt.write_knowledge_grant(vault, agent_tokens)
        dmt.mint_gateway_tokens(vault, rotate=None)
        # Pin the prometheus webhook token before minting keeps it, so
        # Alertmanager's authenticated webhook to ingestion matches.
        pin_alertmanager_webhook_token(vault)
        dmt.mint_webhook_tokens(vault)

        seed_supplied_secrets(vault)

    print("bootstrap done")


if __name__ == "__main__":
    main()
