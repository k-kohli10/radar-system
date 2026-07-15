# Testing

## Framework

- **pytest-asyncio for all async tests.** Async code paths are tested with real
  async tests, not sync wrappers.

## Tests must prove behavior, not pass on interfaces

- A test that only exercises an interface shape and always passes is not
  acceptable for a critical guarantee.
- **Where a guarantee is critical, prove it mutation-style:** show the test
  FAILS when the guarantee is removed. Example: the `SKIP LOCKED` concurrency
  test in this repo must fail if `SKIP LOCKED` is dropped — otherwise it proves
  nothing about concurrent poller isolation.

## The Phase 3 bar for critical-path tests

The three named tests introduced in Phase 3 are the standard for what a
critical-path test must demonstrate:

1. **Outbox atomicity** — the state change and its outbox write commit together
   or not at all.
2. **Concurrent poller isolation** — two pollers running at once never claim the
   same event (`SKIP LOCKED`).
3. **Processed-event idempotency** — replaying an already-processed event is a
   no-op.

New critical-path work is expected to meet this same bar.

## Real Postgres for done-conditions

- **Critical done-conditions are verified against real Postgres, not just
  mocks.** Guarantees that depend on database semantics — `SKIP LOCKED`,
  `NOW()`, foreign keys, transactional atomicity — cannot be proven against
  mocks and must run against a real database.
