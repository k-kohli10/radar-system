# 🏷️ ADR 0014: Event Schema Versioning Rules

**Status**: Accepted
**Date**: 2025-01-15
**Author**: Kashyap

---

## Contents

- [Context](#context)
- [Event Structure](#event-structure)
- [Versioning Scheme](#versioning-scheme)
- [What Is a Breaking Change](#what-is-a-breaking-change)
- [Rules](#rules)
- [Current Event Types and Versions](#current-event-types-and-versions)
- [Payload Schemas (v1)](#payload-schemas-v1)
- [Decision Record](#decision-record)

---

## Context

Outbox events carry a JSON payload. As RADAR evolves, these payloads will change.
A field gets added. A field gets renamed. A field gets removed. Without rules for
how this happens, you end up with a pile of events in the outbox that a newer
version of an agent cannot parse, or worse, silently misreads.

This document defines the rules.

---

## Event Structure

Every outbox event has this envelope, which never changes:

```json
{
  "event_id": "uuid",
  "event_type": "alert.normalized",
  "schema_version": "1",
  "correlation_id": "uuid",
  "occurred_at": "2025-01-15T10:30:00Z",
  "payload": {}
}
```

The `payload` field is where schema changes happen. The envelope fields are frozen.

---

## Versioning Scheme

`schema_version` is a simple integer starting at 1. It increments by 1 for every
breaking change. Non-breaking changes do not increment the version.

```
schema_version: "1"    initial payload shape
schema_version: "2"    first breaking change
schema_version: "3"    second breaking change
```

No semver. No minor versions. No patch versions. One number. Breaking or not breaking.

---

## What Is a Breaking Change

A breaking change is anything that requires the consumer to update its parsing code
to avoid a runtime error or silent data corruption.

| Breaking | Not breaking |
|---|---|
| Removing a field the consumer reads | Adding a new optional field the consumer does not need to read |
| Renaming a field the consumer reads | Adding a new enum value the consumer handles with a default case |
| Changing the type of a field (string to integer, object to array) | Adding fields to a nested object the consumer ignores |
| Changing the semantic meaning of a field value (status codes, enum values) | |

---

## Rules

### Rule 1: Never remove or rename a field without incrementing the version

If you remove `service_name` from `alert.normalized`, existing events in the outbox
with the old schema will fail to parse. Consumers that already processed some events
will break on the next batch.

Add a new field instead of renaming. Deprecate the old one. Remove it only after
all consumers have migrated and no old-schema events remain in the outbox.

### Rule 2: Consumers must handle unknown fields without crashing

Use Pydantic's `model_config = ConfigDict(extra="ignore")` on all event payload
models. A consumer receiving an event with extra fields it does not recognize must
ignore them and continue processing.

This makes adding new fields non-breaking by definition.

```python
class AlertNormalizedPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    event_id: UUID
    service_name: str
    alert_name: str
    severity: str
    fired_at: datetime
```

### Rule 3: Consumers must check schema_version before parsing

Each consumer reads the `schema_version` from the envelope before parsing the
payload. If the version is higher than the consumer knows how to handle, the
consumer rejects the event and writes it to the dead letter queue with reason
`schema_version_unsupported`.

```python
SUPPORTED_SCHEMA_VERSIONS = {"alert.normalized": {"1", "2"}}

def can_handle(event: OutboxEvent) -> bool:
    supported = SUPPORTED_SCHEMA_VERSIONS.get(event.event_type, set())
    return event.schema_version in supported
```

This prevents silent data corruption from a consumer misreading a newer event shape.

### Rule 4: Both schema versions must be supported during any transition

When you introduce schema_version 2 for `alert.normalized`, the consumer must
handle both version 1 and version 2 simultaneously for a transition period. This
period lasts until all version 1 events have been processed and drained from the
outbox.

```python
def parse_alert_normalized(event: OutboxEvent) -> AlertNormalizedPayload:
    if event.schema_version == "1":
        return AlertNormalizedPayloadV1.model_validate(event.payload)
    if event.schema_version == "2":
        return AlertNormalizedPayloadV2.model_validate(event.payload)
    raise UnsupportedSchemaVersion(event.schema_version)
```

### Rule 5: Old schema versions are removed only when the outbox is fully drained

Do not remove support for schema_version 1 while there are still version 1 events
sitting in the outbox (status=pending or status=dead_letter). Check the outbox table
before removing old version handling:

```sql
SELECT COUNT(*) FROM outbox_events
WHERE event_type = 'alert.normalized'
  AND payload->>'schema_version' = '1'
  AND status IN ('pending', 'processing', 'dead_letter');
```

If the count is zero, it is safe to remove v1 handling.

### Rule 6: Schema changes are documented in CHANGELOG.md

Every schema version bump gets a CHANGELOG entry:

```markdown
## [Unreleased]
### Changed
- alert.normalized: schema_version 1 -> 2
  - Added: `alert_group_id` (UUID, optional) for multi-alert correlation
  - Deprecated: none
  - Removed: none
  - Migration: consumers must handle both v1 (no alert_group_id) and v2
```

---

## Current Event Types and Versions

```
alert.normalized              schema_version: 1
incident.plan_requested       schema_version: 1
incident.reasoning_requested  schema_version: 1
recommendation.created        schema_version: 1
```

All at version 1. This is the baseline. Any change to any of these payloads must
follow the rules above.

---

## Payload Schemas (v1)

### alert.normalized

```json
{
  "alert_id": "uuid",
  "source": "prometheus",
  "fingerprint": "sha256hex",
  "service_name": "order-service",
  "alert_name": "OrderProcessingFailureRate",
  "severity": "critical",
  "labels": {},
  "annotations": {},
  "fired_at": "2025-01-15T10:30:00Z"
}
```

### incident.plan_requested

```json
{
  "incident_id": "uuid",
  "service_name": "order-service",
  "alert_name": "OrderProcessingFailureRate",
  "severity": "critical",
  "alert_count": 1,
  "opened_at": "2025-01-15T10:30:00Z"
}
```

### incident.reasoning_requested

```json
{
  "incident_id": "uuid",
  "plan_id": "uuid",
  "service_name": "order-service",
  "alert_name": "OrderProcessingFailureRate",
  "severity": "critical",
  "alert_count": 1,
  "opened_at": "2025-01-15T10:30:00Z"
}
```

### recommendation.created

```json
{
  "recommendation_id": "uuid",
  "incident_id": "uuid",
  "is_fallback": false,
  "confidence": "medium",
  "service_name": "order-service"
}
```

---

## Decision Record

Integer schema versions. Extra fields ignored. Version checked before parsing.
Both versions supported during transitions. Old versions removed only after drain.
All changes documented in CHANGELOG.
