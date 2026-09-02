from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wiki_memory.contracts import SourceSelection
from wiki_memory.cli import build_parser, run as run_cli
from wiki_memory.engine import MemoryEngine
from wiki_memory.events import EventActor, MemoryEvent, PluginRef, uuid7
from wiki_memory.ingestion import SourceIngestionRuntime
from wiki_memory.layout import init_memory
from wiki_memory.object_store import FileObjectStore
from wiki_memory.operations import publication_preview, publish_private_event
from wiki_memory.postgres_source import PostgresSourceConnector
from wiki_memory.team import (
    Principal,
    Role,
    TeamClient,
    can_read,
    ensure_team_vault,
    normalize_acl,
    shared_vault_slug,
)
from wiki_memory.team_repository import PostgresTeamRepository
from wiki_memory.team_server import create_app, database_dsn_from_environment


@unittest.skipUnless(importlib.util.find_spec("psycopg"), "psycopg is not installed")
class TeamDatabaseEnvironmentTests(unittest.TestCase):
    def test_split_database_settings_quote_special_passwords(self) -> None:
        from psycopg.conninfo import conninfo_to_dict

        with patch.dict(
            os.environ,
            {
                "DATABASE_HOST": "postgres",
                "DATABASE_PORT": "5432",
                "DATABASE_NAME": "wiki_memory",
                "DATABASE_USER": "wiki_memory",
                "DATABASE_PASSWORD": "synthetic @:/ password",
            },
            clear=True,
        ):
            parsed = conninfo_to_dict(database_dsn_from_environment())
        self.assertEqual(parsed["password"], "synthetic @:/ password")
        self.assertEqual(parsed["host"], "postgres")

    def test_event_row_normalizes_legacy_byte_text(self) -> None:
        event = MemoryEvent(
            event_type="source.captured",
            stream_id="row:bytes",
            idempotency_key="row-bytes",
            actor=EventActor("user", "member"),
            plugin=PluginRef("test", "1.0.0"),
            scope="team",
            space_id="space",
            acl=normalize_acl({}, owner="member", space_id="space"),
            payload={"value": "UTF-8 canonical event"},
        )
        row = {
            "event_id": event.event_id,
            "event_type": event.event_type,
            "schema_version": event.schema_version,
            "stream_id": event.stream_id,
            "stream_version": event.stream_version,
            "idempotency_key": event.idempotency_key,
            "scope": event.scope,
            "space_id": event.space_id,
            "actor_json": event.to_dict()["actor"],
            "occurred_at": event.occurred_at,
            "recorded_at": event.recorded_at,
            "correlation_id": event.correlation_id,
            "causation_id": event.causation_id,
            "plugin_json": event.to_dict()["plugin"],
            "evidence_refs_json": event.evidence_refs,
            "acl_json": event.acl,
            "payload_json": event.payload,
            "position": 1,
            "event_hash": None,
        }

        def as_bytes(value):
            if isinstance(value, str):
                return value.encode()
            if isinstance(value, dict):
                return {as_bytes(key): as_bytes(item) for key, item in value.items()}
            if isinstance(value, list):
                return [as_bytes(item) for item in value]
            return value

        restored = PostgresTeamRepository._row_to_event(as_bytes(row))
        self.assertEqual(restored.scope, "team")
        self.assertEqual(restored.actor.id, "member")
        self.assertEqual(restored.payload["value"], "UTF-8 canonical event")


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL is not configured")
class TeamPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repository = PostgresTeamRepository(os.environ["TEST_DATABASE_URL"])
        cls.repository.initialize()
        cls.temporary = tempfile.TemporaryDirectory()
        cls.object_store = FileObjectStore(Path(cls.temporary.name) / "objects")
        cls.run_id = uuid.uuid4().hex[:12]
        cls.space = f"integration-{cls.run_id}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def event(self, index: int) -> MemoryEvent:
        return MemoryEvent(
            event_type="source.captured",
            stream_id=f"integration:{self.run_id}:concurrent",
            idempotency_key=f"integration-{self.run_id}-{index}",
            actor=EventActor("user", "member"),
            plugin=PluginRef("integration", "1.0.0"),
            scope="team",
            space_id=self.space,
            acl=normalize_acl({}, owner="member", space_id=self.space),
            payload={"value": f"searchable synthetic {index}"},
        )

    def test_concurrent_append_worker_and_search(self) -> None:
        # A shared stream is the worst normal contention case: every append must
        # receive a contiguous version without dropping or silently merging one
        # of the 100 concurrent deliveries.
        with ThreadPoolExecutor(max_workers=20) as pool:
            results = list(pool.map(lambda index: self.repository.append(self.event(index)), range(100)))
        versions = sorted(event.stream_version for event, _ in results)
        self.assertEqual(versions, list(range(1, 101)))
        projected = self.repository.run_jobs_once(200)
        self.assertGreaterEqual(projected["completed"], 100)
        results = self.repository.search("searchable", 125, {self.space}, False)
        self.assertEqual(len(results), 1)
        self.assertIn("searchable synthetic", results[0]["snippet"])

    def test_worker_withholds_unverifiable_evidence_before_indexing(self) -> None:
        content = b"worker must verify evidence before projection"
        digest = hashlib.sha256(content).hexdigest()
        source = Path(self.temporary.name) / f"worker-proof-{self.run_id}.txt"
        source.write_bytes(content)
        self.object_store.put_file(digest, source, "text/plain")
        event = MemoryEvent(
            event_type="source.captured",
            stream_id=f"integration:{self.run_id}:corrupt-worker-proof",
            idempotency_key=f"integration-{self.run_id}-corrupt-worker-proof",
            actor=EventActor("user", "member"),
            plugin=PluginRef("integration", "1.0.0"),
            scope="team",
            space_id=self.space,
            evidence_refs=[f"sha256:{digest}"],
            acl=normalize_acl({}, owner="member", space_id=self.space),
            payload={"body": "withheldworkerproof"},
        )
        self.repository.append(event)
        self.object_store._path(digest).write_bytes(b"x" * len(content))

        projected = self.repository.run_jobs_once(100, evidence_verify=self.object_store.verify)

        self.assertGreaterEqual(projected["failed"], 1)
        self.assertEqual(
            self.repository.search("withheldworkerproof", 10, {self.space}, False),
            [],
        )

    def test_search_applies_acl_before_full_text_ranking(self) -> None:
        restricted = MemoryEvent(
            event_type="source.captured",
            stream_id=f"integration:{self.run_id}:restricted",
            idempotency_key=f"integration-{self.run_id}-restricted",
            actor=EventActor("user", "owner-only"),
            plugin=PluginRef("integration", "1.0.0"),
            scope="team",
            space_id=self.space,
            acl=normalize_acl(
                {"audience": "explicit", "classification": "restricted"},
                owner="owner-only",
                space_id=self.space,
            ),
            payload={"value": "uniquerestrictedtoken"},
        )
        self.repository.append(restricted)
        self.repository.run_jobs_once(100)
        denied = self.repository.search(
            "uniquerestrictedtoken",
            10,
            {self.space},
            False,
            principal_id="ordinary-reader",
            groups=set(),
        )
        allowed = self.repository.search(
            "uniquerestrictedtoken",
            10,
            set(),
            False,
            principal_id="owner-only",
            groups=set(),
        )
        self.assertEqual(denied, [])
        self.assertEqual(len(allowed), 1)

    def test_sql_acl_prefilter_matches_reference_policy(self) -> None:
        """Search SQL must neither leak nor hide any ACL policy result.

        The repository applies an SQL prefilter before ranking, while the API
        applies ``can_read`` again before response serialization.  Keep the
        two implementations differential-tested across every V1 audience
        path, otherwise an optimization could accidentally become an access
        control divergence.
        """

        token = f"acl-differential-{self.run_id}"
        space_a = f"{self.space}-a"
        space_b = f"{self.space}-b"
        cases = [
            ("team", space_a, "owner-only", {"audience": "explicit"}),
            ("team", space_a, "reader-only", {"readers": ["reader"], "audience": "explicit"}),
            ("team", space_a, "group-only", {"groups": ["analytics"], "audience": "explicit"}),
            ("team", space_a, "space-a", {"audience": "space"}),
            ("team", space_b, "space-b", {"audience": "space"}),
            ("organization", space_b, "organization", {"audience": "organization"}),
        ]
        persisted: list[MemoryEvent] = []
        for index, (scope, space_id, owner, acl) in enumerate(cases):
            event = MemoryEvent(
                event_type="source.captured",
                stream_id=f"integration:{self.run_id}:acl-differential:{index}",
                idempotency_key=f"integration-{self.run_id}-acl-differential-{index}",
                actor=EventActor("user", owner),
                plugin=PluginRef("integration", "1.0.0"),
                scope=scope,  # type: ignore[arg-type]
                space_id=space_id,
                acl=normalize_acl(acl, owner=owner, space_id=space_id),
                payload={"body": f"{token} {owner}"},
            )
            saved, created = self.repository.append(event)
            self.assertTrue(created)
            persisted.append(saved)
        self.repository.rebuild_search_projection()

        principals = [
            Principal("owner-only", frozenset({Role.READER}), frozenset()),
            Principal("reader", frozenset({Role.READER}), frozenset()),
            Principal("group-member", frozenset({Role.READER}), frozenset(), frozenset({"analytics"})),
            Principal("space-member", frozenset({Role.READER}), frozenset({space_a})),
            Principal("outsider", frozenset({Role.READER}), frozenset()),
        ]
        for principal in principals:
            sql_results = self.repository.search(
                token,
                100,
                set(principal.spaces),
                principal.has_any_role(Role.READER, Role.CONTRIBUTOR, Role.CURATOR, Role.ADMIN),
                principal_id=principal.id,
                groups=set(principal.groups),
                all_access=principal.has_any_role(Role.ADMIN),
            )
            actual = {item["eventId"] for item in sql_results}
            expected = {
                event.event_id
                for event in persisted
                if can_read(principal, scope=event.scope, space_id=event.space_id, acl=event.acl)
            }
            self.assertEqual(actual, expected, principal.id)

    def test_concurrent_duplicate_is_idempotent(self) -> None:
        event = MemoryEvent(
            event_type="source.captured",
            stream_id=f"integration:{self.run_id}:idempotent",
            idempotency_key=f"integration-{self.run_id}-idempotent",
            actor=EventActor("user", "member"),
            plugin=PluginRef("integration", "1.0.0"),
            scope="team",
            space_id=self.space,
            acl=normalize_acl({}, owner="member", space_id=self.space),
            payload={"value": "same delivery"},
        )
        copies = [MemoryEvent.from_dict(event.to_dict()) for _ in range(8)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(self.repository.append, copies))
        self.assertEqual(sum(1 for _, created in results if created), 1)
        self.assertEqual({persisted.event_id for persisted, _ in results}, {event.event_id})

    def test_restore_verifier_rejects_tampered_ledger_and_missing_evidence(self) -> None:
        import hashlib
        import psycopg

        content = b"synthetic restoration proof"
        digest = hashlib.sha256(content).hexdigest()
        source = Path(self.temporary.name) / f"restore-proof-{self.run_id}.txt"
        source.write_bytes(content)
        store = self.object_store
        store.put_file(digest, source, "text/plain")
        event = MemoryEvent(
            event_type="source.captured",
            stream_id=f"restore:{self.run_id}",
            idempotency_key=f"restore-proof-{self.run_id}",
            actor=EventActor("user", "operator"),
            plugin=PluginRef("restore-test", "1.0.0"),
            scope="team",
            space_id=self.space,
            evidence_refs=[f"sha256:{digest}"],
            acl=normalize_acl({}, owner="operator", space_id=self.space),
            payload={"body": "synthetic restoration proof"},
        )
        persisted, _ = self.repository.append(event)
        self.assertTrue(self.repository.verify_integrity(store.verify)["ok"])

        with psycopg.connect(os.environ["TEST_DATABASE_URL"]) as connection:
            connection.execute("ALTER TABLE memory_events DISABLE TRIGGER ALL")
            connection.execute("UPDATE memory_events SET event_hash='0' WHERE event_id=%s", (persisted.event_id,))
            connection.execute("ALTER TABLE memory_events ENABLE TRIGGER ALL")
        tampered = self.repository.verify_integrity(store.verify)
        self.assertFalse(tampered["ok"])
        self.assertIn("canonical event hash mismatch", {item["error"] for item in tampered["errors"]})

        with psycopg.connect(os.environ["TEST_DATABASE_URL"]) as connection:
            connection.execute("ALTER TABLE memory_events DISABLE TRIGGER ALL")
            connection.execute("UPDATE memory_events SET event_hash=%s WHERE event_id=%s", (persisted.event_hash, persisted.event_id))
            connection.execute("ALTER TABLE memory_events ENABLE TRIGGER ALL")
        store._path(digest).unlink()
        missing = self.repository.verify_integrity(store.verify)
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["missingEvidence"], [digest])
        store.put_file(digest, source, "text/plain")
        self.assertTrue(self.repository.verify_integrity(store.verify)["ok"])
        store._path(digest).write_bytes(b"corrupted synthetic restoration proof")
        self.assertFalse(store.verify(digest))
        store.put_file(digest, source, "text/plain")
        self.assertTrue(store.verify(digest))
        environment = dict(os.environ)
        environment["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
        environment["TEAM_FILE_OBJECT_STORE"] = str(store.root)
        command = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "team_restore_verify.py")],
            env=environment,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(command.returncode, 0, command.stderr)
        report = json.loads(command.stdout)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["evidenceMode"], "all")

    def test_team_api_enforces_review_and_authorized_search(self) -> None:
        from fastapi.testclient import TestClient

        from wiki_memory.team_server import create_app

        space = self.space
        other_space = f"{space}-other"

        class Tokens:
            def verify(self, token: str) -> Principal:
                principals = {
                    "member": Principal(
                        "api-member",
                        frozenset({Role.CONTRIBUTOR, Role.READER}),
                        frozenset({space}),
                    ),
                    "curator": Principal(
                        "api-curator",
                        frozenset({Role.CURATOR, Role.READER}),
                        frozenset({space}),
                    ),
                    "outsider": Principal("api-outsider", frozenset({Role.READER}), frozenset()),
                    "cross-space": Principal(
                        "api-cross-space",
                        frozenset({Role.CONTRIBUTOR, Role.READER}),
                        frozenset({other_space}),
                    ),
                    "multi-space": Principal(
                        "api-multi-space",
                        frozenset({Role.CONTRIBUTOR, Role.READER}),
                        frozenset({space, other_space}),
                    ),
                    "admin": Principal(
                        "api-admin",
                        frozenset({Role.ADMIN, Role.READER}),
                        frozenset({space}),
                    ),
                    "service": Principal(
                        "api-connector",
                        frozenset({Role.SERVICE}),
                        frozenset({space}),
                        kind="service",
                    ),
                }
                return principals[token]

        store = self.object_store
        restore_attestation_token = "synthetic-restore-attestation-token"
        client = TestClient(
            create_app(self.repository, store, Tokens(), restore_attestation_token)
        )
        member = {"Authorization": "Bearer member"}
        curator = {"Authorization": "Bearer curator"}
        outsider = {"Authorization": "Bearer outsider"}
        cross_space = {"Authorization": "Bearer cross-space"}
        multi_space = {"Authorization": "Bearer multi-space"}
        admin = {"Authorization": "Bearer admin"}
        service = {"Authorization": "Bearer service"}

        healthy = client.get("/v1/health")
        self.assertEqual(healthy.status_code, 200, healthy.text)
        self.assertEqual(healthy.json()["checks"], {"database": True, "objectStore": True})

        class UnavailableObjectStore:
            def has(self, _: str) -> bool:
                raise RuntimeError("synthetic object storage outage")

        unhealthy = TestClient(create_app(self.repository, UnavailableObjectStore(), Tokens())).get("/v1/health")
        self.assertEqual(unhealthy.status_code, 503, unhealthy.text)
        self.assertEqual(unhealthy.json()["checks"], {"database": True, "objectStore": False})

        console = client.get("/console")
        self.assertEqual(console.status_code, 200)
        self.assertNotIn("unsafe-inline", console.headers["content-security-policy"])
        script = client.get("/console/app.js")
        self.assertEqual(script.status_code, 200)
        self.assertNotIn("sessionStorage", script.text)

        self.assertEqual(client.get("/metrics", headers=outsider).status_code, 403)
        malformed_proposal = client.post(
            "/v1/proposals",
            headers=member,
            json={
                "spaceId": space,
                "evidenceRefs": ["not-a-sha256-reference"],
                "assertion": {"body": "must not reach ACL or object storage"},
            },
        )
        self.assertEqual(malformed_proposal.status_code, 422, malformed_proposal.text)
        self.assertEqual(malformed_proposal.json()["detail"], "evidenceRefs must contain SHA-256 references")
        oversized_batch = client.post(
            "/v1/events:append",
            headers=member,
            json={"events": [{}] * 101},
        )
        self.assertEqual(oversized_batch.status_code, 413, oversized_batch.text)
        unapproved_connector_event = MemoryEvent(
            event_type="source.captured",
            stream_id=f"connector:{self.run_id}:unapproved",
            stream_version=1,
            idempotency_key=f"connector-unapproved-{self.run_id}",
            actor=EventActor("connector", "api-connector"),
            plugin=PluginRef("source-unapproved", "1.0.0"),
            scope="team",
            space_id=space,
            acl=normalize_acl({}, owner="api-connector", space_id=space),
            payload={"vault": shared_vault_slug(space), "body": "must not be trusted by a claimed id"},
        )
        rejected_connector = client.post(
            "/v1/events:append", headers=service, json={"events": [unapproved_connector_event.to_dict()]}
        )
        self.assertEqual(rejected_connector.status_code, 403, rejected_connector.text)
        user_claimed_connector = MemoryEvent.from_dict(unapproved_connector_event.to_dict())
        user_claimed_connector.event_id = uuid7()
        user_claimed_connector.stream_id = f"connector:{self.run_id}:user-claim"
        user_claimed_connector.idempotency_key = f"connector-user-claim-{self.run_id}"
        user_claimed_connector.actor = EventActor("connector", "api-member")
        user_claimed_connector.plugin = PluginRef("source-postgres", "1.0.0")
        user_claimed_connector.acl = normalize_acl({}, owner="api-member", space_id=space)
        rejected_user_claim = client.post(
            "/v1/events:append", headers=member, json={"events": [user_claimed_connector.to_dict()]}
        )
        self.assertEqual(rejected_user_claim.status_code, 403, rejected_user_claim.text)
        official_connector_event = MemoryEvent.from_dict(unapproved_connector_event.to_dict())
        official_connector_event.event_id = uuid7()
        official_connector_event.stream_id = f"connector:{self.run_id}:official"
        official_connector_event.idempotency_key = f"connector-official-{self.run_id}"
        official_connector_event.plugin = PluginRef("source-postgres", "1.0.0")
        accepted_connector = client.post(
            "/v1/events:append", headers=service, json={"events": [official_connector_event.to_dict()]}
        )
        self.assertEqual(accepted_connector.status_code, 200, accepted_connector.text)
        metrics = client.get("/metrics", headers=admin)
        self.assertEqual(metrics.status_code, 200, metrics.text)
        self.assertIn("wiki_memory_events_total", metrics.text)
        self.assertIn("wiki_memory_restore_last_success_age_seconds -1.0", metrics.text)
        restore = client.post(
            "/v1/operations/restore-verifications",
            headers=admin,
            json={
                "status": "success",
                "backupId": f"synthetic-pitr-{self.run_id}",
                "eventCount": 0,
                "evidenceCount": 0,
                "detail": {"environment": "temporary", "fixture": True},
            },
        )
        self.assertEqual(restore.status_code, 403, restore.text)
        restore = client.post(
            "/v1/operations/restore-verifications",
            headers={
                **admin,
                "X-Wiki-Memory-Restore-Attestation": restore_attestation_token,
            },
            json={
                "status": "success",
                "backupId": f"synthetic-pitr-{self.run_id}",
                "eventCount": 0,
                "evidenceCount": 0,
                "detail": {"environment": "temporary", "fixture": True},
            },
        )
        self.assertEqual(restore.status_code, 200, restore.text)
        self.assertEqual(restore.json()["verification"]["status"], "success")
        refreshed_metrics = client.get("/metrics", headers=admin)
        self.assertNotIn("wiki_memory_restore_last_success_age_seconds -1.0", refreshed_metrics.text)
        replication = client.post(
            "/v1/replication/status",
            headers=member,
            json={"clientId": "a" * 64, "pullCursor": 0, "outboxPending": 3},
        )
        self.assertEqual(replication.status_code, 200, replication.text)
        fingerprint_hijack = client.post(
            "/v1/replication/status",
            headers=curator,
            json={"clientId": "a" * 64, "pullCursor": 99, "outboxPending": 0},
        )
        self.assertEqual(fingerprint_hijack.status_code, 409, fingerprint_hijack.text)
        replication_metrics = client.get("/metrics", headers=admin)
        self.assertIn("wiki_memory_replication_clients_active 1.0", replication_metrics.text)
        self.assertIn("wiki_memory_replication_outbox_pending 3.0", replication_metrics.text)

        captured = client.post(
            "/v1/captures",
            headers=member,
            json={
                "text": "teamapitoken",
                "spaceId": self.space,
                "scope": "team",
                "idempotencyKey": f"api-team-capture-{self.run_id}",
            },
        )
        self.assertEqual(captured.status_code, 200, captured.text)
        captured_reference = captured.json()["event"]["evidenceRefs"][0]
        reused_event = MemoryEvent(
            event_type="source.captured",
            stream_id=f"source:{other_space}:reused-{self.run_id}",
            stream_version=1,
            idempotency_key=f"api-reused-evidence-{self.run_id}",
            actor=EventActor("user", "api-cross-space"),
            plugin=PluginRef("integration", "1.0.0"),
            scope="team",
            space_id=other_space,
            evidence_refs=[captured_reference],
            acl=normalize_acl({}, owner="api-cross-space", space_id=other_space),
            payload={"vault": shared_vault_slug(other_space), "body": "attempted evidence reuse"},
        )
        unauthorized_reuse = client.post(
            "/v1/events:append", headers=cross_space, json={"events": [reused_event.to_dict()]}
        )
        self.assertEqual(unauthorized_reuse.status_code, 403, unauthorized_reuse.text)
        readable_cross_space_reuse = MemoryEvent.from_dict(reused_event.to_dict())
        readable_cross_space_reuse.event_id = uuid7()
        readable_cross_space_reuse.idempotency_key = f"api-readable-reuse-{self.run_id}"
        readable_cross_space_reuse.actor = EventActor("user", "api-multi-space")
        readable_cross_space_reuse.acl = normalize_acl({}, owner="api-multi-space", space_id=other_space)
        acl_widening_reuse = client.post(
            "/v1/events:append", headers=multi_space, json={"events": [readable_cross_space_reuse.to_dict()]}
        )
        self.assertEqual(acl_widening_reuse.status_code, 409, acl_widening_reuse.text)
        self.repository.run_jobs_once(100)
        self.assertEqual(len(client.post("/v1/search", headers=member, json={"query": "teamapitoken"}).json()["results"]), 1)
        self.assertEqual(client.post("/v1/search", headers=outsider, json={"query": "teamapitoken"}).json()["results"], [])

        proposed = client.post(
            "/v1/captures",
            headers=member,
            json={
                "text": "organizationonlytoken",
                "spaceId": self.space,
                "scope": "organization",
                "idempotencyKey": f"api-organization-capture-{self.run_id}",
            },
        )
        self.assertEqual(proposed.status_code, 200, proposed.text)
        proposal_event = proposed.json()["event"]
        self.assertEqual(proposal_event["eventType"], "source.publication.proposed")
        self.assertEqual(proposal_event["scope"], "team")
        self.assertEqual(proposal_event["acl"]["audience"], "space")
        self.assertEqual(proposal_event["payload"]["publicationTarget"]["scope"], "organization")
        proposal_digest = proposal_event["evidenceRefs"][0].split(":", 1)[1]
        legacy_organization_proposal = MemoryEvent.from_dict(proposal_event)
        legacy_organization_proposal.event_id = uuid7()
        legacy_organization_proposal.idempotency_key = f"legacy-organization-proposal-{self.run_id}"
        legacy_organization_proposal.scope = "organization"
        legacy_organization_proposal.acl = proposal_event["payload"]["publicationTarget"]["acl"]
        legacy_attempt = client.post(
            "/v1/events:append", headers=member, json={"events": [legacy_organization_proposal.to_dict()]}
        )
        self.assertEqual(legacy_attempt.status_code, 409, legacy_attempt.text)
        outsider_events = client.get("/v1/events", headers=outsider).json()["events"]
        self.assertNotIn(proposal_event["eventId"], {event["eventId"] for event in outsider_events})
        self.assertEqual(client.get(f"/v1/blobs/{proposal_digest}", headers=outsider).status_code, 403)
        self.repository.run_jobs_once(100)
        self.assertEqual(
            client.post("/v1/search", headers=member, json={"query": "organizationonlytoken"}).json()["results"],
            [],
        )
        reviewed = client.post(
            f"/v1/proposals/{proposal_event['eventId']}/review",
            headers=curator,
            json={"decision": "accept", "reason": "synthetic approval"},
        )
        self.assertEqual(reviewed.status_code, 200, reviewed.text)
        self.assertEqual(reviewed.json()["event"]["eventType"], "source.published")
        self.assertEqual(reviewed.json()["event"]["scope"], "organization")
        self.assertEqual(reviewed.json()["event"]["acl"]["audience"], "organization")
        reviewed_retry = client.post(
            f"/v1/proposals/{proposal_event['eventId']}/review",
            headers=curator,
            json={"decision": "accept", "reason": "synthetic approval"},
        )
        self.assertEqual(reviewed_retry.status_code, 200, reviewed_retry.text)
        self.assertFalse(reviewed_retry.json()["created"])
        self.repository.run_jobs_once(100)
        self.assertEqual(
            len(client.post("/v1/search", headers=member, json={"query": "organizationonlytoken"}).json()["results"]),
            1,
        )
        self.assertEqual(
            len(
                client.post(
                    "/v1/search", headers=outsider, json={"query": "organizationonlytoken"}
                ).json()["results"]
            ),
            1,
        )
        self.assertEqual(client.get(f"/v1/blobs/{proposal_digest}", headers=outsider).status_code, 200)
        integrity = self.repository.verify_integrity(store.verify)
        self.assertTrue(integrity["ok"], integrity)
        rebuilt = client.post("/v1/operations/rebuild-search", headers=admin)
        self.assertEqual(rebuilt.status_code, 200, rebuilt.text)
        self.assertGreaterEqual(rebuilt.json()["rebuild"]["documents"], 1)
        self.assertEqual(
            len(client.post("/v1/search", headers=member, json={"query": "teamapitoken"}).json()["results"]),
            1,
        )
        digest = captured_reference.split(":", 1)[1]
        blob_path = store._path(digest)
        blob_path.write_bytes(b"x" * blob_path.stat().st_size)
        self.assertEqual(client.get(f"/v1/blobs/{digest}", headers=member).status_code, 404)
        corrupted_search = client.post(
            "/v1/search", headers=member, json={"query": "teamapitoken"}
        )
        self.assertEqual(corrupted_search.status_code, 200, corrupted_search.text)
        self.assertEqual(corrupted_search.json()["results"], [])
        self.assertGreater(corrupted_search.json()["withheldUnverifiableEvidence"], 0)

    def test_team_client_sync_stages_organization_publication_until_review(self) -> None:
        """Private → outbox → Team proposal → curator promotion is one chain."""

        from fastapi.testclient import TestClient

        space = f"{self.space}-publication"

        class Tokens:
            def verify(self, token: str) -> Principal:
                principals = {
                    "member": Principal("member", frozenset({Role.CONTRIBUTOR, Role.READER}), frozenset({space})),
                    "curator": Principal("curator", frozenset({Role.CURATOR, Role.READER}), frozenset({space})),
                    "outsider": Principal("outsider", frozenset({Role.READER}), frozenset()),
                }
                return principals[token]

        api = TestClient(create_app(self.repository, self.object_store, Tokens()))

        class InProcessTeamClient(TeamClient):
            def _request(self, method, path, *, body=None, content_type=None):
                headers = {"Authorization": "Bearer member"}
                if content_type:
                    headers["Content-Type"] = content_type
                response = api.request(method, path, content=body, headers=headers)
                if method == "HEAD" and response.status_code == 404:
                    return 404, b"", dict(response.headers)
                if response.status_code >= 400:
                    raise MemoryError(f"in-process Team returned HTTP {response.status_code}: {response.text}")
                return response.status_code, response.content, dict(response.headers)

        local_root = Path(self.temporary.name) / f"publication-client-{self.run_id}"
        init_memory(
            local_root,
            {
                "name": "Publication client",
                "language": "en",
                "sync_enabled": False,
                "vaults": [{"slug": "knowledge", "title": "Knowledge", "purpose": "Synthetic tests"}],
            },
        )
        local = MemoryEngine(local_root)
        evidence = local.evidence.put_bytes(b"private organization publication", media_type="text/plain")
        private = MemoryEvent(
            event_type="source.captured",
            stream_id=f"private-source:{self.run_id}",
            idempotency_key=f"private-source-{self.run_id}",
            actor=EventActor("user", "member"),
            plugin=PluginRef("integration", "1.0.0"),
            scope="private",
            space_id="local-owner",
            evidence_refs=[evidence.reference],
            payload={"vault": "knowledge", "sourceId": f"private-{self.run_id}", "body": "private organization publication"},
        )
        local.append(private, enqueue=False)
        preview = publication_preview(
            local,
            private.event_id,
            destination_scope="organization",
            destination_space=space,
            principal_id="member",
        )
        staged = publish_private_event(
            local,
            private.event_id,
            principal_id="member",
            destination_scope="organization",
            destination_space=space,
            preview_hash=preview["previewHash"],
        )
        self.assertEqual(staged["event"]["scope"], "team")

        sync = InProcessTeamClient(local, "https://team.invalid", lambda: "member")
        first = sync.sync()
        self.assertTrue(first["ok"], first)
        proposal_id = staged["event"]["eventId"]
        proposal = self.repository.get_event(proposal_id)
        assert proposal is not None
        self.assertEqual(proposal.scope, "team")
        digest = proposal.evidence_refs[0].split(":", 1)[1]
        outsider = {"Authorization": "Bearer outsider"}
        self.assertNotIn(proposal_id, {item["eventId"] for item in api.get("/v1/events", headers=outsider).json()["events"]})
        self.assertEqual(api.get(f"/v1/blobs/{digest}", headers=outsider).status_code, 403)

        review = api.post(
            f"/v1/proposals/{proposal_id}/review",
            headers={"Authorization": "Bearer curator"},
            json={"decision": "accept", "reason": "synthetic curation"},
        )
        self.assertEqual(review.status_code, 200, review.text)
        self.assertEqual(review.json()["event"]["scope"], "organization")
        self.assertNotEqual(review.json()["event"]["streamId"], staged["event"]["streamId"])
        self.assertEqual(review.json()["event"]["streamVersion"], 1)
        repeated_review = api.post(
            f"/v1/proposals/{proposal_id}/review",
            headers={"Authorization": "Bearer curator"},
            json={"decision": "accept", "reason": "synthetic curation retry"},
        )
        self.assertEqual(repeated_review.status_code, 200, repeated_review.text)
        self.assertFalse(repeated_review.json()["created"])
        self.assertEqual(repeated_review.json()["event"]["eventId"], review.json()["event"]["eventId"])
        remaining = api.get("/v1/proposals", headers={"Authorization": "Bearer curator"})
        self.assertEqual(remaining.status_code, 200, remaining.text)
        self.assertNotIn(proposal_id, {item["eventId"] for item in remaining.json()["proposals"]})
        second = sync.sync()
        self.assertTrue(second["ok"], second)
        accepted_local = local.events.get(review.json()["event"]["eventId"])
        assert accepted_local is not None
        self.assertEqual(accepted_local.scope, "organization")
        self.assertEqual(api.get(f"/v1/blobs/{digest}", headers=outsider).status_code, 200)

    def test_team_client_sync_accepts_official_service_connector(self) -> None:
        """An approved connector identity can durably replicate its own local outbox.

        This covers the route used by a `team-client` source plugin: source evidence
        is first committed locally, then its service OIDC identity uploads the blob
        and event.  A human token must not be able to impersonate that actor (tested
        separately above); this test proves the legitimate path remains usable.
        """

        from fastapi.testclient import TestClient

        space = f"{self.space}-service-sync"

        class Tokens:
            def verify(self, token: str) -> Principal:
                if token != "connector-service":
                    raise KeyError(token)
                return Principal(
                    "connector-service",
                    frozenset({Role.SERVICE}),
                    frozenset({space}),
                    kind="service",
                )

        api = TestClient(create_app(self.repository, self.object_store, Tokens()))

        class InProcessTeamClient(TeamClient):
            def _request(self, method, path, *, body=None, content_type=None):
                headers = {"Authorization": "Bearer connector-service"}
                if content_type:
                    headers["Content-Type"] = content_type
                response = api.request(method, path, content=body, headers=headers)
                if method == "HEAD" and response.status_code == 404:
                    return 404, b"", dict(response.headers)
                if response.status_code >= 400:
                    raise MemoryError(f"in-process Team returned HTTP {response.status_code}: {response.text}")
                return response.status_code, response.content, dict(response.headers)

        local_root = Path(self.temporary.name) / f"service-client-{self.run_id}"
        init_memory(
            local_root,
            {
                "name": "Service connector client",
                "language": "en",
                "sync_enabled": False,
                "vaults": [{"slug": "knowledge", "title": "Knowledge", "purpose": "Synthetic tests"}],
            },
        )
        local = MemoryEngine(local_root)
        vault = ensure_team_vault(local_root, space)
        evidence = local.evidence.put_bytes(b"official connector sync", media_type="text/plain")
        source_event = MemoryEvent(
            event_type="source.captured",
            stream_id=f"connector:{self.run_id}:replication",
            idempotency_key=f"connector-replication-{self.run_id}",
            actor=EventActor("connector", "connector-service"),
            plugin=PluginRef("source-postgres", "1.0.0"),
            scope="team",
            space_id=space,
            evidence_refs=[evidence.reference],
            acl=normalize_acl({}, owner="connector-service", space_id=space),
            payload={"vault": vault, "sourceId": f"postgres-{self.run_id}", "body": "official connector sync"},
        )
        persisted, created = local.append(source_event)
        self.assertTrue(created)
        self.assertEqual(local.events.outbox_status_counts(), {"pending": 1})

        report = InProcessTeamClient(local, "https://team.invalid", lambda: "connector-service").sync()
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["pushed"], 1)
        self.assertTrue(report["heartbeatReported"], report)
        self.assertEqual(local.events.outbox_status_counts(), {"accepted": 1})
        remote = self.repository.get_event(persisted.event_id)
        assert remote is not None
        self.assertEqual(remote.actor, EventActor("connector", "connector-service"))
        self.assertEqual(remote.plugin, PluginRef("source-postgres", "1.0.0"))
        self.assertTrue(self.object_store.has(evidence.reference.split(":", 1)[1]))

    def test_postgres_source_refuses_privileged_role_and_replays_overlap_idempotently(self) -> None:
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo

        admin_dsn = os.environ["TEST_DATABASE_URL"]
        schema = f"wm_source_{self.run_id}"
        table = "records"
        table_name = f"{schema}.{table}"
        role = f"wm_ro_{self.run_id}"
        password = "synthetic-read-only-password"
        with psycopg.connect(admin_dsn) as connection:
            connection.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(password)
                )
            )
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            connection.execute(
                sql.SQL(
                    "CREATE TABLE {}.{} (id bigint PRIMARY KEY, updated_at timestamptz NOT NULL, value text NOT NULL)"
                ).format(sql.Identifier(schema), sql.Identifier(table))
            )
            connection.execute(
                sql.SQL("INSERT INTO {}.{} VALUES (%s,%s,%s),(%s,%s,%s)").format(
                    sql.Identifier(schema), sql.Identifier(table)
                ),
                (
                    1,
                    "2026-01-01T00:00:00Z",
                    "one",
                    2,
                    "2026-01-01T00:01:00Z",
                    "two",
                ),
            )
            connection.execute(
                sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                    sql.Identifier(schema), sql.Identifier(role)
                )
            )
            connection.execute(
                sql.SQL("GRANT SELECT ON {}.{} TO {}").format(
                    sql.Identifier(schema), sql.Identifier(table), sql.Identifier(role)
                )
            )

        config = {
            "schemas": [schema],
            "tables": [table_name],
            "columns": {table_name: ["id", "updated_at", "value"]},
        }
        privileged = PostgresSourceConnector(admin_dsn, allowlist=config)
        self.assertFalse(asyncio.run(privileged.check(config, {})).ok)

        read_only_dsn = make_conninfo(admin_dsn, user=role, password=password)
        connector = PostgresSourceConnector(read_only_dsn, allowlist=config)
        self.assertTrue(asyncio.run(connector.check(config, {})).ok)
        selection = SourceSelection(
            {
                table_name: {
                    "columns": ["id", "updated_at", "value"],
                    "primaryKey": ["id"],
                    "updatedAt": "updated_at",
                    "overlapSeconds": 120,
                }
            }
        )
        memory_root = Path(self.temporary.name) / f"source-memory-{self.run_id}"
        init_memory(
            memory_root,
            {
                "name": "Synthetic source",
                "language": "en",
                "sync_enabled": False,
                "vaults": [{"slug": "knowledge", "title": "Knowledge", "purpose": "Tests"}],
            },
        )
        # The public generic CLI activates this optional official plugin by
        # manifest. This prevents Core from growing one command per database.
        generic_root = Path(self.temporary.name) / f"generic-source-memory-{self.run_id}"
        init_memory(
            generic_root,
            {
                "name": "Generic PostgreSQL source",
                "language": "en",
                "sync_enabled": False,
                "vaults": [{"slug": "knowledge", "title": "Knowledge", "purpose": "Tests"}],
            },
        )
        config_path = Path(self.temporary.name) / f"generic-postgres-config-{self.run_id}.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        selection_path = Path(self.temporary.name) / f"generic-postgres-selection-{self.run_id}.json"
        selection_path.write_text(json.dumps({"streams": selection.streams}), encoding="utf-8")
        manifest_path = ROOT / "src" / "wiki_memory" / "plugin_catalog" / "source-postgres" / "plugin.yaml"
        parser = build_parser()
        generic_base = [
            str(generic_root),
            "--manifest", str(manifest_path),
            "--config", str(config_path),
            "--secret-env", "POSTGRES_DSN=GENERIC_POSTGRES_DSN",
        ]
        with patch.dict(os.environ, {"GENERIC_POSTGRES_DSN": read_only_dsn}):
            generic_check = run_cli(parser.parse_args(["connector-check", *generic_base]))
            self.assertTrue(generic_check["ok"])
            generic_sync = run_cli(
                parser.parse_args(
                    [
                        "connector-sync", *generic_base,
                        "--selection", str(selection_path), "--vault", "knowledge", "--instance", f"generic-postgres-{self.run_id}",
                    ]
                )
            )
        self.assertEqual((generic_sync["records"], generic_sync["checkpoints"]), (2, 1))
        runtime = SourceIngestionRuntime(MemoryEngine(memory_root))
        first = asyncio.run(
            runtime.run(
                connector,
                connector_instance_id=f"postgres-{self.run_id}",
                selection=selection,
                vault="knowledge",
            )
        )
        replay = asyncio.run(
            runtime.run(
                connector,
                connector_instance_id=f"postgres-{self.run_id}",
                selection=selection,
                vault="knowledge",
            )
        )
        self.assertEqual(first.records, 2)
        self.assertEqual(replay.records, 0)

        with psycopg.connect(admin_dsn) as connection:
            connection.execute(
                sql.SQL("INSERT INTO {}.{} VALUES (%s,%s,%s)").format(
                    sql.Identifier(schema), sql.Identifier(table)
                ),
                (3, "2026-01-01T00:02:00Z", "three"),
            )
        resumed = asyncio.run(
            runtime.run(
                connector,
                connector_instance_id=f"postgres-{self.run_id}",
                selection=selection,
                vault="knowledge",
            )
        )
        self.assertEqual(resumed.records, 1)


if __name__ == "__main__":
    unittest.main()
