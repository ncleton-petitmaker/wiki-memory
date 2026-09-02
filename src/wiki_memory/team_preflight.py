"""Non-secret operational readiness checks for a configured Team deployment."""

from __future__ import annotations

import os
from typing import Any

from .object_store import ObjectStore
from .team_repository import TeamRepository
from .team_server import oidc_from_environment, object_store_from_environment, repository_from_environment


def _failure(exc: Exception) -> str:
    # Do not put endpoint names, credentials, provider responses, or payloads
    # in a readiness report that operators may paste into tickets.
    return type(exc).__name__


def team_preflight(
    repository: TeamRepository | None = None,
    object_store: ObjectStore | None = None,
    *,
    oidc: object | None = None,
    attestation_configured: bool | None = None,
) -> dict[str, Any]:
    """Inspect only verifiable Team production gates.

    Managed PostgreSQL PITR retention and the exact cross-service recovery
    point cannot be inferred from application credentials, so they remain
    deliberately visible as operator evidence rather than fabricated checks.
    """

    repository = repository or repository_from_environment()
    object_store = object_store or object_store_from_environment()
    oidc = oidc if oidc is not None else oidc_from_environment()
    if attestation_configured is None:
        attestation_configured = bool(os.environ.get("WIKI_MEMORY_RESTORE_ATTESTATION_TOKEN"))
    checks: dict[str, dict[str, Any]] = {}

    try:
        repository.healthcheck()
        checks["database"] = {"ok": True}
    except Exception as exc:
        checks["database"] = {"ok": False, "error": _failure(exc)}

    try:
        object_store.has("0" * 64)
        checks["objectStore"] = {"ok": True}
    except Exception as exc:
        checks["objectStore"] = {"ok": False, "error": _failure(exc)}

    try:
        versioning = object_store.versioning_status()
        checks["objectVersioning"] = {
            "ok": versioning is True,
            "status": "enabled" if versioning is True else "disabled" if versioning is False else "unverifiable",
        }
    except Exception as exc:
        checks["objectVersioning"] = {"ok": False, "status": "unverifiable", "error": _failure(exc)}

    checks["oidc"] = {"ok": oidc is not None, "status": "configured" if oidc is not None else "missing"}
    checks["restoreAttestationChannel"] = {
        "ok": attestation_configured,
        "status": "configured" if attestation_configured else "missing",
    }

    try:
        metrics = repository.operational_metrics()
        age = float(metrics.get("wiki_memory_restore_last_success_age_seconds", -1))
        checks["restoreAttestation"] = {
            "ok": age >= 0,
            "lastSuccessAgeSeconds": age,
        }
    except Exception as exc:
        checks["restoreAttestation"] = {"ok": False, "error": _failure(exc)}

    unmet = [name for name, result in checks.items() if not result["ok"]]
    return {
        "ok": not unmet,
        "checks": checks,
        "unmet": unmet,
        "operatorEvidenceRequired": [
            "Managed PostgreSQL PITR retention and continuous WAL/archive health.",
            "A PostgreSQL and versioned object-store recovery point tested together on this deployment.",
            "CNI enforcement of the declared NetworkPolicy and egress CIDRs.",
        ],
    }
