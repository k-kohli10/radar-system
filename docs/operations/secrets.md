# 🔑 Secrets and Vault

How RADAR's secrets are stored and how they reach the pods, and the two Vault
models — the bundled dev Vault for evaluation, and a **persistent / HCP Vault**
for production where secrets are entered once and CD carries nothing sensitive.

For rotating a secret that already exists, see
[vault-secret-rotation.md](vault-secret-rotation.md). The trust boundary this sits
on is B4 in the [threat model](../architecture/threat-model.md); the mechanism is
ADR 0007 (Vault init-container files only).

## The one rule

Every secret RADAR uses is a **file** delivered by a Vault init-container. Nothing
reads a secret from an environment variable or a baked image layer. Each pod
authenticates to Vault (kubernetes auth, `role=radar-<service>`) and reads only its
own paths under `secret/radar/*`. So "where do secrets live" has one answer —
**Vault** — and the only question is which Vault, and how it gets seeded.

## Two Vault models

| | **Bundled dev Vault** (evaluation) | **Persistent / HCP Vault** (production) |
|---|---|---|
| What | `vault server -dev` inside `platform-deps` | A Vault outside the cluster lifecycle — self-hosted with persistent storage, or [HCP Vault](https://portal.cloud.hashicorp.com/) |
| Lifetime | Dies with the cluster (in-memory) | Outlives every cluster rebuild |
| Seeded by | The `vault-bootstrap` Job, per cluster, from k8s Secrets you create first ([kubernetes.md](kubernetes.md) Step 3) | **You, once** — `vault kv put`, then never again |
| App secrets | Re-supplied each cluster (k8s Secret or CD input) | Entered once; survive teardown |
| CD carries | The provider key / Slack tokens (optional CD inputs → k8s Secrets → dev Vault) | **Nothing sensitive** — only `CD_KUBECONFIG` to reach the cluster |

The dev Vault is the fifteen-minute-quickstart path: everything is in one chart and
a fresh cluster is fully seeded by the bootstrap Job. Its cost is that app secrets
have to be re-supplied every time the cluster is rebuilt, and — if you deploy via
CD — they ride in as GitHub Actions secrets (`RADAR_OPENAI_API_KEY`,
`RADAR_SLACK_*`; see [cd.yml](../../.github/workflows/cd.yml)).

## The persistent / HCP model

Point RADAR at a Vault that lives outside the cluster and the picture inverts: the
secrets are entered **once**, directly into Vault, and survive every `helm upgrade`
and every cluster rebuild. This is the [bring-your-own-backends](../../deploy/examples/bring-your-own-backends/README.md)
shape — you do not install `platform-deps` at all.

**Enter the app secrets once** (the credentials RADAR does not mint — the provider
key and, optionally, Slack):

```bash
vault kv put secret/radar/llm openai_api_key=sk-YOUR-OPENAI-KEY
vault kv put secret/radar/feedback-service \
  slack_bot_token=xoxb-... slack_app_token=xapp-...
vault kv put secret/radar/postgres postgres_dsn=postgresql+asyncpg://...
```

RADAR's own credentials (the per-service agent tokens and gateway-mode grants) are
minted into the same Vault — adapt the bundled bootstrap
([`k8s-vault-bootstrap.py`](../../deploy/helm/platform-deps/files/k8s-vault-bootstrap.py),
token model in [`dev-mint-tokens.py`](../../scripts/dev-mint-tokens.py)) or run an
equivalent. Your Vault needs the kubernetes auth method enabled and a
policy+role `radar-<service>` per service (the BYO README lists the exact
requirements).

**Point the app chart at it** — set `vault.addr` (and `vault.postgresHost`,
`env.RADAR_ELASTICSEARCH_URL`) in your values; no secret values go in the chart,
only the address of the Vault that holds them.

### CD carries nothing sensitive

With the secrets already in the persistent Vault, the deploy pipeline needs no
secret values at all. CD's only secret is `CD_KUBECONFIG` — cluster reach to run
`helm upgrade` — and the optional `RADAR_OPENAI_API_KEY` / `RADAR_SLACK_*` inputs
go **unused** (they exist for the dev-Vault path). The chart references Vault paths,
never secret contents, so nothing sensitive is in git, in the image, or in the CD
logs. Rotating a secret is a `vault kv put` + pod restart
([rotation runbook](vault-secret-rotation.md)) — the pipeline never sees it.

## Relationship to the k8s-Secret intermediary

In the dev-Vault flow the human-supplied secrets reach Vault through k8s Secrets
(`radar-llm-keys`, `radar-slack-keys`) that the bootstrap reads. A persistent Vault
removes that hop — you `vault kv put` straight into `secret/radar/*`. Removing the
intermediary from the *dev* flow too (seeding the dev Vault directly, dropping the
two k8s Secrets and the bootstrap's read of them) was investigated in Phase 12 and
left as optional follow-up "Commit B"; it needs care to preserve the
feedback-service agent token and the base-path creation the bootstrap does today.
