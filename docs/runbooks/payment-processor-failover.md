---
runbook_id: payment-processor-failover
title: Payment Processor Failover
services:
  - payment-gateway
severity: critical
status: fixture
---

# Payment Processor Failover

## Summary

The primary payment processor is unusable and payments need to move to the
secondary. This runbook covers the decision to fail over, the execution, and the
return — the last of which is where failovers most often go wrong.

Failover is not free and is not automatic. The secondary typically has different
rates, different supported methods, and no knowledge of authorizations held at
the primary. Moving traffic mid-incident means accepting a set of trade-offs
deliberately, which is why the decision threshold belongs in a runbook rather
than in the moment.

The default is to wait. Most processor incidents resolve in minutes, and a
failover executed at minute three often costs more than the outage it was meant
to avoid.

## Symptoms

- `PaymentGatewayErrorRate` sustained near total failure rather than partially
  elevated. Failover is a response to *unusable*, not to *degraded*.
- The processor's status page confirming an incident, with no stated recovery
  time or one measured in hours.
- Errors affecting all card types, all BINs, and all regions uniformly —
  consistent with the processor rather than with anything in our request path.
- Retries failing identically, confirming this is not a transient blip that will
  clear on its own.
- Checkout timeouts rising as failed authorizations consume the checkout budget.

## Impact

While the primary is down and before failover completes, no payments succeed —
a total revenue stop. That is what justifies accepting the trade-offs of moving.

Failover itself carries risk. In-flight authorizations at the primary may
complete after we stop tracking them, which produces exactly the discrepancies
covered in `payment-reconciliation-mismatch`. Expect reconciliation work
afterwards and plan for it as part of the operation rather than discovering it
later.

The secondary may not support every payment method the primary does. Customers
using an unsupported method will fail even after a successful failover, so
"payments restored" is usually partial rather than complete.

## Likely Causes

1. **Processor outage.** A full or partial platform incident at the primary.
   Distinguishing signal: their status page, and uniform failure across all
   traffic. The main reason to fail over.
2. **Processor rate limiting or contract issue.** Sustained 429s or a commercial
   suspension. Distinguishing signal: 429 or account-level errors rather than
   5xx, and possibly a billing or contract cause rather than a technical one.
3. **Regional network partition.** The processor is healthy but unreachable from
   our infrastructure. Distinguishing signal: reachable from outside our network,
   unreachable from inside. Failover may be the wrong response — fixing the path
   may be faster.
4. **Sustained latency without errors.** The processor answers, but so slowly
   that checkout times out. Distinguishing signal: authorizations eventually
   succeed, so errors stay low while timeouts climb. This is the ambiguous case
   where failover is a judgment call.
5. **Planned migration or maintenance.** A scheduled move rather than an
   incident. Distinguishing signal: it is on the calendar — and the procedure
   below should be rehearsed here rather than first attempted under pressure.

## Investigation

1. **Confirm the primary is genuinely unusable, not degraded.** Check error rate,
   the processor's status page, and whether retries succeed at all. Failover
   against a recovering processor is worse than waiting.
2. **Estimate the recovery time.** If the processor states minutes, wait. If it
   states hours or says nothing, prepare to fail over. This estimate is the
   single input that most determines the right decision.
3. **Verify the secondary is actually ready.** Credentials valid, connectivity
   confirmed, configuration current. A secondary that has not been exercised
   recently is a hypothesis, not a fallback — check before committing.
4. **Confirm which payment methods the secondary supports.** Knowing what will
   still fail after failover shapes customer communication and prevents a second
   round of surprise.
5. **Rule out a network cause.** Test reachability from outside our network. For
   cause 3 the fix is the path, not the processor, and failing over leaves the
   real fault in place.
6. **Record the failover start time.** In-flight authorizations at the primary
   around this moment are the reconciliation set, and reconstructing it later is
   far harder than noting it now.

## Resolution

**To fail over:** switch the configured processor for the `payment-gateway` to
the secondary and restart. Verify with a low-value test transaction end to end —
authorization, capture, and appearance in the secondary's dashboard — before
declaring payments restored. A configuration change that has not completed a real
transaction has not been verified.

**Watch for a partial recovery.** Confirm the error rate drops for the methods
the secondary supports, and identify what still fails. Communicate that subset
rather than announcing full restoration.

**During the failover window**, record every authorization the secondary handles
and every one the primary may have completed after the switch. This is the
reconciliation input and it cannot be reconstructed accurately afterwards.

**To fail back — the step that is most often mishandled:** do it deliberately,
during business hours, after the primary has been stable for a meaningful
period. Failing back during the same incident, or overnight because "the primary
is up again," risks a second outage with a tired team. There is rarely urgency;
the secondary is working.

**After returning**, reconcile both processors for the entire window. Two
processors that each handled part of a period is exactly the condition that
produces mismatches, and it should be treated as expected work rather than a
surprise.

## Escalation

Page the payment-gateway on-call and treat any total payment failure as an
incident immediately. The failover *decision* should involve an engineering lead
and the business — it has commercial implications, including different
processing rates, that are not engineering's to make alone.

Notify the business before failing over, not after. If the secondary does not
support some payment methods, that is a customer-facing consequence they need to
communicate.

Engage the primary processor's support channel in parallel, and keep the
relationship open — their recovery estimate is the main input to the fail-back
decision.

Involve finance after any failover. Two processors handling one period changes
settlement and reconciliation, and they should know before month-end rather than
during it.

## Related

- `payment-gateway-errors` — the alert that leads here. Its cause 1 is an
  upstream processor incident, and its resolution says to fail over if a
  secondary is configured. This runbook is that procedure.
- `payment-reconciliation-mismatch` — the expected aftermath. Failover windows
  produce authorizations at the primary that our side stopped tracking, which is
  precisely the discrepancy that runbook handles.
- `payment-credential-rotation` — a failover only works if the secondary's
  credentials are valid, which depends on that rotation procedure having been
  followed.
