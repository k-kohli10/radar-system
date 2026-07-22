---
runbook_id: checkout-session-state-loss
title: Checkout Session State Loss
services:
  - checkout-service
severity: high
status: fixture
---

# Checkout Session State Loss

## Summary

Customers are losing their cart or checkout progress partway through the flow
and being returned to an earlier step or an empty basket. Nothing errors —
`checkout-service` responds successfully to every request — but the session it
responds about is not the one the customer had a moment ago.

This is a state problem, not an availability problem, and that is why it hides.
Every dashboard is green: latency is normal, error rate is flat, no alert fires.
The signal arrives through support tickets and abandonment metrics rather than
through monitoring, which typically makes it hours old by the time anyone looks.

## Symptoms

- Customer reports of empty carts, lost addresses, or being sent back to the
  start of checkout after completing steps.
- Cart abandonment elevated at a *specific* step rather than spread across the
  funnel — the step where state is being lost.
- Session lookups returning empty for sessions that should still be valid, and
  new session identifiers being issued more often than new visitors arrive. That
  ratio is the clearest quantitative signal.
- No errors and no latency change in `checkout-service`. A successful response
  about the wrong state looks identical to a correct one.
- In the affinity case, the problem correlates with pod restarts or scaling
  events rather than with time of day.

## Impact

Directly revenue-affecting and disproportionately damaging to trust: a customer
who loses a filled cart usually does not rebuild it. Unlike a timeout, there is
nothing to retry — the state is gone, and the work of re-entering it falls on
the customer.

The impact is invisible to system monitoring, which means it can persist for
days at low volume. A 2% session-loss rate produces no alert and no error budget
consumption while quietly removing a slice of every day's revenue.

Nothing is charged and no order exists, so there is no reconciliation work. The
cost is entirely lost sales plus support contacts from customers who assume they
made a mistake.

## Likely Causes

1. **Session store eviction under memory pressure.** The store is full and
   evicting live sessions to make room. Distinguishing signal: eviction counters
   rising, session loss correlating with store memory usage rather than with
   deploys. The most common cause and the easiest to confirm.
2. **Session affinity lost during a rollout or scale event.** Requests land on a
   replica that does not hold the session, because affinity broke when pods
   changed. Distinguishing signal: loss clustered tightly around deploys or
   scaling, and absent between them.
3. **Session TTL shorter than real checkout duration.** Sessions expire while
   customers are still shopping. Distinguishing signal: loss concentrated among
   customers with long sessions, and a step change following a configuration
   change to TTL.
4. **Session identifier not being propagated.** A cookie attribute change,
   domain mismatch, or client-side handling problem means the customer presents
   no identifier and receives a fresh session. Distinguishing signal: new
   session creation rate far exceeding new visitor rate, and correlation with a
   frontend release rather than a backend one.
5. **Session store failover or partial outage.** The store lost data or a node
   went away with sessions on it. Distinguishing signal: a sharp cliff affecting
   many customers simultaneously, rather than a steady low-level rate.

## Investigation

1. **Compare new-session rate against new-visitor rate.** If sessions are being
   created far faster than visitors arrive, existing customers are being issued
   fresh sessions — which confirms state loss and separates it from ordinary
   abandonment. This is the measurement that turns anecdote into a finding.
2. **Check session store memory and eviction counters.** Rising evictions is
   cause 1 and is confirmable in seconds.
3. **Correlate loss timing with deploys and scaling events.** Tight clustering
   around pod changes is cause 2; a steady background rate is not.
4. **Check the configured TTL against real checkout duration.** Compare TTL
   against the p95 time customers actually take. A TTL below that is cause 3 and
   will affect exactly the customers who are most engaged.
5. **Inspect the session identifier end to end.** Confirm it is issued, stored,
   and returned with the attributes expected. Cause 4 usually traces to a
   frontend change rather than anything in `checkout-service`.
6. **Check store cluster health and recent failovers.** A cliff-shaped onset
   points here, and the store's own logs will date it precisely.

## Resolution

**Eviction (cause 1):** raise session store capacity, or reduce what is stored
per session. Sessions holding entire product records rather than identifiers are
a common and easily fixed source of pressure. Raising the eviction threshold
without reducing size only delays recurrence.

**Affinity (cause 2):** move session state out of pod-local memory into the
shared store so any replica can serve any request. Affinity is a workaround, not
a design — it fails at every rollout by construction, and rollouts are routine.

**TTL (cause 3):** raise the TTL above realistic checkout duration with margin,
and consider extending it on activity rather than issuing it once at session
start. Customers who browse for an hour before buying are valuable, not
anomalous.

**Identifier propagation (cause 4):** correct the cookie attributes or client
handling and verify across the browsers and domains actually in use. This is a
frontend fix even though the symptom appears in checkout metrics.

**Store failover (cause 5):** restore the store and accept that in-flight
sessions are lost. If this recurs, session state needs replication — an
architectural change rather than an incident response.

**After any fix**, verify with the new-session-to-new-visitor ratio rather than
with support ticket volume, which lags by hours and undercounts by far more than
it reports.

## Escalation

Page the checkout-service on-call if session loss is widespread — a cliff-shaped
onset affecting many customers is an incident even without an alert firing.

For a low steady rate, this is a working-hours investigation, but it should not
be left indefinitely on the grounds that nothing is alerting. Silent revenue loss
that no dashboard reports is precisely the kind of problem that persists because
it never demands attention.

Bring in the frontend team for cause 4 and the platform team for store capacity
or replication changes. Escalate to product if the TTL is being set below real
customer behaviour, since that is a product decision about how long a cart should
live, not purely a technical one.

## Related

- `checkout-timeout-rate` — the other major checkout failure, and distinguishable
  by whether anything errors. Timeouts are loud and alert; session loss is silent
  and returns successful responses about the wrong state.
- `checkout-cart-abandonment-spike` — the metric that often surfaces this
  problem. Rule out session loss before concluding an abandonment spike is a
  business or pricing issue, because this is the technical cause that most
  resembles one.
