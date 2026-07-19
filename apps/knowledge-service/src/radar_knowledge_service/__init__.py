"""RADAR knowledge service: retrieval grounding for the reasoner.

Turns the runbook corpus in ``docs/runbooks/`` into a searchable index, and
answers the reasoner's request for context about an incident with the runbook
sections most relevant to it. Without this, the reasoner analyses an incident
from its metadata and investigation plan alone; with it, the RCA is grounded in
what the on-call documentation actually says about that alert.

Two hard rules shape the design:

- **Only the gateway talks to models.** Embeddings, reranking, and CRAG grading
  all go through ``llm-gateway``. This service holds no provider API key and
  imports no provider SDK, so swapping the embedding model is gateway config,
  not a change here. The one thing that is not free: the vector dimension is
  baked into the Elasticsearch ``dense_vector`` mapping, so a model with
  different dimensions means an index migration and a full re-index.
- **Retrieval is index-side.** BM25 and kNN both run in Elasticsearch, and only
  the top-k comes back. The corpus is never loaded into memory to be scored in
  Python.

Indexing is incremental and content-addressed: chunk ids are content hashes, so
a re-run re-embeds only the chunks whose content actually changed. A full
rebuild is never required and never performed.

Layout:

- ``chunking`` — pure functions: runbook markdown to stable, hashed chunks.
"""
