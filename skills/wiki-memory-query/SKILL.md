---
name: wiki-memory-query
description: Search one or more Wiki Memory vaults and answer current or point-in-time questions with traceable citations. Use when the user asks what their memory contains, what it knew at a date, what was true at a date, or requests a sourced synthesis.
---

# Wiki Memory query

Read [the architecture contract](../../references/architecture.md). Run `wiki-memory query <root> <question>` to use QMD's local hybrid search. If QMD is unavailable, the command returns a text-search fallback and identifies that limitation.

Use current facts by default. For questions about one of the two timelines:

- "que savait la mémoire à cette date ?" means system time; pass `--system-at <ISO_DATE>`;
- "qu'était vrai à cette date ?" means world time; pass `--valid-at <ISO_DATE>`.

The CLI also recognizes these French and English formulations when they contain an ISO or unambiguous numeric date. Ask for clarification when the date itself is ambiguous; never guess a locale or year.

Open the highest-value retrieved notes and their linked sources before answering. Distinguish documented facts, inferences, open questions, and unverified claims. Cite relative Markdown paths or wikilinks next to each material claim.

Treat results marked `evidence_only` as proof to inspect, never as a current fact by themselves. Base the answer's state on included Wiki facts and syntheses; use source notes to verify and cite those facts.

State the temporal viewpoint used. Cite the source of every material fact and briefly identify superseded or expired facts reported under `excluded_stale_facts`; do not silently hide that history. For a world-time query, say when undated facts were excluded because `valid_from` is unknown. For a system-time query, say when legacy facts were excluded because `recorded_at` is unknown.

Do not answer from general knowledge as though it came from the memory. Say when the memory is silent, conflicting, or insufficient. Search across vaults only when their confidentiality and audience boundaries permit the current request.
