<p align="center">
  <img src="assets/wiki-memory-hero.svg" alt="Wiki Memory — Your knowledge. Structured, sourced, and yours." width="100%">
</p>

<p align="center">
  <a href="https://github.com/ncleton-petitmaker/wiki-memory/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/ncleton-petitmaker/wiki-memory/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/ncleton-petitmaker/wiki-memory/releases"><img alt="Release" src="https://img.shields.io/github/v/release/ncleton-petitmaker/wiki-memory?display_name=tag&sort=semver"></a>
  <a href="LICENSE"><img alt="MIT License" src="https://img.shields.io/badge/license-MIT-7cf7c2"></a>
  <a href="https://www.python.org/"><img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-76a9ff"></a>
  <img alt="macOS, Linux, Windows" src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-c4b5fd">
</p>

<p align="center">
  <strong>A memory you can trust because it keeps the receipts.</strong><br>
  Local-first for one person. Governed, auditable, and self-hosted for a team.
</p>

<p align="center">
  <a href="#solo-quick-start"><strong>Get started</strong></a> ·
  <a href="#plugins-and-sources">Plugins</a> ·
  <a href="#optional-team-deployment">Teams</a> ·
  <a href="docs/ARCHITECTURE.md">Architecture</a> ·
  <a href="README.fr.md">Français</a>
</p>

---

## Your knowledge. Structured, sourced, and yours.

Wiki Memory is an MIT, local-first, self-hosted memory engine with a capability-based plugin architecture. Start offline with no account or server. Add the optional `team` bundle only when you need shared spaces, OIDC identities, ACLs, review, audit, and replication.

Every answer can retain its original evidence, dates, author, and exact extractor version. The canonical record is append-only; Markdown, search, summaries, and future graph views are rebuildable projections.

| Built for daily use | Built for trust |
| --- | --- |
| Capture a note, a file, an audio recording, a social save, or a database record through the same connector contract. | No plugin writes directly into your derived knowledge. Evidence is content-addressed and durable before its event commits. |
| Keep everything private by default, readable in Markdown and usable offline. | Team sharing is explicit. ACLs are applied before search, contradictions go to review, and no semantic merge happens silently. |

Current version: `1.0.0-alpha.24`. The executable V1 foundation is present; stable `1.0.0` remains gated on production recovery evidence and an external audit. The exact evidence status is recorded in [Release evidence](docs/RELEASE_EVIDENCE.md).

## Canonical model

```text
source → durable SHA-256 blob → append-only SQLite event → projections
                                                        ├─ Markdown / Obsidian
                                                        ├─ QMD search
                                                        └─ facts and summaries
```

Original evidence and the event ledger are authoritative. Markdown is readable and editable, but rebuildable. Manual edits are hash-detected, preserved as evidence, and turned into review proposals; the projector never silently overwrites them.

Events retain actor, event and learning time, stream version, idempotency key, exact plugin version, evidence references, scope, and ACL. Blobs are flushed before referenced events commit. Reusing an idempotency key with different semantic content is rejected.

## Solo quick start

```bash
python -m pip install -e .
wiki-memory init ./Memory --spec ./onboarding.json
wiki-memory profile-doctor ./Memory --profile solo
wiki-memory capture ./Memory --vault knowledge --text "Sourced decision"
wiki-memory query ./Memory "what was decided?"
wiki-memory verify ./Memory
```

Solo includes Markdown, QMD, Docling, social capture, MCP, immutable Syncthing packs, and verified local backup. The local HTTP API only binds to loopback and uses the OS keychain, with an out-of-memory-root `0600` fallback.

## Plugins and sources

Plugins declare capabilities, dependencies, runtime, permissions, secrets, data classes, config schema, health check, and stop timeout. Their visible lifecycle is `DISCOVERED → PENDING → STARTING → ACTIVE → DRAINING → STOPPED`, with `FAILED` and `QUARANTINED` branches. Partial startup effects unwind in reverse order. Untrusted Python plugins require explicit developer mode; isolated executable/OCI runtimes are never loaded into Core.

Official V1 connectors cover generic files/URLs/text, browser-assisted social sources, MP3/M4A/WAV audio with local whisper.cpp or explicitly enabled Mistral, read-only PostgreSQL with mandatory schema/table/column allowlists, Docling, and immutable Syncthing transport. Plaud exports use the generic audio path and retain Plaud as provenance; there is no Plaud API dependency.

