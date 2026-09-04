# 🔑 ADR 0020: Static Token Auth for Internal Services in V1

> Renumbered from ADR 0013 when this was moved out of
> `docs/implementation_plan.md`. ADR 0013 was already taken by
> [0013-watcher-correlation-scope.md](0013-watcher-correlation-scope.md), a different decision.

**Status**: Accepted
**Date**: 2025-01-15
**Author**: Kashyap Kohli

---

## Contents

- [Context](#-context)
- [Decision](#-decision)
- [Options Considered](#-options-considered)
- [What This Looks Like in Practice](#-what-this-looks-like-in-practice)
- [Security Properties This Provides](#-security-properties-this-provides)
- [Known Limitations (v1)](#-known-limitations-v1)
- [Migration Path](#-migration-path)
- [Decision Record](#-decision-record)

---

## 🧭 Context

RADAR services call each other. The outbox-worker calls agent HTTP endpoints. Agents
call the LLM gateway. The knowledge service calls the LLM gateway. These internal
calls need some form of authentication so that a misconfigured or compromised service
cannot call the LLM gateway and run up an API bill or exfiltrate context.

The options considered were: no auth, static shared tokens, JWT with short TTL,
and mutual TLS.

---

## ⚖️ Decision

Static 32-byte hex tokens per service. One token per service. Stored in Vault.
Loaded at startup via init-container. Validated on every internal request.

Token format: `secrets.token_hex(32)` which produces a 64-character hex string.

At the LLM gateway specifically, each token also maps to exactly one allowed mode,
enforcing that watcher can only make fast calls, reasoner can only make extended
calls, and so on.

---

## 🔀 Options Considered

Static per-service tokens win because they enforce the per-mode restriction and
give an audit trail with two lines of code per service and one config value.

| | What it's for | Why RADAR skips it |
|---|---|---|
| **No auth** | Leaning on Kubernetes network policies alone to gate internal calls | Any process reaching the gateway could run up API bills; the per-mode restriction (watcher can only make fast calls) becomes unenforceable, and there is no audit trail of which service made which LLM call |
| **JWT, short TTL** | Limiting the blast radius of a leaked token, as for human logins | Agents are 24/7 pods, so short TTL adds a token-issuer service, per-service refresh logic, and expiry-mid-request handling. A compromised container can read its own token from memory anyway, so the 15-minute window buys little |
| **Mutual TLS** | Cryptographic proof of caller identity between services | Needs a CA plus issuance, rotation, and distribution. It proves identity, not authorization, so the per-mode restriction still needs token-level enforcement on top: mTLS plus tokens |

---

## 🛠️ What This Looks Like in Practice

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

## 🛡️ Security Properties This Provides

- Internal endpoints are not callable without a valid token
- Each service has a unique token, so a compromised token is scoped to one service
- The LLM gateway enforces mode restrictions per token, limiting blast radius
- Tokens are never in logs, never in environment variables, never in code
- Vault access is controlled by Kubernetes service account roles
- The audit log records which service made which LLM call

---

## ⚠️ Known Limitations (v1)

Acceptable gaps for v1 on a homelab, each addressed by the migration path below:

- A compromised container can read its own token from memory
- Caller identity rests on possession of the token, not cryptographic proof (that is mTLS)
- Tokens are long-lived; they rotate on restart, not on a timer
- Authorization is per-mode, not per-request

---

## 🚚 Migration Path

When RADAR has external users, real production traffic, and a dedicated ops concern
for security:

1. Keep static tokens as the fallback
2. Add cert-manager to the cluster
3. Issue certificates per service via cert-manager
4. Enable mTLS via a service mesh (Linkerd is lighter than Istio)
5. Remove token validation once mTLS is fully rolled out

The migration is incremental and does not require rewriting application code.

---

## ✔️ Decision Record

Static 32-byte hex tokens in Vault for v1. Revisit for v2 if RADAR has external
users or a security audit that identifies this as an unacceptable risk.
