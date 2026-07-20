# knowledge-service

Indexes the runbook corpus and serves grounding context to the reasoner.

The corpus lives in [`docs/runbooks/`](../../docs/runbooks/); its frontmatter and
chunking contract are documented in that directory's README, and the join
between runbook frontmatter and the Prometheus alert rules is enforced by
`tests/test_runbook_alert_contract.py`.

## Design constraints

- **Never calls a model provider directly.** Embeddings (and later reranking and
  CRAG grading) go through `llm-gateway`, which is the only service holding
  provider API keys. This service authenticates with a per-mode gateway token
  from Vault.
- **Retrieval is index-side.** BM25 and kNN both execute in Elasticsearch;
  only top-k crosses the wire. The corpus is never scored in Python.
- **Indexing is incremental.** Chunk ids are content hashes, so a re-run
  re-embeds only what changed. There is no full-rebuild path.
- **Touches only `runbook_documents`** and the Elasticsearch index — never the
  pipeline tables.

## Modules

| module | role |
|---|---|
| `chunking` | Pure functions: runbook markdown → stable, content-addressed chunks. No I/O. |
| `reconciliation` | Pure functions: what a run must embed, delete, and skip. No I/O. |
| `embeddings` | Gateway client for `/v1/embed`. Never calls a provider directly. |
| `indexer` | The I/O shell: reads the corpus, performs the reconciled work, records the manifest. |

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
