---
runbook_id: payment-reconciliation-mismatch
title: Payment Reconciliation Mismatch
services:
  - payment-gateway
severity: critical
status: fixture
---

# Payment Reconciliation Mismatch

## Summary

The payment processor's record of what was authorized disagrees with our record
of what was ordered. In the direction that matters most, the processor has
authorizations we have no order for — customers charged for purchases that do
not exist in our system.

This is a money problem, not an availability problem. Nothing is down, no alert
is firing, and the platform is serving traffic normally. The discrepancy is
discovered by comparing two systems that each believe they are correct, which is
why it is found hours or days after it happens rather than in the moment.

Direction determines urgency. Charges without orders are the serious case:
customers have paid for nothing and will not all notice. Orders without charges
are revenue we failed to collect — worth fixing, but nobody is owed money.

## Symptoms

- Processor settlement totals not matching our recorded order totals for the
  same period, which is how this is normally detected.
- Individual authorizations at the processor with no corresponding order,
  payment record, or correlation id on our side.
- Customer contacts reporting a charge with no order confirmation — often the
  first signal, and it lags the event by a day or more.
- Orders in a paid state with no matching processor authorization, the reverse
  direction.
- Frequently preceded by an incident: a `PaymentGatewayErrorRate` window, a
  gateway deploy, a timeout spike, or a processor outage. The mismatch is usually
  the residue of a failure that has already been resolved.

## Impact

Direct financial exposure and a regulatory concern. Customers charged without an
order are owed a refund whether or not they notice, and "they did not complain"
is not a resolution.

The reputational cost exceeds the monetary one. A customer who discovers an
unexplained charge on their statement loses trust in a way that a failed checkout
does not, and the discovery usually happens well after the incident, when nobody
is watching for it.

There is also an accounting consequence: settlement figures that do not
reconcile complicate month-end close and, if unresolved, become an audit finding.
Finance needs to know early, not at the end of the period.

## Likely Causes

1. **Authorization succeeded, response lost.** The processor authorized the
   payment and our side never received or processed the confirmation — a
   timeout, a crash, or a network failure on the return path. Distinguishing
   signal: mismatches clustered in a window where gateway errors or timeouts
   were elevated. The most common cause, and the one
   `payment-gateway-errors` warns about.
2. **Order creation failed after successful payment.** Payment completed, then
   order persistence failed. Distinguishing signal: mismatches correlate with an
   `OrderProcessingFailureRate` window rather than a gateway one.
3. **Retry produced a duplicate authorization.** A retried request authorized
   twice while only one order exists. Distinguishing signal: two authorizations
   with near-identical amounts and timestamps against one order — the customer is
   charged twice.
4. **Refund or void not propagated.** A cancellation processed on our side
   without a matching reversal at the processor, or the reverse. Distinguishing
   signal: mismatches concentrated among cancelled or refunded orders.
5. **Timing or boundary artifact.** Authorizations near a settlement cutoff
   landing in different periods on each side. Distinguishing signal: mismatches
   cluster tightly at period boundaries and resolve themselves in the next
   period. **Rule this out before treating anything as a real discrepancy** — it
   is the most common false alarm.

## Investigation

1. **Establish the direction and the magnitude.** Count authorizations without
   orders and orders without authorizations separately, with totals for each.
   These are different problems with different urgency, and a single net figure
   conceals both.
2. **Rule out the boundary artifact first.** Re-run the comparison with a window
   extended past the settlement cutoff on both sides. If the discrepancy
   disappears, it was never real — and treating a timing artifact as lost money
   sends everyone chasing nothing.
3. **Correlate the mismatch window against known incidents.** Compare against
   gateway error rate, order failure rate, and deploy history. Cause 1 and cause
   2 are usually identifiable from timing alone.
4. **Trace individual cases end to end by correlation id.** Take a sample of
   unmatched authorizations and follow each through gateway logs, order records,
   and outbox events. The point at which the trail stops names the cause
   precisely.
5. **Check for duplicate authorizations.** Look for multiple authorizations with
   the same card, amount, and a short interval. Cause 3 charges customers twice
   and is the most urgent to remediate.
6. **Verify the reconciliation job itself.** Confirm it ran, covered the full
   period, and did not silently skip records. A reconciliation report is evidence
   only if the process producing it is sound — check the tool before trusting its
   output.

## Resolution

**Charges without orders (causes 1 and 2):** decide per case whether to fulfil
or refund. If the customer intended the purchase and stock is available,
creating the order honours the transaction and is usually the better outcome.
Where fulfilment is not possible, refund proactively and tell the customer —
they are owed the money regardless of whether they have noticed.

**Duplicate authorizations (cause 3):** void or refund the duplicate
immediately. This is the highest-priority remediation because the customer has
been charged twice for one purchase, and it erodes trust faster than any single
failure.

**Orders without charges (reverse direction):** attempt to collect if the
authorization is still valid; otherwise treat as a write-off and record it.
Do not silently cancel a fulfilled order — the goods have shipped.

**Unpropagated refunds (cause 4):** complete the missing reversal on whichever
side lacks it, then find the propagation gap. A refund that succeeds on one side
only will recur.

**Boundary artifact (cause 5):** no action beyond correcting the comparison
window. Document the cutoff behaviour so the next reconciliation does not
re-raise it.

**Systemically**, the fix for causes 1 and 2 is that payment and order creation
must be recoverable as a unit — an idempotency key on the authorization plus a
reconciliation path that can complete a half-finished transaction. A design where
a lost response means a lost order will keep producing these.

## Escalation

Notify finance as soon as a genuine mismatch is confirmed — before root cause,
and before remediation. They need the exposure figure early, and they own the
accounting treatment.

Page the payment-gateway on-call if mismatches are ongoing rather than
historical. A discrepancy that is still growing is an active incident; a
discrepancy from a resolved outage last week is remediation work.

Escalate to customer service with the affected list before customers contact
them, so the first conversation is proactive. Involve compliance for any material
exposure — charges without goods have regulatory implications beyond the refund
itself.

## Related

- `payment-gateway-errors` — the most common origin. That runbook's investigation
  step 5 checks whether authorizations succeeded upstream while erroring on our
  side; when it finds that, the residue is handled here.
- `payment-decline-rate` — unrelated to this. A decline produces no charge and no
  order, which is consistent on both sides and never appears as a mismatch.
- `order-service-high-failure-rate` — cause 2's origin, where payment succeeds
  and order creation fails afterwards. That runbook's instruction to reconcile
  charged-but-unfulfilled orders leads here.
