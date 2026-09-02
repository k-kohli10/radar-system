---
runbook_id: payment-decline-rate
title: Payment Decline Rate
alert_name: PaymentDeclineRate
services:
  - payment-gateway
severity: medium
status: fixture
---

# Payment Decline Rate

## Summary

Card declines are running above 2 per second, measured as a rate over the last
2 minutes. A decline is not a fault: the authorization call succeeded, reached
the issuer, and the issuer refused the card. Our systems worked correctly.

That is why this is `severity=medium` while `payment-gateway-errors` is
critical, and why the first question is not "what is broken" but "is this
external, or did we change something." A decline spike is usually a signal
about traffic, an issuer, a BIN range, a fraud rule, a bot, rather than a
signal about the platform. If authorization calls are *failing* rather than
being refused, `payment-gateway-errors` is the correct runbook.

## Symptoms

- `PaymentDeclineRate` firing, `service=payment-gateway`, `severity=medium`.
- `rate(payment_declines_total[2m])` above 2/s. This is the only range-vector
  alert in the platform: declines are a counter, so what matters is the rate of
  change, not an instantaneous level. It takes roughly 3 minutes of sustained
  declines to fire, because the rate window fills before the `for` duration
  begins to count.
- Decline rate elevated while `payment_gateway_error_rate` stays flat. That
  combination is the signature of this runbook rather than the errors one.
- Declines concentrated in a subset: one issuer, one BIN range, one country,
  one card type: rather than spread evenly across traffic.
- Checkout conversion falling with no corresponding latency or error signal, as
  customers reach payment and are refused.

## Impact

Revenue-affecting but not a platform fault, which makes this the alert most
likely to be misread in both directions: dismissed as "just declines" when a
fraud rule is wrongly refusing legitimate customers, or escalated as an outage
when an issuer is having a bad afternoon.

Customers who are declined generally retry with another card, so a portion of
the lost revenue recovers on its own. Concentrated declines: a single issuer or
BIN: are the exception, because affected customers may have no alternative card
from a different issuer.

There is no financial exposure and no reconciliation debt: nothing was charged,
no order was created, and the customer received an immediate answer. Nothing
needs cleaning up afterwards.

## Likely Causes

1. **Issuer-side incident.** One bank's authorization systems are degraded or
   refusing broadly. Distinguishing signal: declines concentrated in a single
   issuer or BIN range while other issuers stay at baseline. The most common
   cause and, like an upstream processor incident, not ours to fix.
2. **Our own fraud rules tightened.** A rule change, a threshold adjustment, or
   a model update refusing more traffic than intended. Distinguishing signal: a
   step change aligned with a fraud-configuration deploy, and declines spread
   across issuers rather than concentrated. This is the cause that is genuinely
   ours and the most important to rule in or out early.
3. **Card testing or carding attack.** An attacker running stolen card numbers
   through checkout, which declines at a very high rate by nature.
   Distinguishing signal: a sharp spike in *attempts* alongside declines,
   unusual traffic patterns, many distinct cards from few sources, low-value
   orders. This warrants a security response, not a payments one.
4. **Expired card wave.** A seasonal cluster of cards expiring, most visible at
   month boundaries. Distinguishing signal: decline reason codes dominated by
   expiry, and a gradual rise rather than a step.
5. **Currency, region, or routing change.** A change to how transactions are
   presented to issuers: merchant category, currency, or acquirer routing,
   causing more refusals. Distinguishing signal: declines rising for one region
   or currency after a configuration change.

## Investigation

1. **Confirm these are declines, not errors.** Check `payment_gateway_error_rate`
   first. If errors are also elevated, work `payment-gateway-errors` instead:
   a system that cannot complete authorization calls is a more urgent problem
   than issuers refusing cards, and the two need opposite responses.
2. **Break declines down by issuer and BIN range.** This is the decisive split.
   Concentration in one issuer or BIN is cause 1 and external; an even spread
   across issuers points inward, to cause 2 or 3.
3. **Read the decline reason codes.** Issuers return a reason. "Insufficient
   funds" spread across issuers is normal business variance. "Do not honour"
   concentrated in one issuer is cause 1. Expiry-dominated is cause 4.
4. **Check for fraud-rule changes.** Review recent changes to fraud
   configuration and thresholds. A step change in declines aligned with such a
   change is cause 2: and it means we are refusing legitimate customers, which
   is worse than it looks on the dashboard.
5. **Check attempt volume, not just decline volume.** Cause 3 shows up as
   attempts rising far faster than normal with a very high decline ratio. If
   attempts are flat and only the decline *ratio* moved, it is not an attack.
6. **Compare against the same window last week.** Declines have a strong daily
   and weekly shape. A spike that matches last week's pattern at the same hour
   may be normal traffic composition rather than an incident.

## Resolution

**Issuer incident (cause 1):** there is no fix on our side. Confirm the
concentration, notify the business that a specific issuer's customers cannot
pay, and monitor for recovery. If the processor supports retry routing for that
issuer, it may help marginally: do not retry broadly, as repeated declines can
count against merchant standing with the issuer.

**Fraud rules (cause 2):** roll back the rule change if legitimate customers are
being refused. This is the cause with a real fix and a real cost to leaving in
place: every wrongly-declined customer is a lost sale plus a support contact.
Re-tune against historical traffic before re-enabling rather than adjusting live.

**Card testing (cause 3):** escalate to security. The payments response is
secondary: rate-limit the source, enable additional verification, and block
offending patterns. Do not simply loosen fraud rules to reduce the decline
rate: that is the attacker's goal.

**Expiry wave (cause 4):** no engineering action. If it recurs predictably at
month boundaries, a proactive card-update prompt is a product improvement, not
an incident response.

**Routing or currency change (cause 5):** revert the configuration change and
verify decline rates return to baseline before attempting it again with issuer
guidance.

**Before closing**, confirm the decline rate returned to baseline and record
which cause it was. Declines have enough natural variance that an unexplained
spike which "resolved itself" is worth a note: the next one may be the same
cause and recognising it saves the investigation.

## Escalation

Does not auto-page at `severity=medium`. Handle it during working hours unless
one of the escalating conditions below applies.

Escalate to the payments team and the business if the decline rate more than
doubles from baseline and stays there for 15 minutes, or if declines are
concentrated in a major issuer: that becomes a customer-communication question
rather than an engineering one.

Escalate to security immediately for suspected card testing, regardless of
severity or hour. An attacker running stolen cards through our checkout is a
security incident that happens to surface on a payments dashboard, and treating
it as a payments problem loses time.

Escalate to the fraud team if a rule change is refusing legitimate customers.
That is silent revenue loss and, unlike an issuer incident, it does not recover
on its own.

## Related

- `payment-gateway-errors`: the alert most easily confused with this one, and
  the distinction that matters most. There, authorization calls *fail* and the
  platform is at fault; here, calls succeed and the issuer refuses. Check
  `payment_gateway_error_rate` to tell them apart: flat means this runbook,
  elevated means that one.
- `checkout-timeout-rate`: unrelated despite both affecting purchases. Declines
  are immediate answers, not timeouts, so a decline spike does not slow checkout
  and will not produce that alert.
- `order-service-high-failure-rate`: what applies if payment *succeeded* and the
  order still did not appear. A decline means no charge and no order, which is
  the consistent, expected outcome.
