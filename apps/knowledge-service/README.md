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
