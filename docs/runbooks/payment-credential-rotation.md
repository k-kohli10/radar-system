---
runbook_id: payment-credential-rotation
title: Payment Credential Rotation
services:
  - payment-gateway
severity: high
status: fixture
---

# Payment Credential Rotation

## Summary

Payment processor API credentials need to be replaced — on a schedule, on
expiry, or urgently after a suspected compromise. Rotation is routine, but it is
the routine operation with the worst failure mode in the platform: a botched
rotation takes payments to a near-total error rate within seconds of the first
pod restarting.

The failure is binary and immediate. Credentials do not degrade — they work
until they do not — so there is no partial signal warning that a rotation is
going wrong. That is what makes the verification step non-negotiable and why the
order of operations matters more than the individual actions.

The rule that prevents nearly every rotation incident: **validate the new
credential against the processor before it becomes the one in use.**

## Symptoms

Signals that rotation is *needed*:

- Credential expiry approaching, from the processor's dashboard or a tracked
  expiry date.
- A compromise or suspected exposure — a leaked key, a departed engineer with
  access, a credential committed to a repository.
- A scheduled rotation interval elapsing under policy.

Signals that a rotation has *gone wrong*:

- `PaymentGatewayErrorRate` spiking to near 100% within seconds of a restart,
  with 401 or 403 responses from the processor.
- Failures on rotated replicas while un-rotated ones still succeed, producing a
  partial outage that tracks the rollout rather than traffic.
- Startup failures in `payment-gateway` if the secret is malformed or absent.

## Impact

A correctly executed rotation has no customer impact and no downtime.

A failed one is a total payment outage: every authorization fails with an
authentication error, no purchases complete, and the blast radius is every
customer trying to pay. The recovery is fast once diagnosed — restore the
previous credential — but the diagnosis is the slow part if nobody knows a
rotation was in progress. An unannounced rotation turns a two-minute fix into a
twenty-minute investigation.

Urgency differs sharply by trigger. A scheduled rotation can wait for a good
window. A compromised credential is an active security exposure, and the risk of
leaving it live usually exceeds the risk of rotating under pressure.

## Likely Causes

Causes of a rotation going wrong:

1. **New credential never validated before cutover.** Typo, wrong environment,
   or a credential not yet activated at the processor. Distinguishing signal:
   401 or 403 immediately on the first rotated pod. The dominant cause and
   entirely preventable.
2. **Old credential revoked before the new one was live everywhere.** Revoking
   too early breaks every replica still using it. Distinguishing signal: failures
   spreading as pods restart, with revocation preceding full rollout.
3. **Secret updated in Vault but not reloaded.** Credentials load at startup, so
   a Vault change alone does nothing until a restart. Distinguishing signal:
   nothing changes at all after rotation — which is easy to mistake for success.
4. **Wrong environment's credential.** A sandbox key in production or the
   reverse. Distinguishing signal: authentication succeeds but transactions
   behave incorrectly, which is worse than failing outright because it is not
   immediately obvious.
5. **Partial rotation across replicas.** Some pods restarted, some not, leaving a
   mixed fleet. Distinguishing signal: an error rate that sits at an odd fraction
   matching the ratio of restarted pods.

## Investigation

Before rotating:

1. **Confirm the new credential is active at the processor.** A credential
   issued but not yet activated will fail, and this is checkable in advance.
2. **Validate it out of band.** Make a low-value authorization or a
   credential-check call using the new credential before it is deployed
   anywhere. This single step prevents cause 1, which is most rotation
   incidents.
3. **Confirm the old credential remains valid.** Overlap is what makes a
   zero-downtime rotation possible; without it, rotation is a cutover with no
   fallback.

If a rotation has gone wrong:

4. **Check whether a rotation is in progress at all.** The first question for any
   sudden authentication failure. This is why rotations are announced — it turns
   the diagnosis into a single question.
5. **Compare rotated and un-rotated replicas.** Failures tracking the rollout
   rather than traffic confirm cause 2 or 5.
6. **Verify the secret's content and that pods actually reloaded it.** Cause 3
   presents as nothing having happened, which is indistinguishable from success
   until the old credential later expires.

## Resolution

**The rotation procedure, in order:**

1. Obtain the new credential and confirm it is active at the processor.
2. Validate it out of band with a real call. Do not skip this.
3. Write it to Vault alongside the old one — both valid simultaneously.
4. Restart `payment-gateway` replicas in a rolling fashion, watching the error
   rate between each batch rather than only at the end.
5. Verify a real authorization succeeds on the new credential.
6. Only then revoke the old credential at the processor.
7. Confirm the error rate is at baseline for a full traffic cycle before
   considering the rotation complete.

Steps 3 and 6 are the ones that make this zero-downtime: overlap first, revoke
last. Reversing them turns a routine operation into an outage.

**If rotation failed (cause 1 or 2):** restore the previous credential and
restart. This is why the old credential is not revoked until the new one is
proven — the rollback path only exists while both are valid.

**If the secret did not reload (cause 3):** restart the pods. Verify by making a
transaction rather than by reading configuration, since a stale credential in
memory is invisible from the outside.

**If the wrong environment's credential was used (cause 4):** treat any
transactions processed under it as suspect and reconcile the window. This can
produce charges in the wrong environment, which is a
`payment-reconciliation-mismatch` problem.

**For a compromised credential**, the order changes: revoke immediately, accept
the brief outage, and rotate under pressure. Leaving a compromised payment
credential live to preserve uptime is the wrong trade.

## Escalation

Announce every rotation in the incident channel before starting, including
scheduled ones. This costs nothing and removes the most expensive step of
diagnosing a failed rotation.

Page the payment-gateway on-call if a rotation causes a payment outage. Restore
the old credential first and diagnose afterwards — the rollback is fast and safe
precisely because the overlap was preserved.

Involve security for any compromise-driven rotation, and let them set the
urgency. The decision to accept downtime rather than leave an exposed credential
live is theirs, not the on-call engineer's.

Notify finance if transactions were processed under a wrong-environment
credential. Those need reconciliation and may not appear in the expected
settlement at all.

## Related

- `payment-gateway-errors` — its cause 2 is exactly a failed rotation, with the
  signature near-100% error rate and 401/403 responses. That runbook diagnoses
  the outage; this one covers the procedure that avoids or reverses it.
- `payment-processor-failover` — depends on the secondary's credentials being
  valid, which depends on this procedure having been followed for the secondary
  as well as the primary.
- `payment-reconciliation-mismatch` — the aftermath if transactions were
  processed under a wrong-environment credential.
