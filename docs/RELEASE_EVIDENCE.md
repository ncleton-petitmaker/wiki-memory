# Release evidence matrix

This document records what the repository itself can prove. It is deliberately
not a marketing checklist: a row is `proven` only when its command exercised
the stated boundary, and a row requiring a production provider or independent
review remains open until that party supplies evidence.

## Reproducible, synthetic gates

| Requirement | Executable evidence | Evidence status for alpha.6 |
| --- | --- | --- |
| Durable solo capture and QMD retrieval at 100,000 documents | `scripts/load_benchmark.py --documents 100000 --qmd --assert-targets --report …` | Passed on 2026-09-02 with QMD 2.8.3: capture p95 16.926 ms and search p95 293.296 ms. The exact report is [`local-100k-qmd-2026-09-02.json`](evidence/local-100k-qmd-2026-09-02.json), SHA-256 `517e691e0b5c7b2e99a47bbeb17531ed362027d50edb8e3f3051049dbb09833e`. |
| Team retrieval at one million fragments | `scripts/team_load_benchmark.py --fragments 1000000 --assert-target --report …` | Harness present; no immutable full-scale report is attached to alpha.6, so no measured latency is claimed. |
| 100 active source streams and 500 authorized members | `scripts/team_load_benchmark.py --workers 100 --members 500 --assert-operational-scale --report …` | Harness present; no immutable capacity report is attached to alpha.6. |
| Team WAL recovery protocol | `scripts/team_pitr_rehearsal.py` | Exercised by the release validation workflow against synthetic PostgreSQL and verified recovered evidence. |
| Solo crash boundary | `scripts/crash_campaign.py` | Exercised by the release validation workflow; acknowledged writes are verified after recovery. |
| Canonical Team ledger and object verification | `scripts/team_restore_verify.py` | Exercised by the synthetic WAL recovery rehearsal. |
| ACL predicate / database prefilter equivalence | `tests/test_team_postgres.py::PostgresTeamRepositoryTests.test_sql_acl_prefilter_matches_reference_policy` | Exercised in the Team PostgreSQL integration job. |
| Content-addressed evidence cannot widen ACL | `tests/test_team_postgres.py::TeamPostgresTests.test_team_api_enforces_review_and_authorized_search` | Exercised in the Team PostgreSQL integration job. |
| Organization promotion remains non-public until review | `tests/test_team_postgres.py::TeamPostgresTests.test_team_api_enforces_review_and_authorized_search` | Exercised in the Team PostgreSQL integration job. |
| Private-to-Team organization workflow | `tests/test_team_postgres.py::TeamPostgresTests.test_team_client_sync_stages_organization_publication_until_review` | Exercised in the Team PostgreSQL integration job. |
| Configured Team readiness | `wiki-memory team-preflight` | Exercised by its non-secret configuration test; an operator still supplies deployment evidence. |

All fixtures for these commands are synthetic. The supplied benchmark tools
refuse to claim a full-scale target from a smaller corpus.

## Current gate status

- `proven by alpha.6 CI`: canonical append-only ledger, content-addressed
  evidence, projections/rebuild, plugin contracts, isolated Team paths,
  ACL-before-search, crash recovery, and a synthetic WAL-recovery rehearsal.
- `not yet evidenced`: the one-million-fragment Team target and the
  100-connector/500-member capacity target. The harnesses exist, but a target
  is not a result until its JSON report is retained and reviewable.
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

The release workflow treats a semantic pre-release such as `v1.0.0-alpha.6`
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
