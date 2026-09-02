from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wiki_memory.audio import AudioIngestor, MistralTranscriber
from wiki_memory.backup import create_backup, restore_backup, verify_backup
from wiki_memory.capture import capture_item
from wiki_memory.cli import build_parser, run as run_cli
from wiki_memory.config import MemoryError
from wiki_memory.contracts import (
    CheckResult,
    ConnectorSpec,
    SourceCatalog,
    SourceConnector,
    SourceMessage,
    SourceSelection,
    Transcript,
    TranscriptSegment,
    TranscriptionProvider,
)
from wiki_memory.engine import MemoryEngine
from wiki_memory.events import EventActor, MemoryEvent, PluginRef
from wiki_memory.ingestion import SourceIngestionRuntime
from wiki_memory.layout import init_memory
from wiki_memory.operations import (
    capture_projection_edits,
    publication_preview,
    publish_private_event,
    propose_assertion,
    review_local_proposal,
    review_projection_edit,
)
from wiki_memory.oidc import OIDCConfig, OIDCVerifier
from wiki_memory.plugins import PluginManager, PluginManifest, PluginState, RemoteSourceConnector
from wiki_memory.plugin_signatures import signing_payload, team_signature_verifier_from_environment
from wiki_memory.postgres_source import DebeziumMessageAdapter, PostgresSourceConnector
from wiki_memory.profiles import profile_report, verify_official_catalog
from wiki_memory.replication import export_event_pack, import_event_pack
from wiki_memory.search import query_memory
from wiki_memory.social_source import SocialBrowserConnector
from wiki_memory.team import (
    Principal,
    Role,
    TeamClient,
    can_read,
    derive_acl,
    detach_team,
    normalize_acl,
    team_session_path,
)

sys.path.insert(0, str(ROOT / "scripts"))
from crash_campaign import run_campaign


def spec() -> dict:
    return {
        "name": "Synthetic V1",
        "language": "en",
        "sync_enabled": False,
        "vaults": [{"slug": "knowledge", "title": "Knowledge", "purpose": "Synthetic tests"}],
    }


class FakeConnector(SourceConnector):
    async def spec(self) -> ConnectorSpec:
        return ConnectorSpec("source-fake", "Fake", {"type": "object"})

    async def check(self, config: dict, secret_handles: dict) -> CheckResult:
        return CheckResult(True, "ok")

    async def discover(self, config: dict) -> SourceCatalog:
        return SourceCatalog(())

    async def read(self, selection: SourceSelection, cursor, signal=None):
        yield SourceMessage(
            type="record",
            stream="records",
            emitted_at="2026-01-01T00:00:00Z",
            source_id="1",
            source_version="v1",
            payload={"id": 1, "value": "synthetic"},
        )
        yield SourceMessage(type="checkpoint", stream="records", emitted_at="2026-01-01T00:00:01Z", cursor={"id": 1})


class FakeRemoteSourceService:
    async def call(self, method: str, params: dict | None = None):
        if method == "spec":
            return {"id": "source-isolated", "displayName": "Isolated", "configSchema": {"type": "object"}}
        if method == "check":
            return {"ok": True, "message": "ok"}
        if method == "discover":
            return {"streams": [{"name": "records", "schema": {"type": "object"}, "primaryKey": ["id"]}]}
        if method == "read":
            return {
                "messages": [
                    {
                        "type": "record",
                        "stream": "records",
                        "emittedAt": "2026-01-01T00:00:00Z",
                        "sourceId": "isolated-1",
                        "payload": {"id": "isolated-1"},
                    },
                    {"type": "checkpoint", "stream": "records", "emittedAt": "2026-01-01T00:00:01Z", "cursor": {"id": 1}},
                ]
            }
        raise AssertionError(method)


class FakePagedRemoteSourceService:
    def __init__(self) -> None:
        self.cursors: list[object] = []

    async def call(self, method: str, params: dict | None = None):
        if method != "read":
            raise AssertionError(method)
        params = params or {}
        self.cursors.append(params.get("cursor"))
        if len(self.cursors) == 1:
            return {
                "messages": [
                    {"type": "record", "stream": "records", "emittedAt": "2026-01-01T00:00:00Z", "sourceId": "one", "payload": {"id": 1}},
                    {"type": "checkpoint", "stream": "records", "emittedAt": "2026-01-01T00:00:01Z", "cursor": {"offset": 1}},
                ],
                "done": False,
            }
        return {
            "messages": [
                {"type": "record", "stream": "records", "emittedAt": "2026-01-01T00:00:02Z", "sourceId": "two", "payload": {"id": 2}},
                {"type": "checkpoint", "stream": "records", "emittedAt": "2026-01-01T00:00:03Z", "cursor": {"offset": 2}},
            ],
            "done": True,
        }


class FakeTranscriber(TranscriptionProvider):
    id = "transcriber.fake"

    def __init__(self, model: str):
        self.model = model

    def capabilities(self):
        return {"diarization": False, "segmentTimestamps": True, "maxSeconds": 3600}

    async def transcribe(self, audio_path: str, *, language, diarize, timestamp_granularity, context_bias):
        return Transcript(
            text="Synthetic transcript",
            language="en",
            provider=self.id,
            model=self.model,
            segments=(TranscriptSegment(0.0, 1.0, "Synthetic transcript"),),
            settings={"temperature": 0},
            diarized=False,
        )


class FakeDecoder:
    def split(self, path: Path, maximum_seconds: int, directory: Path):
        return [(path, 0.0)]


