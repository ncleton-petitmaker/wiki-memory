---
name: wiki-memory-router
description: Decide whether new information belongs in an existing Wiki Memory vault or requires a new one. Use before creating a memory, vault, client space, research area, or other classification boundary.
---

# Wiki Memory router

Read [the architecture contract](../../references/architecture.md). Inspect `memory.config.yaml`, `vaults.registry.yaml`, and candidate `vault.yaml` files.

Write the request characteristics to a temporary JSON file and run `wiki-memory recommend-vault <root> --request <file>`. Treat its ranking as evidence, not automatic authorization.

- Reuse the clear top vault when purpose, audience, lifecycle, and confidentiality fit.
- Ask the user when the result is `ask`, when content crosses confidentiality boundaries, or when the request is ambiguous.
- Propose a new vault when the result is `new_vault` and a separate purpose, audience, lifecycle, or confidentiality boundary exists.
- Prefer taxonomy changes over a new vault for a topic difference alone.
- If client isolation is enabled, each client is a separate vault.

After confirmation, create a spec matching `schemas/vault.schema.json`, run `wiki-memory create-vault`, and record a short routing rationale.
