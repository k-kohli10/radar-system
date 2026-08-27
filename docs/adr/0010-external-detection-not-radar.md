# 📡 ADR 0010: Detection Is External, RADAR Does Not Watch Metrics

## Status
Accepted

## Context
"Anomaly detection" in RADAR's name could imply RADAR itself evaluates metrics or log
patterns to decide when something is wrong. Prometheus (metric-threshold alerting) and
Kibana Watcher (log-pattern alerting) already solve that problem well, are already
part of the target operational stack, and are what a real SRE team would already have
running. Building a second, RADAR-owned detection layer would duplicate that
capability and introduce a second source of truth for "is something wrong right now."

## Decision
RADAR does not detect anomalies. Detection is entirely owned by Prometheus alertmanager
and Kibana Watcher, configured via alert rules that live in
`deploy/prometheus/alerting-rules.yml`. That's config, not RADAR application code.
RADAR receives only pre-fired alerts, via `POST /alerts/prometheus` and
`POST /alerts/kibana` on the ingestion service. RADAR's job starts at correlation, not
detection.

## Consequences
- RADAR has no metric-evaluation or log-pattern-matching code anywhere in the
  codebase. That entire problem space is out of scope by design, not by oversight.
- Alert thresholds and rules are edited under `deploy/`, independent of any
  application deployment. An SRE can tune what fires without touching application
  code.
- RADAR's correctness for a given incident depends on the upstream alert firing
  correctly in the first place. RADAR has no way to catch a condition that Prometheus
  or Kibana Watcher didn't already flag.
- This boundary is why the product is described as an "Incident Intelligence
  Platform," not an anomaly detector. The name is aspirational about the domain, the
  architecture is precise about the boundary.
