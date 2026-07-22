---
runbook_id: inventory-stock-reservation-leak
title: Inventory Stock Reservation Leak
services:
  - inventory-service
severity: high
status: fixture
---

# Inventory Stock Reservation Leak

## Summary

Stock reservations are being created but not released, so available inventory
falls steadily while physical stock is untouched. Products show as out of stock
that are sitting in the warehouse.

Nothing here is slow and nothing errors. `inventory-service` answers every query
quickly and correctly according to its own records — the records are simply
wrong, because they count reservations that no longer correspond to a live
checkout. This is a correctness failure wearing an availability costume, and it
is invisible to latency and error monitoring alike.

The direction matters: this leak makes real stock *unavailable*. Its mirror,
where reservations are released that should not be, produces the opposite and
more damaging failure covered in `inventory-oversell-incident`.

## Symptoms

- Available stock declining without corresponding sales. The gap between
  physical stock and available stock widening over time is the definitive signal.
- Products reported out of stock that the warehouse confirms are present. This
  is usually how it is discovered, via merchandising or customer service rather
  than monitoring.
- Active reservation count climbing monotonically and not falling during quiet
  periods. Reservations should turn over; a count that only rises is leaking.
- Reservation age distribution with a long tail — reservations far older than
  any realistic checkout duration, which cannot correspond to live sessions.
- No latency change, no error rate change, no alert. The service is healthy by
  every operational measure.

## Impact

Lost sales on products that are genuinely available, which is the most avoidable
form of revenue loss: nothing is broken physically, nothing is overselling, and
the only thing preventing the sale is a stale record.

The impact compounds silently. A slow leak takes days to make a popular product
unbuyable, and because it presents as ordinary stock depletion, it usually gets
explained away as demand until someone compares against the warehouse.

Merchandising damage extends beyond the direct loss: products marked
out-of-stock are demoted or hidden in listings and search, so recovery is not
immediate once the leak is fixed. The item has to regain its position.

## Likely Causes

1. **Reservation release missing on an abandoned checkout.** Reservations are
   created when checkout begins and released on completion, but abandoned
   checkouts never release. Distinguishing signal: leak rate tracks the
   abandonment rate, and reservation ages cluster just beyond typical session
   length. The most common cause.
2. **Release skipped on an error path.** The release runs on success only, so
   any failure mid-checkout leaks its reservation. Distinguishing signal: leak
   rate correlates with checkout error or timeout rate rather than with
   abandonment — expect this alongside `CheckoutTimeoutRate`.
3. **Expiry job stopped or failing.** The sweeper that reclaims stale
   reservations is not running. Distinguishing signal: leak begins abruptly with
   no application change, and reservation ages extend without bound rather than
   plateauing.
4. **Expiry window longer than checkout duration.** The sweeper works but waits
   far longer than any real checkout takes, so stock is held needlessly.
   Distinguishing signal: reservations do clear, but only after hours, and the
   steady-state reserved quantity is large.
5. **Orphaned reservations from a failed order path.** Orders that failed after
   reserving stock — see `order-service-high-failure-rate` — leave reservations
   with no owning order. Distinguishing signal: reservation count rising in step
   with order processing failures.

## Investigation

1. **Compare available stock against physical stock.** For an affected product,
   compare what the system reports available, what is reserved, and what the
   warehouse holds. A large reserved quantity with no matching live checkouts
   confirms the leak and quantifies it immediately.
2. **Plot active reservations over 24 hours.** Healthy reservations oscillate
   with traffic and fall overnight. Monotonic growth through the overnight
   trough is conclusive.
3. **Read the reservation age distribution.** Anything older than the longest
   plausible checkout is leaked by definition. The shape of the tail
   distinguishes cause 3 (unbounded) from cause 4 (bounded but too long).
4. **Check the expiry sweeper is running and succeeding.** Confirm last run
   time and outcome. A silently failing job is cause 3 and is the fastest to
   confirm or exclude.
5. **Correlate leak rate with abandonment, checkout errors, and order
   failures.** These three correlations separate causes 1, 2, and 5 from each
   other, and each points at a different code path.
6. **Sample leaked reservations and trace their origin.** Take a handful of old
   reservations and find what created them and what should have released them.
   This is what identifies the specific missing release rather than the general
   category.

## Resolution

**Immediate relief, any cause:** release reservations older than the maximum
plausible checkout duration. This restores availability within minutes and is
safe — a reservation that old cannot belong to a live session. Do this before
root-causing; the stock is the urgent part.

**Missing release on abandonment (cause 1):** reservations must carry a TTL and
expire on their own rather than depending on an explicit release. A design where
the only path to freeing stock is a successful checkout will leak, because not
all checkouts succeed.

**Error-path leak (cause 2):** release reservations in a construct that runs on
every exit path, not only the success path. Same class of bug as a connection
leak, and it responds to the same discipline.

**Sweeper stopped (cause 3):** restore the job and alert on its absence.
A reclaim job that fails silently is worse than no job, because the design
depends on it while nothing verifies it runs.

**Expiry window too long (cause 4):** shorten it to just beyond realistic
checkout duration. Too long holds stock needlessly; too short releases stock
from live checkouts and risks overselling — this is a genuine trade-off, and
erring slightly long is the safer direction.

**Orphaned from failed orders (cause 5):** work the order failure first, then
reclaim the orphans. They will keep accumulating while order processing fails.

**After any fix**, verify the reserved quantity returns to a stable oscillating
baseline rather than a lower monotonic climb. A reduced leak is still a leak.

## Escalation

Page the inventory-service on-call if popular products are unbuyable. The stock
is real and the loss is entirely avoidable, which makes this more urgent than
its silent presentation suggests.

Escalate to merchandising once availability is restored, so demoted products can
be reinstated in listings. Fixing the data does not automatically undo the
merchandising consequence.

Bring in the checkout or order-service owner for causes 2 and 5 — the missing
release lives in their code path, not in `inventory-service`, even though the
symptom surfaces here.

## Related

- `inventory-oversell-incident` — the mirror failure and the more damaging one.
  This runbook covers stock held that should be free; that one covers stock sold
  that does not exist. Check which direction the discrepancy runs before acting,
  because the fixes are opposites.
- `inventory-check-latency` — unrelated. That is inventory answering slowly; this
  is inventory answering quickly with wrong numbers.
- `checkout-timeout-rate` — a common upstream driver via cause 2, since timed-out
  checkouts are exactly the ones that never reach their release step.
