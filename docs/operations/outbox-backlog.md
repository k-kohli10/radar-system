# 🚨 Runbook: outbox backlog

**Alert:** `OutboxBacklogHigh`: `radar_outbox_depth > 100` for 5m.
**Dashboard:** `outbox-health`.

## Contents

- 🔎 [Symptom](#symptom)
- 🩺 [Diagnose (check in this order)](#diagnose-check-in-this-order)
- 🛠️ [Recover](#recover)
- ✔️ [Verify](#verify)
- 🆘 [If recovery doesn't work / known limits / when to escalate](#if-recovery-doesnt-work--known-limits--when-to-escalate)

## Symptom
Pending outbox events are piling up (`radar_outbox_depth` climbing) and the
pipeline is lagging: incidents open but plans and recommendations arrive late or
not at all. Inter-agent delivery goes through the outbox, so a backlog stalls
everything downstream of it.

## Diagnose (check in this order)
1. **Is a target agent down?** Most backlogs are this. Check `up{job="radar"}` /
   `RadarAgentDown`. A down watcher / planner / reasoner / feedback-service means
   dispatches to it fail and re-queue.
2. **Are dispatches failing?** Check `radar_outbox_retry_total` rising, and the
   outbox-worker (`:8094`) logs: `401` means the worker's token for that target is
   stale (a botched rotation, see below); connection refused means the target is
   down or its dispatch URL is wrong.
3. **Events stuck processing?** `radar_outbox_processing` high and not draining
   means a worker crashed mid-dispatch. The **reaper** recovers these automatically on
   each sweep; confirm the outbox-worker is running.
4. **Dead-lettering?** `radar_outbox_dead_letter_total` rising means a *poison* event
   the target permanently rejects (e.g. a 422). Retrying will never clear it.

## Recover
- **Down target:** bring it back (`make dev-apps-up`, or restart the pod). The
  backlog drains once dispatch resumes.
- **401s:** the target's token was rotated without restarting the outbox-worker.
  Follow [vault-secret-rotation](vault-secret-rotation.md) and restart the target
  **and** the outbox-worker.
- **Stuck `processing`:** the reaper clears these; if it isn't, restart the
  outbox-worker so the reaper task runs.
- **Dead-lettered events:** fix the underlying cause first (a 422 is a bad
  payload/contract, so retrying won't help), then requeue from the dead-letter set.

## Verify
`radar_outbox_depth` trends back toward 0 on the `outbox-health` dashboard, and
`radar_outbox_retry_total` / `radar_outbox_dead_letter_total` stop climbing.

## If recovery doesn't work / known limits / when to escalate
- **Depth keeps climbing after the target is healthy and tokens are valid:** the
  worker may be throughput-bound, with dispatch slower than inflow. Check dispatch
  p95 on the dashboard. Scaling the worker is a Phase 12/13 concern; escalate to
  the maintainer rather than restart-looping.
- **Dead-lettered poison events never self-heal.** They need a human decision:
  fix the payload/contract or drop it. Escalate with the specific `event_type`.
- **Known limit:** the `> 100` threshold is provisional (no real throughput data
  yet). A short burst can trip it without a real problem, so correlate with the
  retry and dead-letter rates before acting.
