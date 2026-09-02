---
runbook_id: order-service-deployment-rollback
title: Order Service Deployment Rollback
services:
  - order-service
severity: high
status: fixture
---

# Order Service Deployment Rollback

## Summary

A deploy of `order-service` has made things worse and needs to be reversed. This
runbook covers the decision, roll back or fix forward, and the execution,
including the case where a database migration has made a plain rollback unsafe.

Several other runbooks say "check for a recent deploy, and roll back if one
lines up." This is the one that says how, and when you cannot. The command
itself is one line; everything difficult about a rollback is either deciding
quickly enough or discovering that the schema has moved underneath you.

The default is to roll back. Restoring service comes before understanding the
fault, and a rollback that turns out to have been unnecessary costs far less
than a diagnosis conducted during an outage.

## Symptoms

- Any order-service alert firing within roughly 30 minutes of a rollout:
  failure rate, memory, latency, or error volume stepping up rather than
  drifting.
- A step change aligned with the rollout timestamp. Deploy-caused regressions
  step; resource-caused ones ramp. That shape difference is the single most
  useful signal for attributing a problem to a deploy.
- New error types in logs that did not appear before the rollout, particularly
  serialization, validation, or schema errors.
- Regression present on new pods and absent on old ones during a partially
  completed rollout: the cleanest possible evidence, available only while both
  versions are running.
- Readiness probe failures or crash-looping pods on the new version, which stall
  the rollout with mixed versions serving traffic.

## Impact

Varies with what the deploy broke, from unnoticed to total outage. The impact
that matters for this runbook is the *duration*: every minute spent deciding is
a minute of whatever the regression is doing.

A partially completed rollout is its own hazard. Two versions serving
simultaneously can produce inconsistent behaviour that is worse than either
version alone: particularly when a schema change means the two disagree about
what the data means.

Rollback itself is not free. It restarts every pod, dropping in-flight work, and
briefly reduces capacity. This is almost always worth it, but it is a real cost
and it is why "roll back and see" is not the answer for a marginal regression.

## Likely Causes

1. **Schema or migration change.** A migration applied by the deploy that the
   previous version cannot read, or a column the new code requires.
   Distinguishing signal: database errors mentioning columns or constraints, and
   a migration in the release. **This is the case that blocks a plain rollback**
   and must be identified before rolling back, not after.
2. **Configuration or environment change.** A changed default, a new required
   environment value, or a secret that was not created. Distinguishing signal:
   startup failures or configuration errors rather than request failures.
3. **Dependency or contract change.** A message format, an API version, or a
   client library upgrade that disagrees with a service on the other side.
   Distinguishing signal: errors at an integration boundary, and the other
   service reporting problems at the same time.
4. **Resource profile change.** New code legitimately needing more memory or
   more connections than its limits allow. Distinguishing signal: OOM-kills or
   pool exhaustion beginning after a deploy that changed no limits.
5. **Genuine logic regression.** A bug reaching production. Distinguishing
   signal: wrong behaviour rather than failed behaviour: orders processed
   incorrectly rather than not at all. The hardest to spot, because nothing
   errors.

## Investigation

1. **Confirm the deploy is actually implicated.** `kubectl rollout history
   deployment/order-service -n ecommerce` and compare the rollout timestamp
   against when the signal changed. Correlation within a few minutes is enough
   to act on; do not wait for proof.
2. **Check whether the release included a migration. Do this before rolling
   back.** A rollback with an applied backward-incompatible migration takes the
   old code back to a schema it cannot read, turning a partial regression into a
   full outage. This single check is why this runbook exists.
3. **Compare old and new pods if the rollout is still in progress.** If the
   regression appears only on new pods, attribution is certain and the rollout
   can be halted immediately with `kubectl rollout pause`.
4. **Read the new error types.** Startup and configuration errors point at cause
   2; integration-boundary errors at cause 3; resource errors at cause 4.
5. **Decide: roll back or fix forward.** Roll back by default. Fix forward only
   when the migration makes rollback unsafe, when the fix is genuinely trivial
   and understood, or when the regression is mild and a rollback would cost more
   than it saves.

## Resolution

**Standard rollback, no migration involved:**

```
kubectl rollout undo deployment/order-service -n ecommerce
kubectl rollout status deployment/order-service -n ecommerce
```

Watch the regression's own signal return to baseline. If it does not within a
few minutes of pods becoming ready, the deploy was not the cause: stop and
re-investigate rather than rolling back further.

**Rollback blocked by a migration (cause 1):** do not roll back blindly. Options,
in order of preference: fix forward with a small corrective release if the fault
is understood; reverse the migration first and then roll back, if the migration
has a tested down-path; or roll back code and immediately apply a compatibility
shim. Involve the database on-call for any of these: migration recovery under
incident pressure is where a bad incident becomes an unrecoverable one.

**Stalled rollout:** `kubectl rollout pause` to stop new pods replacing old ones
while you decide. Pausing with a healthy majority still on the old version is
often better than either completing or reversing in a hurry.

**Configuration cause (cause 2):** correct the configuration and redeploy rather
than rolling back, if the code itself is sound. A rollback that leaves the bad
configuration in place fixes nothing.

**After any rollback:** confirm the regression cleared, verify no in-flight
orders were lost during the restart, and leave the bad version un-redeployed
until the fault is understood. A rollback that is silently re-applied by the next
pipeline run is not a rollback.

## Escalation

Page the order-service on-call if the regression is customer-affecting. For a
rollback during business hours with the deploying engineer available, that
person is usually the fastest path: they know what changed.

Bring in the database on-call before rolling back any release containing a
migration. This is not optional and not a judgment call; it is the case where
acting alone most often makes things worse.

Escalate to an incident if a rollback fails, if the rollout is stuck with mixed
versions serving traffic, or if rolling back does not clear the regression. The
last case means the deploy was a coincidence and the real cause is still
unidentified while attention has been spent elsewhere.

## Related

- `order-service-high-failure-rate`: names a recent deploy as its most common
  cause and sends you here to execute the rollback.
- `order-service-high-memory`: cause 3 there is a deploy raising the memory
  baseline, which is a legitimate resource change rather than a fault. Read that
  runbook before rolling back a deploy whose only symptom is higher memory.
- `order-service-connection-pool-exhaustion`: a deploy that changes pool
  configuration or concurrency can cause it, and the fix may be a configuration
  correction rather than a rollback.
