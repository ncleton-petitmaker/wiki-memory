from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wiki_memory.events import EventActor, MemoryEvent, PluginRef
from wiki_memory.engine import MemoryEngine
from wiki_memory.layout import init_memory
from wiki_memory.local_api import create_local_app
from wiki_memory.team import normalize_acl, team_session_path


HAS_HTTP_TEST_STACK = bool(importlib.util.find_spec("fastapi") and importlib.util.find_spec("httpx"))


@unittest.skipUnless(HAS_HTTP_TEST_STACK, "server test dependencies are not installed")
class LocalApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "memory"
        init_memory(
            self.root,
            {
                "name": "Synthetic HTTP",
                "language": "en",
                "sync_enabled": False,
                "vaults": [{"slug": "knowledge", "title": "Knowledge", "purpose": "Synthetic tests"}],
            },
        )
        from fastapi.testclient import TestClient

        self.client = TestClient(create_local_app(self.root, token="synthetic-local-token"))
        self.headers = {"Authorization": "Bearer synthetic-local-token"}

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_local_api_is_private_by_default_and_reviews_only_private_proposals(self) -> None:
        self.assertEqual(self.client.get("/v1/health").status_code, 401)
        self.assertEqual(self.client.get("/v1/health", headers=self.headers).status_code, 200)

        capture = self.client.post(
            "/v1/captures",
            headers=self.headers,
            json={"vault": "knowledge", "text": "HTTP synthetic memory", "sourceType": "note"},
        )
        self.assertEqual(capture.status_code, 200, capture.text)
        evidence_reference = capture.json()["evidence_refs"][0]

        shared_capture = self.client.post(
            "/v1/captures",
            headers=self.headers,
            json={"vault": "knowledge", "text": "must remain private", "scope": "team"},
        )
        self.assertEqual(shared_capture.status_code, 422)

        private_proposal = self.client.post(
            "/v1/proposals",
            headers=self.headers,
            json={
                "vault": "knowledge",
                "assertion": {"title": "Private fact", "body": "Verified locally"},
                "evidenceRefs": [evidence_reference],
            },
        )
        self.assertEqual(private_proposal.status_code, 200, private_proposal.text)
        proposal_id = private_proposal.json()["event"]["eventId"]
        review = self.client.post(
            f"/v1/proposals/{proposal_id}/review",
            headers=self.headers,
            json={"decision": "accept"},
        )
        self.assertEqual(review.status_code, 200, review.text)

        shared_proposal = self.client.post(
            "/v1/proposals",
            headers=self.headers,
            json={
                "scope": "team",
                "spaceId": "marketing",
                "assertion": {"title": "Shared candidate", "body": "Needs a Team curator"},
                "evidenceRefs": [evidence_reference],
            },
        )
        self.assertEqual(shared_proposal.status_code, 200, shared_proposal.text)
        shared_id = shared_proposal.json()["eventId"]
        forbidden_review = self.client.post(
            f"/v1/proposals/{shared_id}/review",
            headers=self.headers,
            json={"decision": "accept"},
        )
        self.assertEqual(forbidden_review.status_code, 400)
        self.assertIn("Team server", forbidden_review.json()["error"])

        forged_decision = MemoryEvent(
            event_type="assertion.accepted",
            stream_id="assertion:marketing:forged",
            stream_version=1,
            idempotency_key="forged-shared-decision",
            actor=EventActor("user", "local-owner"),
            plugin=PluginRef("synthetic", "1.0.0"),
            scope="team",
            space_id="marketing",
            acl=normalize_acl({}, owner="local-owner", space_id="marketing"),
            payload={"assertionId": "forged", "vault": "team-marketing"},
        )
        rejected_append = self.client.post(
            "/v1/events:append",
            headers=self.headers,
            json={"events": [forged_decision.to_dict()]},
        )
        self.assertEqual(rejected_append.status_code, 409)

        search = self.client.post(
            "/v1/search", headers=self.headers, json={"query": "Verified locally", "limit": 10}
        )
        self.assertEqual(search.status_code, 200, search.text)
        self.assertTrue(search.json()["results"])

    def test_local_api_rejects_invalid_json_cleanly(self) -> None:
        response = self.client.post(
            "/v1/search",
            headers={**self.headers, "Content-Type": "application/json"},
            content=b"not-json",
        )
        self.assertEqual(response.status_code, 400)

    def test_local_api_rejects_malformed_request_shapes_without_500(self) -> None:
        cases = (
            ("/v1/captures", {"text": "missing vault"}),
            ("/v1/events:append", {"events": "not-an-array"}),
            ("/v1/search", {"query": ["not", "a", "string"]}),
            ("/v1/proposals", {"assertion": ["not", "an", "object"]}),
            ("/v1/proposals/missing/review", {"decision": ["not", "a", "string"]}),
        )
        for path, payload in cases:
            with self.subTest(path=path):
                response = self.client.post(path, headers=self.headers, json=payload)
                self.assertEqual(response.status_code, 422, response.text)

    def test_local_api_refuses_to_serve_corrupt_evidence(self) -> None:
        captured = self.client.post(
            "/v1/captures",
            headers=self.headers,
            json={"vault": "knowledge", "text": "synthetic evidence integrity"},
        )
        self.assertEqual(captured.status_code, 200, captured.text)
        reference = captured.json()["evidence_refs"][0]
        engine = MemoryEngine(self.root)
        engine.evidence.path(reference).write_bytes(b"x" * engine.evidence.metadata(reference).size)
        response = self.client.get(f"/v1/blobs/{reference.split(':', 1)[1]}", headers=self.headers)
        self.assertEqual(response.status_code, 500, response.text)
        self.assertIn("integrity", response.json()["detail"])

    def test_local_api_hides_revoked_team_events_and_evidence(self) -> None:
        engine = MemoryEngine(self.root)
        evidence = engine.evidence.put_bytes(b"team-only-proof", media_type="text/plain")
        event = MemoryEvent(
            event_type="source.captured",
            stream_id="source:marketing:team-only",
            idempotency_key="team-only-http-test",
            actor=EventActor("user", "member"),
            plugin=PluginRef("team-contribution", "1.0.0"),
            scope="team",
            space_id="marketing",
            evidence_refs=[evidence.reference],
            acl=normalize_acl({}, owner="member", space_id="marketing"),
            payload={"vault": "team-marketing", "body": "team only"},
        )
        engine.append(event, enqueue=False)
        digest = evidence.sha256

        self.assertEqual(self.client.get("/v1/events", headers=self.headers).json()["events"], [])
        self.assertEqual(self.client.get(f"/v1/blobs/{digest}", headers=self.headers).status_code, 404)

        session_path = team_session_path(self.root)
        session_path.parent.mkdir(parents=True, exist_ok=True)
        checked_at = datetime.now(timezone.utc).replace(microsecond=0)
        lease_expires_at = checked_at + timedelta(hours=1)
        session_path.write_text(
            json.dumps(
                {
                    "principalId": "reader",
                    "kind": "user",
                    "roles": ["reader"],
                    "spaces": ["marketing"],
                    "groups": [],
                    "checkedAt": checked_at.isoformat().replace("+00:00", "Z"),
                    "offlineLeaseExpiresAt": lease_expires_at.isoformat().replace("+00:00", "Z"),
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(len(self.client.get("/v1/events", headers=self.headers).json()["events"]), 1)
        self.assertEqual(self.client.get(f"/v1/blobs/{digest}", headers=self.headers).status_code, 200)

        session_path.write_text(
            json.dumps(
                {
                    "principalId": "reader",
                    "kind": "user",
                    "roles": ["reader"],
                    "spaces": [],
                    "groups": [],
                    "checkedAt": checked_at.isoformat().replace("+00:00", "Z"),
                    "offlineLeaseExpiresAt": lease_expires_at.isoformat().replace("+00:00", "Z"),
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(self.client.get("/v1/events", headers=self.headers).json()["events"], [])
        self.assertEqual(self.client.get(f"/v1/blobs/{digest}", headers=self.headers).status_code, 404)

        session_path.write_text(
            json.dumps(
                {
                    "principalId": "reader",
                    "kind": "user",
                    "roles": ["reader"],
                    "spaces": ["marketing"],
                    "groups": [],
                    "checkedAt": "2026-01-01T00:00:00Z",
                    "offlineLeaseExpiresAt": "2026-01-01T01:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(self.client.get("/v1/events", headers=self.headers).json()["events"], [])
        self.assertEqual(self.client.get(f"/v1/blobs/{digest}", headers=self.headers).status_code, 404)


if __name__ == "__main__":
    unittest.main()
