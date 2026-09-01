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
  A service needing two modes holds TWO tokens, one per mode — never one token
  granting both, which the gateway's grant model does not represent anyway. Its
  secret then carries ``gateway_token_<mode>`` fields instead of a bare
  ``gateway_token``.

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
first would leave the worker sending a token the target no longer accepts. For a
service holding several gateway tokens it rotates ALL of them: a partial rotation
leaves a mix of fresh and stale credentials, and the stale one fails on whichever
code path happens to use that mode.

Prints service names and 6-character prefixes. Never a token value.

Note on the running system: rotation is hot. ``--rotate`` keeps the outgoing token
accepted — it is written to ``agent_token_previous``, which every service accepts
alongside its current ``agent_token`` — so the target and worker pods can restart in
any order without a mid-roll dispatch being rejected. Once both are back up,
``--finalize SERVICE`` clears the previous token; restart the target once more to
stop accepting it. See docs/roadmap.md.
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
    "feedback-service",
)

#: Targets the outbox worker dispatches to. Its dispatch_tokens map is rebuilt
#: from exactly these, every run. feedback-service joined here in Phase 9: without
#: its entry the worker has no token for the reasoner's recommendation.created
#: event and refuses to dispatch it (``no_dispatch_token``, permanent → immediate
#: dead-letter), which would look like a Slack problem when the real cause is a
#: missing token map entry.
DISPATCH_TARGETS = (
    "watcher-agent",
    "planner-agent",
    "reasoner-agent",
    "feedback-service",
)

#: Gateway grants: service -> the modes it may use, ONE TOKEN PER MODE.
#:
#: "One token = one mode" is a Locked Decision and is unchanged here: a service
#: needing two modes gets two separate tokens, each granting exactly one. It does
#: NOT get one token granting two. That keeps the blast radius of a leak at one
#: mode, and it is why this maps to a tuple rather than to a list of modes on a
#: single grant.
#:
#: This table is AUTHORITATIVE: a (service, mode) pair not listed here is pruned
#: from the map on the next run. Otherwise deleting a grant would be cosmetic —
#: the credential would keep working, which is the opposite of what deleting it
#: is supposed to mean.
GATEWAY_GRANTS: dict[str, tuple[str, ...]] = {
    "reasoner-agent": ("extended",),
    # Phase 8: the knowledge service embeds runbook chunks and queries. It never
    # calls OpenAI directly — the gateway is the only thing holding provider keys
    # — so indexing and retrieval both depend on the embed grant existing.
    #
    # `reason` is for CRAG grading, which asks an LLM whether each retrieved
    # chunk actually supports the incident. It was originally granted for
    # cross-encoder reranking; that stage was measured and removed (see
    # tests/retrieval/probes.yaml), and the grant stays because CRAG needs the
    # same mode. Separate token, separate grant: a leaked embed token cannot be
    # spent on reasoning, which is the more expensive mode by an order of
    # magnitude.
    "knowledge-service": ("embed", "reason"),
}


def gateway_field(mode: str) -> str:
    """Field in a service's own Vault secret holding the token for ``mode``.

    A service with a SINGLE grant also keeps the bare ``gateway_token`` field,
    because that is the name its pod already reads (see the reasoner's
    ``GATEWAY_TOKEN_SECRET``). Multi-grant services get per-mode fields only —
    there is no defensible answer to which of two tokens the bare name would
    mean, and a name that silently points at one of them is how the wrong
    credential gets sent.
    """
    return f"gateway_token_{mode}"


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
        if existing and service == rotate:
            # Two-phase rotation: keep the outgoing token accepted (every service
            # accepts agent_token_previous alongside agent_token) while the target
            # and worker restart, so an event dispatched mid-roll is never rejected
            # — a 401 is permanent and would dead-letter it. Cleared by --finalize.
            secret["agent_token_previous"] = existing
            print(f"  {service:<16} agent_token_previous kept    {brief(existing)}")
        secret["agent_token"] = token
        vault.write(service_path(service), secret)
        tokens[service] = token
        verb = "ROTATED" if service == rotate else "minted "
        print(f"  {service:<16} agent_token  {verb} {brief(token)}")
    return tokens


def finalize_agent_rotation(vault: Vault, service: str) -> None:
    """Drop a service's ``agent_token_previous`` once a rotation has converged.

    After ``--rotate`` and both the target and worker have restarted, the previous
    token is no longer in flight — clearing it stops the retired credential from
    being accepted. A no-op when there is no previous token (the steady state).
    """
    secret = vault.read(service_path(service))
    if secret.pop("agent_token_previous", None) is None:
        print(f"  {service:<16} agent_token_previous none    (already finalized)")
        return
    vault.write(service_path(service), secret)
    print(f"  {service:<16} agent_token_previous CLEARED — restart {service}")


