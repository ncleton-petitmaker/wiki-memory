---
name: wiki-memory-lint
description: Audit a Wiki Memory for invalid metadata, missing originals, broken wikilinks, orphan sources, temporal contradictions, and broken supersession history. Use after ingestion, before sharing, or when the memory behaves inconsistently.
---

# Wiki Memory lint

Run `wiki-memory lint <root>`. Explain errors separately from warnings:

- errors mean a required invariant is broken and should block a clean handoff;
- warnings identify broken links or sources that may legitimately be unlinked but need review.

Also inspect focused Wiki facts and syntheses for semantic contradictions. Semantic detection belongs to this skill; deterministic ordering belongs to the CLI. For each contradictory pair, run:

```text
wiki-memory lint <root> --contradiction <FACT_A> <FACT_B>
```

Present `resolution_proposals` to the user. A `ready` proposal means both world dates are comparable: the fact with the later `valid_from` is current, while the older fact would receive `valid_until`, `invalidated_at`, and `superseded_by`; the newer fact would receive `supersedes`. An `ambiguous` proposal means dates are missing, equal, invalid, or otherwise insufficient. Never choose an order or edit files in that case without the user's answer.

Lint is always read-only. Repair only when the user asked for fixes or explicitly accepted a proposal. Preserve fact bodies, immutable sources, and archived revisions; an accepted replacement may only create the new fact and complete lifecycle frontmatter on the old one. For structural interpretation, read [the architecture contract](../../references/architecture.md) and [source invariants](../../references/source-schema.md).
