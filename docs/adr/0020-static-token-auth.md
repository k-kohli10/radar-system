# 🔑 ADR 0020: Static Token Auth for Internal Services in V1

> Renumbered from ADR 0013 when this was moved out of
> `docs/implementation_plan.md`. ADR 0013 was already taken by
> [0013-watcher-correlation-scope.md](0013-watcher-correlation-scope.md), a different decision.

**Status**: Accepted
**Date**: 2025-01-15
**Author**: Kashyap

---

## Contents

- [Context](#context)
- [Decision](#decision)
- [Why Not No Auth](#why-not-no-auth)
- [Why Not JWT](#why-not-jwt)
- [Why Not Mutual TLS](#why-not-mutual-tls)
- [What This Looks Like in Practice](#what-this-looks-like-in-practice)
- [Security Properties This Provides](#security-properties-this-provides)
- [Security Properties This Does Not Provide](#security-properties-this-does-not-provide)
- [Migration Path](#migration-path)
- [Decision Record](#decision-record)

---

## Context

RADAR services call each other. The outbox-worker calls agent HTTP endpoints. Agents
call the LLM gateway. The knowledge service calls the LLM gateway. These internal
calls need some form of authentication so that a misconfigured or compromised service
cannot call the LLM gateway and run up an API bill or exfiltrate context.

The options considered were: no auth, static shared tokens, JWT with short TTL,
and mutual TLS.

---

## Decision

Static 32-byte hex tokens per service. One token per service. Stored in Vault.
Loaded at startup via init-container. Validated on every internal request.

Token format: `secrets.token_hex(32)` which produces a 64-character hex string.

At the LLM gateway specifically, each token also maps to exactly one allowed mode,
enforcing that watcher can only make fast calls, reasoner can only make extended
calls, and so on.

---

## Why Not No Auth

No internal auth means any process that can reach the LLM gateway can call it.
In a Kubernetes cluster with network policies this is partially mitigated but:

- Network policies are another thing to get right and maintain
- A bug in any service that makes it call the gateway unintentionally would go
  through unchecked
- The mode restriction (watcher cannot make extended calls) would not be enforceable
- There is no audit trail of which service made which LLM call

Static tokens add two lines of code per service and one config value. The cost is
negligible and the benefit is real.

---

## Why Not JWT

JWT with short TTL is the common recommendation for internal service auth in
microservices. The argument is that short-lived tokens limit the blast radius of
a compromised token.

The problems for this specific use case:

**Agents are Kubernetes deployments, not humans.** JWT short TTL makes sense when
a user logs in, gets a token, and uses it for an hour. It does not make much sense
when a pod is running 24/7 and needs a valid token at all times. Short TTL means
you need a token refresh mechanism, which means:
- A token issuing service or endpoint
- Logic in every service to detect expiry and refresh
- Handling the race condition where a token expires mid-request
- Something to go wrong at 3am when the token issuer has an outage

**The security benefit is smaller than it appears.** A compromised container in
your Kubernetes cluster can read memory, environment variables, and mounted files.
If an attacker has that level of access, the difference between a 15-minute JWT
and a long-lived token is not the critical security boundary. The critical boundary
is preventing the container from being compromised in the first place.

**It adds complexity before value is proven.** RADAR does not have users. It does
not have external-facing auth. Adding JWT infrastructure now is solving a problem
that does not exist yet.

---

## Why Not Mutual TLS

mTLS means every service has a certificate and every service validates the caller's
certificate. This is the gold standard for internal service auth in production
microservices at companies with dedicated platform teams.

For this project:

**Certificate management is a significant operational burden.** You need a CA,
certificate issuance, rotation, and distribution. Tools like cert-manager help but
add another moving part. On a small self-hosted deployment, this is substantial
overhead.

**It provides authenticity, not authorization.** mTLS tells you that the caller
is who they say they are, but it does not tell you what they are allowed to do.
For the mode restriction (watcher cannot call extended) you still need
application-level enforcement on top of mTLS. So you end up with mTLS plus tokens
anyway.

**It is a production-grade solution for a system that has not proven its design
yet.** Invest in mTLS after the system works and has users. Not before.

---

## What This Looks Like in Practice

Vault stores one secret per service:

```
secret/radar/watcher-agent    -> agent_token: <64 char hex>
secret/radar/planner-agent    -> agent_token: <64 char hex>
secret/radar/reasoner-agent   -> agent_token: <64 char hex>
secret/radar/knowledge-service -> agent_token: <64 char hex>
secret/radar/outbox-worker    -> agent_token: <64 char hex>
```

The init-container writes the token to `/vault/secrets/agent_token` at pod startup.
The service reads it once at startup via the config loader. If the file is missing,
`/readyz` returns 503 and the pod does not receive traffic.

Every internal HTTP call includes the header `X-Radar-Agent-Token: <token>`.
Services validate this in a FastAPI dependency that runs before every non-health
handler.

If a token is compromised:
1. Generate a new token: `python -c "import secrets; print(secrets.token_hex(32))"`
2. Update the Vault secret
3. Restart the affected pod

Total recovery time: under 2 minutes. No token issuer to fix, no certificate to
rotate, no distributed secret to invalidate.

---

## Security Properties This Provides

- Internal endpoints are not callable without a valid token
- Each service has a unique token, so a compromised token is scoped to one service
- The LLM gateway enforces mode restrictions per token, limiting blast radius
- Tokens are never in logs, never in environment variables, never in code
- Vault access is controlled by Kubernetes service account roles
- The audit log records which service made which LLM call

---

## Security Properties This Does Not Provide

- Protection against a compromised container reading its own token from memory
- Cryptographic proof of caller identity (that is mTLS)
- Short-lived credentials that auto-expire
- Fine-grained per-request authorization

These are acceptable gaps for v1 on a homelab.

---

## Migration Path

When RADAR has external users, real production traffic, and a dedicated ops concern
for security:

1. Keep static tokens as the fallback
2. Add cert-manager to the cluster
3. Issue certificates per service via cert-manager
4. Enable mTLS via a service mesh (Linkerd is lighter than Istio)
5. Remove token validation once mTLS is fully rolled out

The migration is incremental and does not require rewriting application code.

---

## Decision Record

Static 32-byte hex tokens in Vault for v1. Revisit for v2 if RADAR has external
users or a security audit that identifies this as an unacceptable risk.
