"""Mint and rotate RADAR's internal tokens in the local dev Vault.

Every other secret in this repo is created by hand and only *pulled* by script.
That was survivable while there was one token; Phase 7 introduces per-service
agent tokens plus the outbox worker's map of them, and a hand-maintained map
drifts from the secrets it points at. Drift here is not a typo — it is every
dispatch to that target failing 401, which the worker classifies as permanent and
dead-letters immediately, with no retry. So the map is never authored: it is
*derived*, here, from the same values that were just minted.

Two token systems, deliberately distinct (see docs/adr/0011 and the agent-token
section of the implementation plan):

- **agent tokens** — ``X-Radar-Agent-Token`` on ``POST /events``. Each service has
  its own, so a leaked token is scoped to one service. The outbox worker is the
  only caller of those endpoints, so it holds ``dispatch_tokens``: a map from
  target service to *that target's* token, and it sends the target's, not its own.
- **gateway tokens** — the llm-gateway's mode IAM. A separate value with its own
  grant (``service`` + one ``allowed_mode``), so the reasoner's authority to spend
  ``extended`` tokens rotates independently of its identity on the event bus.

Usage (normally via ``make tokens`` / ``make rotate SERVICE=...``)::

    uv run python scripts/dev-mint-tokens.py                      # mint what's missing
    uv run python scripts/dev-mint-tokens.py --rotate reasoner-agent

**Minting is idempotent and never clobbers.** A service that already has a token
keeps it, so this is safe to run on a clean machine, on a half-configured one, or
twice in a row. It is also *convergent*: every run rebuilds ``dispatch_tokens``
from the current per-service tokens, so a map that drifted (or a Vault that lost
its data — the dev server is in-memory) is repaired by running it again.

``--rotate SERVICE`` is the renew/replace path: it generates a fresh token for
that service and performs **both** writes — the service's own secret, and the
worker's ``dispatch_tokens`` entry that points at it. A rotation that did only the
first would leave the worker sending a token the target no longer accepts.

Prints service names and 6-character prefixes. Never a token value.

Note on the running system: rotation is not hot. Between the two pods restarting,
the worker sends the old token and the target rejects it — and a 401 is permanent,
so those events are dead-lettered rather than retried. Rotate on a drained
pipeline (check outbox depth first). See docs/roadmap.md.
"""

from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path
from typing import Any

import httpx
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Services with an inbound agent token — anything exposing a guarded endpoint.
#: The worker is here for its own sake: it has no /events, but its /admin/*
#: dead-letter endpoints are guarded by its own token.
AGENT_SERVICES = (
    "outbox-worker",
    "watcher-agent",
    "planner-agent",
    "reasoner-agent",
    "knowledge-service",
)

#: Targets the outbox worker dispatches to. Its dispatch_tokens map is rebuilt
#: from exactly these, every run. Phase 9's feedback-service joins this list when
#: it exists; until then a recommendation.created event has nowhere to go and will
#: dead-letter, which is correct and expected.
DISPATCH_TARGETS = (
    "watcher-agent",
    "planner-agent",
    "reasoner-agent",
)

#: Gateway grants: service -> its single allowed mode. One token, one mode, per
#: the Locked Decision. This table is AUTHORITATIVE: a service not listed here has
#: no business calling the gateway, and its token is pruned from the map on the
#: next run. Otherwise deleting a grant would be cosmetic — the credential would
#: keep working, which is the opposite of what deleting it is supposed to mean.
GATEWAY_GRANTS = {
    "reasoner-agent": "extended",
    # Phase 8: the knowledge service embeds runbook chunks and queries. It never
    # calls OpenAI directly — the gateway is the only thing holding provider keys
    # — so indexing and retrieval both depend on this grant existing.
    "knowledge-service": "embed",
}

#: Inbound webhook tokens, one per alert source (ADR 0011): each source's token is
#: its own secret so one can be revoked without touching the others. Minted here
#: rather than seeded because they are generated, not human-supplied — and if they
#: lived only on disk, a wiped Vault would lose them permanently.
WEBHOOK_SOURCES = ("prometheus", "kibana", "mock")

GATEWAY_PATH = "secret/data/radar/llm-gateway"
INGESTION_PATH = "secret/data/radar/ingestion"


def service_path(service: str) -> str:
    return f"secret/data/radar/{service}"


def new_token() -> str:
    """A 64-char hex token, the platform's one token shape."""
    return secrets.token_hex(32)


def brief(token: str) -> str:
    """A safe-to-print handle for a token: its first 6 hex chars."""
    return f"[{token[:6]}…]"


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


class Vault:
    """The dev Vault's KV v2 API, reduced to the three calls this needs."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def read(self, path: str) -> dict[str, Any]:
        """Return the secret's data, or ``{}`` if nothing is stored there."""
        response = self._client.get(f"/v1/{path}")
        if response.status_code == 404:
            return {}
        response.raise_for_status()
        data: dict[str, Any] = response.json()["data"]["data"]
        return data

    def write(self, path: str, data: dict[str, Any]) -> None:
        """Replace the secret at ``path`` with ``data``."""
        response = self._client.post(f"/v1/{path}", json={"data": data})
        response.raise_for_status()


def mint_agent_tokens(vault: Vault, *, rotate: str | None) -> dict[str, str]:
    """Ensure every service has an agent token. Returns ``{service: token}``.

    Existing tokens are preserved — this is the property that makes the command
    safe to re-run — except for ``rotate``, whose token is replaced.
    """
    tokens: dict[str, str] = {}
    for service in AGENT_SERVICES:
        secret = vault.read(service_path(service))
        existing = secret.get("agent_token")
        if existing and service != rotate:
            tokens[service] = existing
            print(f"  {service:<16} agent_token  kept    {brief(existing)}")
            continue
        token = new_token()
        secret["agent_token"] = token
        vault.write(service_path(service), secret)
        tokens[service] = token
        verb = "ROTATED" if service == rotate else "minted "
        print(f"  {service:<16} agent_token  {verb} {brief(token)}")
    return tokens


