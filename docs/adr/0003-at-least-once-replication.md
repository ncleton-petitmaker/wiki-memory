# ADR 0003 — At-least-once replication with semantic idempotency

Status: accepted.

Connectors, offline clients, and file transport inevitably retry after ambiguous failure. Delivery is at least once. Checkpoints advance after prior durable writes; stream versions detect stale state; idempotency keys deduplicate only when semantic fingerprints match.

Consequences: retries are safe, key collisions are errors, blobs upload before events, and Syncthing moves immutable packs/blobs rather than SQLite.
