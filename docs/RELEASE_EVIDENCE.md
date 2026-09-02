# Release evidence matrix

This document records what the repository itself can prove. It is deliberately
not a marketing checklist: a row is `proven` only when its command exercised
the stated boundary, and a row requiring a production provider or independent
review remains open until that party supplies evidence.

## Reproducible, synthetic gates

| Requirement | Executable evidence | Evidence status for alpha.23 |
| --- | --- | --- |
| Durable solo capture and QMD retrieval at 100,000 documents | `scripts/load_benchmark.py --documents 100000 --qmd --assert-targets --report …` | Passed on 2026-09-02 with QMD 2.8.3: capture p95 16.926 ms and search p95 293.296 ms. Retained report: [`local-100k-qmd-2026-09-02.json`](evidence/local-100k-qmd-2026-09-02.json). |
| Team retrieval at one million fragments | `scripts/team_load_benchmark.py --fragments 1000000 --assert-target --report …` | Passed on 2026-09-02: search p95 3.502 ms; retained report: [`team-1m-2026-09-02.json`](evidence/team-1m-2026-09-02.json). |
| 100 active source streams and 500 authorized members | `scripts/team_load_benchmark.py --workers 100 --members 500 --assert-operational-scale --report …` | Passed on 2026-09-02: 100 streams, 500 members and zero failed authorized searches. |
| Team WAL recovery protocol | `scripts/team_pitr_rehearsal.py` | Exercised by the release validation workflow against synthetic PostgreSQL and verified recovered evidence. |
| Solo crash boundary | `scripts/crash_campaign.py` | Exercised by the release validation workflow; acknowledged writes are verified after recovery. A 100-round APFS run on macOS 26.5.2 also recovered all 98 acknowledged writes, verified their evidence, and rebuilt 102 events: [`solo-crash-apfs-2026-09-02.json`](evidence/solo-crash-apfs-2026-09-02.json). This is APFS evidence only, not a claim about other supported filesystems. |
| Canonical Team ledger and object verification | `scripts/team_restore_verify.py` | Exercised by the synthetic WAL recovery rehearsal. |
| ACL predicate / database prefilter equivalence | `tests/test_team_postgres.py::PostgresTeamRepositoryTests.test_sql_acl_prefilter_matches_reference_policy` | Exercised in the Team PostgreSQL integration job. |
| Content-addressed evidence cannot widen ACL | `tests/test_team_postgres.py::TeamPostgresTests.test_team_api_enforces_review_and_authorized_search` | Exercised in the Team PostgreSQL integration job. |
| Organization promotion remains non-public until review | `tests/test_team_postgres.py::TeamPostgresTests.test_team_api_enforces_review_and_authorized_search` | Exercised in the Team PostgreSQL integration job. |
| Private-to-Team organization workflow | `tests/test_team_postgres.py::TeamPostgresTests.test_team_client_sync_stages_organization_publication_until_review` | Exercised in the Team PostgreSQL integration job. |
| Configured Team readiness | `wiki-memory team-preflight` | Exercised by its non-secret configuration test; an operator still supplies deployment evidence. |
| Compose reference topology | GitHub Actions `compose-smoke` | The CI job builds the Team image, boots API, worker, PostgreSQL, MinIO and bucket initialization, waits for health, then calls `/v1/health`. The release repeats this against the exact pushed image digest before publication. |

All fixtures for these commands are synthetic. The supplied benchmark tools
refuse to claim a full-scale target from a smaller corpus.
The retained report bytes are bound by the
[`evidence/SHA256SUMS`](evidence/SHA256SUMS) manifest; CI runs
`scripts/verify_evidence.py` to reject missing, altered, unlisted, invalid,
or non-successful reports. Crash reports additionally require their
acknowledgement, evidence, and projection-rebuild outcome fields.

## Current gate status

- `proven by alpha.23 CI`: canonical append-only ledger, content-addressed
  evidence, projections/rebuild, plugin contracts, isolated Team paths,
  ACL-before-search, crash recovery, and a synthetic WAL-recovery rehearsal.
- `proven by retained synthetic reports`: the 100,000-document local target,
  Team one-million-fragment target, and 100-connector/500-member capacity.
- `must be evidenced per deployment`: managed PostgreSQL PITR retention,
  versioned S3/MinIO recovery point, restore rehearsal against that exact pair,
  alert delivery for restore age, and enforcement of the declared egress
  NetworkPolicy by the selected CNI.
- `requires an independent party`: external review of plugin trust/isolation and
  Team authorization boundaries, using the
  [external review package](EXTERNAL_SECURITY_REVIEW.md).

The last two bullets are release requirements for a stable `1.0.0`; they are
not silently satisfied by local test output. See [RELIABILITY.md](RELIABILITY.md)
and [TEAM_SELF_HOSTING.md](TEAM_SELF_HOSTING.md) for the operator procedures.

## Stable-tag control

The release workflow treats a semantic pre-release such as `v1.0.0-alpha.23`
as an alpha. A stable tag such as `v1.0.0` first enters the GitHub
`stable-release` environment. Configure that environment with required
reviewers and its three variables, each pointing to a reviewable HTTPS record:

- `WIKI_MEMORY_PERFORMANCE_EVIDENCE`: immutable report bundle from the exact
  release commit containing successful local 100k, Team 1M, and operational
  capacity harness outputs;

- `WIKI_MEMORY_PRODUCTION_RECOVERY_EVIDENCE`: the exact PostgreSQL PITR plus
  versioned-object recovery rehearsal for that deployment;
- `WIKI_MEMORY_EXTERNAL_AUDIT_EVIDENCE`: the independent plugin/authorization
  audit report.

`scripts/release_gate.py` rejects the stable release before tests, artifacts,
or publication when any record is absent or malformed. This is an explicit
human control, not a substitute for the underlying evidence.
