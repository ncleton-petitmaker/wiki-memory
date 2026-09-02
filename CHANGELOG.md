# Changelog

All notable user-facing changes are documented here. Wiki Memory follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.0.0-alpha.4] - 2026-09-02

- Stop publishing untraceable benchmark numbers as release evidence.
- Require a reviewable full-scale performance evidence bundle before a stable
  release, alongside production recovery and independent security review.
- Mark every pre-release explicitly in GitHub Release, including future alpha,
  beta, and release-candidate tags.

## [1.0.0-alpha.3] - 2026-09-02

- Fix release and CI Helm assertions to use POSIX `grep`, available on every
  hosted runner, instead of an undeclared `rg` dependency.
- Quote the PostgreSQL service port mappings so the workflow files are valid
  portable YAML as well as GitHub Actions configuration.
- Run Team-server configuration tests only where the optional `server` bundle
  is installed; the Team integration job continues to execute them with the
  real dependency set.
- Make the official plugin catalogue byte-stable across GitHub runners and
  preserve isolated connector execution on Windows without inheriting secrets.
- Keep content-addressed evidence and search-result path identifiers portable
  across Windows, macOS, and Linux; Team entitlement filtering is therefore
  tested against the same public path contract everywhere.
- Publish concurrent local evidence with atomic create-only semantics, while
  retaining verified repair of a corrupted canonical blob on Windows.
- Audit the frozen project dependency graph rather than mutable runner tooling.

## [1.0.0-alpha.2] - 2026-09-02

### Added

- Team search can be atomically rebuilt from canonical PostgreSQL events, with an admin operation and audit entry.
- `team_restore_verify.py` validates restored Team event hashes, contiguous streams, evidence objects, and the rebuilt search projection before recording an aggregate rehearsal attestation.
- `wiki-memory team-preflight` fails closed when its verifiable Team readiness gates are absent: database/object-store reachability, bucket versioning, OIDC, the dedicated restore-attestation channel, or a successful rehearsal.
- Stable release tags now require protected-environment links to the deployment recovery rehearsal and independent security audit; prerelease tags cannot claim those external gates.
- Synthetic local performance harness with explicit 100,000-document release-gate mode.
- Generic `connector-check`, `connector-discover`, and `connector-sync` commands for every `SourceConnector`, including explicitly opted-in third-party manifests in solo mode.
- Isolated executable/OCI `source.*` capabilities now receive the normal source contract through bounded RPC batches and durable checkpoint handoff.
- Team external plugins now require both an Ed25519 manifest signature and a separately configured administrator allowlist.

### Changed

- Team readiness now fails closed when PostgreSQL or object storage is unavailable.
- An organization publication begins a separately replayable public stream after curation, causally linked to its inaccessible Team proposal; global organization replication never stalls on a filtered predecessor.
- Restore attestations now require both an admin identity and a distinct restoration-attestation secret.
- Simultaneous first captures now serialize SQLite WAL negotiation instead of failing with a transient database lock.
- Local and Team APIs verify content-addressed evidence before serving it; corrupted object-store copies are atomically repaired when a verified replacement is uploaded.

## [1.0.0-alpha.1] - 2026-09-02

### Added

- Canonical SQLite event ledger, UUIDv7 streams, semantic idempotency, durable outbox and checkpoints.
- Content-addressed evidence with verified atomic writes and complete projection rebuild.
- Capability plugin loader, lifecycle/cleanup, profiles, schemas, conformance kit, and quarantine policy.
- Solo HTTP/MCP gateways, verified backup/restore, immutable event-pack transport, and reviewed Markdown edits.
- Optional Team API/client/worker with OIDC, ACLs, review, audit, offline replication, PostgreSQL/S3, Compose, Helm, metrics, and OTLP.
- Generic audio plus Mistral/whisper.cpp providers and PostgreSQL snapshot/CDC contracts.

### Changed

- Markdown is now a rebuildable projection; evidence and events are authoritative.
- Syncthing transports immutable blobs and packs instead of a live memory database.

- Optional bi-temporal frontmatter for Wiki facts and syntheses, with preserved supersession history.
- Current, world-time, and system-time query views with explicit stale-fact reporting.
- Read-only temporal maintenance and contradiction-resolution proposals.
- A plain-language onboarding graph explaining sources, verifiability, and fact replacement.

## [0.0.0] - 2026-09-01

### Added

- Initial Wiki Memory baseline.
- Direct French onboarding without a technical prompt to copy.
- Adaptive memory organization based on the user's own plan or available ChatGPT context.
- Durable sibling `Agent/` and `Mémoire/` installation folders.
- Optional Syncthing synchronization configured separately for both folders.
- Source-grounded Markdown vaults, local search, document ingestion, social capture, quality checks, and privacy safeguards.

[Unreleased]: https://github.com/ncleton-petitmaker/wiki-memory/compare/v1.0.0-alpha.4...HEAD
[1.0.0-alpha.4]: https://github.com/ncleton-petitmaker/wiki-memory/releases/tag/v1.0.0-alpha.4
[1.0.0-alpha.3]: https://github.com/ncleton-petitmaker/wiki-memory/releases/tag/v1.0.0-alpha.3
[1.0.0-alpha.2]: https://github.com/ncleton-petitmaker/wiki-memory/releases/tag/v1.0.0-alpha.2
[1.0.0-alpha.1]: https://github.com/ncleton-petitmaker/wiki-memory/releases/tag/v1.0.0-alpha.1
[0.0.0]: https://github.com/ncleton-petitmaker/wiki-memory/releases/tag/v0.0.0
