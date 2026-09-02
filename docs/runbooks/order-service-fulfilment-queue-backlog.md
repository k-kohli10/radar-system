---
runbook_id: order-service-fulfilment-queue-backlog
title: Order Service Fulfilment Queue Backlog
services:
  - order-service
severity: high
status: fixture
---

# Order Service Fulfilment Queue Backlog

## Summary

Orders are being published to the fulfilment queue faster than they are being
consumed, and the backlog is growing. Every order is present and correct,
nothing has been lost, but orders are sitting unfulfilled for longer than they
should.

This is a consumer-side problem that presents on the producer's dashboard.
`order-service` is working perfectly; the queue is the visible symptom of
something downstream not keeping up. That distinction matters because the
instinct to restart or scale `order-service` is exactly wrong here: it produces
*more* messages into a queue nobody is draining.

## Symptoms

- Fulfilment queue depth climbing steadily rather than oscillating around a
  stable level. A healthy queue is not empty; it is bounded.
- Consumer lag increasing, with the oldest unacknowledged message aging.
  Message age is the better signal than raw depth: a deep queue draining fast is
  fine, a shallow queue that is not draining is not.
- Order creation rate normal, order fulfilment rate depressed. The gap between
  the two curves is the backlog accumulating.
- Customer reports of orders confirmed but not shipped, arriving hours after the
  backlog began.
- No errors in `order-service`. Publishes are succeeding, which is what
  separates this from the publish-failure case.

## Impact

Delayed fulfilment rather than lost orders. Nothing needs reconciliation and no
data is at risk: this is a latency problem measured in hours rather than
milliseconds.

The customer-visible impact is delayed shipping notifications and, if the
backlog persists past a carrier cutoff, missed delivery promises. That makes the
practical deadline a business one rather than a technical one: a backlog cleared
before the daily cutoff costs nothing, and the same backlog cleared an hour later
costs a day of delivery time across every affected order.

If the queue has a retention limit, a sufficiently long backlog risks messages
aging out entirely: at which point delayed fulfilment becomes lost fulfilment.
Establish the retention window early; it converts a vague "we should fix this"
into a hard deadline.

## Likely Causes

1. **Fulfilment consumers stopped or crash-looping.** The consumer deployment is
   down, failing readiness, or restarting repeatedly. Distinguishing signal:
   consume rate at or near zero while publish rate is normal. The fastest cause
   to confirm and the most common.
2. **Consumers slowed by their own downstream.** The warehouse system or a
   third-party logistics API is slow, so each message takes longer to process.
   Distinguishing signal: consume rate depressed but non-zero, and consumer-side
   latency elevated per message.
3. **Poison message blocking a partition.** A message that fails processing and
   is retried forever holds up everything behind it. Distinguishing signal: one
   partition backed up while others drain normally, and the same message id
   repeating in consumer error logs.
4. **Publish rate genuinely above consumer capacity.** A promotion or traffic
   spike produced more orders than fulfilment is provisioned to handle.
   Distinguishing signal: both rates elevated, consume rate at its known ceiling,
   and the backlog growing at the difference.
5. **Consumer scaled down.** An HPA change, a resource limit, or a deliberate
   scale-down left fewer consumers than the workload needs. Distinguishing
   signal: consumer replica count lower than usual with per-consumer throughput
   normal.

## Investigation

1. **Check consumer health before touching anything.** Confirm the fulfilment
   consumer deployment is running, ready, and not restarting. Cause 1 accounts
   for most occurrences and takes seconds to rule in or out.
2. **Compare publish rate against consume rate.** The two curves tell you the
   shape of the problem: consume at zero is cause 1, consume depressed is cause
   2 or 5, both elevated is cause 4.
3. **Read the oldest message age, not just the depth.** Depth without age is
   ambiguous. Age tells you how far behind fulfilment actually is and whether
   the retention window is in play.
4. **Check per-partition distribution.** One partition lagging while others are
   healthy is cause 3, and the blocked partition's consumer log will show the
   same message failing repeatedly.
5. **Check consumer replica count and recent scaling events.** A quiet
   scale-down is easy to miss because nothing failed and no alert fired.
6. **Check the consumer's own dependencies.** If the warehouse or logistics API
   is slow, this is cause 2 and the fix is not in the queue at all.

## Resolution

**Consumers down (cause 1):** restore them. If they are crash-looping, read the
crash reason before restarting again: a consumer that crashes on a specific
message will crash again immediately, which is cause 3 in disguise.

**Slow downstream (cause 2):** work the downstream problem. Scaling consumers
into a slow dependency adds concurrency against something already struggling and
usually makes throughput worse rather than better.

**Poison message (cause 3):** move the offending message to the dead-letter
queue so the partition can drain, then investigate it separately. Do not delete
it: it represents a real order, and it needs handling even if it cannot be
processed automatically.

**Insufficient capacity (cause 4):** scale consumers up. Confirm the downstream
system can absorb the extra concurrency first; fulfilment usually ends at a
system with its own limits.

**Scaled down (cause 5):** restore the replica count and find out why it changed.
An unexplained scale-down will recur.

**In every case**, do not scale or restart `order-service`. It is producing
correctly, and adding producers to a queue that is not draining makes the
backlog grow faster. Confirm recovery by watching oldest-message age fall to
baseline, not by watching depth alone: depth can fall while the oldest messages
still sit unprocessed.

## Escalation

Page the fulfilment on-call, not the order-service on-call. This alert surfaces
on order-service dashboards but the problem is downstream, and routing it by
where it was noticed rather than where it lives costs an escalation hop.

Escalate to an incident if the oldest message age approaches the queue's
retention window: that converts delayed orders into lost ones and is a hard
deadline rather than a judgment call.

Notify the business if the backlog will not clear before the daily carrier
cutoff. That is a customer-commitment decision, and fulfilment and support need
the warning to manage it rather than discovering it from customers.

## Related

- `order-service-high-failure-rate`: the opposite failure, easily confused.
  There, publishes *fail* and orders never reach the queue at all, which needs
  reconciliation. Here, publishes succeed and orders sit in the queue, which
  needs patience or more consumers. Check whether publishes are erroring before
  choosing between them.
- `order-service-connection-pool-exhaustion`: unrelated cause, but both present
  as "orders are not completing." Pool exhaustion stops orders being created;
  this stops created orders being fulfilled.
