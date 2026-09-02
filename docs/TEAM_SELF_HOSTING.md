# Team self-hosting

Team is optional. Removing it leaves private local memory intact. Shared projections become read-only and remain visible only through the last locally cached authorization session; no Core code requires an account.

## Compose evaluation/reference deployment

1. Copy `deploy/team/.env.example` to an ignored `.env`.
2. Replace every placeholder with random values and set `WIKI_MEMORY_IMAGE` to the exact signed GHCR release digest (tags are rejected by the reference Compose file).
3. Configure an HTTPS OIDC issuer and audience, or use the bootstrap token for initial recovery only.
4. Run `docker compose up -d` from `deploy/team`.
5. Put a TLS reverse proxy in front of loopback port 8787.
6. Map IdP groups to flat Wiki Memory spaces with `OIDC_GROUP_SPACE_MAP` (a JSON object); with no map, group names are used as space IDs.
7. Open `/console`, validate OIDC, then remove/disable `WIKI_MEMORY_BOOTSTRAP_TOKEN`.

The configured OIDC role claim must explicitly contain Wiki Memory roles. An authenticated subject with no recognized role receives no implicit `reader` access. The access token must carry a non-empty subject and an expiration; unsigned, expired, or timeless tokens are rejected.

External Team connectors require two independent administrator controls: a
valid Ed25519 manifest signature and an exact approved plugin ID. Configure
`WIKI_MEMORY_TEAM_PLUGIN_TRUST_KEYS` as JSON key-ID → base64 public-key map and
`WIKI_MEMORY_TEAM_APPROVED_PLUGIN_IDS` as a JSON array. Compose reads both from
`.env`; Helm uses `pluginPolicy.trustKeys` and `pluginPolicy.approvedPluginIds`.
They default to no approved external plugins. Do not put a private signing key
in either value; the policy rejects malformed, unsigned, or merely signed-but-
unapproved manifests.

An automated connector uses a dedicated OIDC `service` identity. Its subject
must equal the connector instance ID used for ingestion; Team rejects a human
user token that merely labels an event as a connector. A person contributing a
file or note uses the normal capture/proposal flow instead.

The API accepts at most 100 events per append request by default (`WIKI_MEMORY_MAX_EVENTS_PER_APPEND`, bounded between 1 and 1,000). Keep this deliberately finite: a connector resumes through durable checkpoints rather than placing unbounded work inside one transaction.

The stack includes API, worker, PostgreSQL, MinIO, bucket creation, and object versioning. Every image reference is digest-pinned; Compose deliberately refuses a mutable Wiki Memory tag. Containers are non-root, read-only, `no-new-privileges`, and use tmpfs for temporary uploads.

Compose volumes are durability, not backup. This topology is single-node and is not a production HA promise.

## Helm

`deploy/helm/wiki-memory` deploys replicated API/workers plus optional internal PostgreSQL and MinIO. For production, set:

- `postgresql.internal=false` and `postgresql.dsn` to a managed/HA PostgreSQL with PITR;
- `objectStore.internal=false`, endpoint, bucket, and credentials for a versioned S3-compatible store;
- `image.digest` to the verified GHCR release digest;
- OIDC issuer/audience/JWKS settings and the explicit group-to-space map;
- secrets through External Secrets, Sealed Secrets, or your platform—not checked-in values.

Set `secrets.existingSecret` when a controller manages credentials. That Secret must provide `DATABASE_URL`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `WIKI_MEMORY_BOOTSTRAP_TOKEN` (which may be empty after OIDC bootstrap), and a separate `WIKI_MEMORY_RESTORE_ATTESTATION_TOKEN`. If internal storage is enabled, it must also provide `POSTGRES_PASSWORD`, `MINIO_ROOT_USER`, and `MINIO_ROOT_PASSWORD`. When it is set, the chart does not render an application credential Secret; internal storage passwords are never rendered directly into a workload spec.

Expose the Service only through TLS ingress/API gateway. The Helm chart enables a deny-by-default `NetworkPolicy`: set `networkPolicy.apiIngress` to the gateway peer selector and use the exact `networkPolicy.*CIDRs` for cluster DNS, OIDC, PostgreSQL, object storage, and optionally OTLP. The chart refuses an enabled policy without those required boundaries; it does not accept hostnames as a pretend domain firewall. NetworkPolicy enforcement also requires a CNI that implements it. Nested teams are intentionally unsupported in V1; map OIDC groups directly to Wiki Memory spaces.

Start from an explicit policy like the following, replacing every address from
your own cluster/provider network inventory. Do not use `0.0.0.0/0` merely to
make the chart install: that removes the egress guarantee.

```yaml
networkPolicy:
  apiIngress:
    - namespaceSelector:
        matchLabels: {kubernetes.io/metadata.name: ingress-system}
      podSelector:
        matchLabels: {app.kubernetes.io/name: ingress-gateway}
  dnsCIDRs: [10.96.0.10/32]
  oidcCIDRs: [203.0.113.20/32]
  postgresCIDRs: [198.51.100.16/28] # only when postgresql.internal=false
  objectStoreCIDRs: [198.51.100.64/28] # only when objectStore.internal=false
  otlpCIDRs: [10.42.8.14/32] # omit when OTLP is disabled
```

