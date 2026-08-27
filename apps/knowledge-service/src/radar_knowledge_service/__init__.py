"""RADAR knowledge service: retrieval grounding for the reasoner.

Turns the runbook corpus in ``docs/runbooks/`` into a searchable index, and
answers the reasoner's request for context about an incident with the runbook
sections most relevant to it, so the RCA is grounded in what the on-call
documentation actually says about that alert.

Two hard rules shape the design:

- **Only the gateway talks to models.** Embeddings and CRAG grading both go
  through ``llm-gateway``. This service holds no provider API key and imports no
  provider SDK, so swapping the embedding model is gateway config, with one
  caveat: the vector dimension is baked into the Elasticsearch ``dense_vector``
  mapping, so a model with different dimensions means an index migration and a
  full re-index.
- **Retrieval is index-side.** BM25 and kNN both run in Elasticsearch, and only
  the top-k comes back. The corpus is never loaded into memory to be scored in
  Python.

Indexing is incremental and content-addressed: chunk ids are content hashes, so
a re-run re-embeds only the chunks whose content actually changed.
"""
