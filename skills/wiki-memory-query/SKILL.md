---
name: wiki-memory-query
description: Search one or more Wiki Memory vaults and answer with traceable citations. Use when the user asks what their memory contains, requests a synthesis, or needs relevant notes and sources retrieved.
---

# Wiki Memory query

Read [the architecture contract](../../references/architecture.md). Run `wiki-memory query <root> <question>` to use QMD's local hybrid search. If QMD is unavailable, the command returns a text-search fallback and identifies that limitation.

Open the highest-value retrieved notes and their linked sources before answering. Distinguish documented facts, inferences, open questions, and unverified claims. Cite relative Markdown paths or wikilinks next to each material claim.

Do not answer from general knowledge as though it came from the memory. Say when the memory is silent, conflicting, or insufficient. Search across vaults only when their confidentiality and audience boundaries permit the current request.
