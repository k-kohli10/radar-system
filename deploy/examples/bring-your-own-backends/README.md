# 🏗️ Example: bring-your-own-backends

The production pattern. RADAR's app chart runs against **your** managed backends
instead of the bundled dev stack: you do not install `platform-deps`.

## What you provide

| Dependency | What RADAR needs |
|---|---|
| **Postgres** | A database + user. Set `vault.postgresHost` and store the DSN at `secret/radar/postgres` (`postgres_dsn`). RADAR runs the schema migration itself (the `db-migration` Job), or run it yourself and set `migration.enabled: false`. |
| **Elasticsearch** | A reachable cluster. Set `env.RADAR_ELASTICSEARCH_URL`. |
| **Vault** | Reachable, with the **kubernetes auth method** configured (see below) and `secret/radar/*` populated. Set `vault.addr`. |
| **LLM provider** | The API key(s) at `secret/radar/llm` (e.g. `openai_api_key`). |
| **Slack** (optional) | `slack_bot_token` + `slack_app_token` at `secret/radar/feedback-service`. |

## Vault setup

Each service's pod authenticates to Vault with the kubernetes auth method as
`role=radar-<service>`, bound to ServiceAccount `<service>` in the `radar`
namespace, and reads its secrets from `secret/radar/*`. Your Vault must have:

- `auth/kubernetes` enabled and configured for your cluster (a token reviewer
  with `system:auth-delegator`);
- a policy + role `radar-<service>` per service granting read on that service's
  paths;
- the secrets seeded (agent/gateway tokens, provider key, `postgres_dsn`, …).

The bundled bootstrap is the exact reference. Read
[`deploy/helm/platform-deps/files/k8s-vault-bootstrap.py`](../../helm/platform-deps/files/k8s-vault-bootstrap.py)
and the token model in
[`scripts/dev-mint-tokens.py`](../../../scripts/dev-mint-tokens.py). Adapt it to
your Vault, or run your own equivalent.

## Install

```bash
# (platform-deps is NOT installed)
helm install radar deploy/helm/radar -n radar --create-namespace \
  -f deploy/examples/bring-your-own-backends/values.yaml
```

Fill in every `<...>` in [`values.yaml`](values.yaml) first.
