---
runbook_id: inventory-oversell-incident
title: Inventory Oversell Incident
services:
  - inventory-service
severity: critical
status: fixture
---

# Inventory Oversell Incident

## Summary

More units have been sold than exist. Available stock has gone negative, or
orders have been accepted against stock already committed to other orders. Every
oversold order is a promise the warehouse cannot keep.

This is the most consequential inventory failure, and unlike the others it
cannot be fixed by correcting a number. The orders are placed, the customers are
charged, and someone has to decide which of them will not be fulfilled. Speed
matters more here than in any other inventory runbook: the cost grows with every
additional order accepted, and it becomes unrecoverable the moment fulfilment
starts shipping against phantom stock.

**Stop the bleeding before diagnosing.** Preventing further oversell is the
first action, ahead of understanding why it happened.

## Symptoms

- Available stock negative for one or more products: the unambiguous signal,
  and one that should never occur under correct behaviour.
- Fulfilment reporting orders for items with no physical stock, usually the way
  this is discovered.
- Sold quantity exceeding the quantity that was available at the start of the
  window, for a specific product.
- Reservation count lower than the number of live checkouts, indicating
  reservations are being released or skipped when they should be held.
- Frequently correlated with a flash sale, promotion, or launch: high
  concurrency on a small number of products is the condition under which this
  failure mode appears.

## Impact

Critical, customer-facing, and financially real. Customers have been charged for
items that cannot ship, and every one of those orders needs cancellation,
refund, and an apology. Unlike a decline or a timeout, the customer completed
the purchase successfully and has every reason to expect it.

The damage is worst on promotional launches, which is precisely when this failure
is most likely: high concurrency on limited stock is both the trigger condition
and the moment with the most customer attention.

Fulfilment cost compounds if shipping begins before the discrepancy is caught:
partially shipped oversold orders mean split shipments, returns, and manual
reconciliation. This is why halting fulfilment for affected items ranks above
diagnosis.

## Likely Causes

1. **Race condition on concurrent reservation.** Two or more checkouts read the
   same available quantity and both reserve against it, because the read and the
   decrement are not atomic. Distinguishing signal: oversell concentrated on
   popular items during high-concurrency periods, and the oversold amount
   scaling with concurrency. The most common cause by a wide margin.
2. **Reservation released prematurely.** Stock is freed while a checkout is
   still live, so another customer takes it. Distinguishing signal: the mirror of
   `inventory-stock-reservation-leak`: expiry too aggressive, or a release
   firing on the wrong event.
3. **Stock level corrected upward in error.** A manual adjustment, a bad import,
   or a sync from an upstream system set availability above physical stock.
   Distinguishing signal: a step change in stock level with no corresponding
   receipt of goods.
4. **Reservation bypassed on a code path.** An order path that decrements stock
   without reserving, or that skips the availability check entirely.
   Distinguishing signal: oversell traced to orders from one channel or one API
   route.
5. **Stale cache serving old availability.** Availability answered from a cache
   showing stock that has since been committed. Distinguishing signal: oversell
   with correct database state: the database was right and the answer came from
   somewhere else.

## Investigation

1. **Quantify the exposure first.** How many products are oversold, by how many
   units, and how many orders are affected. This number drives every subsequent
   decision, including whether to halt sales entirely.
2. **Stop further oversell before continuing.** Set affected products
   unavailable, or halt the affected channel. Every minute of diagnosis with the
   product still purchasable adds orders to the recovery set.
3. **Determine whether the database or the answer was wrong.** Check whether the
   database's own stock records were correct at the time orders were accepted. If
   they were, availability came from a cache or a bypassed path, pointing at
   cause 4 or 5.
4. **Examine reservation timing on oversold orders.** Reservations created
   within milliseconds of each other against the same quantity confirm cause 1.
   This is visible in reservation timestamps and is usually conclusive.
5. **Check for recent stock adjustments.** An upward correction with no goods
   receipt is cause 3 and is often the simplest explanation.
6. **Identify the channel and route of oversold orders.** Concentration in one
   path points at cause 4 and narrows the code search sharply.

## Resolution

**Immediate, before anything else:** make affected products unavailable and halt
fulfilment for them. Recovering an oversold order is far cheaper before it ships
than after.

**Race condition (cause 1):** reservation must be atomic: a conditional
decrement that fails when insufficient stock remains, rather than a read
followed by a write. Optimistic concurrency with a retry is acceptable;
read-then-write is not, and no amount of narrowing the window makes it correct.

**Premature release (cause 2):** lengthen the expiry window and fix the
erroneous release trigger. Note the trade-off against
`inventory-stock-reservation-leak`: too long leaks stock, too short oversells.
Oversell is the more damaging direction, so err long.

**Erroneous adjustment (cause 3):** correct the stock level against a physical
count, and require verification for manual upward adjustments. An adjustment
that increases availability without a goods receipt should not be a single
person's unchecked action.

**Bypassed reservation (cause 4):** close the path. Every route that commits
stock must go through the same reservation logic; a second implementation will
drift from the first.

**Stale cache (cause 5):** availability reads for the reservation decision must
not come from cache. Cache the product catalogue, not the commitment decision.

**Recovery, in every case:** reconcile oversold orders against physical stock,
decide allocation deliberately (order time is the defensible rule), then cancel
and refund the remainder with proactive communication. Do not let affected
customers discover it from a delivery date that silently slips.

## Escalation

Page the inventory-service on-call immediately and treat this as an incident
from the first confirmed negative stock. This is the one inventory failure where
the response must begin before the cause is known.

Notify fulfilment and customer service at once, ahead of root cause. Fulfilment
must stop shipping affected items and customer service will start receiving
contacts as soon as cancellations go out.

Escalate to business and finance for any material exposure: refunds, goodwill
compensation, and the decision about which customers are cancelled are not
engineering calls. Engineering's job is the accurate list and the stopped
bleeding.

## Related

- `inventory-stock-reservation-leak`: the mirror failure. That one holds stock
  that should be free and costs sales; this one frees stock that should be held
  and costs orders. Same subsystem, opposite direction, and the expiry window is
  the shared dial that trades one against the other.
- `inventory-check-latency`: a performance problem, not a correctness one. A
  slow inventory check delays a customer; this one makes a promise that cannot
  be kept.
- `inventory-cache-invalidation-storm`: relevant via cause 5, since serving
  availability from cache is what makes stale reads possible in the first place.
