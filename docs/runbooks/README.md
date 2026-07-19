# Runbooks

Operational runbooks for the services simulated by `apps/platform-sim`. These are
the corpus the knowledge-service indexes and the reasoner retrieves against.

> **These are fixtures.** Every file here carries `status: fixture` in its
> frontmatter. They are drafted to be structurally uniform and factually
> plausible so retrieval, reranking, and CRAG grading have real content to work
> against — not to read as though a specific on-call engineer wrote them. A
> hand-editing pass for voice is deferred until before this repo is
> portfolio-facing. `status: fixture` is the machine-visible marker for that
> outstanding work; a runbook that has had the voice pass becomes
> `status: reviewed`.

## Frontmatter contract

Every runbook begins with a YAML frontmatter block. These fields are the join
keys between the corpus, the Postgres manifest, and the alerting rules — a typo
in any of them fails **silently** (retrieval simply returns nothing, forever),
which is why `tests/` asserts the join rather than trusting it.

```yaml
---
runbook_id: order-service-high-failure-rate
title: Order Service High Failure Rate
alert_name: OrderProcessingFailureRate
services:
  - order-service
severity: critical
status: fixture
---
```

| field | required | notes |
|---|---|---|
| `runbook_id` | yes | Stable identifier, must equal the filename stem. Maps to `runbook_documents.runbook_id` (unique, ≤128 chars). |
| `title` | yes | Human-readable. Prepended to every chunk as a breadcrumb (see below). |
| `alert_name` | Tier-1 only | Must match an `alert:` name in `deploy/prometheus/alerting-rules.yml` **exactly**. Omitted on depth runbooks, which no single alert triggers. |
| `services` | yes | List, because a runbook may span services. Every entry must appear as a `service:` label in the alerting rules. Pre-filters retrieval. |
| `severity` | yes | `critical \| high \| medium \| low`. For Tier-1, must match the alert rule's `severity:` label. |
| `status` | yes | `fixture \| reviewed`. See the note above. |

### Tier-1 runbooks

A **Tier-1** runbook describes an alert that can actually fire. The mapping is
one-to-one and is enforced by test:

| runbook | alert | service | severity |
|---|---|---|---|
| `order-service-high-failure-rate` | `OrderProcessingFailureRate` | order-service | critical |
| `order-service-high-memory` | `OrderServiceHighMemory` | order-service | medium |
| `checkout-timeout-rate` | `CheckoutTimeoutRate` | checkout-service | high |
| `inventory-check-latency` | `InventoryCheckLatency` | inventory-service | high |
| `payment-gateway-errors` | `PaymentGatewayErrorRate` | payment-gateway | critical |
| `payment-decline-rate` | `PaymentDeclineRate` | payment-gateway | medium |

Runbooks beyond this table are **depth** runbooks: they carry no `alert_name`
and exist so retrieval has to disambiguate *within* a service, not merely across
services. Distinguishing "order-service is failing to persist orders" from
"order-service is leaking memory" is the harder and more realistic problem.

## Chunking boundary

The indexer chunks on **`##` (H2) section boundaries**, one chunk per section,
with the document title prepended to each chunk as a breadcrumb so a chunk
retrieved in isolation still says what it belongs to. `###` subsections stay
with their parent H2 unless the section exceeds the size cap, in which case it
splits at `###` — still a stable, author-visible boundary.

**There is deliberately no overlap between chunks.** Overlap exists to rescue
fixed-window chunking from cutting mid-thought; semantic section boundaries do
not have that problem. And overlap would actively corrode incremental indexing:
chunk identity is a content hash, so an overlapping window means editing one
section changes the hash of its neighbours too, degrading "re-embed only the
changed chunks" into "re-embed the neighbourhood."

Practical consequence for anyone writing a runbook: **an H2 section is the unit
of retrieval.** Write each section so it stands on its own. A section that only
makes sense after reading the one above it will be retrieved without that
context and will read as a fragment.

## Section structure

Keep sections in this order. It is what makes the corpus uniform enough for
retrieval quality to be about *content* rather than about structural accidents.

- `## Summary` — what is happening, in two or three sentences.
- `## Symptoms` — what an engineer observes: alerts, dashboards, user reports.
- `## Impact` — who is affected and how badly. Revenue-affecting or not.
- `## Likely Causes` — ranked, most common first, each with its distinguishing
  signal.
- `## Investigation` — ordered, concrete steps. Name the actual tool and query.
- `## Resolution` — the fix per cause, plus how to confirm it worked.
- `## Escalation` — when to stop investigating and page someone, and whom.
- `## Related` — links to adjacent runbooks. These are the near-misses retrieval
  has to rank correctly.
