#!/usr/bin/env python3
"""Reject a stable release that lacks externally reviewable evidence.

The repository can prove its synthetic gates, but it cannot infer a managed
PostgreSQL/S3 recovery rehearsal or an independent authorization review.  A
stable tag therefore needs links supplied through the protected GitHub
``stable-release`` environment.  Pre-release tags stay reproducible without
inventing production evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from urllib.parse import urlparse


SEMVER_TAG = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z.-]+))?$")
EVIDENCE_VARIABLES = (
    "WIKI_MEMORY_PERFORMANCE_EVIDENCE",
    "WIKI_MEMORY_PRODUCTION_RECOVERY_EVIDENCE",
    "WIKI_MEMORY_EXTERNAL_AUDIT_EVIDENCE",
)


def is_reviewable_https_url(value: str) -> bool:
    parsed = urlparse(value)
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and parsed.path not in {"", "/"}
        and not parsed.username
        and not parsed.password
        and not parsed.fragment
    )


def gate(tag: str, environment: dict[str, str] | None = None) -> tuple[bool, dict[str, object]]:
    """Return a non-secret release decision for a semantic-version tag."""

    match = SEMVER_TAG.fullmatch(tag)
    if match is None:
        return False, {"tag": tag, "reason": "Release tags must use vMAJOR.MINOR.PATCH[-prerelease]."}
    prerelease = match.group(4)
    if prerelease:
        return True, {"tag": tag, "kind": "prerelease", "stableEvidenceRequired": False}
    environment = environment if environment is not None else os.environ
    supplied = {name: environment.get(name, "").strip() for name in EVIDENCE_VARIABLES}
    invalid = [name for name, value in supplied.items() if not is_reviewable_https_url(value)]
    if invalid:
        return False, {
            "tag": tag,
            "kind": "stable",
            "missingOrInvalidEvidence": invalid,
            "reason": "Stable releases require protected-environment HTTPS evidence links.",
        }
    return True, {
        "tag": tag,
        "kind": "stable",
        "stableEvidenceRequired": True,
        "evidenceLinksAccepted": sorted(supplied),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Enforce evidence requirements for Wiki Memory release tags")
    parser.add_argument("--tag", required=True, help="Git tag, for example v1.0.0-alpha.11")
    args = parser.parse_args()
    ok, report = gate(args.tag)
    print(json.dumps(report, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
