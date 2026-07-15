# Architecture constraints

These are locked decisions. They mirror
[docs/implementation_plan.md](../../docs/implementation_plan.md) and the ADRs in
[docs/adr/](../../docs/adr/). Do not deviate without explicit approval.

## Incident ownership

- **Ingestion owns incident creation.** It is the only service that INSERTs
  incidents, using a **5-minute fingerprint dedup window** to attach duplicate
  alerts to an existing incident.
- **Watcher never INSERTs incidents.** It only loads incidents, enriches them,
  applies suppression/escalation logic, and emits `plan_requested`.

## Outbox pattern

- **Atomicity:** every write to the outbox happens in the **same database
  transaction** as the state change that triggered it. There is no code path
  that mutates state and writes the outbox in separate transactions.
- **Typed payloads:** the `EventEnvelope` typed contract from `radar_contracts`
  is used for **all** outbox payloads. No hand-built dicts.

## Idempotency

- **Every agent checks `processed_events` before handling any event.** An event
  already recorded there is a no-op. This is the guard against duplicate
  delivery.

## Plugin backends

- **Config-driven loader against Protocol interfaces in `radar_contracts`.**
  Concrete backends are resolved at runtime from config; call sites depend only
  on the `radar_contracts` Protocols.
- **No direct vendor imports outside `plugins/`.** Vendor SDKs live behind
  plugins; nothing in `apps/` or `packages/` imports a vendor client directly.

## Tokens

- **Per-service token model, not a shared platform token.** Each service
  authenticates with its own credential; there is no single shared token.
