# Running RADAR on Kubernetes (local evaluation)

This is the complete, single-pass runbook for standing RADAR up on a **local
Kubernetes cluster** (Docker Desktop's built-in Kubernetes, or `kind`). It is
dev/evaluation-grade: it installs an in-cluster copy of the platform dependencies
(Postgres, Vault, Elasticsearch, Kibana, Prometheus, Alertmanager, Grafana) so the
whole system runs with two `helm install`s. For production you run RADAR against
managed backends instead — see `deploy/examples/bring-your-own-backends`.

## What gets installed

Two charts, two namespaces:

| Chart | Namespace | Contents |
|---|---|---|
| `deploy/helm/platform-deps` | `radar-infra` | Postgres, Vault (dev), Elasticsearch, Kibana, Prometheus, Alertmanager, Grafana, and a **Vault bootstrap Job** that enables kubernetes auth + seeds every secret |
| `deploy/helm/radar` | `radar` | The 8 RADAR services, each with a Vault init-container, plus two post-install Jobs: **db-migration** (creates the schema) then **knowledge-indexer** (indexes the runbooks) |

Install order matters: **platform-deps first** (it seeds Vault), then **radar**.

## Prerequisites

- **Docker Desktop** with **≥ 10 GB memory** (Settings → Resources → Memory).
  Elasticsearch + Kibana are the drivers.
- **Kubernetes enabled**: Docker Desktop → Settings → Kubernetes → *Enable
  Kubernetes* → Apply. Wait for the status to go green. (Or use `kind`.)
- `kubectl`, `helm` (v3+), and Docker in your PATH.
- Confirm your context:
  ```bash
  kubectl config current-context      # docker-desktop  (or kind-radar)
  kubectl get nodes                   # one node, Ready
  ```

## Step 1 — metrics-server (needed by the HPAs)

Neither Docker Desktop nor kind ships metrics-server. Install it (the
`--kubelet-insecure-tls` patch is required on both):

```bash
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
kubectl -n kube-system patch deployment metrics-server --type=json \
  -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
```

## Step 2 — build the images

Every service is built from its own Dockerfile at the repo root. Build all eight,
tagged exactly as the chart expects (`ghcr.io/k-kohli10/radar-<service>:0.6.0`):

```bash
cd radar-system
for s in ingestion llm-gateway outbox-worker watcher-agent \
         planner-agent reasoner-agent knowledge-service feedback-service; do
  docker build -f apps/$s/Dockerfile -t ghcr.io/k-kohli10/radar-$s:0.6.0 .
done
```

- **Docker Desktop Kubernetes** shares the Docker image store, so the cluster can
  use these directly (the chart sets `imagePullPolicy: IfNotPresent`). No load step.
- **kind** does not — load each image after building:
  `kind load docker-image ghcr.io/k-kohli10/radar-$s:0.6.0 --name radar`.

Platform-dependency images (Postgres, Vault, Elasticsearch, …) are public and are
pulled by the cluster automatically.

## Step 3 — create the supplied secrets

Two secrets hold credentials RADAR does not mint — your LLM key and (optionally)
Slack. Create them in `radar-infra` **before** installing platform-deps so the
Vault bootstrap seeds them:

```bash
kubectl create namespace radar-infra

kubectl -n radar-infra create secret generic radar-llm-keys \
  --from-literal=openai_api_key=sk-YOUR-OPENAI-KEY

# Optional — only if you have a Slack app with Socket Mode. Without it,
# feedback-service stays not-ready (everything else is unaffected).
kubectl -n radar-infra create secret generic radar-slack-keys \
  --from-literal=slack_bot_token=xoxb-YOUR-BOT-TOKEN \
  --from-literal=slack_app_token=xapp-YOUR-APP-TOKEN
```

## Step 4 — install platform-deps

```bash
helm install radar-infra deploy/helm/platform-deps -n radar-infra
kubectl -n radar-infra get pods -w        # wait until all are Running/Ready, then Ctrl-C
```

The `vault-bootstrap` Job runs automatically once Vault is up. Confirm it finished:

```bash
kubectl -n radar-infra get jobs
kubectl -n radar-infra logs job/vault-bootstrap        # ends with "bootstrap done"
```

## Step 5 — install the RADAR app chart

Give the post-install hooks room — the migration + runbook indexing run as Jobs
(the indexer embeds 17 runbooks), so raise the hook timeout:

```bash
helm install radar deploy/helm/radar -n radar --create-namespace --timeout 10m
```

Helm blocks while the hooks run, in order:
1. **db-migration** (weight 0) — creates the Postgres schema.
2. **knowledge-indexer** (weight 10) — embeds the runbooks into Elasticsearch.

Watch them:
```bash
kubectl -n radar get jobs
kubectl -n radar logs job/db-migration
kubectl -n radar logs job/knowledge-indexer
```

## Step 6 — verify

```bash
kubectl -n radar get pods        # all 8 services Running, READY 1/1
kubectl -n radar get hpa         # ingestion + llm-gateway show CPU metrics
```

Open the dashboards (or use the VSCode Kubernetes extension → right-click Service →
Port Forward):
```bash
kubectl -n radar-infra port-forward svc/grafana 3000:3000    # admin / radar-dev-admin-not-a-secret
kubectl -n radar-infra port-forward svc/kibana  5601:5601
```

## Teardown

```bash
helm uninstall radar -n radar
helm uninstall radar-infra -n radar-infra
# Docker Desktop: Settings → Kubernetes → Reset, or leave it.
# kind: kind delete cluster --name radar
```

---

## Troubleshooting

**A pod is `ImagePullBackOff`.** The cluster can't see a locally built image.
On kind, run the `kind load` for it (Step 2). On Docker Desktop, confirm the image
exists (`docker images | grep radar`) and was built with the exact
`:0.6.0` tag.

**`vault` is `CrashLoopBackOff`.** Check `kubectl -n radar-infra logs vault-... --previous`.
The dev Vault runs with `SKIP_SETCAP=true` under a dropped-capabilities context;
if you see a capability error, confirm you are on the current chart.

**`vault-bootstrap` keeps erroring.** It waits for Vault, so give it a moment. If it
persists, read `kubectl -n radar-infra logs job/vault-bootstrap` — it prints each
step (auth method, per-service roles, seeded secrets).

**An app pod is stuck in `Init`.** Its `vault-init` can't log in. Check
`kubectl -n radar logs <pod> -c vault-init`; this traces back to the bootstrap
having created the `radar-<service>` role.

**`knowledge-service` is `0/1`** with `/readyz` reason `elasticsearch: NotFoundError`.
The runbook index is missing — the `knowledge-indexer` Job did not complete. Check
its log; it depends on `db-migration` having run first (the `runbook_documents`
table) and on the gateway + your OpenAI key.

**`feedback-service` is `0/1`** with `Required secret 'slack_bot_token' not found`.
You did not create `radar-slack-keys` (Step 3). Create it, `helm upgrade
radar-infra`, then `kubectl -n radar rollout restart deploy/feedback-service`. This
is expected when you have no Slack app; the rest of the system is unaffected.

**Trace-export warnings** (`UNAVAILABLE … otel-collector …`) are harmless — the
OTel collector DaemonSet is not part of this dev stack, and traces are best-effort.

**`helm install` times out on hooks.** The indexer is still running; re-check with
`kubectl -n radar get jobs`. Use `--timeout 10m` (Step 5). A failed release can be
re-driven with `helm upgrade radar deploy/helm/radar -n radar --timeout 10m`.
