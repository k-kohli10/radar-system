#!/usr/bin/env bash
#
# Fire a mock alert at ingestion's /alerts/mock endpoint and let the pipeline run
# (ingestion -> watcher -> planner -> reasoner -> llm-gateway -> feedback -> Slack).
# Pick a realistic scenario from a menu, or pass its number as an argument.
#
# Usage:
#   scripts/fire-alert.sh                  # interactive menu, fire once
#   scripts/fire-alert.sh 1                # fire scenario 1 non-interactively
#   scripts/fire-alert.sh custom           # prompt for your own service/alert/severity
#   scripts/fire-alert.sh --every 60       # fire a random scenario every 60s (Ctrl+C to stop)
#   scripts/fire-alert.sh --every 60 3     # fire scenario 3 every 60s (Ctrl+C to stop)
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
LOOP_MODE=0

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

## fire <human-label> <json-body>
## Posts a complete alert (rich labels + annotations, the same shape a real
## Prometheus/Alertmanager webhook carries) so the reasoner has the deploy id,
## error class, timeline, and dependency-health evidence it needs for a
## high-confidence RCA.
fire() {
  local label="$1" body="$2"
  echo "→ firing: ${label}"
  curl -s -w '\nHTTP %{http_code}\n' -X POST "${BASE}/alerts/mock" \
    -H "X-Radar-Webhook-Token: ${TOKEN}" \
    -H "Content-Type: application/json" \
    -d "$body"
  # In loop mode the watch cheatsheet would repeat every tick — print it once only.
  [ "$LOOP_MODE" = 1 ] && return 0
  cat <<EOF

Fired. Watch it flow:
  kubectl -n radar logs -f deploy/reasoner-agent --tail=20     # RCA written here
  kubectl -n radar logs -f deploy/feedback-service --tail=20   # Slack delivery
  @radar open  /  @radar last 5                                # from Slack
  SELECT is_fallback, confidence, left(root_cause,120) FROM recommendations
    ORDER BY created_at DESC LIMIT 1;                          # via psql (port 55432)
EOF
}

# --- args: optional `--every <seconds>` for continuous firing, optional scenario ---
INTERVAL=""
CHOICE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --every|--loop|--interval)
      shift; INTERVAL="${1:-}"
      [ -n "$INTERVAL" ] || { echo "--every requires a seconds value"; exit 2; }
      shift ;;
    --every=*|--loop=*|--interval=*) INTERVAL="${1#*=}"; shift ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) CHOICE="$1"; shift ;;
  esac
done

if [ -n "$INTERVAL" ]; then
  case "$INTERVAL" in
    ''|*[!0-9]*) echo "--every needs a positive integer of seconds, got: '$INTERVAL'"; exit 2 ;;
  esac
  LOOP_MODE=1
  if [ "$CHOICE" = "custom" ] || [ "$CHOICE" = "6" ]; then
    echo "custom is interactive — not supported with --every"; exit 2
  fi
fi

