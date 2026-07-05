# ADR 0012: Self-Hosted Runner for CD

## Status
Accepted

## Context
RADAR's deployment target is a home lab Kubernetes cluster (one ARM64 control-plane VM,
two x86_64 P400 workers) with no public ingress by default. GitHub Actions' hosted
runners can build and push multi-arch images fine, but they cannot reach the cluster's
internal API server to run `helm upgrade` without exposing that API server to the
internet — either directly or through a tunnel. The two realistic options, per the
implementation plan, were a self-hosted GitHub Actions runner deployed on one of the
P400 worker nodes, or a Tailscale tunnel from GitHub's hosted runners into the home lab
network.

## Decision
A self-hosted GitHub Actions runner, deployed on a P400 worker node, with direct
in-cluster access to run `helm upgrade` against the local `kubeconfig`. CI (lint,
typecheck, test, path-based change detection, multi-arch `docker buildx` builds tagged
by git SHA) still runs on GitHub-hosted runners; only the CD step that touches the
cluster runs on the self-hosted runner.

## Consequences
- No tunnel to operate, no Tailscale account/ACLs to maintain, no additional network
  egress path into the home lab from the public internet.
- The self-hosted runner is a workload on the same P400 worker nodes running RADAR
  itself — it competes for CPU/memory with application pods and is a maintenance
  item (OS updates, runner version bumps) outside of Kubernetes.
- The runner's host has direct cluster-admin-equivalent access via its local
  kubeconfig; it must be treated as a trusted, secured machine, not just another CI
  agent — it is the single node that can push changes into the cluster.
- If the P400 hosting the runner goes down, CD is blocked until the runner comes back;
  CI (build/test) is unaffected since it stays on GitHub-hosted infrastructure.
