---
name: wiki-memory-doctor
description: Diagnose Wiki Memory installation, Docling, QMD, vault layout, Obsidian compatibility, optional Syncthing configuration, and backup readiness. Use during setup, after moving devices, or when capture, search, or enabled synchronization fails.
---

# Wiki Memory doctor

First verify that the installation root contains sibling `Agent/` and `Mémoire/` directories, run the plugin's `scripts/bootstrap.py --check`, then run `wiki-memory doctor <installation-root>/Mémoire` and report blocking errors before warnings. Treat canonical-ledger, evidence-hash, and projection failures as blocking. If Python, Node.js, Obsidian, Docling, or QMD is missing, report the official link and supported install command. Syncthing is required only when `memory.config.yaml` has `sync.enabled: true`; otherwise report it as unused. When enabled, verify separate folder entries for `Agent/` and `Mémoire/.wiki-memory/data/`, confirm that transport excludes `events.sqlite3` and outbox state, and verify arriving packs only after blobs. With user permission, run `scripts/bootstrap.py --yes --open-links` for core dependencies, or add `--with-syncthing` only when synchronization is enabled.

When synchronization is enabled, if `.stignore` differs from `syncthing.ignore.template`, replace it only after confirming the local device's additional ignore rules are preserved. Remind the user that `.stignore` itself is not synchronized and must be verified on every device. Skip these checks when synchronization is disabled.

Treat missing Syncthing versioning or backup as a warning, not proof of data loss. Do not alter Syncthing device or folder settings without an explicit request. Use `wiki-memory privacy-scan` before publishing or sharing the plugin or a template.