def write_knowledge_grant(vault: Vault, agent_tokens: dict[str, str]) -> None:
    """Give the reasoner a copy of the knowledge-service's agent token.

    The reasoner calls ``POST /v1/context`` on the knowledge service (Phase 8's
    context API), and the caller presents the TARGET's token — the same rule the
    worker's ``dispatch_tokens`` follows. Rewritten on every run from the token
    just established, so a knowledge-service rotation converges here without a
    second command; a hand-maintained copy would drift exactly the way the
    dispatch map used to.
    """
    token = agent_tokens["knowledge-service"]
    path = service_path("reasoner-agent")
    secret = vault.read(path)
    secret["knowledge_token"] = token
    vault.write(path, secret)
    print(
        f"  reasoner-agent   knowledge_token rebuilt {brief(token)} "
        f"(-> knowledge-service)"
    )


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
    """Ensure every granted (service, mode) pair has its own token.

    The gateway map is keyed BY TOKEN, so rotating means removing the old key and
    adding a new one — not editing a value in place. The service's own secret gets
    a copy under the field its pod reads.

    Rotating a multi-grant service rotates ALL of its tokens. Rotating only one
    would leave the service holding a mix of fresh and stale credentials, and the
    stale one fails exactly where it is least expected: on the code path that
    happens to use the other mode.
    """
    gateway = vault.read(GATEWAY_PATH)
    raw = gateway.get("gateway_tokens")
    doc = yaml.safe_load(raw) if raw else {}
    tokens: dict[str, dict[str, str]] = (doc or {}).get("tokens") or {}

    granted = {
        (service, mode) for service, modes in GATEWAY_GRANTS.items() for mode in modes
    }

    # Prune first: a token whose PAIR is no longer granted is revoked, not merely
    # un-refreshed. Pruning on service alone would miss the case this function now
    # has to handle — a service keeping one grant while losing another — leaving
    # the dropped mode's token live and spendable.
    for token, grant in list(tokens.items()):
        if (grant["service"], grant["allowed_mode"]) not in granted:
            tokens.pop(token)
            print(
                f"  {grant['service']:<16} {gateway_field(grant['allowed_mode']):<22} "
                f"PRUNED  {brief(token)} (no longer granted)"
            )

    by_pair = {
        (grant["service"], grant["allowed_mode"]): tok for tok, grant in tokens.items()
    }

    for service, modes in GATEWAY_GRANTS.items():
        for mode in modes:
            field = gateway_field(mode)
            existing = by_pair.get((service, mode))
            if existing and service != rotate:
                print(f"  {service:<16} {field:<22} kept    {brief(existing)}")
                token = existing
            else:
                if existing:  # rotating: drop the old key entirely
                    tokens.pop(existing, None)
                token = new_token()
                tokens[token] = {"service": service, "allowed_mode": mode}
                verb = "ROTATED" if service == rotate else "minted "
                print(f"  {service:<16} {field:<22} {verb} {brief(token)}")

            # The pod reads its own token from its own secret; the gateway reads
            # the map. Both must carry the same value, so they are written
            # together.
            if service in AGENT_SERVICES:
                secret = vault.read(service_path(service))
                secret[field] = token
                if len(modes) == 1:
                    # Single-grant services keep the bare name their pod reads.
                    secret["gateway_token"] = token
                elif secret.pop("gateway_token", None) is not None:
                    # Multi-grant: the bare field is ambiguous, so it is removed
                    # rather than left pointing at whichever mode was written
                    # last. Leaving it would keep a credential alive under a name
                    # nothing refreshes — live, stale, and silent.
                    print(
                        f"  {service:<16} {'gateway_token':<22} REMOVED "
                        f"(ambiguous: {len(modes)} grants)"
                    )
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
            "pointing at it. The outgoing agent token stays accepted until "
            "`--finalize` (see docs/roadmap.md)."
        ),
    )
    parser.add_argument(
        "--finalize",
        metavar="SERVICE",
        help=(
            "Clear this service's agent_token_previous after a --rotate has "
            "converged (target and worker restarted), so the retired token stops "
            "being accepted. Restart the service once more afterward."
        ),
    )
    args = parser.parse_args()

    known = set(AGENT_SERVICES) | set(GATEWAY_GRANTS)
    if args.rotate and args.rotate not in known:
        sys.exit(
            f"ERROR: unknown service {args.rotate!r}. Known: {', '.join(sorted(known))}"
        )
    if args.finalize and args.finalize not in set(AGENT_SERVICES):
        sys.exit(
            f"ERROR: unknown service {args.finalize!r} for --finalize. "
            f"Known: {', '.join(sorted(AGENT_SERVICES))}"
        )
    if args.rotate and args.finalize:
        sys.exit("ERROR: pass --rotate or --finalize, not both.")

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
            if args.finalize:
                print(f"finalizing rotation for {args.finalize} at {addr}\n")
                finalize_agent_rotation(vault, args.finalize)
                print(
                    "\ndone — pull with `make agent-secrets`, then restart "
                    f"{args.finalize}"
                )
                return
            print(f"minting into Vault at {addr}")
            if args.rotate:
                print(
                    f"ROTATING {args.rotate} — restart it and outbox-worker (any "
                    "order), then `make rotate-finalize`\n"
                )
            agent_tokens = mint_agent_tokens(vault, rotate=args.rotate)
            rebuild_dispatch_map(vault, agent_tokens)
            write_knowledge_grant(vault, agent_tokens)
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
