# 🚢 ADR 0012: Deployment Targets (Docker and Ephemeral Kubernetes)

## Status

Accepted.

## Context

RADAR ships as multi-arch container images and needs a way to run the whole system for
development, demos, and testing. Two contexts matter: a single machine for local
end-to-end runs, and a managed Kubernetes cluster for the k8s path (the Phase 12 Helm
chart and the Phase 13 load test).

RADAR rebuilds its state from scratch on every start. The dev Vault re-seeds via
`make seed && make tokens`, the runbook index rebuilds via `make index`, and test data
is disposable. That makes an on-demand cluster a natural fit for the k8s work.

## Decision

- **Local end-to-end runs use the two-stack Docker deployment** (`make docker-up`):
  the `radar-infra` and `radar-apps` compose stacks on a shared network. This is the
  primary way to run and demo the full pipeline on one machine.
- **The Kubernetes target is a managed Kubernetes (K3s) cluster, provisioned on demand and
  torn down between sessions.** The provider bills hourly, so intermittent testing through
  Phases 12 and 13 stays inexpensive.
- **CD runs on a GitHub-hosted runner and deploys directly.** The cluster exposes a public,
  authenticated API, so the runner runs `helm upgrade` against a stored kubeconfig. CI
  (lint, typecheck, test, path-based change detection, multi-arch buildx) stays on
  hosted runners.

## Consequences

- CD depends on GitHub-hosted runners and the cluster API, keeping the moving parts to the
  deploy workflow and the cluster credentials.
- The cluster kubeconfig and API token are deployment credentials held as GitHub secrets,
  scoped to the deploy workflow, since they carry cluster-admin reach.
- An ephemeral cluster proves CD during an active session; between sessions the local
  Docker deployment covers running the product.
- Teardown removes the cluster's volumes and load balancer along with the cluster, so
  billing stops cleanly.
- Images build for linux/amd64 (the cluster and x86 CI) and linux/arm64 (local Docker on Apple
  Silicon), so the multi-arch buildx step stays.

## CI/CD workflow topology

Three purpose-named workflows, kept minimal; the load-bearing rationale that used
to live in inline comments is recorded here:

- **`ci.yml`** (push/PR): `lint`, `test`, `helm` (chart validation). Two gotchas:
  Docker Hub login raises the pull rate limit that throttles shared runners (skipped
  on forks with no secrets); and `scripts/assert-required-tests-ran.py` is a
  no-silent-skip guard because `pytest` exits 0 on SKIP, so failed infra would
  otherwise go green-with-skips.
- **`build.yml`** (push/PR): path-gated multi-arch build + boot-smoke via
  `scripts/detect-changed-services.py`. It never pushes.
- **`deploy.yml`** (`workflow_dispatch`): builds + pushes the eight images
  (amd64-only: the cluster is amd64, so arm64 here is wasted QEMU time), then
  `helm upgrade`. Manual because the cluster is ephemeral. The cluster steps run in
  the `kubernetes` environment; Required reviewers on it gate the deploy behind an
  approval. `helm upgrade radar` deliberately omits `--wait`: `--wait` blocks on
  Deployment readiness before the post-install hooks run, but knowledge-service isn't
  ready until the `knowledge-indexer` hook builds its index. That would make `--wait`
  deadlock. The Verify-rollout step is the readiness gate instead.

## Startup ordering inside the radar chart

Infra-before-apps is guaranteed by the release order (`platform-deps --wait`, then
`radar`) and each pod's `vault-init` init-container. Within the app tier, a
per-service `dependsOn` list renders a wait-for init-container that blocks a
service until each named peer's `/readyz` returns 200, enforcing
`llm-gateway -> knowledge-service -> consumers`. `ingestion` and `outbox-worker`
are intentionally ungated: they depend only on the database and tolerate peer
unavailability through the outbox + readiness-retry design. Because gated consumers
wait on the indexer hook, the Verify-rollout timeout is 300s.
