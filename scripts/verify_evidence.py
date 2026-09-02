#!/usr/bin/env python3
"""Verify immutable benchmark evidence shipped with the repository.

The release evidence document may summarize a benchmark, but a checksum
manifest is the authoritative binding between a retained report and its bytes.
Keeping this verifier in Python makes the check identical on macOS, Linux, and
Windows rather than depending on a platform-specific ``sha256sum`` binary.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path


CHECKSUM_LINE = re.compile(r"^([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._-]*\.json)$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify(directory: Path) -> list[str]:
    """Return human-readable integrity errors for a retained evidence directory."""

    manifest = directory / "SHA256SUMS"
    if manifest.is_symlink() or not manifest.is_file():
        return [f"missing regular checksum manifest: {manifest}"]

    errors: list[str] = []
    listed: set[str] = set()
    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return [f"checksum manifest is not UTF-8: {manifest}"]
    if not lines:
        return [f"checksum manifest is empty: {manifest}"]

    for line_number, line in enumerate(lines, start=1):
        match = CHECKSUM_LINE.fullmatch(line)
        if match is None:
            errors.append(f"invalid checksum line {line_number}: {line!r}")
            continue
        expected, filename = match.groups()
        if filename in listed:
            errors.append(f"duplicate checksum entry: {filename}")
            continue
        listed.add(filename)
        report = directory / filename
        if report.is_symlink() or not report.is_file():
            errors.append(f"missing regular evidence report: {filename}")
            continue
        actual = sha256_file(report)
        if actual != expected:
            errors.append(f"checksum mismatch for {filename}: expected {expected}, got {actual}")

    actual_reports = {path.name for path in directory.glob("*.json") if path.is_file() and not path.is_symlink()}
    unlisted = sorted(actual_reports - listed)
    if unlisted:
        errors.append(f"evidence reports missing from checksum manifest: {', '.join(unlisted)}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Wiki Memory retained evidence checksums")
    parser.add_argument(
        "--directory",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "docs" / "evidence",
        help="directory containing SHA256SUMS and evidence JSON files",
    )
    args = parser.parse_args()
    errors = verify(args.directory.resolve())
    if errors:
        print("Evidence verification failed:", file=sys.stderr)
        print("\n".join(f"- {error}" for error in errors), file=sys.stderr)
        return 1
    print(f"Evidence checksums verified: {args.directory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
