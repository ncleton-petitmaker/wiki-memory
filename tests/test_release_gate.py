"""Tests for the release evidence boundary."""

from __future__ import annotations

import unittest

from scripts.release_gate import gate


class ReleaseGateTests(unittest.TestCase):
    def test_prerelease_needs_no_fabricated_production_evidence(self) -> None:
        ok, report = gate("v1.0.0-alpha.1", {})
        self.assertTrue(ok)
        self.assertEqual(report["kind"], "prerelease")

    def test_stable_release_requires_two_reviewable_evidence_links(self) -> None:
        ok, report = gate("v1.0.0", {})
        self.assertFalse(ok)
        self.assertEqual(set(report["missingOrInvalidEvidence"]), {
            "WIKI_MEMORY_PRODUCTION_RECOVERY_EVIDENCE",
            "WIKI_MEMORY_EXTERNAL_AUDIT_EVIDENCE",
        })

        ok, report = gate(
            "v1.0.0",
            {
                "WIKI_MEMORY_PRODUCTION_RECOVERY_EVIDENCE": "https://evidence.example/recovery/2026-09-02",
                "WIKI_MEMORY_EXTERNAL_AUDIT_EVIDENCE": "https://audit.example/reports/wiki-memory-v1",
            },
        )
        self.assertTrue(ok)
        self.assertEqual(report["kind"], "stable")

    def test_non_semantic_tag_is_rejected(self) -> None:
        ok, _ = gate("latest", {})
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
