# DocMind development notes

DocMind is being reduced to one authenticated search path over the registered
source, folder, or document scope. The active retrieval contract is:

```text
BM25 128 + dense 128 -> RRF(k=60) -> at most 8 chunks per document
-> at most 128 chunks -> first 2,400 characters per chunk
-> Jina reranker v3.5 -> top 5
```

The previous semantic folder-routing, generated routing-card, catalog publish,
and shadow-comparison pipeline has been removed. Folder selection is explicit
in the request and both retrieval lanes receive the same authorization and
active-version filter before candidate generation.

For Windows/WSL2 development, use the isolated instructions in
`../docs/WINDOWS-DEVELOPMENT-DOCKER.md`. Do not start the old deployment
Compose files or mount the three encrypted source roots into Linux containers.

The current parser baseline still requires its empty development object store.
That does not authorize permanent plaintext retention for the future cloud
source ingestion path. Follow the temporary-file and cleanup contract in the
repository-root `PRD.md`.
