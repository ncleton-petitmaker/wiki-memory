"""Tests for retained release-evidence integrity checks."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from scripts.verify_evidence import verify


class EvidenceIntegrityTests(unittest.TestCase):
    def write_report(self, directory: Path, name: str = "report.json") -> tuple[str, Path]:
        report = directory / name
        report.write_text('{"ok":true}\n', encoding="utf-8")
        return hashlib.sha256(report.read_bytes()).hexdigest(), report

    def test_repository_evidence_matches_its_checksum_manifest(self) -> None:
        directory = Path(__file__).resolve().parents[1] / "docs" / "evidence"
        self.assertEqual(verify(directory), [])

    def test_rejects_tampered_or_unlisted_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            digest, report = self.write_report(directory)
            (directory / "SHA256SUMS").write_text(f"{digest}  {report.name}\n", encoding="utf-8")
            self.assertEqual(verify(directory), [])
            report.write_text('{"ok":false}\n', encoding="utf-8")
            self.assertTrue(any("checksum mismatch" in error for error in verify(directory)))
            self.write_report(directory, "other.json")
            self.assertTrue(any("missing from checksum manifest" in error for error in verify(directory)))

    def test_rejects_nonportable_or_unsafe_checksum_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "SHA256SUMS").write_text(
                "0" * 64 + "  ../not-an-evidence.json\n",
                encoding="utf-8",
            )
            self.assertTrue(any("invalid checksum line" in error for error in verify(directory)))


if __name__ == "__main__":
    unittest.main()
