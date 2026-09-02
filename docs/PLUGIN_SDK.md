# Plugin SDK and source connector contract

## Manifest

Every plugin directory contains `plugin.yaml` validated against `schemas/plugin-manifest.schema.json`.

```yaml
apiVersion: wiki-memory/v1
id: source-example
version: 1.0.0
minimumSdkVersion: 1.0.0
runtime: python
entrypoint: package.module:activate
provides: [source.example]
requires: [events, evidence, projection.markdown]
permissions:
  filesystem: [selected-files]
  network: [api.example.test]
  secrets: [EXAMPLE_TOKEN]
  subprocess: false
  dataClasses: [source, memory]
configSchema: config.schema.json
healthCheck: services
stopTimeoutSeconds: 30
```

`configSchema` must resolve inside the plugin catalog and is checked before import. A plugin receives only named secret handles. `healthCheck: services` verifies that every declared provided capability was actually registered.

The bundled `plugin_catalog/catalog.json` lists every official manifest with its SHA-256 and version. Profile startup verifies complete coverage, every hash, and manifest identity before it trusts an official plugin. The release pipeline publishes that catalog as `plugin-catalog.json` plus a Cosign bundle, and includes it in signed release checksums.

## Python activation

```python
def activate(ctx, config):
    ledger = ctx.require("events")
    token = ctx.secret("EXAMPLE_TOKEN")
    connector = ExampleConnector(token, config)
    ctx.provide("source.example", connector)
```

Use `ctx.effect(...)` for every acquired file watcher, thread, socket, subscription, or child process. Cleanup is LIFO. Do not write Markdown, search indexes, or projection checkpoints directly. Preserve evidence through the `evidence` service, then append an event.

Third-party Python runs in process only under explicit solo developer mode. Team catalog entries must be trusted or verified by a configured signature verifier. Privilege-bearing connectors use the isolated host: `runtime: executable` requires an absolute `command` array, and `runtime: oci` requires a digest-pinned `image` plus an optional command. Both speak newline-delimited JSON on standard I/O using `wiki-memory-plugin-host/v1`; startup must answer `ready` with exactly the declared capabilities. Core exposes each capability only as an async `service.call(method, params)` facade, never as a direct event-store, filesystem, or secret object.

OCI runs are read-only, drop Linux capabilities, use a private writable `/runtime`, and disable networking unless the manifest declares it. Executable isolation has the same protocol and sanitized environment, but does not claim to be an OS sandbox; Team should use OCI for hostile connectors.

An isolated `source.*` capability implements `spec`, `check`, `discover`, and
`read` through `call` messages. `read` returns either one final array of source
messages or `{ "messages": [...], "done": false }`, with at most 10,000
messages in one RPC result; the host protocol itself caps each JSON line at
1 MiB. A non-final batch must contain at least one checkpoint. Core consumes
that checkpoint before requesting the next batch with its cursor and refuses
more than 1,000 batches in one run. It wraps this in the normal asynchronous
`SourceConnector` interface, so an OCI or executable source receives the same
evidence-before-event and checkpoint guarantees as an in-process source
without receiving the ledger, filesystem root, or arbitrary secrets.

## Team signature and approval policy

Team does not use solo developer mode. An external Team plugin must be an
isolated `executable` or `oci` manifest, carry an Ed25519 signature, and have
its exact plugin ID approved by an administrator. Add this object to the
manifest after producing the signature over the canonical UTF-8 payload
returned by `wiki_memory.plugin_signatures.signing_payload(manifest)`:

```yaml
signature:
  algorithm: ed25519
  keyId: engineering-release-2026
  value: BASE64_RAW_ED25519_SIGNATURE
```