Every `SourceConnector` is operated through the same core commands rather than
needing a connector-specific change in Core:

```bash
wiki-memory connector-check ./Memory --plugin source-social-browser --config social.json
wiki-memory connector-discover ./Memory --plugin source-social-browser --config social.json
wiki-memory connector-sync ./Memory --plugin source-social-browser --config social.json \
  --selection social-selection.json --vault knowledge --instance browser-workstation
```

An explicit third-party manifest can use the same flow; an in-process Python
plugin requires `--developer-mode` in solo, while executable/OCI connectors
stay capability-isolated. See [Plugin SDK](docs/PLUGIN_SDK.md).

See [Plugin SDK](docs/PLUGIN_SDK.md), then run:

```bash
python scripts/schema_validate.py
python scripts/plugin_conformance.py
```

## Optional Team deployment

Team includes FastAPI, PostgreSQL, S3/MinIO, OIDC, RBAC+ACL, risk-based review, audit, authorization-before-search, an offline outbox, a transactional worker, and `/console`.

```bash
cd deploy/team
# Copy .env.example, replace every placeholder, then pin WIKI_MEMORY_IMAGE
# to the signed release digest.
docker compose up -d
```

Compose binds API traffic to `127.0.0.1:8787`; put TLS termination in front. A Helm chart is available under `deploy/helm/wiki-memory`. Read [Team self-hosting](docs/TEAM_SELF_HOSTING.md) before production: OIDC, PostgreSQL PITR, object versioning, and tested restores are mandatory.

“Keep this” stays private. Publishing requires an exact preview hash. Team sources become evidence while extracted knowledge remains a proposal. Stale writes create conflict proposals; there is no silent semantic merge.

## APIs, backup, and synchronization

HTTP and MCP expose capture, search, evidence, proposals, explicit publication, and curator review. Useful commands:

```bash
wiki-memory mcp-serve ./Memory
wiki-memory serve ./Memory
wiki-memory team-sync ./Memory --server https://memory.example
wiki-memory backup ./Memory ./backup.tar.gz
wiki-memory rebuild ./Memory
wiki-memory event-pack-export ./Memory
```

Syncthing receives only immutable packs and content-addressed blobs, never live SQLite. Backups use SQLite’s online backup API, per-file hashes, safe extraction, integrity checks, and event-count verification.

## Honest alpha limits

- Team search currently uses PostgreSQL full-text search, not server-side vectors.
- Offline Team-cache search is authorization-first lexical search; QMD embeddings are deliberately private-vault-only.
- Executable and OCI plugins run through a capability-scoped NDJSON host. Executable processes are isolated from Core but not a host-level sandbox; Team should require signed OCI plugins for hostile connectors.
- Plugin-owned forward migrations are durable and replay-safe for in-process, executable, and OCI plugins; a healthy replacement is staged, swaps its full capability set atomically, then drains/restarts dependents.
- The Debezium envelope adapter is included; Kafka Connect operations remain external.
- The Helm chart’s internal PostgreSQL/MinIO are evaluation defaults; production HA, PITR, and automated restore testing must be supplied by the operator.
- The 500-member/100-connector rehearsal and synthetic Team WAL-recovery rehearsal are implemented and exercised. Production PITR plus versioned object-store recovery on the operator's actual infrastructure, and an external authorization/isolation audit, remain stable-release gates. Solo acknowledged-event durability has a reproducible `kill -9` campaign, but it must still be run on each supported filesystem before stable release.
- No canonical graph or semantic CRDT is included.

Run the full local checks with:

```bash
python -m unittest discover -s tests -v
python -m compileall -q src scripts tests
python scripts/schema_validate.py
python scripts/plugin_conformance.py
python scripts/privacy_scan.py .
python -m pip_audit --skip-editable
```

GitHub contains code, specs, CI, signed releases, and synthetic fixtures only—never user memory. Documentation: [Architecture](docs/ARCHITECTURE.md), [Plugin SDK](docs/PLUGIN_SDK.md), [Team](docs/TEAM_SELF_HOSTING.md), [Reliability](docs/RELIABILITY.md), [Release verification](docs/VERIFY_RELEASE.md), [Release evidence](docs/RELEASE_EVIDENCE.md), [CLI](docs/CLI_REFERENCE.md), and [Security](SECURITY.md).

License: [MIT](LICENSE). [Version française](README.fr.md).
