# CLI reference

Bootstrap installs the `wiki-memory` executable in Wiki Memory's isolated runtime and prints its full path. Examples below assume it is available on `PATH`.

## Global

```bash
wiki-memory --version
wiki-memory --help
```

Configuration accepts YAML. JSON is also accepted because it is valid YAML syntax.

## Initialize a memory

First prepare the required two-folder installation layout:

```bash
wiki-memory prepare-installation INSTALLATION_ROOT --agent-source PLUGIN_DIRECTORY
```

This creates sibling `Agent/` and `Mémoire/` directories, copies only public agent files when needed, and excludes secrets and reproducible local state. Then initialize the memory:

```bash
wiki-memory init INSTALLATION_ROOT/Mémoire --spec ONBOARDING.yaml
```

Creates a new memory from a specification matching `schemas/onboarding.schema.json`. The target must be empty.

## Create a vault

```bash
wiki-memory create-vault ROOT --spec VAULT.yaml
```

Creates and registers an independent vault matching `schemas/vault.schema.json`.

## Recommend a vault

```bash
wiki-memory recommend-vault ROOT --request REQUEST.yaml
```

Ranks registered vaults by purpose, audience, lifecycle, confidentiality, includes, excludes, and keywords. The result is `existing_vault`, `ambiguous`, or `new_vault`.

## Capture

```bash
wiki-memory capture ROOT --vault SLUG --file FILE
wiki-memory capture ROOT --vault SLUG --url URL --content TEXT
wiki-memory capture ROOT --vault SLUG --text TEXT
```

Optional metadata:

```text
--title TITLE
--author AUTHOR
--published-at ISO_DATE
--connector NAME
--source-type TYPE
--status fact|inference|open_question|unverified
--media PATH_OR_URL
--docling
```

`--file`, `--url`, and `--text` are mutually exclusive. `--media` can be repeated.

The JSON result includes `fact_temporal_defaults` for an immediately extracted fact. `recorded_at` is automatic. `valid_from` uses the explicit source date when available and remains null when the source is undated; capture time is never used as a substitute. An undated source also produces an `open_questions` entry for the ingestion workflow to record in the vault's gaps report.

## Ingest with Docling

```bash
wiki-memory ingest ROOT --vault SLUG --file FILE
wiki-memory ingest ROOT --vault SLUG --url URL
```

`ingest` uses Docling conversion. The original and raw capture remain separate from the derived Markdown.

## Import social captures

```bash
wiki-memory social-import ROOT --vault SLUG --input ITEMS.json
```

The input must match `schemas/social-capture.schema.json`. Browser automation creates this temporary normalized file; the deterministic importer validates, deduplicates, and versions it.

## Import Karakeep

```bash
wiki-memory karakeep-import ROOT --vault SLUG --input EXPORT.json
```

Karakeep remains an optional import source, not the source of truth.

## Index

```bash
wiki-memory index ROOT
wiki-memory index ROOT --no-embed
```

Configures one QMD collection per vault, updates the local index, and optionally refreshes embeddings.

## Query

```bash
wiki-memory query ROOT "QUESTION" --limit 10
wiki-memory query ROOT "What did the memory know?" --system-at 2025-06-01
wiki-memory query ROOT "What was true?" --valid-at 2025-06-01
```

Uses QMD when available and returns structured local results. A text-search fallback remains available for basic recovery. Current facts are used by default. `--system-at` filters on when the memory knew a fact; `--valid-at` filters on when the fact was true in the world. French and English questions equivalent to "what did the memory know" or "what was true" also select the matching axis when they contain an ISO or `DD/MM/YYYY` date. The result reports the selected temporal view and lists stale facts excluded from it.

## Lint

```bash
wiki-memory lint ROOT
wiki-memory lint ROOT --contradiction VAULT/02-Wiki/FACT_A.md VAULT/02-Wiki/FACT_B.md
```

Checks source frontmatter, raw-file references, broken and ambiguous wikilinks, orphaned sources, temporal metadata, and supersession chains. `--contradiction` can be repeated. It compares a semantically identified pair and returns a read-only `ready` or `ambiguous` resolution proposal; it never edits either note.

## Temporal maintenance

```bash
wiki-memory maintenance ROOT --older-than-months 6
```

Lists undated facts, current facts whose newest dated source is older than the selected number of calendar months, and broken supersession chains. The command is read-only and never deletes or rewrites a note.

## Doctor

```bash
wiki-memory doctor ROOT
```

Checks dependencies, global config, vault registry, folder roles, optional Syncthing policy, backup/versioning confirmation, and lint status. Missing Syncthing is non-blocking when synchronization is disabled. Errors are blocking; warnings identify risk without claiming corruption.

## Optional Syncthing setup

```bash
wiki-memory syncthing-setup ROOT
wiki-memory syncthing-setup ROOT --device-id DEVICE_ID --device-name NAME
```

`ROOT` must be the installation's `Mémoire/` directory beside `Agent/`. The command requires `sync_enabled: true`, creates ignore files in both folders, registers `Agent/` and `Mémoire/` as separate Syncthing folders, optionally pairs and shares both with another device, verifies both configured paths, and records both folder IDs. It does not treat synchronization as backup.

## Privacy scan

```bash
wiki-memory privacy-scan PATH
```

Scans a repository or template for likely secrets, browser credentials, private keys, and personal absolute paths.

## Bootstrap

Run from the plugin directory:

```bash
python3 scripts/bootstrap.py --check
python3 scripts/bootstrap.py --yes --open-links
python3 scripts/bootstrap.py --yes --with-syncthing --open-links
python3 scripts/bootstrap.py --dry-run
```

On Windows, use `py -3`. `--check` never changes the system. `--yes` authorizes supported package-manager commands for selected applications. Syncthing is selected only with `--with-syncthing`. `--open-links` opens official download pages when automation is unavailable.
