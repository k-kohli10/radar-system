# 🐳 Running RADAR in Docker

RADAR runs as two docker-compose stacks on one shared network.

## Contents

- [Overview](#overview)
- [From scratch](#-from-scratch)
- [Host ports](#-host-ports)
- [End-to-end test](#-end-to-end-test)
- [Commands](#-commands)
- [Limitations](#-limitations)

## Overview

| Stack | File | Contents |
|-------|------|----------|
| `radar-infra` | `docker-compose-infra.yml` | Postgres, Elasticsearch, Vault, Prometheus, Grafana, Alertmanager, otel-collector, fluent-bit, Kibana |
| `radar-apps`  | `docker-compose-apps.yml`  | The 8 deployable services + `platform-sim` |

The apps stack joins infra's `radar-infra_default` network as external, so **infra must
be up first**. This is separate from the native `make dev-apps` workflow — run one
or the other, not both (they share host ports).

Each app has a `<name>-vault-init` sidecar that pulls that service's secrets from
Vault into a shared `/vault/secrets` volume, then the app boots once the sidecar
completes — the compose form of the k8s init-container pattern in
`docs/implementation_plan.md`.

## 🚀 From scratch

```bash
scripts/bootstrap.sh          # generate .env; then set OPENAI_API_KEY (and SLACK_* if used)
make docker-up                # infra --wait -> seed+tokens+migrate -> apps up --build
make docker-apps-ps
```

`make docker-up` runs: infra up (`--wait`), then `seed`/`tokens` (Vault via
`127.0.0.1:8200`), `migrate` (Postgres via `:5432`), then the apps. Seeding and
migrations are host steps because infra publishes Vault and Postgres on loopback.

## 🔌 Host ports

| Service | Port | Service | Port |
|---------|------|---------|------|
| llm-gateway | 8081 | outbox-worker | 8094 |
| ingestion | 8090 | knowledge-service | 8095 |
| watcher-agent | 8091 | feedback-service | 8096 |
| planner-agent | 8092 | platform-sim | 8099 |
| reasoner-agent | 8093 | | |

Every image listens internally on 8080; inside the network services address each
other as `<name>:8080`.

## 🧪 End-to-end test

With the stack up (see [From scratch](#-from-scratch); `OPENAI_API_KEY` in `.env`
is required — the gateway won't start without it), verify the full alert → RCA path.

**1. Health.**

```bash
make docker-apps-ps                    # all app containers Up
curl -s localhost:8081/readyz; echo    # llm-gateway ready
curl -s localhost:8099/healthz; echo   # platform-sim (a scrape target — no /readyz)
```

Prometheus targets at http://localhost:9090/targets show the `*:8080` containers UP
(the `host.docker.internal` ones are DOWN by design here). Grafana is at
http://localhost:3000 (`admin` / `GRAFANA_ADMIN_PASSWORD` from `.env`).

**2. Index runbooks.** knowledge-service reports `not_ready` (503) until the
runbook index exists — its readiness checks the index's vector dimension. This
also enables grounded retrieval:

```bash
make agent-secrets && make index      # pulls host secrets, embeds runbooks into ES
curl -s localhost:8095/readyz; echo   # now ready
```

**3. Fire an alert and watch it flow** — post directly to ingestion with its
webhook token (read from the container). This drives ingestion → watcher →
planner → reasoner → RCA:

```bash
TOK=$(docker exec radar-apps-ingestion-1 cat /vault/secrets/webhook_token_mock)
curl -s -X POST http://127.0.0.1:8090/alerts/mock \
  -H "X-Radar-Webhook-Token: $TOK" -H "Content-Type: application/json" \
  -d '{"service_name":"order-service","alert_name":"OrderServiceHighMemory","severity":"medium"}'; echo
docker compose -p radar-apps logs -f reasoner-agent
```

> Use a fresh `service_name` per run — a repeat within 5 minutes deduplicates onto
> the open incident instead of creating a new one.

**4. Confirm the RCA landed.**

```bash
docker exec radar-infra-postgres-1 psql -U radar -d radar -c \
  "select llm_provider, confidence, is_fallback, \
          context_bundle->'retrieval'->>'outcome' as retrieval, left(root_cause,55) \
   from recommendations order by created_at desc limit 3;"
```

Expect `is_fallback = f` (real LLM) and `retrieval = grounded` (a runbook matched).
`is_fallback = t` is the templated fallback — what you'd see with the gateway down.

**Note on chaos endpoints.** `POST /chaos/*` on platform-sim spikes a metric so the
Prometheus rules fire (visible at http://localhost:9090/alerts), e.g.
`curl -XPOST localhost:8099/chaos/order-failures -H 'Content-Type: application/json'
-d '{"rate":0.5,"duration_seconds":600}'`. It exercises the alerting path only — it
does **not** reach ingestion, because Alertmanager v0.27 cannot send the
`X-Radar-Webhook-Token` header (a documented Phase-12 deferral), so that webhook
401s. Drive the incident pipeline with the direct POST above.

## 🛠 Commands

```bash
make docker-infra-up      # infra only (--wait)
make docker-apps-up       # apps only (build + up) — requires Vault already seeded
make docker-apps-restart  # re-pull secrets + restart apps (after re-seed / rotate)
make docker-apps-ps       # apps status
make docker-apps-logs     # follow apps logs
make docker-apps-down     # stop apps, remove secret volumes
make docker-down          # everything, volumes included
```

**Secrets are read at boot.** The vault-init sidecar materialises them once, then
the app starts — so Vault must be seeded *before* the apps come up (`make docker-up`
does this; a standalone `make docker-apps-up` assumes `make seed && make tokens`
already ran). After re-seeding or `make rotate`, a plain `up` won't refresh a
running app — run `make docker-apps-restart` to re-run the sidecars and restart it.

## ⚠️ Limitations

- **App logs aren't shipped to Elasticsearch.** fluent-bit tails the native
  `.dev-run/*.log`; containers log to stdout (`docker logs`). Container-log
  shipping lands with the Phase 12 Helm chart.
- **Seeding is a host step**, not yet a containerised one-shot.
- **Secret volumes are disk-backed**, not tmpfs (the sidecar exits before the app
  starts). `make docker-apps-down` clears them.
