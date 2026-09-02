# Release evidence matrix

This document records what the repository itself can prove. It is deliberately
not a marketing checklist: a row is `proven` only when its command exercised
the stated boundary, and a row requiring a production provider or independent
review remains open until that party supplies evidence.

## Reproducible, synthetic gates

| Requirement | Evidence | Latest local rehearsal |
| --- | --- | --- |
| Durable solo capture and QMD retrieval at 100,000 documents | `scripts/load_benchmark.py --documents 100000 --qmd --assert-targets` | capture p95 22.419 ms; QMD retrieval p95 320.940 ms; 100,000 measured documents plus 100 warm-up writes |
| Team retrieval at one million fragments | `scripts/team_load_benchmark.py --fragments 1000000 --assert-target` | PostgreSQL search p95 5.607 ms; 1,001,000 events including 1,000 warm-up events; atomic projection rebuild 116.795 s |
| 100 active source streams and 500 authorized members | `scripts/team_load_benchmark.py --workers 100 --members 500 --assert-operational-scale` | 10,100 synthetic events; 500 distinct group-authorized principals; 100 simultaneous member searches; zero failed authorization-filtered searches |
| Team WAL recovery protocol | `scripts/team_pitr_rehearsal.py` | PostgreSQL base backup, event committed after backup, archived WAL replay into a new cluster, complete Team restore verification of both events and all referenced evidence |
| Solo crash boundary | `scripts/crash_campaign.py` | acknowledged events are accepted only after evidence and ledger durability; recovery verifies ledger and rebuilds projections |
| Canonical Team ledger and object verification | `scripts/team_restore_verify.py` | rebuilds Team search, checks stream order/event hashes, and verifies every referenced object by default |
| ACL predicate / database prefilter equivalence | `tests/test_team_postgres.py::PostgresTeamRepositoryTests.test_sql_acl_prefilter_matches_reference_policy` | real PostgreSQL differential test across owners, explicit users, groups, spaces, and organization scope |
| Content-addressed evidence cannot widen ACL | `tests/test_team_postgres.py::TeamPostgresTests.test_team_api_enforces_review_and_authorized_search` | real Team API tests reject both a guessed restricted hash and cross-space reuse by a member authorized in the source space |
| Organization promotion remains non-public until review | `tests/test_team_postgres.py::TeamPostgresTests.test_team_api_enforces_review_and_authorized_search` | real Team API test proves that an outsider sees neither the proposed event nor its blob before curator acceptance, then receives the accepted organization event |
| Private-to-Team organization workflow | `tests/test_team_postgres.py::TeamPostgresTests.test_team_client_sync_stages_organization_publication_until_review` | real PostgreSQL end-to-end test covers private capture, outbox upload, Team-scoped proposal, outsider denial, curator acceptance, pull of the organization event, and post-acceptance evidence access |
| Configured Team readiness | `wiki-memory team-preflight` | checks database/object-store reachability, S3/MinIO versioning, OIDC configuration, a dedicated restoration-attestation channel, and a successful restore attestation without emitting credentials or endpoints |

All fixtures for these commands are synthetic. The supplied benchmark tools
refuse to claim a full-scale target from a smaller corpus.

## Current gate status

- `proven locally`: canonical append-only ledger, content-addressed evidence,
  projections/rebuild, plugin contracts, isolated Team paths, ACL-before-search,
  crash recovery, full-scale local and Team search, connector/member capacity,
  and a synthetic WAL-recovery rehearsal.
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

The release workflow treats a semantic pre-release such as `v1.0.0-alpha.3`
as an alpha. A stable tag such as `v1.0.0` first enters the GitHub
`stable-release` environment. Configure that environment with required
reviewers and its two variables, each pointing to a reviewable HTTPS record:

- `WIKI_MEMORY_PRODUCTION_RECOVERY_EVIDENCE`: the exact PostgreSQL PITR plus
  versioned-object recovery rehearsal for that deployment;
- `WIKI_MEMORY_EXTERNAL_AUDIT_EVIDENCE`: the independent plugin/authorization
  audit report.

`scripts/release_gate.py` rejects the stable release before tests, artifacts,
or publication when either record is absent or malformed. This is an explicit
human control, not a substitute for the underlying evidence.
