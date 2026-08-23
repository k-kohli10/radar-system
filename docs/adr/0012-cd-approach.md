# ADR 0012: Deployment Targets (Docker and Ephemeral Kubernetes)

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
