from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wiki_memory.object_store import ObjectStore
from wiki_memory.team_preflight import team_preflight


class FakeRepository:
    def __init__(self, *, healthy: bool = True, restore_age: float = 12.0) -> None:
        self.healthy = healthy
        self.restore_age = restore_age

    def healthcheck(self) -> None:
        if not self.healthy:
            raise RuntimeError("synthetic unavailable database")

    def operational_metrics(self) -> dict[str, float]:
        return {"wiki_memory_restore_last_success_age_seconds": self.restore_age}


class FakeStore(ObjectStore):
    def __init__(self, *, reachable: bool = True, versioning: bool | None = True) -> None:
        self.reachable = reachable
        self.versioning = versioning

    def has(self, digest: str) -> bool:
        if not self.reachable:
            raise RuntimeError("synthetic unavailable object store")
        return False

    def put_file(self, digest: str, path: Path, media_type: str = "application/octet-stream") -> None:
        raise AssertionError("not used by preflight")

    def open(self, digest: str):
        raise AssertionError("not used by preflight")

    def versioning_status(self) -> bool | None:
        return self.versioning


class TeamPreflightTests(unittest.TestCase):
    def test_preflight_reports_all_verifiable_gates_without_secrets(self) -> None:
        report = team_preflight(FakeRepository(), FakeStore(), oidc=object(), attestation_configured=True)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["unmet"], [])
        self.assertEqual(report["checks"]["objectVersioning"]["status"], "enabled")
        self.assertIn("operatorEvidenceRequired", report)

    def test_preflight_fails_closed_for_missing_restore_versioning_or_oidc(self) -> None:
        report = team_preflight(
            FakeRepository(restore_age=-1), FakeStore(versioning=None), oidc=None, attestation_configured=False
        )
        self.assertFalse(report["ok"])
        self.assertEqual(
            set(report["unmet"]),
            {"objectVersioning", "oidc", "restoreAttestationChannel", "restoreAttestation"},
        )
        self.assertEqual(report["checks"]["objectVersioning"]["status"], "unverifiable")