def rebuild_dispatch_map(vault: Vault, agent_tokens: dict[str, str]) -> None:
    """Rewrite the worker's ``dispatch_tokens`` from the tokens just established.

    Derived, never authored — so it cannot drift from the per-service secrets it
    points at. This is also the second write of a rotation: the target's new token
    is worthless until the worker knows to send it.
    """
    mapping = {target: agent_tokens[target] for target in DISPATCH_TARGETS}
    path = service_path("outbox-worker")
    secret = vault.read(path)
    secret["dispatch_tokens"] = yaml.safe_dump(mapping, sort_keys=True)
    vault.write(path, secret)
    print(f"  outbox-worker    dispatch_tokens rebuilt for {len(mapping)} target(s):")
    for target, token in sorted(mapping.items()):
        print(f"      -> {target:<16} {brief(token)}")


def mint_gateway_tokens(vault: Vault, *, rotate: str | None) -> None:
    """Ensure every gateway-calling service has a token granting its one mode.

    The gateway map is keyed BY TOKEN, so rotating a service means removing its
    old key and adding a new one — not editing a value in place. The service's own
    secret gets a copy under ``gateway_token``, which is the file its pod reads.
    """
    gateway = vault.read(GATEWAY_PATH)
    raw = gateway.get("gateway_tokens")
    doc = yaml.safe_load(raw) if raw else {}
    tokens: dict[str, dict[str, str]] = (doc or {}).get("tokens") or {}

    # Prune first: a token whose service is no longer granted is revoked, not
    # merely un-refreshed. GATEWAY_GRANTS is the source of truth for who may call
    # the gateway, so removing a line from it must actually take the key away.
    for token, grant in list(tokens.items()):
        if grant["service"] not in GATEWAY_GRANTS:
            tokens.pop(token)
            print(
                f"  {grant['service']:<16} gateway_token PRUNED  {brief(token)} "
                f"(no longer granted)"
            )

    by_service = {grant["service"]: tok for tok, grant in tokens.items()}

    for service, mode in GATEWAY_GRANTS.items():
        existing = by_service.get(service)
        if existing and service != rotate:
            print(f"  {service:<16} gateway_token kept    {brief(existing)} ({mode})")
            continue
        if existing:  # rotating: drop the old key entirely
            tokens.pop(existing, None)
        token = new_token()
        tokens[token] = {"service": service, "allowed_mode": mode}
        verb = "ROTATED" if service == rotate else "minted "
        print(f"  {service:<16} gateway_token {verb} {brief(token)} ({mode})")
        # The pod reads its own token from its own secret; the gateway reads the
        # map. Both must carry the same value, so they are written together.
        if service in AGENT_SERVICES:
            secret = vault.read(service_path(service))
            secret["gateway_token"] = token
            vault.write(service_path(service), secret)

    gateway["gateway_tokens"] = yaml.safe_dump({"tokens": tokens}, sort_keys=False)
    vault.write(GATEWAY_PATH, gateway)


def mint_webhook_tokens(vault: Vault) -> None:
    """Ensure each alert source has its own inbound webhook token.

    Not rotatable through ``--rotate``, which takes a service: these are keyed by
    *source*, and ingestion is one service holding all three. Rotate one by
    deleting its field in Vault and re-running — the mint path will regenerate it.
    """
    secret = vault.read(INGESTION_PATH)
    for source in WEBHOOK_SOURCES:
        field = f"webhook_token_{source}"
        existing = secret.get(field)
        if existing:
            print(f"  ingestion        {field:<26} kept    {brief(existing)}")
            continue
        token = new_token()
        secret[field] = token
        print(f"  ingestion        {field:<26} minted  {brief(token)}")
    vault.write(INGESTION_PATH, secret)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--rotate",
        metavar="SERVICE",
        help=(
            "Replace this service's tokens with fresh ones. Rotates its agent "
            "token, its gateway token if it has one, and the worker's map entry "
            "pointing at it. Rotate on a drained pipeline (see docs/roadmap.md)."
        ),
    )
    args = parser.parse_args()

    known = set(AGENT_SERVICES) | set(GATEWAY_GRANTS)
    if args.rotate and args.rotate not in known:
        sys.exit(
            f"ERROR: unknown service {args.rotate!r}. Known: {', '.join(sorted(known))}"
        )

    env = read_env()
    addr = env.get("VAULT_ADDR", "http://localhost:8200")
    root = env.get("VAULT_DEV_ROOT_TOKEN")
    if not root:
        sys.exit("ERROR: VAULT_DEV_ROOT_TOKEN not set in .env")

    try:
        with httpx.Client(
            base_url=addr, headers={"X-Vault-Token": root}, timeout=10
        ) as client:
            vault = Vault(client)
            print(f"minting into Vault at {addr}")
            if args.rotate:
                print(f"ROTATING {args.rotate} — restart that pod and outbox-worker\n")
            agent_tokens = mint_agent_tokens(vault, rotate=args.rotate)
            rebuild_dispatch_map(vault, agent_tokens)
            mint_gateway_tokens(vault, rotate=args.rotate)
            mint_webhook_tokens(vault)
    except httpx.ConnectError:
        sys.exit(
            f"ERROR: cannot reach Vault at {addr} — is the stack up? "
            "(make dev / make start s=vault)"
        )

    print("\ndone — pull the files with `make agent-secrets` / `make gateway-secrets`")


if __name__ == "__main__":
    main()