class V1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "memory"
        init_memory(self.root, spec())

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_event_requires_existing_evidence_and_is_immutable(self) -> None:
        engine = MemoryEngine(self.root)
        event = MemoryEvent(
            event_type="test.recorded",
            stream_id="test:1",
            idempotency_key="missing-evidence",
            actor=EventActor("system", "test"),
            plugin=PluginRef("test", "1.0.0"),
            evidence_refs=["sha256:" + "0" * 64],
            payload={},
        )
        with self.assertRaises(MemoryError):
            engine.append(event)
        evidence = engine.evidence.put_bytes(b"synthetic", media_type="text/plain")
        event.evidence_refs = [evidence.reference]
        persisted, created = engine.append(event)
        self.assertTrue(created)
        self.assertTrue(engine.evidence.verify(evidence.reference))
        with self.assertRaises(sqlite3.DatabaseError):
            with engine.events.connect() as connection:
                connection.execute("UPDATE events SET event_type='changed' WHERE event_id=?", (persisted.event_id,))

    def test_verify_detects_referenced_evidence_without_metadata_sidecar(self) -> None:
        engine = MemoryEngine(self.root)
        evidence = engine.evidence.put_bytes(b"synthetic", media_type="text/plain")
        engine.append(
            MemoryEvent(
                event_type="test.recorded",
                stream_id="test:metadata",
                idempotency_key="metadata-sidecar",
                actor=EventActor("system", "test"),
                plugin=PluginRef("test", "1.0.0"),
                evidence_refs=[evidence.reference],
                payload={},
            )
        )
        engine.evidence.path(evidence.reference).with_suffix(".metadata.json").unlink()
        with self.assertRaises(MemoryError):
            engine.append(
                MemoryEvent(
                    event_type="test.recorded",
                    stream_id="test:metadata-new-reference",
                    idempotency_key="metadata-sidecar-new-reference",
                    actor=EventActor("system", "test"),
                    plugin=PluginRef("test", "1.0.0"),
                    evidence_refs=[evidence.reference],
                    payload={},
                )
            )
        verification = engine.verify()
        self.assertFalse(verification["ok"])
        self.assertEqual(verification["corruptEvidence"], [evidence.reference])

    def test_projection_refuses_corrupt_canonical_evidence(self) -> None:
        engine = MemoryEngine(self.root)
        evidence = engine.evidence.put_bytes(b"canonical projection proof", media_type="text/plain")
        event = MemoryEvent(
            event_type="source.captured",
            stream_id="source:knowledge:corrupt-projection",
            idempotency_key="corrupt-projection-proof",
            actor=EventActor("system", "test"),
            plugin=PluginRef("test", "1.0.0"),
            evidence_refs=[evidence.reference],
            payload={
                "sourceId": "corrupt-projection",
                "vault": "knowledge",
                "partition": "tests",
                "title": "Corrupt evidence must not project",
                "body": "This must never become derived Markdown.",
            },
        )
        engine.append(event, project=False)
        blob = engine.evidence.path(evidence.reference)
        blob.write_bytes(b"x" * blob.stat().st_size)

        result = engine.projections.update("projection.markdown")

        self.assertIsNotNone(result.error)
        self.assertIn(evidence.reference, result.error or "")
        self.assertIsNone(engine.events.projection_checkpoint("projection.markdown"))
        self.assertFalse((self.root / "knowledge" / "01-Sources" / "items" / "tests" / "corrupt-projection.md").exists())

    def test_query_withholds_existing_projection_when_its_evidence_is_corrupt(self) -> None:
        captured = capture_item(
            self.root,
            "knowledge",
            source_type="note",
            title="Verifiable search",
            text="needle only backed by canonical evidence",
        )
        engine = MemoryEngine(self.root)
        reference = captured["evidence_refs"][0]
        blob = engine.evidence.path(reference)
        blob.write_bytes(b"x" * blob.stat().st_size)

        result = query_memory(self.root, "needle")

        self.assertEqual(result["results"], [])
        self.assertIn(
            "unverifiable-evidence",
            {item["reason"] for item in result["excluded_stale_facts"]},
        )

    def test_accepted_assertion_requires_evidence_at_ledger_boundary(self) -> None:
        engine = MemoryEngine(self.root)
        unsourced = MemoryEvent(
            event_type="assertion.accepted",
            stream_id="assertion:local-owner:unsourced",
            idempotency_key="unsourced-accepted-assertion",
            actor=EventActor("user", "local-owner"),
            plugin=PluginRef("test", "1.0.0"),
            payload={"assertionId": "unsourced", "vault": "knowledge"},
        )
        with self.assertRaisesRegex(MemoryError, "Accepted assertions require evidence"):
            engine.append(unsourced)

        procedural = MemoryEvent(
            event_type="assertion.accepted",
            stream_id="assertion:local-owner:procedure",
            idempotency_key="procedural-accepted-assertion",
            actor=EventActor("user", "local-owner"),
            plugin=PluginRef("test", "1.0.0"),
            payload={"assertionId": "procedure", "vault": "knowledge", "kind": "procedural"},
        )
        accepted, created = engine.append(procedural)
        self.assertTrue(created)
        self.assertEqual(accepted.event_type, "assertion.accepted")

    def test_idempotency_rejects_different_content(self) -> None:
        engine = MemoryEngine(self.root)
        base = dict(
            event_type="test.recorded",
            stream_id="test:idempotence",
            idempotency_key="same-key",
            actor=EventActor("system", "test"),
            plugin=PluginRef("test", "1.0.0"),
        )
        engine.append(MemoryEvent(payload={"value": 1}, **base))
        with self.assertRaises(MemoryError):
            engine.append(MemoryEvent(payload={"value": 2}, **base))

    def test_untrusted_event_shapes_raise_memory_error_not_python_type_errors(self) -> None:
        valid = MemoryEvent(
            event_type="test.recorded",
            stream_id="test:untrusted-shape",
            idempotency_key="untrusted-shape",
            actor=EventActor("system", "test"),
            plugin=PluginRef("test", "1.0.0"),
            payload={},
        ).to_dict()
        malformed = []
        for field, value in (
            ("actor", []),
            ("plugin", "not-an-object"),
            ("payload", ["not", "an", "object"]),
            ("acl", ["not", "an", "object"]),
            ("evidenceRefs", None),
            ("occurredAt", ["not", "a", "timestamp"]),
        ):
            candidate = dict(valid)
            candidate[field] = value
            malformed.append(candidate)
        malformed.append({"eventType": "test.recorded"})

        for candidate in malformed:
            with self.subTest(candidate=candidate):
                with self.assertRaises(MemoryError):
                    MemoryEvent.from_dict(candidate)

    def test_concurrent_solo_captures_leave_projection_and_checkpoint_consistent(self) -> None:
        def capture(index: int):
            return capture_item(
                self.root,
                "knowledge",
                source_type="note",
                text=f"Concurrent synthetic memory {index}",
            )

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(capture, range(12)))
        engine = MemoryEngine(self.root)
        self.assertEqual(engine.events.count(), 12)
        self.assertTrue(engine.verify()["ok"])
        self.assertEqual(len({result["event_id"] for result in results}), 12)
        for result in results:
            self.assertTrue(Path(result["path"]).is_file())

    def test_kill9_campaign_never_loses_acknowledged_event_or_evidence(self) -> None:
        result = run_campaign(
            self.root,
            rounds=8,
            events_per_worker=8,
            delay=0.01,
            seed=17,
        )
        self.assertTrue(result["ok"])
        self.assertGreater(result["terminatedWorkers"], 0)
        self.assertGreater(result["acknowledgedEvents"], 0)
        self.assertGreaterEqual(result["ledgerEvents"], result["acknowledgedEvents"])

    def test_markdown_edit_becomes_evidence_and_survives_rebuild(self) -> None:
        captured = capture_item(self.root, "knowledge", source_type="note", text="Generated content")
        path = Path(captured["path"])
        edited = path.read_text(encoding="utf-8") + "\nHuman correction.\n"
        path.write_text(edited, encoding="utf-8")
        engine = MemoryEngine(self.root)
        detected = capture_projection_edits(engine)
        self.assertEqual(detected["modified"], 1)
        proposal = detected["proposals"][0]["event"]
        with self.assertRaises(MemoryError):
            engine.rebuild()
        review_projection_edit(
            engine,
            proposal_event_id=proposal["eventId"],
            actor_id="local-owner",
            decision="accept",
        )
        before = path.read_bytes()
        result = engine.rebuild()
        self.assertTrue(result["ok"])
        self.assertEqual(path.read_bytes(), before)
        self.assertIn(b"Human correction", before)

    def test_missing_markdown_projection_state_fails_closed_until_forced_rebuild(self) -> None:
        captured = capture_item(self.root, "knowledge", source_type="note", text="Rebuildable projection")
        path = Path(captured["path"])
        state = self.root / ".wiki-memory" / "projections" / "markdown-generated.sqlite3"
        self.assertTrue(state.is_file())
        for suffix in ("", "-wal", "-shm"):
            Path(str(state) + suffix).unlink(missing_ok=True)

        engine = MemoryEngine(self.root)
        with self.assertRaisesRegex(MemoryError, "state is missing"):
            engine.rebuild()
        # Recovery remains possible, but only through an operator's explicit
        # destructive acknowledgement after they have reviewed local files.
        rebuilt = engine.rebuild(force=True)
        self.assertTrue(rebuilt["ok"])
        self.assertIn("Rebuildable projection", path.read_text(encoding="utf-8"))

    def test_backup_restore_and_rebuild(self) -> None:
        capture_item(self.root, "knowledge", source_type="note", text="Backup evidence")
        # Profile activation creates a live operational SQLite database. Backup
        # must snapshot it through SQLite's API rather than copy its WAL bytes.
        profile_report(self.root, "solo")
        legacy_session = self.root / ".wiki-memory" / "team-session.json"
        legacy_session.write_text('{"principalId":"must-not-back-up"}', encoding="utf-8")
        archive = Path(self.temp.name) / "backup.tar.gz"
        self.assertTrue(create_backup(self.root, archive)["ok"])
        self.assertTrue(verify_backup(archive)["ok"])
        restored = Path(self.temp.name) / "restored"
        result = restore_backup(archive, restored)
        self.assertEqual(result["events"], 1)
        self.assertTrue(MemoryEngine(restored).verify()["ok"])
        self.assertTrue((restored / ".wiki-memory" / "data" / "plugin-versions.sqlite3").is_file())
        self.assertFalse((restored / ".wiki-memory" / "team-session.json").exists())

    def test_backup_refuses_unreviewed_projection_edits(self) -> None:
        captured = capture_item(self.root, "knowledge", source_type="note", text="Before edit")
        path = Path(captured["path"])
        path.write_text(path.read_text(encoding="utf-8") + "\nUnreviewed.\n", encoding="utf-8")
        with self.assertRaises(MemoryError):
            create_backup(self.root, Path(self.temp.name) / "unsafe-backup.tar.gz")

    def test_event_pack_is_idempotent_after_blobs_arrive(self) -> None:
        capture_item(self.root, "knowledge", source_type="note", text="Pack evidence")
        pack = Path(export_event_pack(self.root)["path"])
        target = Path(self.temp.name) / "target"
        init_memory(target, spec())
        shutil.copytree(
            self.root / ".wiki-memory" / "data" / "blobs",
            target / ".wiki-memory" / "data" / "blobs",
            dirs_exist_ok=True,
        )
        first = import_event_pack(target, pack)
        second = import_event_pack(target, pack)
        self.assertEqual(first["imported"], 1)
        self.assertEqual(second["duplicates"], 1)

    def test_event_pack_conflict_preserves_acl_without_copying_sensitive_payload(self) -> None:
        source = Path(self.temp.name) / "pack-source"
        target = Path(self.temp.name) / "pack-target"
        init_memory(source, spec())
        init_memory(target, spec())
        acl = normalize_acl(
            {"audience": "explicit", "classification": "restricted"},
            owner="member",
            space_id="marketing",
        )
        common = {
            "event_type": "test.recorded",
            "stream_id": "shared-conflicting-stream",
            "actor": EventActor("user", "member"),
            "plugin": PluginRef("synthetic", "1.0.0"),
            "scope": "team",
            "space_id": "marketing",
            "acl": acl,
        }
        MemoryEngine(source).append(
            MemoryEvent(idempotency_key="source-delivery", payload={"secret": "incoming"}, **common),
            enqueue=False,
        )
        MemoryEngine(target).append(
            MemoryEvent(idempotency_key="target-delivery", payload={"secret": "current"}, **common),
            enqueue=False,
        )
        pack = Path(export_event_pack(source)["path"])
        result = import_event_pack(target, pack)
        self.assertFalse(result["ok"])
        conflict = list(MemoryEngine(target).events.iter_events(event_types={"replication.conflict.detected"}))[0]
        self.assertEqual(conflict.scope, "team")
        self.assertEqual(conflict.acl, acl)
        self.assertNotIn("secret", json.dumps(conflict.payload))

    def test_connector_checkpoint_advances_after_record(self) -> None:
        engine = MemoryEngine(self.root)
        result = asyncio.run(
            SourceIngestionRuntime(engine).run(
                FakeConnector(),
                connector_instance_id="fake-1",
                selection=SourceSelection({"records": {}}),
                vault="knowledge",
            )
        )
        self.assertEqual(result.records, 1)
        self.assertEqual(engine.events.connector_checkpoint("fake-1", "records"), {"id": 1})
        event = next(engine.events.iter_events())
        self.assertTrue(event.evidence_refs)

    def test_audio_preserves_transcript_revision_as_evidence(self) -> None:
        audio = Path(self.temp.name) / "sample.wav"
        audio.write_bytes(b"synthetic-wave")
        engine = MemoryEngine(self.root)
        first = asyncio.run(
            AudioIngestor(engine, FakeTranscriber("model-a"), FakeDecoder()).ingest(
                audio, vault="knowledge", diarize=False
            )
        )
        second = asyncio.run(
            AudioIngestor(engine, FakeTranscriber("model-b"), FakeDecoder()).ingest(
                audio, vault="knowledge", diarize=False
            )
        )
        self.assertNotEqual(first["eventId"], second["eventId"])
        self.assertTrue(engine.evidence.has(first["transcriptEvidence"]))
        self.assertEqual(engine.events.stream_version("source:knowledge:" + first["sourceId"]), 3)

    def test_acl_intersection_never_widens(self) -> None:
        first = normalize_acl({"groups": ["g1", "g2"]}, owner="a", space_id="marketing")
        second = normalize_acl({"groups": ["g2", "g3"]}, owner="b", space_id="marketing")
        derived = derive_acl([first, second], owner="curator", destination_space="marketing")
        self.assertEqual(derived["groups"], ["g2"])
        allowed = Principal("reader", frozenset({Role.READER}), frozenset({"marketing"}), frozenset({"g2"}))
        denied = Principal("reader2", frozenset({Role.READER}), frozenset({"marketing"}), frozenset({"g1"}))
        self.assertTrue(can_read(allowed, scope="team", space_id="marketing", acl=derived))
        self.assertFalse(can_read(denied, scope="team", space_id="marketing", acl=derived))

    def test_team_pull_downloads_blob_before_event(self) -> None:
        content = b"remote proof"
        digest = __import__("hashlib").sha256(content).hexdigest()
        event = MemoryEvent(
            event_type="test.remote",
            stream_id="remote:1",
            stream_version=1,
            idempotency_key="remote-1",
            actor=EventActor("user", "member"),
            plugin=PluginRef("test", "1.0.0"),
            scope="team",
            space_id="marketing",
            acl=normalize_acl({}, owner="member", space_id="marketing"),
            evidence_refs=["sha256:" + digest],
            payload={"value": "synthetic"},
        )

        class Client(TeamClient):
            def _request(self, method, path, **kwargs):
                if path == "/v1/session":
                    session = {
                        "principalId": "member",
                        "kind": "user",
                        "roles": ["reader"],
                        "spaces": ["marketing"],
                        "groups": ["marketing"],
                        "checkedAt": "2026-01-01T00:00:00Z",
                    }
                    return 200, json.dumps(session).encode(), {}
                if path.startswith("/v1/events"):
                    return 200, json.dumps({"events": [event.to_dict()], "cursor": 7}).encode(), {}
                if path == f"/v1/blobs/{digest}":
                    return 200, content, {"Content-Type": "text/plain"}
                if path == "/v1/replication/status":
                    return 200, b'{"ok":true}', {}
                raise AssertionError((method, path))

        engine = MemoryEngine(self.root)
        result = Client(engine, "https://team.invalid", lambda: "synthetic-token").sync()
        self.assertEqual(result["pulled"], 1)
        self.assertTrue(engine.evidence.has("sha256:" + digest))
        self.assertEqual(engine.events.pending_outbox(), [])

    def test_publication_uses_isolated_team_vault_and_detach_is_read_only(self) -> None:
        captured = capture_item(self.root, "knowledge", source_type="note", text="Private first")
        engine = MemoryEngine(self.root)
        preview = publication_preview(
            engine,
            captured["event_id"],
            destination_scope="team",
            destination_space="marketing",
        )
        published = publish_private_event(
            engine,
            captured["event_id"],
            principal_id="local-owner",
            destination_scope="team",
            destination_space="marketing",
            preview_hash=preview["previewHash"],
        )
        self.assertEqual(published["event"]["payload"]["vault"], "team-marketing")
        self.assertTrue((self.root / "team-marketing" / "vault.yaml").is_file())
        with self.assertRaises(MemoryError):
            capture_item(self.root, "team-marketing", source_type="note", text="Private leak")
        detached = detach_team(self.root)
        self.assertEqual(detached["sharedVaultsReadOnly"], ["team-marketing"])
        with self.assertRaises(MemoryError):
            capture_item(self.root, "team-marketing", source_type="note", text="Must not write")

    def test_organization_publication_stays_team_scoped_until_curator_acceptance(self) -> None:
        captured = capture_item(self.root, "knowledge", source_type="note", text="Private organization candidate")
        engine = MemoryEngine(self.root)
        preview = publication_preview(
            engine,
            captured["event_id"],
            destination_scope="organization",
            destination_space="marketing",
        )
        self.assertEqual(preview["eventType"], "source.publication.proposed")
        self.assertEqual(preview["eventScope"], "team")
        self.assertEqual(preview["acl"]["audience"], "space")
        self.assertEqual(preview["payload"]["publicationTarget"]["acl"]["audience"], "organization")
        published = publish_private_event(
            engine,
            captured["event_id"],
            principal_id="local-owner",
            destination_scope="organization",
            destination_space="marketing",
            preview_hash=preview["previewHash"],
        )
        self.assertEqual(published["event"]["scope"], "team")
        self.assertEqual(published["event"]["acl"]["audience"], "space")

    def test_team_projection_is_hidden_after_entitlement_revocation(self) -> None:
        captured = capture_item(self.root, "knowledge", source_type="note", text="Private source")
        engine = MemoryEngine(self.root)
        preview = publication_preview(
            engine,
            captured["event_id"],
            destination_scope="team",
            destination_space="marketing",
            principal_id="owner",
        )
        publish_private_event(
            engine,
            captured["event_id"],
            principal_id="owner",
            destination_scope="team",
            destination_space="marketing",
            preview_hash=preview["previewHash"],
        )
        session_path = team_session_path(self.root)
        session_path.parent.mkdir(parents=True, exist_ok=True)
        session_path.write_text(
            json.dumps(
                {
                    "principalId": "member",
                    "kind": "user",
                    "roles": ["reader"],
                    "spaces": ["marketing"],
                    "groups": [],
                }
            ),
            encoding="utf-8",
        )
        authorized_results = query_memory(self.root, "Private source")["results"]
        self.assertTrue(any(item["file"].startswith("team-marketing/") for item in authorized_results))
        session_path.write_text(
            json.dumps(
                {
                    "principalId": "member",
                    "kind": "user",
                    "roles": ["reader"],
                    "spaces": [],
                    "groups": [],
                }
            ),
            encoding="utf-8",
        )
        results = query_memory(self.root, "Private source")["results"]
        self.assertTrue(all(not item["file"].startswith("team-marketing/") for item in results))

    def test_retracted_assertion_is_not_returned_as_current_knowledge(self) -> None:
        engine = MemoryEngine(self.root)
        evidence = engine.evidence.put_bytes(b"synthetic proof", media_type="text/plain")
        proposal = propose_assertion(
            engine,
            actor_id="local-owner",
            scope="private",
            space_id="local-owner",
            assertion={"vault": "knowledge", "title": "Retractable", "body": "Unique retractable fact"},
            evidence_refs=[evidence.reference],
        )["event"]
        accepted = review_local_proposal(
            engine,
            actor_id="local-owner",
            proposal_event_id=proposal["eventId"],
            decision="accept",
        )
        accepted_retry = review_local_proposal(
            engine,
            actor_id="local-owner",
            proposal_event_id=proposal["eventId"],
            decision="accept",
        )
        self.assertEqual(accepted_retry["eventId"], accepted["eventId"])
        assertion_id = accepted["payload"]["assertionId"]
        before = query_memory(self.root, "Unique retractable fact")["results"]
        self.assertTrue(any(assertion_id in item["file"] for item in before))
        review_local_proposal(
            engine,
            actor_id="local-owner",
            proposal_event_id=proposal["eventId"],
            decision="retract",
        )
        after = query_memory(self.root, "Unique retractable fact")["results"]
        self.assertFalse(any(assertion_id in item["file"] for item in after))

    def test_postgres_allowlist_and_debezium_delete(self) -> None:
        connector = PostgresSourceConnector(
            "postgresql://invalid",
            allowlist={"schemas": ["public"], "tables": ["public.allowed"], "columns": {"public.allowed": ["id"]}},
        )
        connector._validate_selection("public.allowed", {"columns": ["id"], "primaryKey": ["id"]})
        with self.assertRaises(MemoryError):
            connector._validate_selection("public.secret", {"columns": ["id"], "primaryKey": ["id"]})
        deleted = DebeziumMessageAdapter().parse(
            {"payload": {"op": "d", "before": {"id": 1}, "source": {"lsn": 42}, "ts_ms": 1000}, "key": {"id": 1}},
            "public.allowed",
        )
        self.assertEqual(deleted.type, "delete")
        self.assertEqual(deleted.cursor, {"lsn": 42})

    def test_social_browser_connector_uses_the_standard_durable_contract(self) -> None:
        capture = Path(self.temp.name) / "social-export.json"
        capture.write_text(
            json.dumps(
                {
                    "items": [
                        {
                            "connector": "reddit",
                            "source_url": "https://reddit.com/r/example/comments/contract",
                            "published_at": "2026-01-01T00:00:00Z",
                            "text": "A normalized, user-approved browser capture.",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        connector = SocialBrowserConnector(capture)

        async def run() -> None:
            check = await connector.check({}, {})
            self.assertTrue(check.ok)
            catalog = await connector.discover({})
            self.assertEqual(catalog.streams[0].name, "social.items")
            result = await SourceIngestionRuntime(MemoryEngine(self.root)).run(
                connector,
                connector_instance_id="browser-export",
                selection=SourceSelection({"social.items": {}}),
                vault="knowledge",
            )
            self.assertEqual((result.records, result.checkpoints), (1, 1))

        asyncio.run(run())
        events = list(MemoryEngine(self.root).events.iter_events())
        self.assertEqual(events[-1].event_type, "source.captured")
        self.assertEqual(events[-1].plugin.id, "source-social-browser")

    def test_generic_connector_cli_works_without_a_source_specific_command(self) -> None:
        capture = Path(self.temp.name) / "cli-social-export.json"
        capture.write_text(
            json.dumps(
                {"items": [{"connector": "reddit", "source_url": "https://reddit.com/r/example/comments/cli", "text": "CLI source"}]}
            ),
            encoding="utf-8",
        )
        config = Path(self.temp.name) / "cli-social-config.json"
        config.write_text(json.dumps({"inputPath": str(capture)}), encoding="utf-8")
        selection = Path(self.temp.name) / "cli-social-selection.json"
        selection.write_text(json.dumps({"streams": {"social.items": {}}}), encoding="utf-8")
        parser = build_parser()
        base = [str(self.root), "--plugin", "source-social-browser", "--config", str(config)]
        check = run_cli(parser.parse_args(["connector-check", *base]))
        self.assertTrue(check["ok"])
        catalog = run_cli(parser.parse_args(["connector-discover", *base]))
        self.assertEqual(catalog["streams"][0]["name"], "social.items")
        result = run_cli(
            parser.parse_args(
                [
                    "connector-sync", *base, "--selection", str(selection), "--vault", "knowledge", "--instance", "cli-social"
                ]
            )
        )
        self.assertEqual((result["records"], result["checkpoints"]), (1, 1))

    def test_isolated_source_adapter_preserves_the_source_contract(self) -> None:
        connector = RemoteSourceConnector(FakeRemoteSourceService())

        async def check() -> None:
            spec = await connector.spec()
            self.assertEqual(spec.id, "source-isolated")
            self.assertTrue((await connector.check({}, {})).ok)
            catalog = await connector.discover({})
            self.assertEqual(catalog.streams[0].primary_key, ("id",))
            messages = [item async for item in connector.read(SourceSelection({"records": {}}), None)]
            self.assertEqual([item.type for item in messages], ["record", "checkpoint"])

        asyncio.run(check())

    def test_isolated_source_adapter_only_pages_after_a_checkpoint(self) -> None:
        remote = FakePagedRemoteSourceService()
        connector = RemoteSourceConnector(remote)

        async def collect() -> list[SourceMessage]:
            return [item async for item in connector.read(SourceSelection({"records": {}}), None)]

        messages = asyncio.run(collect())
        self.assertEqual([item.source_id for item in messages if item.type == "record"], ["one", "two"])
        self.assertEqual(remote.cursors, [None, {"records": {"offset": 1}}])

    def test_generic_connector_cli_loads_an_explicit_solo_developer_plugin(self) -> None:
        module = types.ModuleType("wiki_memory_external_source_fixture")

        def activate(context, _config):
            context.provide("source.external-fixture", FakeConnector())

        module.activate = activate
        sys.modules[module.__name__] = module
        try:
            plugin_dir = Path(self.temp.name) / "external-source"
            plugin_dir.mkdir()
            (plugin_dir / "config.schema.json").write_text(
                json.dumps({"type": "object", "additionalProperties": False}), encoding="utf-8"
            )
            manifest = plugin_dir / "plugin.yaml"
            manifest.write_text(
                "\n".join(
                    [
                        "apiVersion: wiki-memory/v1",
                        "id: source-external-fixture",
                        "version: 1.0.0",
                        "minimumSdkVersion: 1.0.0",
                        "runtime: python",
                        "entrypoint: wiki_memory_external_source_fixture:activate",
                        "provides: [source.external-fixture]",
                        "requires: [events, evidence, projection.markdown]",
                        "permissions: {filesystem: [], network: [], secrets: []}",
                        "configSchema: config.schema.json",
                        "healthCheck: services",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            config = Path(self.temp.name) / "external-source-config.json"
            config.write_text("{}", encoding="utf-8")
            selection = Path(self.temp.name) / "external-source-selection.json"
            selection.write_text(json.dumps({"streams": {"records": {}}}), encoding="utf-8")
            with self.assertRaises(MemoryError):
                run_cli(
                    build_parser().parse_args(
                        [
                            "connector-check",
                            str(self.root),
                            "--manifest", str(manifest),
                            "--config", str(config),
                        ]
                    )
                )
            result = run_cli(
                build_parser().parse_args(
                    [
                        "connector-sync",
                        str(self.root),
                        "--manifest", str(manifest),
                        "--developer-mode",
                        "--config", str(config),
                        "--selection", str(selection),
                        "--vault", "knowledge",
                        "--instance", "external-fixture",
                    ]
                )
            )
            self.assertEqual((result["records"], result["checkpoints"]), (1, 1))
        finally:
            sys.modules.pop(module.__name__, None)

    def test_plugin_config_schema_is_enforced(self) -> None:
        manifest = PluginManifest.load(
            ROOT / "src" / "wiki_memory" / "plugin_catalog" / "source-postgres" / "plugin.yaml"
        )
        with self.assertRaises(MemoryError):
            manifest.validate_config({})

    def test_plugin_requiring_a_newer_sdk_is_rejected_before_execution(self) -> None:
        with self.assertRaises(MemoryError):
            PluginManifest.from_dict(
                {
                    "apiVersion": "wiki-memory/v1",
                    "id": "synthetic-future",
                    "version": "1.0.0",
                    "minimumSdkVersion": "99.0.0",
                    "runtime": "python",
                    "entrypoint": "synthetic:activate",
                    "provides": [],
                    "requires": [],
                    "permissions": {},
                    "configSchema": "config.schema.json",
                }
            )

    @unittest.skipUnless(importlib.util.find_spec("cryptography"), "cryptography is not installed")
    def test_team_plugin_signature_requires_both_valid_signature_and_admin_approval(self) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        raw = {
            "apiVersion": "wiki-memory/v1",
            "id": "source-signed-fixture",
            "version": "1.0.0",
            "minimumSdkVersion": "1.0.0",
            "runtime": "oci",
            "image": "registry.example.test/source@sha256:" + "b" * 64,
            "provides": ["source.signed-fixture"],
            "requires": [],
            "permissions": {},
            "configSchema": "../config-empty.schema.json",
            "signature": {"algorithm": "ed25519", "keyId": "team-fixture", "value": ""},
        }
        manifest = PluginManifest.from_dict(
            raw, ROOT / "src/wiki_memory/plugin_catalog/parser-docling/plugin.yaml"
        )
        raw["signature"]["value"] = base64.b64encode(private_key.sign(signing_payload(manifest))).decode("ascii")
        signed = PluginManifest.from_dict(raw, ROOT / "src/wiki_memory/plugin_catalog/parser-docling/plugin.yaml")
        trust_keys = json.dumps({"team-fixture": base64.b64encode(public_key).decode("ascii")})
        with patch.dict(
            os.environ,
            {
                "WIKI_MEMORY_TEAM_PLUGIN_TRUST_KEYS": trust_keys,
                "WIKI_MEMORY_TEAM_APPROVED_PLUGIN_IDS": json.dumps([signed.id]),
            },
            clear=False,
        ):
            verifier = team_signature_verifier_from_environment()
            self.assertTrue(verifier(signed))
            manager = PluginManager(require_signatures=True, signature_verifier=verifier)
            manager.add(signed, {})
            self.assertEqual(manager.fibers[signed.id].state, PluginState.DISCOVERED)
        with patch.dict(
            os.environ,
            {
                "WIKI_MEMORY_TEAM_PLUGIN_TRUST_KEYS": trust_keys,
                "WIKI_MEMORY_TEAM_APPROVED_PLUGIN_IDS": "[]",
            },
            clear=False,
        ):
            self.assertFalse(team_signature_verifier_from_environment()(signed))

    def test_plugin_startup_failure_cleans_up_lifo_and_filters_secrets(self) -> None:
        cleanup_order: list[str] = []
        observed_secrets: dict[str, str] = {}
        module = types.ModuleType("wiki_memory_synthetic_plugin")

        def activate(context, _config):
            observed_secrets.update(context.secret_handles)
            context.effect(lambda: (None, lambda: cleanup_order.append("first")))
            context.effect(lambda: (None, lambda: cleanup_order.append("second")))
            raise RuntimeError("synthetic startup failure")

        module.activate = activate
        sys.modules[module.__name__] = module
        try:
            manifest = PluginManifest.from_dict(
                {
                    "apiVersion": "wiki-memory/v1",
                    "id": "synthetic-cleanup",
                    "version": "1.0.0",
                    "minimumSdkVersion": "1.0.0",
                    "runtime": "python",
                    "entrypoint": f"{module.__name__}:activate",
                    "provides": [],
                    "requires": [],
                    "permissions": {"secrets": ["ALLOWED_TOKEN"]},
                    "configSchema": "../config-empty.schema.json",
                    "stopTimeoutSeconds": 1,
                },
                ROOT / "src/wiki_memory/plugin_catalog/parser-docling/plugin.yaml",
            )
            manager = PluginManager(trusted_plugins={manifest.id})
            manager.add(manifest, {})
            asyncio.run(
                manager.activate_all(
                    {"ALLOWED_TOKEN": "synthetic-handle", "UNDECLARED_TOKEN": "must-not-pass"}
                )
            )
            self.assertEqual(manager.fibers[manifest.id].state, PluginState.FAILED)
            self.assertEqual(cleanup_order, ["second", "first"])
            self.assertEqual(observed_secrets, {"ALLOWED_TOKEN": "synthetic-handle"})
        finally:
            sys.modules.pop(module.__name__, None)

    def test_executable_plugin_runs_out_of_process_with_capability_rpc(self) -> None:
        plugin_script = Path(self.temp.name) / "isolated_plugin.py"
        plugin_script.write_text(
            """import json
import sys

start = json.loads(sys.stdin.readline())
assert start[\"protocol\"] == \"wiki-memory-plugin-host/v1\"
assert start[\"secrets\"] == {\"EXAMPLE_TOKEN\": \"synthetic-secret\"}
print(json.dumps({\"protocol\": start[\"protocol\"], \"type\": \"ready\", \"provides\": [\"source.synthetic\", \"utility.synthetic\"]}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    if request[\"type\"] == \"stop\":
        break
    if request[\"type\"] == \"call\":
        if request[\"capability\"] == \"source.synthetic\" and request[\"method\"] == \"spec\":
            result = {\"id\": \"source.synthetic\", \"displayName\": \"Synthetic\", \"configSchema\": {\"type\": \"object\"}}
        elif request[\"capability\"] == \"source.synthetic\" and request[\"method\"] == \"check\":
            result = {\"ok\": True, \"message\": \"checked\"}
        elif request[\"capability\"] == \"source.synthetic\" and request[\"method\"] == \"read\":
            result = {\"messages\": [{\"type\": \"record\", \"stream\": \"records\", \"emittedAt\": \"2026-01-01T00:00:00Z\", \"sourceId\": \"isolated-1\", \"sourceVersion\": \"v1\", \"payload\": {\"id\": \"isolated-1\"}}, {\"type\": \"checkpoint\", \"stream\": \"records\", \"emittedAt\": \"2026-01-01T00:00:01Z\", \"cursor\": {\"done\": True}}]}
        else:
            result = {\"capability\": request[\"capability\"], \"method\": request[\"method\"], \"params\": request[\"params\"]}
        print(json.dumps({\"protocol\": request[\"protocol\"], \"type\": \"result\", \"id\": request[\"id\"], \"ok\": True, \"result\": result}), flush=True)
""",
            encoding="utf-8",
        )
        manifest = PluginManifest.from_dict(
            {
                "apiVersion": "wiki-memory/v1",
                "id": "synthetic-isolated",
                "version": "1.0.0",
                "minimumSdkVersion": "1.0.0",
                "runtime": "executable",
                "command": [sys.executable, "-u", str(plugin_script)],
                "provides": ["source.synthetic", "utility.synthetic"],
                "requires": [],
                "permissions": {"secrets": ["EXAMPLE_TOKEN"]},
                "configSchema": "../config-empty.schema.json",
                "healthCheck": "services",
            },
            ROOT / "src/wiki_memory/plugin_catalog/parser-docling/plugin.yaml",
        )
        manager = PluginManager(trusted_plugins={manifest.id})
        manager.add(manifest, {})
        asyncio.run(manager.activate_all({"EXAMPLE_TOKEN": "synthetic-secret", "UNDECLARED": "hidden"}))
        source = manager.services.get("source.synthetic")
        self.assertIsInstance(source, SourceConnector)
        self.assertTrue(asyncio.run(source.check({}, {})).ok)

        result = asyncio.run(
            SourceIngestionRuntime(MemoryEngine(self.root)).run(
                source,
                connector_instance_id="isolated-fixture",
                selection=SourceSelection({"records": {}}),
                vault="knowledge",
            )
        )
        self.assertEqual((result.records, result.checkpoints), (1, 1))
        utility = manager.services.get("utility.synthetic")
        response = asyncio.run(utility.call("ping", {"value": "synthetic"}))
        self.assertEqual(response, {"capability": "utility.synthetic", "method": "ping", "params": {"value": "synthetic"}})
        asyncio.run(manager.stop_all())
        self.assertEqual(manager.fibers[manifest.id].state, PluginState.STOPPED)

    def test_oci_plugin_defaults_to_a_read_only_networkless_sandbox(self) -> None:
        """The OCI command line is an executable security boundary, not docs.

        A tiny stand-in for the OCI runtime lets this test inspect the exact
        invocation without requiring Docker on every development machine.
        """

        runtime = Path(self.temp.name) / "fake-oci-runtime.py"
        invocation = Path(self.temp.name) / "oci-invocation.json"
        runtime.write_text(
            f'''#!/usr/bin/env python3
import json
import sys
from pathlib import Path

start = json.loads(sys.stdin.readline())
Path({str(invocation)!r}).write_text(json.dumps({{"argv": sys.argv[1:], "start": start}}), encoding="utf-8")
print(json.dumps({{"protocol": start["protocol"], "type": "ready", "provides": ["source.oci"]}}), flush=True)
for line in sys.stdin:
    if json.loads(line).get("type") == "stop":
        break
''',
            encoding="utf-8",
        )
        runtime.chmod(0o700)
        runtime_command = runtime
        if os.name == "nt":
            # Windows does not execute a shebang script directly.  The
            # production OCI runtime is an .exe, but this test double needs a
            # small .cmd bridge so it exercises the same argument contract.
            wrapper = Path(self.temp.name) / "fake-oci-runtime.cmd"
            wrapper.write_text(
                f'@echo off\r\n"{sys.executable}" "{runtime}" %*\r\n',
                encoding="utf-8",
            )
            runtime_command = wrapper
        manifest = PluginManifest.from_dict(
            {
                "apiVersion": "wiki-memory/v1",
                "id": "synthetic-oci",
                "version": "1.0.0",
                "minimumSdkVersion": "1.0.0",
                "runtime": "oci",
                "image": "registry.example.test/synthetic@sha256:" + "a" * 64,
                "provides": ["source.oci"],
                "requires": [],
                "permissions": {"network": []},
                "configSchema": "../config-empty.schema.json",
                "healthCheck": "services",
            },
            ROOT / "src/wiki_memory/plugin_catalog/parser-docling/plugin.yaml",
        )
        with patch.dict(os.environ, {"WIKI_MEMORY_OCI_RUNTIME": str(runtime_command)}):
            manager = PluginManager(trusted_plugins={manifest.id})
            manager.add(manifest, {})
            asyncio.run(manager.activate_all())
            self.assertEqual(manager.fibers[manifest.id].state, PluginState.ACTIVE, manager.fibers[manifest.id].message)
            asyncio.run(manager.stop_all())

        observed = json.loads(invocation.read_text(encoding="utf-8"))
        arguments = observed["argv"]
        self.assertEqual(arguments[0], "run")
        for option in ("--rm", "--interactive", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--network=none"):
            self.assertIn(option, arguments)
        self.assertIn("--tmpfs=/tmp:rw,noexec,nosuid,size=64m", arguments)
        self.assertIn("--workdir=/runtime", arguments)
        self.assertIn("--env=HOME=/runtime", arguments)
        self.assertTrue(any(item.startswith("type=bind,src=") and item.endswith(",dst=/runtime,rw") for item in arguments))
        self.assertEqual(observed["start"]["permissions"]["network"], [])

    def test_executable_plugin_forward_migration_is_durable_and_runs_before_publish(self) -> None:
        plugin_script = Path(self.temp.name) / "isolated_migration_plugin.py"
        migration_log = Path(self.temp.name) / "isolated-migrations.jsonl"
        plugin_script.write_text(
            f'''import json
import sys
from pathlib import Path

log = Path({str(migration_log)!r})
start = json.loads(sys.stdin.readline())
print(json.dumps({{"protocol": start["protocol"], "type": "ready", "provides": ["source.migrating"]}}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    if request["type"] == "stop":
        break
    if request["type"] == "migrate":
        if request["entrypoint"] == "synthetic.migrations:fail":
            print(json.dumps({{"protocol": request["protocol"], "type": "migration-result", "id": request["id"], "ok": False}}), flush=True)
            continue
        with log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({{"entrypoint": request["entrypoint"], "from": request["fromVersion"], "to": request["toVersion"]}}) + "\\n")
        print(json.dumps({{"protocol": request["protocol"], "type": "migration-result", "id": request["id"], "ok": True}}), flush=True)
''',
            encoding="utf-8",
        )
        state_database = Path(self.temp.name) / "isolated-plugin-versions.sqlite3"

        def manifest(version: str, migrations: list[dict] | None = None) -> PluginManifest:
            return PluginManifest.from_dict(
                {
                    "apiVersion": "wiki-memory/v1",
                    "id": "synthetic-isolated-migration",
                    "version": version,
                    "minimumSdkVersion": "1.0.0",
                    "runtime": "executable",
                    "command": [sys.executable, "-u", str(plugin_script)],
                    "provides": ["source.migrating"],
                    "requires": [],
                    "permissions": {},
                    "configSchema": "../config-empty.schema.json",
                    "healthCheck": "services",
                    "migrations": migrations or [],
                },
                ROOT / "src/wiki_memory/plugin_catalog/parser-docling/plugin.yaml",
            )

        initial = PluginManager(
            trusted_plugins={"synthetic-isolated-migration"}, state_database=state_database
        )
        initial.add(manifest("1.0.0"), {})
        asyncio.run(initial.activate_all())
        asyncio.run(initial.stop_all())

        upgraded = PluginManager(
            trusted_plugins={"synthetic-isolated-migration"}, state_database=state_database
        )
        upgraded.add(
            manifest(
                "1.1.0",
                [{"fromVersion": "1.0.0", "toVersion": "1.1.0", "entrypoint": "synthetic.migrations:upgrade"}],
            ),
            {},
        )
        asyncio.run(upgraded.activate_all())
        self.assertEqual(upgraded.fibers["synthetic-isolated-migration"].state, PluginState.ACTIVE)
        self.assertTrue(upgraded.services.has("source.migrating"))
        self.assertEqual(
            [json.loads(line) for line in migration_log.read_text(encoding="utf-8").splitlines()],
            [{"entrypoint": "synthetic.migrations:upgrade", "from": "1.0.0", "to": "1.1.0"}],
        )
        self.assertEqual(upgraded.version_store.version("synthetic-isolated-migration"), "1.1.0")
        asyncio.run(upgraded.stop_all())

        rejected = PluginManager(
            trusted_plugins={"synthetic-isolated-migration"}, state_database=state_database
        )
        rejected.add(
            manifest(
                "1.2.0",
                [{"fromVersion": "1.1.0", "toVersion": "1.2.0", "entrypoint": "synthetic.migrations:fail"}],
            ),
            {},
        )
        asyncio.run(rejected.activate_all())
        self.assertEqual(rejected.fibers["synthetic-isolated-migration"].state, PluginState.FAILED)
        self.assertFalse(rejected.services.has("source.migrating"))
        self.assertEqual(rejected.version_store.version("synthetic-isolated-migration"), "1.1.0")

    def test_plugin_upgrade_runs_durable_forward_migration_once(self) -> None:
        module = types.ModuleType("wiki_memory_synthetic_migration")
        observed: list[tuple[str, str]] = []

        def activate(_context, _config):
            return None

        def migrate(_context, from_version, to_version):
            observed.append((from_version, to_version))

        module.activate = activate
        module.migrate = migrate
        sys.modules[module.__name__] = module
        state_database = Path(self.temp.name) / "plugin-versions.sqlite3"

        def manifest(version: str, migrations: list[dict] | None = None) -> PluginManifest:
            return PluginManifest.from_dict(
                {
                    "apiVersion": "wiki-memory/v1",
                    "id": "synthetic-migration",
                    "version": version,
                    "minimumSdkVersion": "1.0.0",
                    "runtime": "python",
                    "entrypoint": f"{module.__name__}:activate",
                    "provides": [],
                    "requires": [],
                    "permissions": {},
                    "configSchema": "../config-empty.schema.json",
                    "migrations": migrations or [],
                },
                ROOT / "src/wiki_memory/plugin_catalog/parser-docling/plugin.yaml",
            )

        try:
            initial = PluginManager(trusted_plugins={"synthetic-migration"}, state_database=state_database)
            initial.add(manifest("1.0.0"), {})
            asyncio.run(initial.activate_all())
            asyncio.run(initial.stop_all())

            upgraded = PluginManager(trusted_plugins={"synthetic-migration"}, state_database=state_database)
            upgraded.add(
                manifest(
                    "1.1.0",
                    [{"fromVersion": "1.0.0", "toVersion": "1.1.0", "entrypoint": f"{module.__name__}:migrate"}],
                ),
                {},
            )
            asyncio.run(upgraded.activate_all())
            self.assertEqual(observed, [("1.0.0", "1.1.0")])
            asyncio.run(upgraded.stop_all())

            restarted = PluginManager(trusted_plugins={"synthetic-migration"}, state_database=state_database)
            restarted.add(manifest("1.1.0"), {})
            asyncio.run(restarted.activate_all())
            self.assertEqual(observed, [("1.0.0", "1.1.0")])
        finally:
            sys.modules.pop(module.__name__, None)

    def test_plugin_upgrade_stages_then_swaps_capabilities_and_restarts_dependents(self) -> None:
        provider_module = types.ModuleType("wiki_memory_synthetic_upgrade_provider")
        dependent_module = types.ModuleType("wiki_memory_synthetic_upgrade_dependent")
        dependent_versions: list[str] = []

        def provider_activate(context, _config):
            context.provide("service.synthetic", {"version": context.manifest.version})

        def provider_migrate(_context, _from_version, _to_version):
            return None

        def dependent_activate(context, _config):
            version = context.require("service.synthetic")["version"]
            dependent_versions.append(version)
            context.provide("service.dependent", {"providerVersion": version})

        provider_module.activate = provider_activate
        provider_module.migrate = provider_migrate
        dependent_module.activate = dependent_activate
        sys.modules[provider_module.__name__] = provider_module
        sys.modules[dependent_module.__name__] = dependent_module

        def manifest(plugin_id: str, version: str, entrypoint: str, provides: list[str], requires: list[str], migrations=None):
            return PluginManifest.from_dict(
                {
                    "apiVersion": "wiki-memory/v1",
                    "id": plugin_id,
                    "version": version,
                    "minimumSdkVersion": "1.0.0",
                    "runtime": "python",
                    "entrypoint": entrypoint,
                    "provides": provides,
                    "requires": requires,
                    "permissions": {},
                    "configSchema": "../config-empty.schema.json",
                    "healthCheck": "services",
                    "migrations": migrations or [],
                },
                ROOT / "src/wiki_memory/plugin_catalog/parser-docling/plugin.yaml",
            )

        try:
            manager = PluginManager(trusted_plugins={"synthetic-provider", "synthetic-dependent"})
            manager.add(
                manifest(
                    "synthetic-provider", "1.0.0", f"{provider_module.__name__}:activate", ["service.synthetic"], []
                )
            )
            manager.add(
                manifest(
                    "synthetic-dependent", "1.0.0", f"{dependent_module.__name__}:activate", ["service.dependent"], ["service.synthetic"]
                )
            )
            asyncio.run(manager.activate_all())
            result = asyncio.run(
                manager.upgrade(
                    "synthetic-provider",
                    manifest(
                        "synthetic-provider",
                        "1.1.0",
                        f"{provider_module.__name__}:activate",
                        ["service.synthetic"],
                        [],
                        [{"fromVersion": "1.0.0", "toVersion": "1.1.0", "entrypoint": f"{provider_module.__name__}:migrate"}],
                    ),
                )
            )
            self.assertTrue(result["ok"])
            self.assertEqual(manager.services.get("service.synthetic")["version"], "1.1.0")
            self.assertEqual(manager.services.get("service.dependent")["providerVersion"], "1.1.0")
            self.assertEqual(dependent_versions, ["1.0.0", "1.1.0"])
        finally:
            sys.modules.pop(provider_module.__name__, None)
            sys.modules.pop(dependent_module.__name__, None)

    def test_plugin_upgrade_rejects_candidate_missing_a_staged_capability(self) -> None:
        module = types.ModuleType("wiki_memory_synthetic_incomplete_upgrade")

        def activate(context, _config):
            if context.manifest.version == "1.0.0":
                context.provide("service.synthetic", {"version": "1.0.0"})

        module.activate = activate
        sys.modules[module.__name__] = module

        def manifest(version: str) -> PluginManifest:
            return PluginManifest.from_dict(
                {
                    "apiVersion": "wiki-memory/v1",
                    "id": "synthetic-incomplete-upgrade",
                    "version": version,
                    "minimumSdkVersion": "1.0.0",
                    "runtime": "python",
                    "entrypoint": f"{module.__name__}:activate",
                    "provides": ["service.synthetic"],
                    "requires": [],
                    "permissions": {},
                    "configSchema": "../config-empty.schema.json",
                    "healthCheck": "services",
                },
                ROOT / "src/wiki_memory/plugin_catalog/parser-docling/plugin.yaml",
            )

        try:
            manager = PluginManager(trusted_plugins={"synthetic-incomplete-upgrade"})
            manager.add(manifest("1.0.0"))
            asyncio.run(manager.activate_all())
            with self.assertRaisesRegex(MemoryError, "Replacement plugin did not become healthy"):
                asyncio.run(manager.upgrade("synthetic-incomplete-upgrade", manifest("1.1.0")))
            self.assertEqual(manager.fibers["synthetic-incomplete-upgrade"].state, PluginState.ACTIVE)
            self.assertEqual(manager.services.get("service.synthetic"), {"version": "1.0.0"})
        finally:
            sys.modules.pop(module.__name__, None)

    def test_network_connectors_reject_unsafe_endpoint_schemes_and_credentials(self) -> None:
        engine = MemoryEngine(self.root)
        with self.assertRaises(MemoryError):
            TeamClient(engine, "ftp://localhost", lambda: "synthetic")
        with self.assertRaises(MemoryError):
            TeamClient(engine, "https://user:password@team.example", lambda: "synthetic")
        with self.assertRaises(MemoryError):
            MistralTranscriber("synthetic", base_url="file://localhost/tmp/socket")
        with self.assertRaises(MemoryError):
            OIDCVerifier(OIDCConfig("https://user:password@identity.example", "wiki-memory"))

    def test_solo_profile_activates_without_server_or_login(self) -> None:
        report = profile_report(self.root, "solo")
        self.assertTrue(report["ok"])
        self.assertTrue(all(plugin["state"] == "active" for plugin in report["plugins"]))

    def test_official_plugin_catalog_rejects_manifest_drift(self) -> None:
        catalog = Path(self.temp.name) / "catalog"
        shutil.copytree(ROOT / "src/wiki_memory/plugin_catalog", catalog)
        verify_official_catalog(catalog)
        manifest = catalog / "backup-local" / "plugin.yaml"
        manifest.write_text(manifest.read_text(encoding="utf-8") + "\n# altered\n", encoding="utf-8")
        with self.assertRaisesRegex(MemoryError, "manifest hash mismatch"):
            verify_official_catalog(catalog)

    def test_team_server_manifest_declares_every_runtime_secret(self) -> None:
        manifest = PluginManifest.load(
            ROOT / "src" / "wiki_memory" / "plugin_catalog" / "team-server" / "plugin.yaml"
        )
        self.assertTrue(
            {
                "DATABASE_URL",
                "DATABASE_PASSWORD",
                "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY",
                "WIKI_MEMORY_BOOTSTRAP_TOKEN",
                "WIKI_MEMORY_RESTORE_ATTESTATION_TOKEN",
            }.issubset(manifest.permissions.secrets)
        )


if __name__ == "__main__":
    unittest.main()
