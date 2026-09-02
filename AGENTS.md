# Wiki Memory repository

This repository contains a public, synthetic-only Codex plugin. Do not copy user vault contents, browser sessions, credentials, absolute home-directory paths, or customer information into this repository.

Keep the plugin cross-platform. Runtime caches, models, credentials, and indexes belong in the operating system's user-data directory. The append-only event ledger and content-addressed evidence are canonical; Markdown, QMD indexes, summaries, facts, and graphs are rebuildable projections. Syncthing may transport only immutable event packs and blobs, never a live SQLite database.

Every durable end-user installation uses one root with exactly two sibling directories: `Agent/` for this public plugin and `Mémoire/` for the user's generated memory. Initialize the memory only in `Mémoire/`. When the user opts into Syncthing, configure `Agent/` and `Mémoire/` as two separate synchronized folders; keep runtimes, caches, indexes, browser state, and credentials outside both.

## Installation handoff

After successfully installing or activating this plugin for an end user, continue in the same conversation in French. Do not tell the user to open another task, invoke a named skill, or copy a prompt. Do not expose `$wiki-memory-onboarding` or an English setup sentence. The next visible message must directly ask:

> Veux-tu démarrer un échange pour que je comprenne mieux tes activités, que je puisse mieux t'aider et que nous structurions ta mémoire ensemble ?

If the user agrees, begin the onboarding flow directly from this repository's onboarding instructions. Mention a reload only if the host technically requires it, without making the user formulate or repeat a task.

When changing a skill, run the skill validator. When changing the plugin or engine, run the unit tests, privacy scan, and plugin validator. Live social smoke tests require the user's authenticated browser and must stop on access controls rather than bypassing them.