fire_scenario() {
  local CHOICE="$1"
  case "$CHOICE" in
  1) fire "order-service / OrderProcessingFailureRate (critical)" '{
    "service_name": "order-service",
    "alert_name": "OrderProcessingFailureRate",
    "severity": "critical",
    "status": "firing",
    "labels": {
      "alertname": "OrderProcessingFailureRate",
      "service": "order-service",
      "severity": "critical",
      "namespace": "ecommerce",
      "deployment": "order-service",
      "error_class": "SQLSTATE_23505",
      "env": "prod",
      "team": "orders"
    },
    "annotations": {
      "summary": "Order processing failure rate 42% (threshold 5%) on order-service",
      "description": "Failure rate step-changed to 42% beginning 4 minutes after deploy order-service@v2.8.1 (deploy id d-9f2a1). 91% of failures are Postgres UniqueViolation SQLSTATE 23505 on orders_pkey. payment-gateway and inventory-service report healthy.",
      "runbook_url": "https://runbooks.internal/order-processing-failure-rate",
      "dashboard": "https://grafana.internal/d/order-pipeline",
      "value": "0.42"
    }
  }' ;;
  2) fire "checkout-service / CheckoutTimeoutRate (critical)" '{
    "service_name": "checkout-service",
    "alert_name": "CheckoutTimeoutRate",
    "severity": "critical",
    "status": "firing",
    "labels": {
      "alertname": "CheckoutTimeoutRate",
      "service": "checkout-service",
      "severity": "critical",
      "namespace": "ecommerce",
      "deployment": "checkout-service",
      "dependency": "payment-gateway",
      "env": "prod",
      "team": "checkout"
    },
    "annotations": {
      "summary": "Checkout timeout rate 12% (threshold 3%) on checkout-service",
      "description": "p95 checkout latency rose from 380ms to 9.4s over 6 minutes; 78% of timeouts are on the synchronous call to payment-gateway, whose PaymentGatewayErrorRate is also firing. No checkout-service deploy in the last 2 hours — points to a downstream dependency, not a checkout regression.",
      "runbook_url": "https://runbooks.internal/checkout-timeout-rate",
      "dashboard": "https://grafana.internal/d/checkout-pipeline",
      "value": "0.12"
    }
  }' ;;
  3) fire "order-service / OrderServiceHighMemory (high)" '{
    "service_name": "order-service",
    "alert_name": "OrderServiceHighMemory",
    "severity": "high",
    "status": "firing",
    "labels": {
      "alertname": "OrderServiceHighMemory",
      "service": "order-service",
      "severity": "high",
      "namespace": "ecommerce",
      "deployment": "order-service",
      "container": "order-service",
      "env": "prod",
      "team": "orders"
    },
    "annotations": {
      "summary": "order-service memory at 92% of limit (threshold 85%)",
      "description": "Working-set memory climbed steadily from 61% to 92% over 45 minutes with flat request volume — a leak pattern, not load. Onset ~20 min after deploy order-service@v2.8.1. Two pods already OOMKilled and restarted in the last 15 minutes.",
      "runbook_url": "https://runbooks.internal/order-service-high-memory",
      "dashboard": "https://grafana.internal/d/order-pipeline",
      "value": "0.92"
    }
  }' ;;
  4) fire "payment-gateway / PaymentGatewayErrorRate (critical)" '{
    "service_name": "payment-gateway",
    "alert_name": "PaymentGatewayErrorRate",
    "severity": "critical",
    "status": "firing",
    "labels": {
      "alertname": "PaymentGatewayErrorRate",
      "service": "payment-gateway",
      "severity": "critical",
      "namespace": "payments",
      "deployment": "payment-gateway",
      "upstream": "processor-primary",
      "env": "prod",
      "team": "payments"
    },
    "annotations": {
      "summary": "Payment gateway error rate 7% (threshold 2%)",
      "description": "5xx rate from payment-gateway to the upstream processor jumped to 7% starting 3 minutes ago. 84% of errors are HTTP 503 upstream connect timeouts. No payment-gateway deploy recently; the upstream status page shows elevated latency in us-east — likely an upstream dependency incident, not a code regression.",
      "runbook_url": "https://runbooks.internal/payment-gateway-error-rate",
      "dashboard": "https://grafana.internal/d/payments",
      "value": "0.07"
    }
  }' ;;
  5) fire "inventory-service / InventoryCheckLatency (high)" '{
    "service_name": "inventory-service",
    "alert_name": "InventoryCheckLatency",
    "severity": "high",
    "status": "firing",
    "labels": {
      "alertname": "InventoryCheckLatency",
      "service": "inventory-service",
      "severity": "high",
      "namespace": "ecommerce",
      "deployment": "inventory-service",
      "dependency": "cache-inventory",
      "env": "prod",
      "team": "inventory"
    },
    "annotations": {
      "summary": "inventory-service p95 availability-check latency 3.8s (threshold 2s)",
      "description": "p95 latency on GET /availability rose from 240ms to 3.8s over 10 minutes. Cache hit rate on the inventory cache dropped from 96% to 41% after a cache-node eviction, so requests are falling through to Postgres. No inventory-service deploy in the last hour.",
      "runbook_url": "https://runbooks.internal/inventory-check-latency",
      "dashboard": "https://grafana.internal/d/inventory",
      "value": "3.8"
    }
  }' ;;
  6|custom)
    read -rp "service_name: " CS
    read -rp "alert_name: " CA
    read -rp "severity [critical/high/low/info]: " CSEV
    read -rp "summary: " CSUM
    read -rp "description (evidence — deploy id, error class, dependency health): " CDESC
    CS="${CS:?service_name required}"; CA="${CA:?alert_name required}"; CSEV="${CSEV:-critical}"
    fire "${CS} / ${CA} (${CSEV}) [custom]" "$(printf '{"service_name":"%s","alert_name":"%s","severity":"%s","status":"firing","labels":{"alertname":"%s","service":"%s","severity":"%s","env":"prod"},"annotations":{"summary":"%s","description":"%s"}}' \
      "$CS" "$CA" "$CSEV" "$CA" "$CS" "$CSEV" "${CSUM:-manual test alert}" "${CDESC:-}")" ;;
  *) echo "invalid choice: ${CHOICE} (expected 1-6 or 'custom')"; exit 2 ;;
  esac
}

# --- ingestion reachability + token, resolved once (reused across loop ticks) ---
ensure_ingestion
TOKEN="$(kubectl -n "$NS" exec "deploy/${SVC}" -- cat /vault/secrets/webhook_token_mock)"
[ -n "$TOKEN" ] || { echo "failed to read mock webhook token from the ingestion pod"; exit 1; }

# --- driver: fire once, or every INTERVAL seconds until Ctrl+C ---
if [ "$LOOP_MODE" = 1 ]; then
  if [ -n "$CHOICE" ]; then
    echo "Firing scenario ${CHOICE} every ${INTERVAL}s — Ctrl+C to stop."
  else
    echo "Firing a random scenario every ${INTERVAL}s — Ctrl+C to stop."
  fi
  trap 'echo; echo "stopped firing."; exit 0' INT
  while true; do
    fire_scenario "${CHOICE:-$(( (RANDOM % 5) + 1 ))}"
    sleep "$INTERVAL"
  done
else
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
  fire_scenario "$CHOICE"
fi
