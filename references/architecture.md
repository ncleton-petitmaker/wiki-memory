# Wiki Memory architecture contract

The append-only event ledger and content-addressed original evidence are the source of truth. Markdown is a readable and editable projection. QMD indexes, embeddings, converted caches, browser state, and logs are reproducible local state; credentials and runtime caches never belong in a memory root.

Each installation root contains two sibling directories: `Agent/` for the public plugin and `Mémoire/` for personal knowledge. The memory root is always `Mémoire/`; it contains `memory.config.yaml`, `vaults.registry.yaml`, `AGENTS.md`, and independent vaults. When enabled, Syncthing shares `Agent/` and the immutable transport directory `Mémoire/.wiki-memory/data/`; transport ignore rules exclude SQLite and outbox state. Each vault declares localized folder names in `vault.yaml`; always resolve roles through that mapping.

Logical roles:

- `inbox`: unprocessed captures;
- `sources`: immutable originals, normalized source notes, and revisions;
- `wiki`: living concepts that may be merged, split, or refactored;
- `outputs`: syntheses and deliverables;
- `journal`: chronological activity;
- `meta`: gaps, contradictions, and orphan reports;
- `assets`: media referenced by Markdown.

Never rewrite a source note to make a claim cleaner. Add interpretation to the Wiki layer and link the source. Use `fact`, `inference`, `open_question`, or `unverified` when epistemic status matters.

Wiki facts and syntheses may use optional bi-temporal frontmatter. `valid_from` and `valid_until` describe when a fact is true in the world. `recorded_at` and `invalidated_at` describe when the memory learned and invalidated it. `supersedes` links a replacement to the older fact; `superseded_by` links the older fact back to its replacement. The note body is never rewritten to hide history. Only confirmed lifecycle metadata may be completed.

For a new fact, set `recorded_at` automatically. Prefer a date explicitly attached to the fact; otherwise inherit an explicit publication, meeting, or sent date from its source. Never derive `valid_from` from capture time, filesystem time, or the current clock. Leave it null and record an open temporal question when the source has no date.

Current queries exclude facts whose world or system interval has ended. System-time snapshots filter on `recorded_at <= t < invalidated_at`; world-time snapshots filter on `valid_from <= t < valid_until`. Cite the source and disclose stale, legacy, or undated facts excluded by the chosen view.

Vault boundaries are based on purpose, audience, lifecycle, or confidentiality. A topic difference alone is normally a taxonomy concern, not a new vault.
