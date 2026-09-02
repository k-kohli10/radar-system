#!/usr/bin/env bash
#
# Live load benchmark against the Kubernetes deployment.
#
# Fires N concurrent, distinct alerts at ingestion and drives the REAL pipeline
# end to end (ingestion -> watcher -> planner -> reasoner -> real LLM gateway ->
# recommendation), then reports:
#   1. No data loss  — exactly N incidents, N plans, N recommendations for the run,
#      and how many silently fell back to a template (a masked LLM failure).
#   2. Incident-pipeline latency — p50/p95/p99/max of `opened_at -> recommendation
#      created_at` (both Postgres now(), one clock): the same span the
#      `radar_incident_duration_seconds` panel observes. With the real gateway this
#      INCLUDES model time, so it is true end-to-end pipeline latency.
#
# Slack protection: feedback-service delivers an RCA card per recommendation, so N
# alerts would post N cards. This script scales feedback-service to 0 for the run,
# and on cleanup PURGES this run's queued feedback deliveries before restoring it —
# so your Slack channel is never bombarded.
#
# Usage:
#   scripts/load-benchmark.sh              # 100 concurrent alerts (the plan's number)
#   scripts/load-benchmark.sh 25           # a smaller run
#   DRAIN_TIMEOUT=1800 scripts/load-benchmark.sh   # wait longer for the drain
#
# Requires: kubectl pointed at the cluster; curl. Reaches ingestion on
# localhost:8080, reusing an existing port-forward or starting a temporary one.
set -uo pipefail

NS=radar
INFRA_NS=radar-infra
SVC=ingestion
PG=postgres-0
PORT="${INGESTION_PORT:-8080}"
BASE="http://localhost:${PORT}"
COUNT="${1:-100}"
DRAIN_TIMEOUT="${DRAIN_TIMEOUT:-1200}"          # seconds to wait for the drain
RUNID="$(date +%H%M%S)"
PREFIX="loadgen-${RUNID}-"                       # tags THIS run's incidents
TEMP_PF_PID=""
FEEDBACK_REPLICAS=""

command -v kubectl >/dev/null || { echo "kubectl not found on PATH"; exit 3; }
command -v curl    >/dev/null || { echo "curl not found on PATH"; exit 3; }
case "$COUNT" in ''|*[!0-9]*) echo "count must be a positive integer, got '$COUNT'"; exit 2;; esac

# psql helper: run a query in the in-cluster Postgres, tuple-only, unaligned.
# No `-i`: `psql -c` needs no stdin, and `exec -i` blocks on stdin when this runs
# detached (no TTY). `< /dev/null` and --request-timeout keep it from ever hanging.
psql_q() {
  kubectl -n "$INFRA_NS" --request-timeout=25s exec "$PG" -- \
    psql -U radar -d radar -tAc "$1" < /dev/null
}

cleanup() {
  [ -n "$TEMP_PF_PID" ] && kill "$TEMP_PF_PID" 2>/dev/null
}
trap cleanup EXIT INT TERM

# --- restore feedback-service, purging THIS run's queued cards first ------------
restore_feedback() {
  [ -n "$FEEDBACK_REPLICAS" ] || return 0
  echo
  echo "Protecting Slack: purging this run's queued feedback deliveries…"
  local purged
  purged="$(psql_q "
    DELETE FROM outbox_events
    WHERE target_service ILIKE '%feedback%'
      AND correlation_id IN (
        SELECT correlation_id FROM incidents WHERE service_name LIKE '${PREFIX}%'
      );
    SELECT 'ok';" 2>/dev/null | head -1)"
  echo "  purged queued feedback events for run ${RUNID} (result: ${purged:-none})"
  echo "Restoring feedback-service to ${FEEDBACK_REPLICAS} replica(s)…"
  kubectl -n "$NS" scale deploy/feedback-service --replicas="$FEEDBACK_REPLICAS" >/dev/null
  FEEDBACK_REPLICAS=""
}

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

# Count recommendations produced for THIS run so far.
run_rec_count() {
  psql_q "SELECT count(*) FROM recommendations r
          JOIN incidents i ON r.incident_id = i.id
          WHERE i.service_name LIKE '${PREFIX}%';"
}

echo "=== RADAR live load benchmark ==================================="
echo "run id: ${RUNID}   alerts: ${COUNT}   drain timeout: ${DRAIN_TIMEOUT}s"
echo "namespace: ${NS} (infra: ${INFRA_NS})   real LLM gateway: yes"
echo "================================================================"

ensure_ingestion
TOKEN="$(kubectl -n "$NS" exec "deploy/${SVC}" -- cat /vault/secrets/webhook_token_mock)"
[ -n "$TOKEN" ] || { echo "could not read webhook token from ingestion"; exit 1; }

