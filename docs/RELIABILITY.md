# Reliability contract and V1 release gates

## Implemented invariants

- SQLite WAL, `synchronous=FULL`, explicit immediate write transaction.
- Immutable event table enforced by database triggers.
- UUIDv7 events, contiguous per-stream versions, semantic idempotency collision detection.
- Evidence write/hash/fsync/atomic rename before event commit.
- Events reject absent evidence; tombstones drive projection deletion.
- Projection checkpoint/version and persistent operational failures.
- LIFO plugin cleanup, visible pending/failed/quarantined states.
- Safe backup extraction, per-file hash manifest, SQLite integrity and event-count verification.
- Team event/job transactional outbox, advisory stream lock, `FOR UPDATE SKIP LOCKED`, retry/dead state.
- ACL filtering on events, search results, and blob reads.
- Download-and-hash evidence before replicated event insertion.
- No silent Markdown overwrite or stale semantic merge.

## Failure injection matrix

Every row must be automated before stable `1.0.0`:

| Boundary | Injection | Required result |
| --- | --- | --- |
| evidence temp | process kill | no event; temp may be collected |
| evidence rename | process kill | orphan blob allowed; no missing referenced blob |
| SQLite before commit | kill/timeout | no acknowledgement; retry safe |
| SQLite after commit | lost response | retry returns same semantic event |
| projection | exception/disk full | event acknowledged; failure visible; checkpoint not advanced |
| connector checkpoint | kill | earlier records replay idempotently |
| Team blob upload | timeout | reupload verified/idempotent |
| Team event append | lost receipt | same idempotency key returns receipt |
| Team stale stream | concurrent write | conflict proposal; no merge |
| Team shared stream | 100 concurrent writes | contiguous versions; every delivery retained |
| worker | kill while locked | transaction rollback/job reclaim |
| backup | corrupt/missing member | verification/restore refuses |
| total projection loss | delete projections | deterministic rebuild |

## Performance gates

These are targets, not alpha claims:

- local text capture p95 < 500 ms;
- local search p95 < 1 s at 100,000 documents;
- Team search p95 < 2 s at 1,000,000 fragments;
- 500 members and 100 active connectors;
- zero acknowledged event loss under randomized `kill -9` campaigns;
- byte-stable relevant projection content after repeated rebuilds.

Load fixtures must be synthetic. Reports must state hardware, OS, PostgreSQL/object-store versions, corpus shape, warm/cold cache, sample count, percentile method, and error rate.

`scripts/load_benchmark.py` is the local synthetic harness. A smaller run is useful only as a smoke test; `--assert-targets` refuses to evaluate the release thresholds unless it has actually processed 100,000 documents. Preserve its JSON output with the release candidate.

```bash
uv run python scripts/load_benchmark.py --documents 100000 --warmup 100 --queries 20 --qmd --assert-targets \
  --root /secure/temporary/wiki-memory-perf/memory \
  --report /secure/temporary/wiki-memory-perf/report.json
```

`scripts/team_load_benchmark.py` uses PostgreSQL’s actual append transaction and ACL-filtered full-text search paths. It feeds independent synthetic source streams concurrently; it never bulk-COPYs rows or bypasses canonical event validation. It similarly refuses to claim the Team release threshold unless it has actually inserted one million synthetic fragments:

```bash
DATABASE_URL='postgresql://…' \
uv run python scripts/team_load_benchmark.py --fragments 1000000 --warmup 1000 --queries 20 --assert-target \
  --report /secure/temporary/wiki-memory-team-perf/report.json
```

The same harness has a separate operational-capacity gate. It drives 100 independent source streams, then proves that 500 distinct group-authorized identities can search concurrently without an ACL failure. It is deliberately separate from the one-million-fragment search measurement so both claims retain their exact corpus and concurrency conditions.

```bash
DATABASE_URL='postgresql://…' \
uv run python scripts/team_load_benchmark.py --fragments 10000 --warmup 100 --queries 10 \
  --workers 100 --members 500 --member-workers 100 --assert-operational-scale \
  --report /secure/temporary/wiki-memory-team-capacity/report.json
```

## Restore gate

A release candidate must restore a solo archive and a Team PITR/object snapshot into clean temporary environments. Verification covers ledger integrity, contiguous stream versions, event hashes, all or statistically justified evidence hashes, projection rebuild, ACL regression corpus, and identical results through CLI/HTTP/MCP for a fixed query set.

No recent successful restore means backup status is failed, regardless of upload or snapshot status.

`scripts/team_restore_verify.py` is the Team-side canonical verifier for a recovered PostgreSQL/object-store pair. It rebuilds Team search from the ledger before checking canonical hashes, contiguous streams, missing evidence, and projection referential integrity. It defaults to all evidence references and can post only aggregate counts to the primary Team API for the restore-age metric. Posting requires an admin identity and a separate restoration-attestation token, so normal administration cannot fabricate a successful rehearsal by itself.

`scripts/team_pitr_rehearsal.py` is a local, fully synthetic WAL-recovery rehearsal: it takes a PostgreSQL base backup, commits an event after that backup, archives WAL, recovers into a fresh cluster, and runs `team_restore_verify.py` against the recovered ledger and object store. It proves the recovery protocol without claiming to configure a managed provider's production PITR or S3 retention policy.

## Security gate

- manifest/config/source schemas pass;
- malicious plugin and undeclared secret tests pass;
- path traversal, archive links, SSRF/egress, oversized uploads, forged actor, invalid OIDC algorithm/signature, and ACL side-channel tests pass;
- SBOM, a hash-locked `pip-audit --strict` vulnerability scan, provenance, checksums, and Cosign signatures publish;
- the Helm chart is rendered in CI with explicit ingress and egress policy values; production still verifies enforcement by its selected CNI;
- external review covers plugin trust/isolation and Team authorization-before-retrieval.

`scripts/crash_campaign.py` is the executable solo crash gate. It starts child writers, force-terminates them at randomized points, and accepts an acknowledgement only after the blob and ledger transaction are durable. One sampled worker is terminated only after it has acknowledged, so even a slow traced runtime proves the post-acknowledgement boundary. Recovery verifies every acknowledged event and blob, then rebuilds Markdown. CI runs a short deterministic campaign; release candidates must retain its JSON output and run the longer documented campaign on each supported filesystem.

```bash
tmpdir="$(mktemp -d)"
uv run python -c 'from pathlib import Path; from wiki_memory.layout import init_memory; import sys; init_memory(Path(sys.argv[1]), {"name":"Crash gate", "language":"en"})' "$tmpdir/memory"
uv run python scripts/crash_campaign.py --root "$tmpdir/memory" --rounds 100 --events-per-worker 20 --delay 0.01 --seed 20260902
```

The current alpha has unit coverage, a reproducible solo crash campaign, real-PostgreSQL concurrent append/search CI, synthetic full-scale local (100,000 documents) plus Team (1,000,000 fragments) harnesses, a 100-connector/500-member operational-capacity rehearsal, and a local synthetic PostgreSQL WAL-recovery rehearsal. Production PITR/object-store restoration and an external authorization/isolation audit remain open. Documentation and release notes must preserve that distinction.
