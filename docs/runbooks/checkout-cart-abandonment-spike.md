---
runbook_id: checkout-cart-abandonment-spike
title: Checkout Cart Abandonment Spike
services:
  - checkout-service
severity: medium
status: fixture
---

# Checkout Cart Abandonment Spike

## Summary

Customers are reaching checkout and leaving without completing a purchase, at a
rate meaningfully above baseline. Abandonment is always high in absolute terms
(a majority of carts are abandoned on a normal day), so what matters here is the
change, not the level.

This runbook exists to answer one question: **is this a system problem or a
business one?** Most of the time it is a business one, and the correct
engineering outcome is a confident handoff rather than a fix. That is an unusual
conclusion for a runbook, and it is why the investigation is ordered to rule out
technical causes first and quickly: a wrong "it's a business issue" leaves a
real defect running.

Note also that this can be a measurement artifact. Confirm the number is real
before investigating a cause for it.

## Symptoms

- Checkout abandonment rate elevated against the same window last week, not
  against yesterday, and not against an absolute threshold. Abandonment has
  strong daily and weekly shape, and comparing across the wrong period
  manufactures spikes that do not exist.
- Conversion falling with no corresponding error rate, latency, or availability
  signal. That absence is meaningful: it is what distinguishes this from a
  technical failure and is checked, not assumed.
- Abandonment concentrated at one funnel step, which usually names the cause:
  shipping cost at the delivery step, friction at the payment step, trust at the
  review step.
- No customer complaints. Customers who hit an error complain; customers who
  choose not to buy simply leave, which is why this reaches monitoring before it
  reaches support.

## Impact

Revenue-affecting, but through customer choice rather than system failure.
Nothing is broken, nothing is charged, and there is no reconciliation or data
risk.

The severity is medium because the response is rarely urgent and rarely
engineering's. The exception is when abandonment is the visible symptom of a
silent technical fault (session loss, a broken payment step on one browser, a
mispriced shipping calculation), in which case the real severity belongs to that
fault and this alert was the only thing that noticed.

An abandonment spike sustained over days is a larger cumulative revenue loss than
most outages, precisely because it never triggers an incident response.

## Likely Causes

1. **A measurement or tracking change.** Analytics instrumentation changed, a
   tag broke, bot traffic entered the denominator, or the funnel definition
   moved. Distinguishing signal: the change coincides with an analytics or
   frontend release, and no downstream business metric moved with it. Check this
   first: investigating a cause for a number that is wrong wastes the entire
   investigation.
2. **A silent technical fault.** Session loss, a payment step failing for a
   subset of browsers or devices, or a broken control that no error metric
   covers. Distinguishing signal: abandonment concentrated in one segment
   (browser, device, region) rather than spread evenly. **This is the case that
   must not be missed**, and segmentation is what surfaces it.
3. **Pricing, shipping, or fee change.** A change to shipping cost, tax display,
   or fees presented late in the flow. Distinguishing signal: abandonment
   concentrated at the step where the cost first appears, and a step change
   aligned with a pricing release.
4. **Traffic mix change.** A marketing campaign, a promotion, or a channel shift
   bringing visitors who convert at a different rate. Distinguishing signal:
   overall traffic up, conversion down, absolute order count flat or higher:
   the rate moved because the denominator changed, not because anything got
   worse.
5. **External factors.** A competitor promotion, a seasonal shift, or a news
   event. Distinguishing signal: everything internal checks out and the change
   correlates with nothing we control. This is a real answer, not a shrug, but
   it is the last one to reach.

## Investigation

1. **Confirm the number is real.** Verify against an independent source:
   completed orders in the database against sessions reaching checkout. If order
   volume is unchanged, the abandonment metric moved without customer behaviour
   moving, and this is cause 1. Do this before anything else.
2. **Compare against the same window last week, not yesterday.** Weekly shape is
   strong. A Monday spike measured against Sunday is usually not a spike.
3. **Segment by browser, device, and region.** This is the check that catches
   cause 2. Even spread suggests a business cause; concentration in one segment
   means something is broken for those customers specifically and this is a
   technical incident wearing business clothing.
4. **Identify the funnel step where customers leave.** The step names the cause
   more reliably than any other signal: delivery step points at shipping cost,
   payment step at payment friction or failure.
5. **Rule out the known silent faults explicitly.** Check
   `checkout-session-state-loss` indicators and payment decline rates. Both
   produce abandonment without producing errors, and both are engineering
   problems.
6. **Check for pricing, shipping, and campaign changes in the window.** Ask
   marketing and merchandising directly rather than inferring; a campaign launch
   is invisible in engineering systems.

## Resolution

**Measurement artifact (cause 1):** correct the instrumentation. Communicate
clearly that the spike was not real: an uncorrected false alarm erodes trust in
the metric, and the next genuine spike will be dismissed.

**Silent technical fault (cause 2):** stop treating this as an abandonment
investigation and work the underlying fault. This alert did its job by surfacing
something no error metric caught.

**Pricing or shipping change (cause 3):** hand to the business with the funnel
data. Engineering's contribution is the evidence (which step, which segment,
how much), not the pricing decision.

**Traffic mix (cause 4):** no action. Document it so the next person reading the
graph does not re-investigate. A conversion rate that fell because traffic grew
is not a regression.

**External factors (cause 5):** hand to the business with the analysis that
internal causes were excluded. "We checked and it is not us" is a complete and
useful answer when it is backed by the checks.

**In all cases**, record which cause it was. Abandonment spikes recur, and the
single biggest time saving on the next one is knowing what the last one turned
out to be.

## Escalation

Does not page. This is a working-hours investigation at `severity=medium`.

Escalate to engineering urgently if segmentation shows concentration in one
browser, device, or region: that is a technical fault affecting a specific
customer population, and it should be treated with the severity of the fault
rather than the severity of this alert.

Hand to product, marketing, or merchandising once technical causes are excluded,
with the funnel and segmentation data attached. Handing over the raw metric
without the analysis usually results in it coming straight back.

Escalate to the business if the spike is sustained for more than a few days. The
cumulative revenue impact of a persistent abandonment shift exceeds most
incidents, and it will not resolve on its own.

## Related

- `checkout-session-state-loss`: the technical cause most likely to be
  misdiagnosed as an abandonment problem. Customers who lose their cart abandon
  it, and nothing errors. Rule this out before concluding the cause is business.
- `checkout-timeout-rate`: if checkout is failing outright, abandonment rises as
  a side effect. That alert is the incident; this one is the shadow it casts.
- `payment-decline-rate`: declines at the payment step produce abandonment
  without any fault on our side, and the decline runbook covers the causes.
