# Deploying RADAR to a managed Kubernetes cluster (CD)

This is the runbook for the **remote Kubernetes deployment target**: a managed
Kubernetes (K3s) cluster that the [`cd`](../../.github/workflows/cd.yml) workflow
deploys to with `helm upgrade` (ADR 0012). It is provisioned on demand for an
active testing session and torn down after — RADAR rebuilds its state from
scratch on every start (the dev Vault re-seeds, the runbook index rebuilds), so an
ephemeral cluster fits.

Two related guides:

- **[kubernetes.md](kubernetes.md)** — standing RADAR up by hand on a *local*
  cluster (Docker Desktop / kind). Same two charts; read it first if you want the
  step-by-step of what CD automates.
- **[docker.md](docker.md)** — the local two-stack Docker deployment, the primary
  way to run the full pipeline on one machine between sessions.

The difference here is that the cluster is remote and a GitHub-hosted runner
drives the deploy. The cluster's API server is publicly reachable and
token-authenticated, so no self-hosted runner is needed.

## How the pieces fit

```
push to main ─┐
              ├─▶ cd workflow (GitHub-hosted runner)
workflow_     ┘        │
dispatch               ├─ build + push 8 images ──▶ GHCR (public, SHA + 0.6.0 tags)
                       └─ helm upgrade ──(kubeconfig secret)──▶ K8s cluster
                              1. platform-deps  (radar-infra ns)
                              2. radar          (radar ns, images pinned to the SHA)
```

platform-deps installs with `--wait` (it blocks on the Vault-bootstrap Job).
The radar chart installs **without** `--wait`, on purpose: Helm still runs and waits
for its post-install hook Jobs (db-migration, then the runbook indexer, which builds
the index), but `--wait` on the release would deadlock — it blocks on every
Deployment's readiness before running the hooks, yet knowledge-service is not ready
until the indexer hook builds its index. A separate **Verify rollout** step then
waits for every app Deployment to become ready — that step is the Phase 12 done-when,
and a green `cd` run means every readiness probe passed.

## One-time setup

You do this once; it survives cluster teardown because it lives in GitHub, not on
the cluster.

### 1. Make the GHCR packages public

The images carry no secrets — every credential is fetched from Vault at runtime —
so the `radar-*` packages are published **public**. Public means pods and the
Vault-bootstrap Job pull with no image-pull secret, which keeps CD and the chart
free of registry credentials.

After the first `cd` run has pushed the packages, open each package under
`github.com/users/k-kohli10/packages`, and in **Package settings → Change
visibility** set it to **Public**. (New GHCR packages default to private.)

### 2. Create the `kubernetes` GitHub environment

The deploy job runs under an [environment](../../.github/workflows/cd.yml) named
`kubernetes`, so the cluster credentials are scoped to it (they carry
cluster-admin reach). In the repo: **Settings → Environments → New environment →
`kubernetes`**, then add its secrets in the next steps. Optionally add a
required-reviewer protection rule so a deploy waits for approval.

## Per-session: bring the cluster up

Provision a managed Kubernetes (K3s) cluster from your provider. The commands
below use `kubectl`/`helm` against whatever kubeconfig your provider hands you;
create the cluster with the provider's CLI or console.

### 3. Create the cluster

Size it for the full stack (the eight app services + in-cluster platform deps;
Elasticsearch is the memory driver): **3 nodes × 2 vCPU / 4 GB**.

From the provider's marketplace/add-ons, enable **only**:

- **metrics-server** — **required**; the ingestion and llm-gateway HPAs read it.
- **an ingress controller** — only needed if you expose the Slack bot over the
  Events API. Socket Mode (below) needs no ingress, so this is optional.

Do **not** add the marketplace Postgres/database apps: the `platform-deps` chart
ships RADAR's own dev backends, and a second Postgres just competes for memory.

Once created, merge the kubeconfig into `~/.kube/config` and select it, then
confirm `kubectl get nodes` shows three `Ready` nodes.

### 4. Store the kubeconfig as a secret

The deploy job reads a **base64-encoded** kubeconfig from the `kubernetes`
environment secret `CD_KUBECONFIG`:

```bash
base64 -w0 < "$KUBECONFIG"    # macOS: `base64 < "$KUBECONFIG"`
```

Paste the output into **Settings → Environments → kubernetes → Add secret →
`CD_KUBECONFIG`**.

### 5. (Optional) provider + Slack secrets

The deploy seeds these into the cluster before `helm upgrade` when they are
present, and skips them otherwise. Add them as `kubernetes` environment secrets:

| Secret | Effect if unset |
|---|---|
| `RADAR_OPENAI_API_KEY` | llm-gateway 401s upstream; the reasoner falls back to template RCAs |
| `RADAR_SLACK_BOT_TOKEN` + `RADAR_SLACK_APP_TOKEN` | feedback-service opens a Slack Socket Mode connection at startup, so it stays **not-ready** until both are set |

The deploy still completes without them — only these two services degrade.

## Deploy

- **Automatic:** merge to `main`. The `cd` workflow builds, pushes, and upgrades.
- **On demand:** **Actions → cd → Run workflow** (`workflow_dispatch`). This is
  the usual path, since the cluster is created per session — bring it up, store
  the kubeconfig, then dispatch.

Watch the run: `build + push` (8 parallel jobs) then `helm upgrade -> k8s`. The
final **Verify rollout** step prints the pods and fails the run if any Deployment
is not fully rolled out.

## Connectivity notes

- **kubectl from your laptop:** select the cluster's kubeconfig, then
  `kubectl -n radar get pods`. The API server is public and token-authed.
- **Slack bot:** RADAR uses **Socket Mode**, an outbound WebSocket from
  feedback-service to Slack — no inbound ingress or public URL is required, which
  is why the default cluster needs no load balancer for the bot to work.
- **Dashboards:** there is no public Grafana ingress by default. Reach it with a
  port-forward: `kubectl -n radar-infra port-forward svc/grafana 3000:3000`.

## Teardown

A managed cluster bills while it runs, so remove it when the session ends.
Deleting the cluster takes its volumes and any load balancer with it, so billing
stops cleanly. Use your provider's CLI/console to delete the cluster.

The GitHub environment and its secrets persist for the next session; only
`CD_KUBECONFIG` needs refreshing, since a re-created cluster gets a new one.
