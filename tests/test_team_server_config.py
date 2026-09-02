"""Fail-closed configuration checks that run before the Team API listens."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from wiki_memory.config import MemoryError
from wiki_memory.object_store import FileObjectStore
from wiki_memory.team_server import create_app


HAS_TEAM_SERVER = bool(importlib.util.find_spec("fastapi"))


class _Repository:
    def initialize(self) -> None:
        return None


@unittest.skipUnless(HAS_TEAM_SERVER, "Team server test dependencies are not installed")
class TeamServerConfigurationTests(unittest.TestCase):
    def test_rejects_invalid_request_limits_during_startup(self) -> None:
        invalid = {
            "WIKI_MEMORY_MAX_JSON_BYTES": "not-a-number",
            "WIKI_MEMORY_MAX_BLOB_BYTES": "0",
            "WIKI_MEMORY_MAX_EVENTS_PER_APPEND": "1001",
            "WIKI_MEMORY_OFFLINE_LEASE_SECONDS": "2678401",
        }
        with tempfile.TemporaryDirectory() as temporary:
            for name, value in invalid.items():
                with self.subTest(name=name), patch.dict("os.environ", {name: value}, clear=False):
                    with self.assertRaisesRegex(MemoryError, name):
                        create_app(_Repository(), FileObjectStore(Path(temporary) / name))

    def test_session_issues_a_bounded_offline_entitlement_lease(self) -> None:
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            "os.environ",
            {"WIKI_MEMORY_BOOTSTRAP_TOKEN": "synthetic-bootstrap", "WIKI_MEMORY_OFFLINE_LEASE_SECONDS": "0"},
            clear=False,
        ):
            client = TestClient(create_app(_Repository(), FileObjectStore(Path(temporary) / "objects")))
            response = client.get("/v1/session", headers={"Authorization": "Bearer synthetic-bootstrap"})
        self.assertEqual(response.status_code, 200, response.text)
        session = response.json()
        self.assertEqual(session["principalId"], "bootstrap-admin")
        self.assertEqual(session["checkedAt"], session["offlineLeaseExpiresAt"])
        self.assertIsNotNone(datetime.fromisoformat(session["offlineLeaseExpiresAt"].replace("Z", "+00:00")))

    def test_rejects_malformed_team_connector_policy_during_startup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            "os.environ", {"WIKI_MEMORY_TEAM_APPROVED_PLUGIN_IDS": "not-json"}, clear=False
        ):
            with self.assertRaisesRegex(MemoryError, "WIKI_MEMORY_TEAM_APPROVED_PLUGIN_IDS"):
                create_app(_Repository(), FileObjectStore(Path(temporary) / "objects"))


if __name__ == "__main__":
    unittest.main()
