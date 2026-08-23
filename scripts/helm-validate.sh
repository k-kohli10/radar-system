#!/usr/bin/env bash
#
# Offline validation of the Phase 12 Helm charts. No cluster: this renders every
# chart with `helm template` and validates the output against the Kubernetes
# schemas with `kubeconform -strict`. Scheduling / readiness (the Phase 12
# done-when) needs a real cluster; this is the static gate that runs in CI and
# before every chart commit. Run via `make helm-validate`.
#
# WHY EACH RENDER HAS A HARDCODED EXPECTED COUNT — DO NOT LOOSEN TO "> 0".
# ----------------------------------------------------------------------
# `helm template BADPATH` or a template guarded off by values can render NOTHING,
# and `kubeconform` EXITS 0 on empty input ("0 resources found") — so an empty or
# partial render validates NOTHING and passes green. That silent-empty-pass is the
# worst false green (same trap as scripts/kubeconform-phase10.sh). The exit code
# alone cannot catch it; the defence is asserting the EXACT resource count each
# render is expected to produce. A ">0" guard would wave through a render that
# quietly dropped half its workloads. When you add/remove a chart resource or an
# example toggles one on/off, update the matching count below ON PURPOSE — that
# deliberate touch-point IS the guard.
#
# The counts are values-dependent, so each (chart, values) pair carries its own
# expected count rather than one number for the whole chart.
#
# NETWORK: kubeconform fetches the Kubernetes JSON schemas at runtime, so this
# needs egress to the schema repo (raw.githubusercontent.com). Pinned as a CI
# prerequisite alongside the Phase 10 kubeconform check.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

# Match the version the charts are validated against locally and in CI. Bump
# deliberately when the target cluster's server version moves.
KUBE_VERSION="${KUBE_VERSION:-1.35.0}"

# The two charts to `helm lint`.
CHARTS=(
  deploy/helm/radar
  deploy/helm/platform-deps
)

# Render matrix: "release|chart|values-file|expected_count|label". An empty
# values-file field means chart defaults only. The example values files are the
# documented install shapes (deploy/examples/), so validating them here pins that
# what we ship as "how to install" actually renders to valid k8s.
RENDERS=(
  "radar|deploy/helm/radar||32|radar chart (default values)"
  "radar|deploy/helm/radar|deploy/examples/minimal/values.yaml|30|radar chart (examples/minimal)"
  "radar|deploy/helm/radar|deploy/examples/bring-your-own-backends/values.yaml|32|radar chart (examples/bring-your-own-backends)"
  "platform-deps|deploy/helm/platform-deps||42|platform-deps chart (default values)"
)

# Distinguish "cannot even validate" (infra) from "charts invalid" (real).
if ! command -v helm >/dev/null 2>&1; then
  echo "helm-validate: CANNOT RUN — helm not found (infra, not a chart problem)"; exit 3
fi
if ! command -v kubeconform >/dev/null 2>&1; then
  echo "helm-validate: CANNOT RUN — kubeconform not found (infra, not a chart problem)"; exit 3
fi

# ---- helm lint: catches template/values errors kubeconform never sees ---------
for chart in "${CHARTS[@]}"; do
  echo "== helm lint $chart =="
  # --strict would fail on the cosmetic "icon is recommended" INFO; we want lint
  # to fail on real WARN/ERROR only, so run without it and let a nonzero exit
  # (ERROR) stop us via set -e.
  helm lint "$chart"
done

# ---- render + kubeconform each combo, with the exact-count guard --------------
fail=0
for row in "${RENDERS[@]}"; do
  IFS='|' read -r release chart values expected label <<<"$row"

  if [[ -n "$values" ]]; then
    rendered="$(helm template "$release" "$chart" -f "$values")"
  else
    rendered="$(helm template "$release" "$chart")"
  fi

  out="$(printf '%s' "$rendered" | kubeconform -strict -summary -kubernetes-version "$KUBE_VERSION" 2>&1)" || true

  # Summary: "N resources found parsing stdin - Valid: V, Invalid: I, Errors: E, Skipped: S"
  parsed="$(echo "$out" | sed -n 's/.*Summary: \([0-9]*\) resources\{0,1\} found parsing stdin - Valid: \([0-9]*\), Invalid: \([0-9]*\), Errors: \([0-9]*\), Skipped: \([0-9]*\).*/\1 \2 \3 \4 \5/p')"
  if [[ -z "$parsed" ]]; then
    echo "helm-validate: CANNOT RUN [$label] — no parseable kubeconform summary (network/schema-fetch problem, not a chart problem):"
    echo "$out"
    exit 2
  fi
  read -r res valid invalid errors skipped <<<"$parsed"

  if (( errors > 0 )); then
    echo "helm-validate: CANNOT TRUST [$label] — $errors ERROR(s): schema-fetch / network egress. NOT 'chart invalid' — check egress to the schema repo."
    echo "$out"; exit 2
  fi
  if (( invalid > 0 )); then
    echo "helm-validate: FAIL [$label] — $invalid rendered resource(s) are INVALID k8s (real schema violations):"
    echo "$out"; fail=1; continue
  fi
  # A skipped resource is one kubeconform has no schema for (e.g. a CRD): -strict
  # would still count it skipped, not invalid. We render only core/built-in kinds,
  # so any skip means an unexpected kind slipped in — treat it as a failure, not a
  # silent pass.
  if (( skipped > 0 )); then
    echo "helm-validate: FAIL [$label] — $skipped resource(s) SKIPPED (no schema): an unexpected kind rendered."
    echo "$out"; fail=1; continue
  fi
  if (( res != expected )); then
    echo "helm-validate: FAIL [$label] — rendered $res resources, EXPECTED $expected."
    echo "  Either an empty/partial render, or the chart's resource set changed — update the expected count in this script deliberately (see header)."
    fail=1; continue
  fi
  echo "helm-validate: OK [$label] — $valid/$expected resources valid (--strict, k8s $KUBE_VERSION)"
done

if (( fail != 0 )); then
  echo "helm-validate: FAILED — see above."
  exit 1
fi
echo "helm-validate: OK — all charts lint clean and render to valid k8s."
