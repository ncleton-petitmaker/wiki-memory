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

## Any source plugin

Every source uses the same check, discovery, and durable-ingestion interface;
Core does not need a bespoke command for a new connector.

```bash
wiki-memory connector-check ROOT --plugin source-social-browser --config social-plugin.json
wiki-memory connector-discover ROOT --plugin source-social-browser --config social-plugin.json
wiki-memory connector-sync ROOT --plugin source-social-browser --config social-plugin.json \
  --selection social-selection.json --vault knowledge --instance browser-workstation
```

`--selection` is an object with a `streams` object. The runtime writes evidence
before events, advances checkpoints only after durable commits, and handles
replay through source identity/version idempotency.

An optional bundled plugin can be activated with `--manifest PATH/plugin.yaml`.
A third-party Python manifest additionally requires explicit `--developer-mode`
in the `solo` profile. Secrets never go into its configuration file: pass each
named, manifest-declared secret through `--secret-env SECRET_NAME=ENV_VAR`.
Executable and OCI plugins remain isolated. The Team profiles retain their
signature/administrator trust policy and do not accept developer mode as an
authorization bypass. When selecting `--profile team-client`, pass its
`serverUrl` through `--profile-config` (an object keyed by plugin ID) and its
token through `--secret-env TEAM_ACCESS_TOKEN=ENV_VAR`. Shared automated
ingestion uses an OIDC `service` identity whose subject is exactly `--instance`;
human users contribute through capture/proposal flows, not a claimed connector
actor.

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

Configures QMD collections for the current, searchable directories of each private vault, updates the local index, and optionally refreshes embeddings. Raw evidence, revision history, assets, and Team-managed vaults are excluded. Authorized Team cache search uses the local lexical path so ACL checks happen before content retrieval.

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

`ROOT` must be the installation's `Mémoire/` directory beside `Agent/`. The command requires `sync_enabled: true`, registers `Agent/` and the immutable transport directory `Mémoire/.wiki-memory/data/` as separate Syncthing folders, and excludes the live SQLite ledger plus local outbox. Blobs and event packs cross devices; each device imports and rebuilds locally. Synchronization is not backup.

## Canonical ledger and projections

```bash
wiki-memory verify ROOT
wiki-memory events ROOT --cursor 0 --limit 100
wiki-memory rebuild ROOT
wiki-memory markdown-edits ROOT --actor local-owner
wiki-memory markdown-edit-review ROOT EVENT_ID accept
wiki-memory markdown-edit-review ROOT EVENT_ID reject --reason "reason"
```

`verify` checks event/stream hashes, SQLite integrity, evidence hashes, and projection failures. Normal rebuild refuses unreviewed Markdown modifications. `markdown-edits` preserves each modified file as evidence and emits a proposal. Shared proposals must be reviewed by Team. `rebuild --force` discards unreviewed projection edits and must be an explicit recovery decision.

## Backup and immutable event packs

```bash
wiki-memory backup ROOT BACKUP.tar.gz
wiki-memory backup-verify BACKUP.tar.gz
wiki-memory backup-restore BACKUP.tar.gz EMPTY_TARGET
wiki-memory event-pack-export ROOT --cursor 0
wiki-memory event-pack-import ROOT PACK.json
```

Blobs must arrive before importing a pack that references them. Imports are idempotent and turn stream-version conflicts into visible conflict events.

## Plugin profiles

```bash
wiki-memory profile-doctor ROOT --profile solo
wiki-memory profile-doctor ROOT --profile team-client --config plugins.yaml
```

The config file is an object keyed by plugin ID. Secret values are read from named environment handles and never written to the memory.

## Audio

```bash
wiki-memory audio-ingest ROOT meeting.m4a --vault knowledge --provider local --whisper-model MODEL --no-diarize
wiki-memory audio-ingest ROOT meeting.m4a --vault knowledge --provider mistral
```

The original is preserved first. Providers and concrete model/settings are recorded. `MISTRAL_API_KEY` is required only for the explicit network provider; local whisper.cpp never enables unsupported diarization silently.

## PostgreSQL source

```bash
wiki-memory postgres-check ROOT --config allowlist.yaml
wiki-memory postgres-discover ROOT --config allowlist.yaml
wiki-memory postgres-sync ROOT --config allowlist.yaml --selection selection.yaml --vault knowledge --instance crm-readonly
```

Set `WIKI_MEMORY_POSTGRES_DSN`. Config must explicitly allow schemas, fully qualified tables, and columns. The account check rejects write-capable and dangerous roles.

## Agent gateways and Team

```bash
wiki-memory mcp-serve ROOT
wiki-memory serve ROOT --host 127.0.0.1 --port 8765
wiki-memory team-sync ROOT --server https://memory.example
wiki-memory team-serve --host 127.0.0.1 --port 8787
wiki-memory team-preflight
```

Solo HTTP refuses non-loopback hosts. Team client tokens use `WIKI_MEMORY_TEAM_TOKEN`; its replication cursor is durable unless explicitly overridden with `--cursor`. Before exposing Team, run `team-preflight` in the API environment. It emits only non-secret readiness states and refuses an unversioned object store, missing OIDC or restoration channel, or a deployment with no successful restore rehearsal.

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
