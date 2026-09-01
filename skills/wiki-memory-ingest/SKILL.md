---
name: wiki-memory-ingest
description: Convert and integrate files, folders, URLs, PDFs, office documents, emails, images, audio, or video into source-grounded, time-aware Wiki facts and syntheses. Use when the user wants material understood, classified, linked, or synthesized rather than merely saved.
---

# Wiki Memory ingestion

Read [the architecture contract](../../references/architecture.md) and [source invariants](../../references/source-schema.md). Use `$wiki-memory-router` before writing.

1. Run `wiki-memory ingest` so Docling preserves the original and creates structured Markdown.
2. Read the normalized source note. Extract only claims supported by that source.
3. Update or create focused notes in the vault's logical Wiki folder. A fact note's body is immutable once recorded; a later correction becomes a new note. Mark interpretation as `inference`, uncertainty as `open_question`, and unsupported material as `unverified`.
4. Link every Wiki claim back to the source item using a relative wikilink.
5. Add optional temporal frontmatter to every new Wiki fact or synthesis:
   - set `recorded_at` automatically to the current UTC timestamp;
   - use a date explicitly attached to the fact when the source provides one, otherwise inherit the source's publication, meeting, or sent date as `valid_from`;
   - never use `captured_at`, filesystem modification time, or the current time as a guessed `valid_from`;
   - leave `valid_from` null when the source has no date and add a linked temporal question to the vault's gaps report;
   - initialize `valid_until`, `invalidated_at`, `supersedes`, and `superseded_by` as null unless a confirmed replacement supplies them.
6. When a new fact contradicts an existing fact, do not rewrite either body. Let the lint workflow propose the temporal order and ask the user before applying an ambiguous replacement.
7. Update relevant outputs, index, journal, gaps, or contradictions without rewriting the source.
8. Run `wiki-memory index` and `wiki-memory lint`.

For a folder, process supported files individually and report failures without discarding successful captures. Do not ingest credentials, browser profiles, caches, or ignored paths.
