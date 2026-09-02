"""CLI boundary tests for the restore-attestation verifier."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class TeamRestoreVerifyTests(unittest.TestCase):
    def test_attestation_requires_a_separate_channel_token(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "scripts/team_restore_verify.py",
                "--attestation-url",
                "https://team.example.test",
                "--admin-token",
                "admin-token",
                "--backup-id",
                "synthetic-restore",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("--attestation-token", result.stderr)


if __name__ == "__main__":
    unittest.main()
