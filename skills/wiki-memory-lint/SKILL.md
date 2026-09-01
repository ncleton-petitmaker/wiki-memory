---
name: wiki-memory-lint
description: Audit a Wiki Memory for invalid source metadata, missing originals, broken wikilinks, orphan sources, and structural drift. Use after ingestion, before sharing, or when the memory behaves inconsistently.
---

# Wiki Memory lint

Run `wiki-memory lint <root>`. Explain errors separately from warnings:

- errors mean a required invariant is broken and should block a clean handoff;
- warnings identify broken links or sources that may legitimately be unlinked but need review.

Repair only when the user asked for fixes. Preserve immutable sources and archived revisions. For structural interpretation, read [the architecture contract](../../references/architecture.md) and [source invariants](../../references/source-schema.md).
