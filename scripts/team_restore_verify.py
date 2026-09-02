#!/usr/bin/env python3
"""Verify a restored Wiki Memory Team database and its object-store evidence.

Run this only against an isolated PITR/database restore and the corresponding
versioned object-store recovery point. It never prints event payloads, blobs,
or OIDC credentials. By default it rebuilds the derived search projection,
then verifies every canonical event and every referenced evidence object.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wiki_memory.team_server import object_store_from_environment, repository_from_environment


def compact_detail(result: dict[str, Any]) -> dict[str, Any]:
    """Keep an API attestation informative while never carrying restored data."""

    return {
        "ledgerOk": bool(result.get("ok")),
        "streams": int(result.get("streams", 0)),
        "evidenceChecked": int(result.get("evidenceChecked", 0)),
        "missingEvidence": len(result.get("missingEvidence", [])),
        "canonicalErrors": len(result.get("errors", [])),
        "searchDocuments": int(result.get("searchDocuments", 0)),
    }


def attest(url: str, admin_token: str, attestation_token: str, backup_id: str, result: dict[str, Any]) -> None:
    body = {
        "status": "success" if result["ok"] else "failed",
        "backupId": backup_id,
        "eventCount": result["events"],
        "evidenceCount": result["evidenceReferences"],
        "detail": compact_detail(result),
    }
    request = urllib.request.Request(
        url.rstrip("/") + "/v1/operations/restore-verifications",
        data=json.dumps(body, separators=(",", ":")).encode(),
        headers={
            "Authorization": f"Bearer {admin_token}",
            "X-Wiki-Memory-Restore-Attestation": attestation_token,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            if response.status != 200:
                raise RuntimeError(f"restore attestation returned HTTP {response.status}")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"restore attestation failed: {exc}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a restored Team ledger and evidence recovery point")
    parser.add_argument(
        "--evidence-sample",
        type=int,
        default=0,
        help="positive deterministic hash-ranked sample; 0 (default) verifies every referenced blob",
    )
    parser.add_argument("--no-rebuild-search", action="store_true", help="verify the existing search projection without rebuilding it")
    parser.add_argument("--attestation-url", help="primary Team API URL to record only aggregate rehearsal status")
    parser.add_argument("--admin-token", help="admin bearer token for --attestation-url")
    parser.add_argument("--attestation-token", help="dedicated restore-attestation token for --attestation-url")
    parser.add_argument("--backup-id", help="operator recovery-point identifier required for an attestation")
    args = parser.parse_args()
    if args.evidence_sample < 0:
        parser.error("--evidence-sample must be zero or positive")
    supplied_attestation = [args.attestation_url, args.admin_token, args.attestation_token, args.backup_id]
    if any(supplied_attestation) and not all(supplied_attestation):
        parser.error("--attestation-url, --admin-token, --attestation-token, and --backup-id must be supplied together")

    repository = repository_from_environment()
    store = object_store_from_environment()
    rebuild: dict[str, int] | None = None
    if not args.no_rebuild_search:
        rebuild = repository.rebuild_search_projection(evidence_verify=store.verify)
    result = repository.verify_integrity(store.verify, evidence_limit=args.evidence_sample)
    report = {
        "ok": result["ok"],
        "rebuild": rebuild,
        "ledger": compact_detail(result),
        "evidenceMode": "all" if args.evidence_sample == 0 else f"deterministic-hash-sample:{args.evidence_sample}",
    }
    if args.attestation_url:
        attest(args.attestation_url, args.admin_token, args.attestation_token, args.backup_id, result)
        report["attested"] = True
    print(json.dumps(report, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
