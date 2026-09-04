# 🔐 ADR 0007: Vault Secrets via Init-Container Only

## Contents

- [Status](#-status)
- [Context](#-context)
- [Decision](#-decision)
- [Consequences](#-consequences)
- [Comparison](#-comparison)

## 🚦 Status
Accepted

## 🧩 Context
Every RADAR service needs secrets at startup: an agent token, and for llm-gateway,
provider API keys. Common patterns for getting Vault secrets into a pod include a
long-running sidecar, injecting secrets as environment variables via Kubernetes
Secrets synced from Vault, or a one-shot init-container that fetches secrets before
the main container starts.

## ✅ Decision
A Vault init-container fetches secrets once, before the main container starts, and
writes them to files on an in-memory (`emptyDir: {medium: Memory}`) volume mounted at
`/vault/secrets`. The main container reads secrets from those files at startup. No
sidecar. No secrets in environment variables. Rotation works by rotating the value in
Vault and restarting the pod. The init-container re-fetches on next start. There is no
live secret refresh without a restart.

## ⚖️ Consequences
- No long-running Vault-aware process in the pod beyond the app itself, which is one
  less container to patch, monitor, and reason about for every workload.
- Secrets never appear in `kubectl describe pod`, pod env, or process listings. They're
  file contents on a `tmpfs` volume, readable only by the app's own filesystem access.
- Rotation requires a pod restart, not a live in-place refresh. Acceptable for RADAR's
  secret set (agent tokens, provider API keys), since none of them need sub-second
  rotation and a rolling restart is cheap.
- Every workload's Helm template carries the same init-container boilerplate (see the
  Vault Init-Container Pattern in the implementation plan). It's one pattern,
  copy-pasted per service rather than abstracted, so it stays inspectable per workload.

## 🆚 Comparison

| Approach | What it's for | Why RADAR skips it |
|---|---|---|
| Sidecar (e.g. `vault-agent` in template-rendering mode) | Continuous secret sync into a running pod | A second long-running container per pod to patch and monitor |
| Environment variables via Kubernetes Secrets synced from Vault | Simple injection into the main container | Visible via `kubectl describe pod` and process introspection; commonly leaks into crash dumps or verbose logs |
