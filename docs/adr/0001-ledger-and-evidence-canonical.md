# ADR 0001 — Event ledger and evidence are canonical

Status: accepted.

Markdown cannot atomically represent concurrent writes, durable acknowledgements, idempotency, or exact extractor provenance. Wiki Memory therefore treats immutable evidence blobs and the append-only event ledger as authority. Markdown, search, facts, summaries, and future graphs are versioned projections.

Consequences: all writes pass evidence then event; projections can be deleted/rebuilt; users retain readable Markdown; manual edits become sourced events instead of hidden canonical mutations.
