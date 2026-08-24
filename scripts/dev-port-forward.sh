#!/usr/bin/env bash
#
# Port-forward every RADAR UI, backend, and app service at once, for local
# inspection of a running cluster (Docker Desktop k8s / kind / managed K8s). Each
# forward auto-restarts if it drops; Ctrl-C stops them all. Local-only — nothing is
# exposed publicly (that would need Ingress; see docs/operations/kubernetes-cd.md).
#
# Usage:
#   scripts/dev-port-forward.sh                 # forward everything
#   scripts/dev-port-forward.sh --infra         # only radar-infra (UIs + backends)
#   scripts/dev-port-forward.sh --apps          # only the 8 app services
#
# Requires: kubectl pointed at the cluster (KUBECONFIG set / current-context right).
set -uo pipefail

# ── forward table: "namespace service local:remote label" ─────────────────────
# radar-infra: platform UIs + backends. radar: the 8 app services (all serve
# /healthz, /readyz, /metrics on 8080), mapped to distinct local ports 8080-8087.
INFRA=(
  "radar-infra elasticsearch 9200:9200   Elasticsearch|http://localhost:9200"
  "radar-infra kibana        5601:5601   Kibana|http://localhost:5601"
  "radar-infra grafana       3000:3000   Grafana|http://localhost:3000 (admin / radar-dev-admin-not-a-secret)"
  "radar-infra prometheus    9090:9090   Prometheus|http://localhost:9090"
  "radar-infra alertmanager  9093:9093   Alertmanager|http://localhost:9093"
  "radar-infra vault         8200:8200   Vault|http://localhost:8200 (token radar-dev-root-token)"
  "radar-infra postgres      55432:5432  Postgres|localhost:55432 (radar / radar-dev-only-not-a-secret, db radar)"
)
APPS=(
  "radar ingestion         8080:8080  ingestion|http://localhost:8080"
  "radar llm-gateway       8081:8080  llm-gateway|http://localhost:8081"
  "radar outbox-worker     8082:8080  outbox-worker|http://localhost:8082"
  "radar watcher-agent     8083:8080  watcher-agent|http://localhost:8083"
  "radar planner-agent     8084:8080  planner-agent|http://localhost:8084"
  "radar reasoner-agent    8085:8080  reasoner-agent|http://localhost:8085"
  "radar knowledge-service 8086:8080  knowledge-service|http://localhost:8086"
  "radar feedback-service  8087:8080  feedback-service|http://localhost:8087"
)

case "${1:-all}" in
  --infra) FORWARDS=("${INFRA[@]}") ;;
  --apps)  FORWARDS=("${APPS[@]}") ;;
  all|"")  FORWARDS=("${INFRA[@]}" "${APPS[@]}") ;;
  -h|--help) sed -n '3,14p' "$0"; exit 0 ;;
  *) echo "unknown arg: $1 (use --infra, --apps, or nothing)"; exit 2 ;;
esac

command -v kubectl >/dev/null || { echo "kubectl not found on PATH"; exit 3; }

pids=()
trap 'echo; echo "stopping all forwards…"; kill "${pids[@]}" 2>/dev/null' EXIT INT TERM

for row in "${FORWARDS[@]}"; do
  read -r ns svc ports _label <<<"$row"
  ( while true; do
      kubectl -n "$ns" port-forward "svc/$svc" "$ports" >/dev/null 2>&1
      # A forward exits when it drops OR when the service has no listener yet.
      sleep 2
    done ) &
  pids+=($!)
done

printf '\n  \033[1;35m━━━ RADAR port-forwards ━━━\033[0m  %d services  ·  \033[2mCtrl-C to stop all\033[0m\n' "${#FORWARDS[@]}"
prev_ns=""
for row in "${FORWARDS[@]}"; do
  read -r ns _svc _ports label <<<"$row"        # label is "Name|url with spaces"
  if [ "$ns" != "$prev_ns" ]; then
    case "$ns" in
      radar-infra) printf '\n  \033[1mPlatform (radar-infra)\033[0m\n' ;;
      radar)       printf '\n  \033[1mApps (radar)\033[0m\n' ;;
    esac
    prev_ns="$ns"
  fi
  printf '    \033[36m%-18s\033[0m %s\n' "${label%%|*}" "${label#*|}"
done
printf '\n  \033[2mPostgres:\033[0m psql -h localhost -p 55432 -U radar -d radar  (pw: radar-dev-only-not-a-secret)\n\n'
wait
