"""Tests for the Compose image-digest startup gate."""

from __future__ import annotations

import unittest

from wiki_memory.compose_policy import image_policy_error, is_approved_image_reference


class ComposePolicyTests(unittest.TestCase):
    DIGEST = "a" * 64
    REFERENCE = f"ghcr.io/ncleton-petitmaker/wiki-memory@sha256:{DIGEST}"

    def test_accepts_only_the_exact_official_digest_shape(self) -> None:
        self.assertTrue(is_approved_image_reference(self.REFERENCE))
        for invalid in (
            "ghcr.io/ncleton-petitmaker/wiki-memory:1.0.0-alpha.9",
            "ghcr.io/ncleton-petitmaker/wiki-memory@sha256:" + "A" * 64,
            "ghcr.io/other/wiki-memory@sha256:" + self.DIGEST,
            self.REFERENCE + " ",
            "",
        ):
            self.assertFalse(is_approved_image_reference(invalid))

    def test_rejects_mutable_or_missing_image_without_an_explicit_override(self) -> None:
        self.assertIsNotNone(image_policy_error("ghcr.io/ncleton-petitmaker/wiki-memory:latest", False))
        self.assertIsNotNone(image_policy_error("", False))
        self.assertIsNone(image_policy_error(self.REFERENCE, False))

    def test_development_override_is_explicit(self) -> None:
        self.assertIsNone(image_policy_error("wiki-memory-compose-ci", True))


if __name__ == "__main__":
    unittest.main()
