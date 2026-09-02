# External security review package

This is the acceptance package for the independent review required before a
stable `1.0.0` release. It deliberately uses only synthetic data. A reviewer
must not receive a real vault, browser profile, OIDC token, database dump, or
object-store credential.

## Review target

Record all of the following in the report before testing:

- Git commit and signed release checksum bundle;
- Python wheel checksum and, when Team is included, the pinned OCI image
  digest and Cosign verification result;
- review date, reviewer organization, and the exact deployment topology;
- PostgreSQL and object-store versions, OIDC implementation, and CNI when
  Kubernetes NetworkPolicy is in scope.

The review covers the code paths in `src/wiki_memory/plugins.py`,
`src/wiki_memory/local_api.py`, `src/wiki_memory/team_server.py`,
`src/wiki_memory/team_repository.py`, `src/wiki_memory/team.py`, and the
Helm/Compose deployment artifacts. It is not a claim that an untrusted
administrator, a compromised endpoint, or an unreviewed third-party IdP is
safe.

## Required review questions

| Boundary | Acceptance criterion | Existing executable evidence |
| --- | --- | --- |
| Plugin trust | An untrusted Python plugin cannot become active outside explicit solo developer mode; an isolated plugin cannot receive Core objects or undeclared secrets. | `tests.test_v1` plugin lifecycle, executable and OCI isolation tests |
| OCI isolation | A digest-pinned OCI plugin is read-only, drops capabilities, has a private writable runtime, and has no network by default. | `test_oci_plugin_defaults_to_a_read_only_networkless_sandbox` |
| Team ACLs | An unauthorized actor cannot learn event, blob, assertion, projection, or search existence/content; authorization occurs before ranking. | real PostgreSQL ACL differential test and Team API tests |
| Publication/review | A contributor cannot bypass curator review for risky promotion, ACL widening, retraction, or stale/conflicting knowledge; an organization proposal and its blob stay Team-scoped before acceptance. | `test_team_api_enforces_review_and_authorized_search` and V1 review tests |
| Replication | A shared event cannot be committed before all referenced evidence is present and verified; retries cannot change an idempotent event. | event pack, Team pull, idempotency and restore verifier tests |
| Deployment | Release images are digest-pinned; workloads are non-root/read-only; the Helm chart fails closed without explicit ingress/egress values. | `scripts/deployment_validate.py` and Helm render CI fixtures |
| Recovery | A restored ledger and every referenced object are validated before a restore attestation is accepted; a normal admin token alone cannot submit it. | `scripts/team_restore_verify.py`, `scripts/team_pitr_rehearsal.py`, Team API test |

## Reviewer procedure

1. Verify the release checksums and Cosign bundles before executing any code.
2. Create a disposable Team environment with a fresh PostgreSQL database,
   FileObjectStore or disposable versioned S3 bucket, and synthetic OIDC
   identities for every role.
3. Run the complete test suite, then the database suite with
   `TEST_DATABASE_URL` set. Independently inspect that the test identities
   include owner, explicit reader, group member, space member, outsider,
   contributor, curator, admin, and service identities.
4. Perform adversarial HTTP tests: forged actor fields, private events sent to
   Team, malformed ACLs, guessed blob hashes, revoked users, stale stream
   writes, decision events sent to generic append, an organization proposal
   queried/downloaded before review, oversized/partial uploads, and cross-space
   projection paths.
5. Review the SQL predicates used by Team search against `can_read`, and test
   timing/result-size differences for denied data with a corpus large enough
   to exercise ranking.
6. Run the isolated plugin protocol against malformed NDJSON, a process that
   never answers, mismatched capabilities, oversized responses, undeclared
   secret requests, and a failed migration. Verify that the old provider
   remains active after an upgrade failure.
7. Render the Helm chart with the supplied external and internal CI fixtures.
   In the actual cluster, verify that the CNI enforces the declared
   NetworkPolicy rather than merely accepting the object.
8. Restore PostgreSQL to an exact recovery point together with the matching
   versioned object-store point, run `scripts/team_restore_verify.py`, and
   verify that its attestation contains aggregates only.

## Required reviewer deliverable

The reviewer supplies a signed report containing:

- version/digest identities from the first section;
- threat model and methods used;
- every finding with severity, reproduction using synthetic data, affected
  boundary, and remediation/retest status;
- an explicit statement for each table criterion: accepted, accepted with
  limitation, or rejected;
- the result of the production recovery rehearsal and CNI egress test.

A finding is closed only after a commit/release digest is retested. Absence of
findings is not sufficient when a criterion was not exercised.
