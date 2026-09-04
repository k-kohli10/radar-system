# ☸️ Running RADAR on Kubernetes (local evaluation)

This is the complete, single-pass runbook for standing RADAR up on a **local
Kubernetes cluster** (Docker Desktop's built-in Kubernetes, or `kind`). It is
dev/evaluation-grade: it installs an in-cluster copy of the platform dependencies
(Postgres, Vault, Elasticsearch, Kibana, Prometheus, Alertmanager, Grafana) so the
whole system runs with two `helm install`s. For production you run RADAR against
managed backends instead: see `deploy/examples/bring-your-own-backends`.

## Contents

- [What gets installed](#-what-gets-installed)
- [Prerequisites](#-prerequisites)
- [Step 0: confirm the cluster](#-step-0-confirm-the-cluster)
- [Step 1: metrics-server (for the HPAs)](#-step-1-metrics-server-for-the-hpas)
- [Step 2: build the 8 images](#-step-2-build-the-8-images)
- [Step 3: create the namespace + supplied secrets](#-step-3-create-the-namespace--supplied-secrets)
- [Step 4: install platform-deps, wait for it](#-step-4-install-platform-deps-wait-for-it)
- [Step 5: confirm the Vault bootstrap finished](#-step-5-confirm-the-vault-bootstrap-finished)
- [Step 6: install the app chart (raise the hook timeout)](#-step-6-install-the-app-chart-raise-the-hook-timeout)
- [Step 7: watch the hooks (migration first, then indexer)](#-step-7-watch-the-hooks-migration-first-then-indexer)
- [Step 8: verify](#-step-8-verify)
- [Step 9: open dashboards (optional)](#-step-9-open-dashboards-optional)
- [Teardown](#-teardown)
- [Troubleshooting](#-troubleshooting)

## 📦 What gets installed

Two charts, two namespaces:

| Chart | Namespace | Contents |
|---|---|---|
| `deploy/helm/platform-deps` | `radar-infra` | Postgres, Vault (dev), Elasticsearch, Kibana, Prometheus, Alertmanager, Grafana, and a **Vault bootstrap Job** that enables kubernetes auth + seeds every secret |
| `deploy/helm/radar` | `radar` | The 8 RADAR services, each with a Vault init-container, plus two post-install Jobs: **db-migration** (creates the schema) then **knowledge-indexer** (indexes the runbooks) |

Install order matters: **platform-deps first** (it seeds Vault), then **radar**.

## 🧰 Prerequisites

| Requirement | Detail |
|---|---|
| Docker Desktop | **≥ 10 GB memory** (Settings → Resources → Memory). Elasticsearch + Kibana are the drivers. |
| Kubernetes enabled | Docker Desktop → Kubernetes → *Create Kubernetes Cluster*. Choose **Kubeadm** (single node) when prompted for a cluster type: it uses Docker's normal image store, so the images you build in Step 2 are directly visible with **no `kind load` step**, and the whole stack fits on one node. The **kind** option is multi-node but *"Requires the containerd image store"* (a global Docker setting change), unnecessary for evaluation, so prefer Kubeadm. Any offered Kubernetes version works (the charts pin none). Wait for the node to go `Ready`. |
| CLIs | `kubectl`, `helm` (v3+), and Docker in your PATH. |

Run the steps in order. Steps 4 and 5 must finish before Step 6 (the bootstrap seeds
the secrets the app pods read). Commands assume you run them from `radar-system/`.

## 🔍 Step 0: confirm the cluster

```bash
kubectl config current-context     # docker-desktop
kubectl get nodes                  # one node, Ready
```

## 📊 Step 1: metrics-server (for the HPAs)

Docker Desktop does not ship metrics-server. Install it (the
`--kubelet-insecure-tls` patch is required):

```bash
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
kubectl -n kube-system patch deployment metrics-server --type=json \
  -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
```

## 🏗️ Step 2: build the 8 images

Kubeadm shares the Docker image store, so the cluster uses these directly (the
chart sets `imagePullPolicy: IfNotPresent`): **no load step**. Tag them exactly as
the chart expects (`ghcr.io/k-kohli10/radar-<service>:0.6.0`):

```bash
cd "/Users/kkohli/Documents/Kashyap Portfolio/RADAR/radar-system"
for s in ingestion llm-gateway outbox-worker watcher-agent \
         planner-agent reasoner-agent knowledge-service feedback-service; do
  docker build -f apps/$s/Dockerfile -t ghcr.io/k-kohli10/radar-$s:0.6.0 . || break
done
```

Platform-dependency images (Postgres, Vault, Elasticsearch, …) are public and are
pulled by the cluster automatically. (On `kind` instead of Kubeadm, add
`kind load docker-image ghcr.io/k-kohli10/radar-$s:0.6.0 --name radar` after each build.)

## 🔐 Step 3: create the namespace + supplied secrets

The LLM key and (optionally) Slack are credentials RADAR does not mint. Create
them in `radar-infra` **before** platform-deps so the Vault bootstrap seeds them.
This is the bundled dev-Vault path, re-supplied per cluster; for a persistent /
HCP Vault where these are entered once and survive cluster rebuilds (and CD carries
nothing sensitive), see [secrets.md](secrets.md).

```bash
kubectl create namespace radar-infra

kubectl -n radar-infra create secret generic radar-llm-keys \
  --from-literal=openai_api_key=sk-YOUR-OPENAI-KEY

# Optional: only if you have a Slack app with Socket Mode. Without it,
# feedback-service stays not-ready (everything else is unaffected).
kubectl -n radar-infra create secret generic radar-slack-keys \
  --from-literal=slack_bot_token=xoxb-YOUR-BOT-TOKEN \
  --from-literal=slack_app_token=xapp-YOUR-APP-TOKEN
```

## 📥 Step 4: install platform-deps, wait for it

```bash
helm install radar-infra deploy/helm/platform-deps -n radar-infra
kubectl -n radar-infra get pods -w        # wait until all are Running/Ready, then Ctrl-C
```

## ✅ Step 5: confirm the Vault bootstrap finished

The `vault-bootstrap` Job runs automatically once Vault is up:

```bash
kubectl -n radar-infra get jobs
kubectl -n radar-infra logs job/vault-bootstrap        # ends with "bootstrap done"
```

## 📦 Step 6: install the app chart (raise the hook timeout)

The post-install hooks run as Jobs (migration, then the indexer embedding 17
runbooks), so give them room:

```bash
helm install radar deploy/helm/radar -n radar --create-namespace --timeout 10m
```

## 👀 Step 7: watch the hooks (migration first, then indexer)

```bash
kubectl -n radar get jobs
kubectl -n radar logs job/db-migration          # creates the Postgres schema (weight 0)
kubectl -n radar logs job/knowledge-indexer     # embeds the runbooks (weight 10)
```

## ✔️ Step 8: verify

```bash
kubectl -n radar get pods        # all 8 services Running, READY 1/1 (7/8 if Slack skipped)
kubectl -n radar get hpa         # ingestion + llm-gateway show CPU metrics
```

## 📈 Step 9: open dashboards (optional)

Or use the VSCode Kubernetes extension → right-click a Service → Port Forward.

```bash
kubectl -n radar-infra port-forward svc/grafana 3000:3000    # http://localhost:3000  (user admin)
kubectl -n radar-infra port-forward svc/kibana  5601:5601
# Grafana's admin password is generated per cluster: read it from the Secret:
kubectl -n radar-infra get secret radar-grafana -o jsonpath='{.data.admin-password}' | base64 -d; echo
```

## 🧹 Teardown

```bash
helm uninstall radar -n radar
helm uninstall radar-infra -n radar-infra
# Docker Desktop: Settings → Kubernetes → Reset, or leave it.
# kind: kind delete cluster --name radar
```

## 🛟 Troubleshooting

**A pod is `ImagePullBackOff`.** The cluster can't see a locally built image.
On kind, run the `kind load` for it (Step 2). On Docker Desktop, confirm the image
exists (`docker images | grep radar`) and was built with the exact
`:0.6.0` tag.

**`vault` is `CrashLoopBackOff`.** Check `kubectl -n radar-infra logs vault-... --previous`.
The dev Vault runs with `SKIP_SETCAP=true` under a dropped-capabilities context;
if you see a capability error, confirm you are on the current chart.

**`vault-bootstrap` keeps erroring.** It waits for Vault, so give it a moment. If it
persists, read `kubectl -n radar-infra logs job/vault-bootstrap`: it prints each
step (auth method, per-service roles, seeded secrets).

**An app pod is stuck in `Init`.** Its `vault-init` can't log in. Check
`kubectl -n radar logs <pod> -c vault-init`; this traces back to the bootstrap
having created the `radar-<service>` role.

**`knowledge-service` is `0/1`** with `/readyz` reason `elasticsearch: NotFoundError`.
The runbook index is missing, because the `knowledge-indexer` Job did not complete.
Check its log; it depends on `db-migration` having run first (the `runbook_documents`
table) and on the gateway + your OpenAI key.

**`feedback-service` is `0/1`** with `Required secret 'slack_bot_token' not found`.
You did not create `radar-slack-keys` (Step 3). Create it, `helm upgrade
radar-infra`, then `kubectl -n radar rollout restart deploy/feedback-service`. This
is expected when you have no Slack app; the rest of the system is unaffected.

**Trace-export warnings** (`UNAVAILABLE … otel-collector …`) are harmless. The
OTel collector DaemonSet is not part of this dev stack, and traces are best-effort.

**`helm install` times out on hooks.** The indexer is still running; re-check with
`kubectl -n radar get jobs`. Use `--timeout 10m` (Step 5). A failed release can be
re-driven with `helm upgrade radar deploy/helm/radar -n radar --timeout 10m`.