# --- protect Slack: scale feedback-service to 0 for the run ---------------------
FEEDBACK_REPLICAS="$(kubectl -n "$NS" get deploy feedback-service -o jsonpath='{.spec.replicas}')"
[ -n "$FEEDBACK_REPLICAS" ] || FEEDBACK_REPLICAS=1
echo "Scaling feedback-service ${FEEDBACK_REPLICAS} -> 0 so Slack is not bombarded…"
kubectl -n "$NS" scale deploy/feedback-service --replicas=0 >/dev/null
trap 'restore_feedback; cleanup' EXIT INT TERM
kubectl -n "$NS" rollout status deploy/feedback-service --timeout=60s >/dev/null 2>&1 || true

# --- fire N concurrent, distinct alerts ----------------------------------------
echo
echo "Firing ${COUNT} concurrent alerts…"
codes_dir="$(mktemp -d)"
started="$(date +%s)"
fire_pids=()
for i in $(seq 0 $((COUNT - 1))); do
  svc="$(printf '%s%03d' "$PREFIX" "$i")"
  body="{\"service_name\":\"${svc}\",\"alert_name\":\"LoadTestFailure\",\"severity\":\"critical\"}"
  curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 --max-time 20 \
    -X POST "${BASE}/alerts/mock" \
    -H "X-Radar-Webhook-Token: ${TOKEN}" -H "Content-Type: application/json" \
    -d "$body" > "${codes_dir}/${i}" &
  fire_pids+=($!)
done
# Wait on the curl PIDs ONLY — a bare `wait` would also block on the temporary
# port-forward child, which runs for the whole script and never exits.
wait "${fire_pids[@]}"
accepted="$(grep -l '^202$' "${codes_dir}"/* 2>/dev/null | wc -l | tr -d ' ')"
echo "  ingestion accepted (202): ${accepted}/${COUNT}"
rm -rf "$codes_dir"

# --- wait for the pipeline to drain --------------------------------------------
echo
echo "Waiting for ${COUNT} recommendations (real LLM — this takes minutes)…"
deadline=$(( $(date +%s) + DRAIN_TIMEOUT ))
while :; do
  done_n="$(run_rec_count | tr -dc '0-9')"; done_n="${done_n:-0}"
  now="$(date +%s)"
  printf '  recommendations: %s/%s   elapsed: %ss\n' "$done_n" "$COUNT" "$((now - started))"
  [ "$done_n" -ge "$COUNT" ] && break
  [ "$now" -ge "$deadline" ] && { echo "  ! drain timeout at ${done_n}/${COUNT}"; break; }
  sleep 10
done

# --- report --------------------------------------------------------------------
echo
echo "=== Results (run ${RUNID}) ====================================="
psql_q "
WITH run AS (
  SELECT i.id, i.opened_at, i.correlation_id
  FROM incidents i WHERE i.service_name LIKE '${PREFIX}%'
),
spans AS (
  SELECT EXTRACT(EPOCH FROM (r.created_at - run.opened_at)) AS secs, r.is_fallback
  FROM recommendations r JOIN run ON r.incident_id = run.id
)
SELECT
  'incidents      : ' || (SELECT count(*) FROM run)                                  UNION ALL
SELECT 'plans          : ' || (SELECT count(*) FROM investigation_plans p JOIN run ON p.incident_id = run.id) UNION ALL
SELECT 'recommendations: ' || (SELECT count(*) FROM spans)                           UNION ALL
SELECT 'fallbacks      : ' || (SELECT count(*) FROM spans WHERE is_fallback)         UNION ALL
SELECT 'outbox pending : ' || (SELECT count(*) FROM outbox_events o WHERE o.correlation_id IN (SELECT correlation_id FROM run) AND o.status IN ('pending','processing')) UNION ALL
SELECT '---' UNION ALL
SELECT 'latency p50 (s): ' || round(percentile_disc(0.50) WITHIN GROUP (ORDER BY secs)::numeric, 3) FROM spans UNION ALL
SELECT 'latency p95 (s): ' || round(percentile_disc(0.95) WITHIN GROUP (ORDER BY secs)::numeric, 3) FROM spans UNION ALL
SELECT 'latency p99 (s): ' || round(percentile_disc(0.99) WITHIN GROUP (ORDER BY secs)::numeric, 3) FROM spans UNION ALL
SELECT 'latency max (s): ' || round(max(secs)::numeric, 3) FROM spans UNION ALL
SELECT 'latency min (s): ' || round(min(secs)::numeric, 3) FROM spans UNION ALL
SELECT 'latency avg (s): ' || round(avg(secs)::numeric, 3) FROM spans;
"
echo "================================================================"
echo "Latency is opened_at -> recommendation.created_at (real LLM included)."
# feedback-service restored + this run's queued cards purged by the EXIT trap.
