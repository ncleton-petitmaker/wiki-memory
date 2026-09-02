"""Fail closed when the reference Compose topology is not digest pinned.

Docker Compose performs variable interpolation but cannot express a regular
expression constraint on an image reference.  This tiny init service closes
that gap before the Team API or worker is allowed to start.
"""

from __future__ import annotations

import os
import re
import sys


OFFICIAL_IMAGE = re.compile(
    r"^ghcr\.io/ncleton-petitmaker/wiki-memory@sha256:[0-9a-f]{64}$"
)


def is_approved_image_reference(reference: str) -> bool:
    # Deliberately do not trim: the configured reference must be byte-for-byte
    # explicit, rather than silently normalizing a deployment typo.
    return bool(OFFICIAL_IMAGE.fullmatch(reference))


def image_policy_error(reference: str, allow_unverified: bool) -> str | None:
    """Return a safe failure message, or ``None`` when startup may continue."""

    if is_approved_image_reference(reference):
        return None
    if allow_unverified:
        return None
    return (
        "WIKI_MEMORY_IMAGE must be the exact signed GHCR digest "
        "ghcr.io/ncleton-petitmaker/wiki-memory@sha256:<64 lowercase hex characters>."
    )


def main() -> int:
    reference = os.environ.get("WIKI_MEMORY_IMAGE", "")
    # This override exists solely for CI/local development, where the image is
    # built in the runner and therefore has no registry digest.  It is absent
    # from the production .env template and visible in Compose configuration.
    allow_unverified = os.environ.get("WIKI_MEMORY_ALLOW_UNVERIFIED_IMAGE") == "1"
    error = image_policy_error(reference, allow_unverified)
    if error:
        print(error, file=sys.stderr)
        return 78
    if allow_unverified and not is_approved_image_reference(reference):
        print("WARNING: allowing an unverified image only because the explicit development override is set.")
    else:
        print("Compose image policy accepted the exact GHCR digest.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
