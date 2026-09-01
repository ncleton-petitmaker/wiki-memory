# Wiki Memory architecture contract

Markdown is the source of truth. QMD indexes, embeddings, converted caches, browser state, and logs are reproducible local state and never belong in a vault.

Each installation root contains two sibling directories: `Agent/` for the public plugin and `Mémoire/` for personal knowledge. The memory root is always `Mémoire/`; it contains `memory.config.yaml`, `vaults.registry.yaml`, `AGENTS.md`, and one directory per independent vault. When the user enables Syncthing, both `Agent/` and `Mémoire/` are configured as separate folders and each contains `.stignore` plus `syncthing.ignore.template`. Each vault declares localized folder names in `vault.yaml`; always resolve roles through that mapping.

Logical roles:

- `inbox`: unprocessed captures;
- `sources`: immutable originals, normalized source notes, and revisions;
- `wiki`: living concepts that may be merged, split, or refactored;
- `outputs`: syntheses and deliverables;
- `journal`: chronological activity;
- `meta`: gaps, contradictions, and orphan reports;
- `assets`: media referenced by Markdown.

Never rewrite a source note to make a claim cleaner. Add interpretation to the Wiki layer and link the source. Use `fact`, `inference`, `open_question`, or `unverified` when epistemic status matters.

Vault boundaries are based on purpose, audience, lifecycle, or confidentiality. A topic difference alone is normally a taxonomy concern, not a new vault.
