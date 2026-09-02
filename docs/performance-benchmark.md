# 📊 Performance Benchmark

How RADAR behaves under a **100-alert burst** on a real Kubernetes cluster, with
the **real LLM** in the loop: measured end to end, not mocked. 🎯

**Headline:** 100 simultaneous alerts → **100 RCAs, zero data loss, zero
fallbacks**, drained in **~7 minutes** on a single reasoner replica.

---

## 📚 Contents

- [🧪 What Was Measured](#-what-was-measured)
- [✅ Results](#-results)
- [🔍 How to Read the Latency](#-how-to-read-the-latency)
- [🆚 vs. the Mocked Baseline](#-vs-the-mocked-baseline)
- [🔁 Reproduce It](#-reproduce-it)

---

## 🧪 What Was Measured

100 distinct alerts fired **concurrently** at the live ingestion endpoint, each
driving the full pipeline (ingestion → watcher → planner → reasoner → **real LLM
gateway** → recommendation) through the Postgres outbox at every hop.

| Setting | Value |
|---|---|
| 🖥️ Cluster | 4-node K3s, RADAR deployed via Helm |
| 🤖 LLM | **real** gateway (live model calls, not mocked) |
| 🔢 Load | 100 distinct alerts, fired at once |
| 🧵 Reasoner | **1 replica** (default) |
| ⏱️ Latency span | `incident.opened_at → recommendation.created_at` (both Postgres `now()`, one clock) |
| 🛠️ Driver | [`scripts/load-benchmark.sh`](../scripts/load-benchmark.sh) |

> 🔕 `feedback-service` was scaled to 0 for the run, so the 100 RCAs did **not**
> post 100 Slack cards. It's restored automatically when the run ends.

---

## ✅ Results

### No data loss

| Metric | Value |
|---|---|
| Alerts accepted (`202`) | **100 / 100** |
| Incidents · plans · recommendations | **100 · 100 · 100** |
| Silent fallbacks (masked LLM failures) | **0** |
| Every incident → its own recommendation | ✅ |

### Latency & throughput

| Measure | Value |
|---|---|
| p50 | **207.3 s** |
| p95 | **385.9 s** |
| p99 | **401.2 s** |
| max | 405.2 s |
| min | 4.9 s |
| avg | 208.7 s |
| Full drain (100 RCAs) | **417 s (~7 min)** |
| Throughput | **~14–15 RCAs / min** (single reasoner replica, real LLM) |

---

## 🔍 How to Read the Latency

These are **queue-position** times, **not** per-RCA processing time. 100 incidents
open in the same second, and one reasoner replica works them off roughly
sequentially through the real LLM, so an incident's latency is mostly *time spent
waiting in the queue*:

- ⚡ **min 4.9 s**, the first incident: one real LLM call, no wait.
- 🐢 **max 405 s**, the last incident: ~6.7 min queued behind the other 99.
- 📈 **p50 ≈ avg ≈ 207 s ≈ ½ · max**: the signature of a linear queue drain.

So the number that describes the system is the **throughput (~14–15 RCAs/min)** and
the **no-data-loss guarantee under a 100× burst**: the durable outbox means nothing
is dropped, doubled, or crossed, it just queues. More reasoner replicas raise
throughput and pull the tail in; the per-RCA LLM cost (~5 s) is the floor.

---

## 🆚 vs. the Mocked Baseline

[`tests/load/`](../tests/load/) runs the same 100-alert shape **in-process with the
LLM mocked**; it isolates RADAR's own queueing latency with model time removed:

| | Mocked, in-process ([tests/load](../tests/load/)) | Live, real LLM (this doc) |
|---|---|---|
| p50 | 4.99 s | 207.3 s |
| p95 | 6.22 s | 385.9 s |
| Includes model time | ❌ | ✅ |
| Runs on the cluster | ❌ | ✅ |

The gap is the real model latency plus real single-replica throughput: exactly what
the mocked test deliberately factors out.

---

## 🔁 Reproduce It

Against a running cluster (`kubectl` pointed at it):

```bash
scripts/load-benchmark.sh 100      # 100 concurrent alerts, real LLM
scripts/load-benchmark.sh 25       # a smaller run
```

The script fires the burst, waits for the drain, and prints the no-data-loss counts
and p50/p95/p99, reading latency straight from the Postgres spans. It protects your
Slack channel automatically (see the note above).
