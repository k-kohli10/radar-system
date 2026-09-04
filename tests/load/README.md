# 🏋️ Load tests

Phase 13's resilience deliverable: drive the pipeline under concurrency and prove
it neither loses data nor hides latency.

## Contents

- [`test_concurrent_alerts.py`: 100 concurrent alerts](#-test_concurrent_alertspy-100-concurrent-alerts)

## 🔀 `test_concurrent_alerts.py`: 100 concurrent alerts

Fires 100 distinct alerts at ingestion at once (via `asyncio.gather`) and drives
the real in-process pipeline: ingestion → watcher → planner → reasoner → the real
outbox-worker → feedback-service, against real Postgres, with only the LLM gateway
mocked (see `tests/e2e/harness.py`). It asserts two properties:

1. **No data loss:**
   - Exactly 100 incidents, 100 investigation plans, and 100 recommendations.
   - Every posted incident gets its own recommendation.
   - The outbox drains to empty: nothing pending, nothing dead-lettered.
   - Not one recommendation is a template fallback (the mock answered every
     call, so a fallback would mean a real failure was masked).

2. **A representative pipeline latency.** `radar_incident_duration_seconds`
   (the incident-pipeline latency panel, deferred from Phase 10 step 12) is
   observed once per recommendation, over the incident's `opened_at →
   recommendation created_at` span (both Postgres `now()`, one clock). The 100
   alerts queue through the outbox, so the histogram gets a real p50/p95/p99
   distribution rather than Phase 10's single best-case point. The test
   asserts the histogram recorded all 100 observations and prints the
   percentiles.

### 🏃 Running it

Needs a Postgres (the suite skips cleanly without one):

```bash
POSTGRES_DSN=postgresql://user:pass@localhost:5432/postgres \
  uv run pytest tests/load/test_concurrent_alerts.py -s
```

### 📊 Representative result

Measured on the dev machine (Apple Silicon, Postgres 16 in Docker), one run:

```
incident-pipeline latency over 100 concurrent alerts (seconds):
p50=4.99  p95=6.22  p99=6.30  max=6.33
```

Read these as the pipeline's own queueing latency, **not** end-user RCA time. The
mock gateway answers instantly, so no model latency is included (the LLM has its
own `radar_llm_duration_seconds`).

The absolute numbers are machine-dependent and reflect the harness's
single-threaded drain: 100 incidents open near-simultaneously and are worked off
sequentially, so latency rises with queue position. That is exactly the shape the
p50/p95/p99 spread captures.

What the test *guarantees* across machines is the no-data-loss property and that
the panel is fed every observation. The percentiles are reported, not asserted
against a fixed threshold.
