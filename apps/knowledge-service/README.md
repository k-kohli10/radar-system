# knowledge-service

Indexes the runbook corpus and serves grounding context to the reasoner.

The corpus lives in [`docs/runbooks/`](../../docs/runbooks/); its frontmatter and
chunking contract are documented in that directory's README, and the join
between runbook frontmatter and the Prometheus alert rules is enforced by
`tests/test_runbook_alert_contract.py`.

## Design constraints

- **Never calls a model provider directly.** Embeddings and CRAG grading go
  through `llm-gateway`, which is the only service holding provider API keys.
  This service holds TWO gateway tokens, one per mode — `gateway_token_embed`
  and `gateway_token_reason` — because "one token = one mode" is a locked
  decision, so a leaked embedding credential cannot be spent on reasoning.
- **Retrieval is index-side.** BM25 and kNN both execute in Elasticsearch;
  only top-k crosses the wire. The corpus is never scored in Python.
- **Indexing is incremental.** Chunk ids are content hashes, so a re-run
  re-embeds only what changed. There is no full-rebuild path.
- **Touches only `runbook_documents`** and the Elasticsearch index — never the
  pipeline tables.

## The retrieval pipeline

```
services pre-filter -> BM25 (top 20)  ┐
                                      ├─ RRF fuse (top 5) -> CRAG grade -> context
                    -> kNN  (top 20)  ┘
```

There is **no cross-encoder rerank stage**. One was built, measured against a
criterion pre-registered before it existed, and removed on the evidence — it did
not reliably fix either probe it targeted, was the pipeline's only source of
run-to-run variance, and cost a `reason`-mode call per incident. The probes and
per-stage baselines are checked in under [`tests/retrieval/`](../../tests/retrieval/),
and the account is in the Phase 8 divergence record in
[`docs/implementation_plan.md`](../../docs/implementation_plan.md).

**CRAG can return nothing, and that is the point.** When every retrieved chunk
grades `insufficient`, the context is empty and the reasoner is told the corpus
does not cover this incident — rather than being handed the least-bad wrong
runbook. That path is gated by
[`tests/e2e/test_crag_empty_context.py`](../../tests/e2e/test_crag_empty_context.py).

## Modules

| module | role |
|---|---|
| `chunking` | Pure: runbook markdown → stable, content-addressed chunks. No I/O. |
| `reconciliation` | Pure: what a run must embed, delete, and skip. No I/O. |
| `query` | Pure: incident fields → the retrieval query string. |
| `fusion` | Pure: reciprocal rank fusion over the two search legs. |
| `crag` | Pure: the grading prompt, reply parsing, and applying grades. |
| `embeddings` | Gateway client for `/v1/embed` (`embed` mode). |
| `crag_client` | Gateway client for `/v1/complete` (`reason` mode). Degrades to ungraded rather than failing. |
| `retrieval` | The I/O shell: embed → both searches → fuse → grade. Satisfies `KnowledgeStore.retrieve`. |
| `indexer` | The I/O shell for indexing: reads the corpus, performs the reconciled work, records the manifest. |
| `api` | `POST /v1/context` — the boundary the reasoner grounds RCAs across. |
| `main` | Service assembly: lifespan, readiness, metrics. |
| `config` | Settings and the two Vault-mounted gateway tokens. |
| `index` | `make index` — one incremental indexing pass. |

The Elasticsearch mapping and the two search primitives live in the
[`plugins/knowledge/elastic/`](../../plugins/knowledge/elastic/) plugin, not
here: no vendor SDK is imported outside `plugins/`.

## The context API

`POST /v1/context`, guarded by this service's agent token. Takes the incident
shape (`service_name`, `alert_name`, `investigation_steps`) and returns graded
chunks.

**An empty result and a failure are different answers, and the status code keeps
them apart.** `{"chunks": []}` with `200` is CRAG's judgment that nothing in the
corpus is relevant. A `503` means retrieval could not run. Collapsing them would
let the reasoner believe "no runbook covers this" when the truth is "retrieval
was down" — so the reasoner records which happened on the stored context bundle.

Each entry carries `grade` (`sufficient` or `partial`) and `status`, which is
`fixture` for the whole corpus until a human review pass. There is deliberately
**no `score`**: RRF fuses by rank and discards scores, and BM25 and cosine are
not on a comparable scale, so any single number would be invented precision.

## Running an indexing pass

```bash
make tokens && make agent-secrets   # mint + pull the two gateway tokens
make gateway                        # the llm-gateway must be up to embed
make index                          # one incremental pass over docs/runbooks/
```

Re-running on an unchanged corpus is a no-op — `embedded=0`, every runbook
skipped — which is the incremental guarantee, visible from the command line.

## Indexed document shape

One Elasticsearch document per chunk, `_id` = `chunk_id`.

| field | type | purpose |
|---|---|---|
| `chunk_id` | keyword | Content hash; also the document `_id`, so re-indexing overwrites rather than duplicates. |
| `runbook_id` | keyword | Which runbook this chunk belongs to. Reconciliation scopes by it. |
| `title`, `section` | text / keyword | Where in the runbook the chunk came from. |
| `text` | text | What was embedded, and the BM25 target. |
| `embedding` | dense_vector | The kNN target. Dimension fixed at index creation. |
| `services` | keyword | **Retrieval pre-filter key.** |
| `severity`, `alert_name`, `ordinal` | keyword / integer | Metadata for the reasoner's context bundle. |
| `status` | keyword | `fixture` until the corpus has a human review pass. Passed through to the reasoner. |
| `indexed_at` | date | When this chunk was written. See below. |

### Why `indexed_at` exists — and what it is *not* for

It is **not** staleness detection. Chunk ids are content hashes and superseded
chunks are deleted, so a chunk in the index necessarily matches the current file;
there is no stale state for it to detect.

It exists to make incremental indexing **observable in production**: after a run
that edited one section, exactly one document carries a newer timestamp, so the
behaviour can be seen in Kibana rather than inferred from logs and tests. It is
also a forensic anchor if reconciliation ever misbehaves ("which chunks did the
last run not touch?").

It is stamped **per run, not per runbook**: every chunk a run writes carries one
identical value, whichever file it came from. That distinction is the whole
justification for the field. Per runbook it would only restate
`runbook_documents.indexed_at`, which Postgres already answers; per run it
answers what Postgres cannot — "which chunks did run N write" — as one term
query instead of a range reconstructed from the manifest. A test indexes two
runbooks in a single run and pins the stamp to one distinct value.

The field is deliberately **not**
part of `chunk_id` — if it were, every run would write new documents instead of
overwriting, duplicating the corpus and defeating incremental indexing. A test
pins that.

## Chunking

One chunk per `##` (H2) section, with the document title prepended as a
breadcrumb so a chunk retrieved alone still says what it belongs to. **No
overlap** — see `chunking.py` for why overlap would corrode incremental
indexing.

Chunk id is `sha256(runbook_id, section, text)`. Its stability is what makes
incremental indexing work: unchanged content must hash identically across runs,
or every run would re-embed the whole corpus.

`###` splitting for oversized sections is documented in the corpus README as the
designated boundary but is deliberately unimplemented — the corpus has no `###`
headings and its largest chunk uses about 5% of the embedding model's input
budget. The indexer asserts each chunk fits the budget and fails loudly instead.
