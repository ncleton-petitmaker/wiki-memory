# Open-source design decisions

Wiki Memory keeps plain Markdown as the durable source of truth and uses replaceable local tools around it.

## Adopted

- [Docling](https://github.com/docling-project/docling), MIT: required conversion layer for PDF, office documents, HTML, email, images, audio, and video. Wiki Memory preserves the original and uses Docling only for the derived Markdown.
- [QMD](https://github.com/tobi/qmd), MIT: required local BM25, vector, and reranked search. Its SQLite index and GGUF models stay outside synchronized vaults and can be rebuilt.

## Ideas incorporated without dependencies

- [Palinode](https://github.com/phasespace-labs/palinode): explicit epistemic states for facts, inferences, open questions, and unverified claims.
- [Obsidian Brain Vault](https://github.com/markfive-proto/obsidian-brain-vault): capture, ingest, compile, and query workflow.

## Interoperability

- [Karakeep](https://github.com/karakeep-app/karakeep): optional JSON import. Karakeep is never required or treated as the source of truth.
- [Claudian](https://github.com/YishenTu/claudian): optional Obsidian interface for users who want an agent inside the desktop vault.

## Deliberately not core

- Basic Memory provides a broader AGPL-licensed Markdown/MCP system. Wiki Memory does not embed it because doing so would add another opinionated runtime and licensing boundary around the same source-of-truth layer.
- Social scraping services and stored Playwright profiles are excluded. Social collection uses the user's controlled Codex browser session and fails closed on access controls.
