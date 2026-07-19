---
runbook_id: payment-gateway-errors
title: Payment Gateway Errors
alert_name: PaymentGatewayErrorRate
services:
  - payment-gateway
severity: critical
status: fixture
---

# Payment Gateway Errors

## Summary

More than 5% of payment authorization calls are returning errors. These are
gateway *faults* — the request failed to complete — and are categorically
different from declines, where the call succeeded and the issuer said no.

That distinction drives everything in this runbook. A decline is a normal
business outcome and needs no engineering response. An error means the platform
could not get an answer at all, so the customer cannot pay for reasons that have
nothing to do with their card. If you are looking at rising *declines* rather
than errors, `payment-decline-rate` is the correct runbook.

## Symptoms

- `PaymentGatewayErrorRate` firing, `service=payment-gateway`,
  `severity=critical`.
- `payment_gateway_error_rate` above 0.05. Baseline is well under 1%; the
  upstream processor is normally reliable and any sustained elevation is
  abnormal.
- Errors concentrated in one HTTP status or error code rather than spread. 5xx
  from the processor points upstream; 4xx points at our own request construction
  or credentials; timeouts point at the network path or processor latency.
- `CheckoutTimeoutRate` firing shortly after, as failed authorizations consume
  checkout's timeout budget and retries extend it further.
- Declines *flat* while errors climb. If both are rising together, suspect a
  processor-side incident affecting authorization broadly rather than a fault on
  our side.

## Impact

Critical and immediately revenue-affecting. Every errored authorization is a
purchase that cannot complete, and unlike a decline the customer has no
remediation available — trying a different card will not help, because the
failure is not about the card.

Nothing is charged, so there is no refund exposure. The cost is lost sales plus
the support load from customers who assume the problem is theirs.

There is a worse case worth ruling out early: if authorizations are succeeding
at the processor but erroring on the response path, the customer may be charged
without an order existing. Confirm which side of the call is failing before
declaring there is no financial exposure.

## Likely Causes

1. **Upstream processor incident.** The payment processor is degraded or down.
   Distinguishing signal: 5xx or timeouts from the processor, their status page
   confirming, and errors affecting all card types and BINs uniformly. The most
   common cause and the one we cannot fix directly.
2. **Expired or rotated API credentials.** Authentication against the processor
   is failing. Distinguishing signal: a sharp step to a very high error rate —
   often near 100% — with 401 or 403 responses, and no partial degradation.
   Credentials fail completely, not gradually.
3. **Network path failure.** DNS, egress, or TLS problems reaching the
   processor. Distinguishing signal: connection-level errors and timeouts rather
   than HTTP status codes, and failures beginning before any request is answered.
4. **Recent deploy.** A change to request construction, a field the processor now
   rejects, or an SDK upgrade with different defaults. Distinguishing signal: 4xx
   validation errors stepping up at rollout time.
5. **Rate limiting by the processor.** Sustained volume above the contracted
   limit, or a burst pattern that trips their throttle. Distinguishing signal:
   429 responses, often correlated with a promotion or a retry storm we are
   generating ourselves.

## Investigation

1. **Separate errors from declines immediately.** Confirm you are looking at
   authorization *failures*, not issuer refusals. These are different metrics and
   different runbooks, and conflating them sends the investigation in a direction
   that cannot succeed.
2. **Aggregate by error code.** In Kibana, query
   `service:payment-gateway AND level:error` over the last 30 minutes and group
   by response status. The dominant code maps almost directly onto the cause
   list: 5xx to cause 1, 401/403 to cause 2, connection errors to cause 3, 4xx to
   cause 4, 429 to cause 5.
3. **Check the processor's status page and our own error onset time.** If their
   incident began before our errors, cause 1 is confirmed and the investigation
   becomes a communication exercise rather than a debugging one.
4. **Verify credential validity and age.** Check when the processor API
   credentials were last rotated and whether they are within their validity
   window. A near-100% error rate with no deploy is cause 2 until proven
   otherwise.
5. **Determine which side of the call failed.** For a sample of errored
   authorizations, check whether the processor recorded an authorization that we
   never successfully received. This is the check that rules out — or confirms —
   customers charged without orders, and it should not be deferred.
6. **Check deploys and traffic.** `kubectl rollout history
   deployment/payment-gateway -n ecommerce`, and compare request volume against
   the same window yesterday for cause 5.

## Resolution

**Processor incident (cause 1):** we cannot fix it. Fail fast rather than
retrying into a known-bad backend — retries extend checkout timeouts and add
load to a struggling processor. If a secondary processor is configured, fail
over. Otherwise notify the business early; this is a case where communication is
the entire available response.

**Credential failure (cause 2):** rotate and redeploy the credentials from
Vault. Confirm the new credential works against the processor before restarting
broadly, so a bad rotation is not repeated across every replica.

**Network path (cause 3):** verify DNS resolution, egress rules, and TLS trust
from an affected pod directly. A recently changed network policy or an expired
intermediate certificate is the usual culprit.

**Recent deploy (cause 4):** roll back — `kubectl rollout undo
deployment/payment-gateway -n ecommerce`. Payment request construction is not a
place to debug forward under load.

**Rate limiting (cause 5):** reduce retry aggressiveness first, since we are
often amplifying our own problem, then request a limit increase if volume is
legitimately higher. Exponential backoff with jitter, not fixed-interval retries.

**Before closing**, reconcile against the processor for the window to confirm no
authorization succeeded on their side without a corresponding order on ours.
The alert clearing says new payments work; it says nothing about the ones that
failed midway.

## Escalation

Page the payment-gateway on-call immediately. This is critical and
revenue-affecting, and payment failures escalate to business stakeholders faster
than any other alert in the platform.

Notify the business and prepare customer messaging if the error rate exceeds 20%
or persists beyond ten minutes. For an upstream processor incident, do this
early — the fix is not ours to make and the only thing engineering controls is
how quickly everyone else finds out.

Engage the processor's support channel for any suspected upstream fault, and
bring in finance if reconciliation shows authorizations succeeding upstream
without matching orders. That combination is a financial exposure, not just an
availability problem.

## Related

- `payment-decline-rate` — the alert most easily confused with this one, and the
  distinction that matters most. Declines are issuers refusing cards, a normal
  business outcome; errors are the gateway failing to complete a call. Different
  causes, different responses, different severity.
- `checkout-timeout-rate` — the downstream symptom. Failed authorizations consume
  checkout's timeout budget, so this alert frequently drags that one with it.
- `order-service-high-failure-rate` — what to check if payments are succeeding
  but orders are not appearing. That is a post-payment failure, not a payment
  failure.
