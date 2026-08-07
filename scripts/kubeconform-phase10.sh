#!/usr/bin/env bash
#
# Static kubeconform validation of ALL Phase 10 Kubernetes manifests. No cluster;
# scheduling proof is Phase 12. Run via `make kubeconform`.
#
# WHY THE EXPECTED COUNTS ARE HARDCODED — DO NOT LOOSEN THIS TO "> 0".
# ---------------------------------------------------------------------
# kubeconform EXITS 0 on empty input ("0 resources found") — a mis-globbed,
# emptied, or wrong-path file list validates NOTHING and passes green. That is a
# validator's silent-empty-pass, the worst kind of false green. The exit code
# alone cannot catch it. The only defence is asserting the EXACT expected
# resource and file counts: a ">0" guard would still wave through a partial run
# that quietly skipped a manifest. If you add or remove a Phase 10 k8s resource,
# update EXPECTED_RESOURCES / EXPECTED_FILES here ON PURPOSE — that deliberate
# touch-point IS the guard, not an inconvenience to engineer away.
#
# NETWORK: kubeconform fetches the Kubernetes JSON schemas from the
# kubernetes-json-schema repo at runtime, so this needs egress to
# raw.githubusercontent.com. Pinned as a Phase 11 CI prerequisite.
set -euo pipefail

IMAGE="ghcr.io/yannh/kubeconform:v0.6.7"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
EXPECTED_RESOURCES=16
EXPECTED_FILES=4

# The curated list of Phase 10 k8s manifests — explicit files, never a directory:
# a directory arg makes kubeconform recurse into the dashboard JSONs and config
# payloads (not k8s manifests) and error on them. PHASE10_MANIFESTS overrides this
# ONLY for the teeth test that proves the empty-run guard; production leaves it
# unset and uses this list + the hardcoded counts as the source of truth.
default_manifests=(
  deploy/otel/collector-daemonset.yaml
  deploy/otel/traces-index-template.yaml
  deploy/fluent-bit/fluent-bit-daemonset.yaml
  deploy/grafana/dashboards-configmaps.yaml
)
if [[ -n "${PHASE10_MANIFESTS+set}" ]]; then
  read -r -a manifests <<<"${PHASE10_MANIFESTS}"
else
  manifests=("${default_manifests[@]}")
fi

# Distinguish "cannot even validate" (infra) from "manifests invalid" (real).
if ! command -v docker >/dev/null 2>&1; then
  echo "kubeconform: CANNOT RUN — docker not found (infra, not a manifest problem)"; exit 3
fi
if ! docker info >/dev/null 2>&1; then
  echo "kubeconform: CANNOT RUN — docker not running (infra, not a manifest problem)"; exit 3
fi
if (( ${#manifests[@]} == 0 )); then
  echo "kubeconform: FAIL — empty manifest list: an empty/partial run validated nothing"; exit 1
fi

args=()
for m in "${manifests[@]}"; do args+=("/work/$m"); done
out="$(docker run --rm -v "$REPO:/work:ro" "$IMAGE" -strict -summary "${args[@]}" 2>&1)" || true
echo "$out"

# Summary: "N resources found in M files - Valid: V, Invalid: I, Errors: E, Skipped: S"
parsed="$(echo "$out" | sed -n 's/.*Summary: \([0-9]*\) resources\{0,1\} found in \([0-9]*\) files\{0,1\} - Valid: \([0-9]*\), Invalid: \([0-9]*\), Errors: \([0-9]*\).*/\1 \2 \3 \4 \5/p')"
if [[ -z "$parsed" ]]; then
  echo "kubeconform: CANNOT RUN — no parseable summary (network/schema-fetch or docker problem, not a manifest problem)"; exit 2
fi
read -r res files valid invalid errors <<<"$parsed"

if (( errors > 0 )); then
  echo "kubeconform: CANNOT TRUST — $errors ERROR(s): schema-fetch / network egress / missing file. NOT 'manifest invalid' — check egress to the schema repo."; exit 2
fi
if (( invalid > 0 )); then
  echo "kubeconform: FAIL — $invalid manifest(s) are INVALID k8s (real schema violations, see above)"; exit 1
fi
if (( res != EXPECTED_RESOURCES || files != EXPECTED_FILES )); then
  echo "kubeconform: FAIL — validated $res resources in $files files, EXPECTED $EXPECTED_RESOURCES in $EXPECTED_FILES."
  echo "  Either an empty/partial run, or the Phase 10 manifest set changed — update the hardcoded counts deliberately (see header)."; exit 1
fi
echo "kubeconform: OK — $valid/$EXPECTED_RESOURCES resources valid across $files manifests (--strict)"
