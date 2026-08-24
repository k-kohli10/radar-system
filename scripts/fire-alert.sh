#!/usr/bin/env bash
#
# Fire a mock alert at ingestion's /alerts/mock endpoint and let the pipeline run
# (ingestion -> watcher -> planner -> reasoner -> llm-gateway -> feedback -> Slack).
# Pick a realistic scenario from a menu, or pass its number as an argument.
#
# Usage:
#   scripts/fire-alert.sh            # interactive menu
#   scripts/fire-alert.sh 1          # fire scenario 1 non-interactively
#   scripts/fire-alert.sh custom     # prompt for your own service/alert/severity
#
# It reads the per-source mock webhook token from the ingestion pod (Vault-mounted),
# and reaches ingestion on localhost:8080 — reusing an existing port-forward if one
# is up, otherwise starting a temporary one for the duration of the run.
#
# Requires: kubectl pointed at the cluster; curl.
set -uo pipefail

NS=radar
SVC=ingestion
PORT="${INGESTION_PORT:-8080}"
BASE="http://localhost:${PORT}"
TEMP_PF_PID=""

command -v kubectl >/dev/null || { echo "kubectl not found on PATH"; exit 3; }
command -v curl    >/dev/null || { echo "curl not found on PATH"; exit 3; }

cleanup() { [ -n "$TEMP_PF_PID" ] && kill "$TEMP_PF_PID" 2>/dev/null; }
trap cleanup EXIT INT TERM

ensure_ingestion() {
  if curl -sf -o /dev/null --max-time 2 "${BASE}/healthz"; then return 0; fi
  echo "ingestion not reachable on ${BASE} — starting a temporary port-forward…" >&2
  kubectl -n "$NS" port-forward "svc/$SVC" "${PORT}:8080" >/dev/null 2>&1 &
  TEMP_PF_PID=$!
  for _ in $(seq 1 15); do
    curl -sf -o /dev/null --max-time 2 "${BASE}/healthz" && return 0
    sleep 1
  done
  echo "could not reach ingestion after starting a port-forward" >&2; exit 1
}

fire() {
  local svc="$1" alert="$2" sev="$3" summary="$4"
  echo "→ firing: ${svc} / ${alert} (${sev})"
  curl -s -w '\nHTTP %{http_code}\n' -X POST "${BASE}/alerts/mock" \
    -H "X-Radar-Webhook-Token: ${TOKEN}" \
    -H "Content-Type: application/json" \
    -d "$(printf '{"service_name":"%s","alert_name":"%s","severity":"%s","labels":{"env":"prod"},"annotations":{"summary":"%s"}}' \
          "$svc" "$alert" "$sev" "$summary")"
  cat <<EOF

Fired. Watch it flow:
  kubectl -n radar logs -f deploy/reasoner-agent --tail=20     # RCA written here
  kubectl -n radar logs -f deploy/feedback-service --tail=20   # Slack delivery
  @radar open  /  @radar last 5                                # from Slack
  SELECT is_fallback, confidence, left(root_cause,120) FROM recommendations
    ORDER BY created_at DESC LIMIT 1;                          # via psql (port 55432)
EOF
}

CHOICE="${1:-}"
if [ -z "$CHOICE" ]; then
  cat <<'MENU'
Select an alert to fire:
  1) order-service     / OrderProcessingFailureRate   (template + runbook — fullest RCA)
  2) checkout-service  / CheckoutTimeoutRate          (template + runbook)
  3) order-service     / OrderServiceHighMemory       (template + runbook)
  4) payment-gateway   / PaymentGatewayErrorRate      (runbook only, _default plan)
  5) inventory-service / InventoryCheckLatency        (runbook only, _default plan)
  6) custom                                           (enter your own)
MENU
  read -rp "choice [1-6]: " CHOICE
fi

ensure_ingestion
TOKEN="$(kubectl -n "$NS" exec "deploy/${SVC}" -- cat /vault/secrets/webhook_token_mock)"
[ -n "$TOKEN" ] || { echo "failed to read mock webhook token from the ingestion pod"; exit 1; }

case "$CHOICE" in
  1) fire "order-service"     "OrderProcessingFailureRate" "critical" "Order failure rate > 5% for 1 minute" ;;
  2) fire "checkout-service"  "CheckoutTimeoutRate"        "critical" "Checkout timeout rate > 3% for 2 minutes" ;;
  3) fire "order-service"     "OrderServiceHighMemory"     "high"     "order-service memory usage > 85%" ;;
  4) fire "payment-gateway"   "PaymentGatewayErrorRate"    "critical" "Payment gateway error rate > 2% for 1 minute" ;;
  5) fire "inventory-service" "InventoryCheckLatency"      "high"     "inventory service p95 latency > 2s" ;;
  6|custom)
    read -rp "service_name: " CS
    read -rp "alert_name: " CA
    read -rp "severity [critical/high/low/info]: " CSEV
    read -rp "summary: " CSUM
    fire "${CS:?service_name required}" "${CA:?alert_name required}" "${CSEV:-critical}" "${CSUM:-manual test alert}" ;;
  *) echo "invalid choice: ${CHOICE} (expected 1-6 or 'custom')"; exit 2 ;;
esac
