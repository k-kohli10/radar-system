# Example: minimal

The smallest way to run RADAR for evaluation: the in-cluster platform
dependencies (dev-grade Postgres, Vault, Elasticsearch, …) plus the eight RADAR
services, with autoscaling turned off so you don't even need metrics-server.

```bash
# 1. supplied secrets (see docs/operations/kubernetes.md for the Slack one)
kubectl create namespace radar-infra
kubectl -n radar-infra create secret generic radar-llm-keys \
  --from-literal=openai_api_key=sk-YOUR-OPENAI-KEY

# 2. platform dependencies (defaults) + its Vault bootstrap
helm install radar-infra deploy/helm/platform-deps -n radar-infra

# 3. the app chart, minimal overlay
helm install radar deploy/helm/radar -n radar --create-namespace \
  -f deploy/examples/minimal/values.yaml --timeout 10m
```

This is dev/evaluation-grade. For a real deployment against managed backends, see
[`../bring-your-own-backends`](../bring-your-own-backends). The full step-by-step
(image build, verification, troubleshooting) is in
[docs/operations/kubernetes.md](../../../docs/operations/kubernetes.md).
