---
name: wiki-memory-ingest
description: Convert and integrate files, folders, URLs, PDFs, office documents, emails, images, audio, or video into a source-grounded Wiki Memory. Use when the user wants material understood, classified, linked, or synthesized rather than merely saved.
---

# Wiki Memory ingestion

Read [the architecture contract](../../references/architecture.md) and [source invariants](../../references/source-schema.md). Use `$wiki-memory-router` before writing.

1. Run `wiki-memory ingest` so Docling preserves the original and creates structured Markdown.
2. Read the normalized source note. Extract only claims supported by that source.
3. Update or create focused notes in the vault's logical Wiki folder. Mark interpretation as `inference`, uncertainty as `open_question`, and unsupported material as `unverified`.
4. Link every Wiki claim back to the source item using a relative wikilink.
5. Update relevant outputs, index, journal, gaps, or contradictions without rewriting the source.
6. Run `wiki-memory index` and `wiki-memory lint`.

For a folder, process supported files individually and report failures without discarding successful captures. Do not ingest credentials, browser profiles, caches, or ignored paths.
