---
name: wiki-memory-capture
description: Preserve a file, URL, pasted text, web page, image, audio, video, email export, or bookmark as an immutable Wiki Memory source. Use for quick capture when semantic document conversion is unnecessary or will happen later.
---

# Wiki Memory capture

Use `$wiki-memory-router` first unless the destination vault is explicit and unambiguous. Read [source invariants](../../references/source-schema.md).

Run `wiki-memory capture` with exactly one of `--file`, `--url`, or `--text`. For a URL captured through a browser, pass the visible extracted content with `--content`; never store browser credentials or hidden session state.

Preserve the original, provenance, and media. Report whether the result was captured, revised, or duplicate. Do not create Wiki claims during capture unless the user also asks for ingestion or synthesis.

Preserve an explicit publication, meeting, or sent date as source metadata when it is available. Never substitute capture time or filesystem time for a missing source date. The command may return temporal defaults for a fact extracted immediately afterward; treat a missing `valid_from` as an open temporal question, not permission to guess.