The OIDC and object-store providers must publish stable network ranges or be
reached through an organization-controlled egress gateway with stable CIDRs.
The platform team owns monitoring of policy enforcement and changes to those
ranges.

## Roles and workflow

- `admin`: configuration, retention, groups, trusted plugins;
- `curator`: proposal decisions and contradiction resolution;
- `contributor`: sources and proposals;
- `reader`: authorized search/evidence;
- `service`: connector-specific identity.

Private data never uploads. A share operation shows the exact payload/evidence and requires its preview hash. Organization publication is always a proposal until curator acceptance. Team rejects private events and non-normalized ACLs. Privileged decision events cannot use the generic append endpoint; they must pass through review. The local HTTP/MCP authority can review private proposals only; shared review is server-only.

Review is mandatory for organization promotion, low confidence, contradictions, sensitive classification, ACL widening, stale base, retraction, or purge. Projection edits in shared spaces also require a curator.

## Required production backup contract

Do not call a Team deployment production-ready until all are true:

1. PostgreSQL continuous WAL archiving and point-in-time recovery are configured.
2. Object versioning and retention are enabled.
3. Database and object backups share a documented recovery point.
4. An automated job restores into a temporary environment.
5. It verifies database integrity, event count, sampled/all evidence SHA-256, and an authorized search.
6. The last successful restore age is exported and alerted.

The application deliberately does not implement a fake “backup succeeded” flag. These controls belong to the operator’s database/object platform; reference runbooks must record their evidence.

Before exposing a Team endpoint, run `wiki-memory team-preflight` with the
same environment as the API. It verifies database/object-store reachability,
S3/MinIO bucket versioning, OIDC configuration, and whether a successful
restore attestation exists. It fails closed for a local FileObjectStore or a
bucket whose versioning cannot be read. Its remaining `operatorEvidenceRequired`
items are intentionally not guessed from application credentials.

After restoring PostgreSQL to an isolated recovery point and exposing the corresponding object-store recovery point through the normal `DATABASE_*` / `S3_*` configuration, run the verifier before admitting traffic:

```bash
uv run python scripts/team_restore_verify.py \
  --attestation-url https://team.example.internal \
  --admin-token "$WIKI_MEMORY_OIDC_ADMIN_TOKEN" \
  --attestation-token "$WIKI_MEMORY_RESTORE_ATTESTATION_TOKEN" \
  --backup-id "postgres-pitr-2026-09-02T00:00:00Z"
```

It rebuilds the derived Team search projection atomically, validates every event hash and stream version, and verifies every referenced object SHA-256 by default. It emits only aggregate counts. `--evidence-sample N` is available for a documented, explicitly sampled rehearsal; it never silently changes the default from full verification. An attestation is accepted only after the verifier completes, includes no restored payload, and presents both an admin identity and the dedicated restoration-attestation secret.

## Observability

`/metrics` exports request counters/latency, events, global position, job states, pending proposals, and the age of the latest successful/failed restore rehearsal. It requires an admin bearer token; configure the Prometheus scrape job accordingly. Set `OTEL_EXPORTER_OTLP_ENDPOINT` to enable FastAPI OTLP tracing. Worker output is JSON. A successful rehearsal must post its aggregate result (never restored contents) to `POST /v1/operations/restore-verifications` as an admin. `wiki_memory_restore_last_success_age_seconds=-1` means no restore has ever been attested and must alert. Scrape and alert on:

- pending/retry/dead jobs and their age;
- active replication clients, maximum cursor lag, and reported client outbox backlog;
- authorization errors and 5xx rate;
- replication/outbox lag from clients;
- proposal queue age;
- PostgreSQL/object-store health;
- PITR/archive failures and last tested restore age.

Technical logs must contain IDs and error classes, not event payload, transcript, evidence content, tokens, or OIDC claims.

## Offline behavior

The solo ledger continues. Shared projections remain readable from the authorized local cache using the last successful Team session. This device-local entitlement snapshot lives in the OS runtime directory, outside the memory root, backups, and Syncthing packs. Shared writes stay in the outbox. On reconnection the client first refreshes identity, roles, groups, and spaces; a removed entitlement immediately hides the corresponding local projection from search. Blobs upload first, events second, then pull resumes from the durable cursor. Any conflict becomes a review proposal and produces `ok: false`; the client never claims a successful sync.

Offline access deliberately uses the last successful session and therefore has the same revocation delay as the offline period. Deployments needing immediate revocation must disable offline shared-cache access at the endpoint/device-management layer; the alpha does not yet implement expiring offline leases.

QMD indexes private vaults only. Shared Team projections use authorization-filtered local lexical retrieval so ACL checks happen before their contents are searched. If an older QMD configuration mentions a Team vault, queries fail over to the safe lexical path until `wiki-memory index` rebuilds the private-only index.