The operator configures `WIKI_MEMORY_TEAM_PLUGIN_TRUST_KEYS` as a JSON map of
`keyId` to base64 raw public key and
`WIKI_MEMORY_TEAM_APPROVED_PLUGIN_IDS` as a JSON array of exact IDs. An absent,
malformed, unapproved, or invalid signature is quarantined. Private signing
keys are never placed in Wiki Memory, Compose, Helm, or GitHub.

`migrations` is a forward-only chain, run before activation and recorded only after activation plus health checks succeed:

```yaml
migrations:
  - fromVersion: 1.0.0
    toVersion: 1.1.0
    entrypoint: package.migrations:upgrade_1_0_to_1_1
```

The handler receives `(ctx, from_version, to_version)` and must be idempotent: a crash before successful activation intentionally replays it. It may migrate only plugin-owned caches/projections/configuration; the canonical event ledger and evidence are never rewritten. Downgrades and incomplete paths are rejected.

`await manager.upgrade(id, manifest, config, secrets)` starts a candidate in a staging registry, verifies it, drains capability consumers, swaps the complete capability set under one registry lock, cleans up the old provider, then reactivates consumers. A candidate failure leaves the active graph untouched. Live upgrades cannot add or remove capabilities; deploy a new profile for topology changes.

For `executable` and `oci` plugins, Core sends the same forward chain over the isolated protocol before publishing any capability:

```json
{"protocol":"wiki-memory-plugin-host/v1","type":"migrate","id":1,"entrypoint":"plugin.migrations:upgrade","fromVersion":"1.0.0","toVersion":"1.1.0"}
```

The host must answer `migration-result` with the same `id` and `ok: true`. An OCI child may update only its mounted `/runtime` state; either isolated runtime receives no ledger or evidence object to rewrite. An executable process remains process-isolated rather than a host OS sandbox.

## SourceConnector

Implement `spec`, `check`, `discover`, and asynchronous `read`; optionally implement `subscribe` and `fetch`. Emit only:

- `record`: stable `sourceId`, optional `sourceVersion`, payload, real occurrence time, and pre-preserved attachment references;
- `delete`: stable identity plus tombstone version/cursor;
- `checkpoint`: resumable cursor after earlier records;
- `schema-change`: complete observed schema;
- `warning`: degraded but non-corrupting condition.

Be explicit in `ConnectorCapabilities`: backfill, incremental, webhooks/subscriptions, hard deletion, schema changes, and attachments. A snapshot connector that cannot observe deletion must set `hard_deletes=False`; UI and Doctor must not call it fully synchronized.

The ingestion runtime persists records as canonical JSON evidence, derives deterministic stream/idempotency identifiers, appends tombstones, and advances checkpoints only after prior commits. A connector must tolerate replay and overlapping cursor windows.

## Database connector rules

- Dedicated read-only account; `check` rejects superuser, role/database creation, replication, or write grants.
- Explicit schema, table, and per-table column allowlists.
- SQL identifiers composed through the driver, never string interpolation.
- Compound `(updated_at, primary_key)` ordering.
- Configurable overlap window, relying on idempotency for duplicates.
- CDC transport separated from the Debezium envelope adapter and LSN checkpoint.
- No reuse of ingestion credentials for agent-driven arbitrary SQL.

The object passed to a source plugin is also the object passed to its
`check`/`discover` contract. Do not hide the source allowlist inside an
implementation-only wrapper: for example, PostgreSQL uses top-level
`schemas`, `tables`, `columns`, and optional `batchSize`. Secrets are always
separate handles, never fields in this object.

## Tests required for a connector

1. invalid config and absent secret;
2. check/discover without data mutation;
3. initial backfill and empty source;
4. retrying the same record and checkpoint;
5. crash before evidence, between evidence/event, and before checkpoint;
6. update, deletion capability, and schema change;
7. path traversal, SSRF/egress, undeclared secret, oversized payload;
8. lifecycle cleanup after partial startup;
9. synthetic fixtures only.

Run `python scripts/plugin_conformance.py` and `python scripts/schema_validate.py` before publishing.
