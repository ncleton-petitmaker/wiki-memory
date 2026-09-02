#!/usr/bin/env python3
"""Fail fast on deployment invariants even where Docker/Helm are unavailable."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def require(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def main() -> int:
    errors: list[str] = []
    compose = (ROOT / "deploy/team/docker-compose.yml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "deploy/team/Dockerfile").read_text(encoding="utf-8")
    validate = (ROOT / "deploy/helm/wiki-memory/templates/validate.yaml").read_text(encoding="utf-8")
    secret = (ROOT / "deploy/helm/wiki-memory/templates/secret.yaml").read_text(encoding="utf-8")
    storage = (ROOT / "deploy/helm/wiki-memory/templates/storage.yaml").read_text(encoding="utf-8")
    workloads = (ROOT / "deploy/helm/wiki-memory/templates/workloads.yaml").read_text(encoding="utf-8")
    network_policy = (ROOT / "deploy/helm/wiki-memory/templates/networkpolicy.yaml").read_text(encoding="utf-8")
    ci_values = (ROOT / "deploy/helm/wiki-memory/values.ci.yaml").read_text(encoding="utf-8")
    internal_ci_values = (ROOT / "deploy/helm/wiki-memory/values.internal-ci.yaml").read_text(encoding="utf-8")
    release_workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    ci_workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    require("127.0.0.1:8787:8787" in compose, "Compose API must bind only to loopback", errors)
    require("read_only: true" in compose and "no-new-privileges:true" in compose, "Compose must harden API/worker", errors)
    require("WIKI_MEMORY_IMAGE:?set WIKI_MEMORY_IMAGE" in compose, "Compose must require a release image digest", errors)
    require(
        "WIKI_MEMORY_TEAM_PLUGIN_TRUST_KEYS:?set" in compose
        and "WIKI_MEMORY_TEAM_APPROVED_PLUGIN_IDS:?set" in compose,
        "Compose must require an explicit external plugin trust policy",
        errors,
    )
    require(
        "compose-smoke:" in ci_workflow
        and "docker compose --project-name wiki-memory-compose-ci" in ci_workflow
        and "up --detach --wait --wait-timeout 120" in ci_workflow
        and "curl --fail --retry 12" in ci_workflow,
        "CI must boot the Compose topology and call its health endpoint",
        errors,
    )
    require(
        "WIKI_MEMORY_ALLOW_UNVERIFIED_IMAGE: '1'" in ci_workflow
        and "WIKI_MEMORY_ALLOW_UNVERIFIED_IMAGE" not in release_workflow,
        "Only the synthetic CI smoke test may opt out of the image-digest gate",
        errors,
    )
    require(
        "compose-smoke:" in release_workflow
        and "needs: [artifacts, image, compose-smoke]" in release_workflow
        and "wiki-memory-release-compose" in release_workflow
        and "WIKI_MEMORY_IMAGE: ghcr.io/${{ github.repository }}@${{ needs.image.outputs.digest }}" in release_workflow,
        "Release publication must wait for a smoke test of the exact pushed image digest",
        errors,
    )
    require("WIKI_MEMORY_IMAGE:-" not in compose and "build:" not in compose, "Compose reference deployment must not use mutable/default or locally built images", errors)
    require(
        "WIKI_MEMORY_ALLOW_UNVERIFIED_IMAGE" not in (ROOT / "deploy/team/.env.example").read_text(encoding="utf-8"),
        "Compose operator template must not offer an unverified-image bypass",
        errors,
    )
    require(
        "image-policy:" in compose
        and "wiki_memory.compose_policy" in compose
        and "image-policy: {condition: service_completed_successfully}" in compose,
        "Compose must gate API and worker startup on executable image-digest validation",
        errors,
    )
    require("@sha256:" in (ROOT / "deploy/team/.env.example").read_text(encoding="utf-8"), "Compose example must require a digest placeholder", errors)
    require("USER wiki-memory" in dockerfile, "Team image must run as non-root", errors)
    require("--require-hashes" in dockerfile, "Team image dependencies must be hash-locked", errors)
    require("image.digest must be set" in validate, "Helm must reject mutable application images", errors)
    for key in ("POSTGRES_PASSWORD", "MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD"):
        require(key + ":" in secret, f"Helm Secret must contain {key}", errors)
        require("key: " + key in storage, f"Helm storage must reference {key} from Secret", errors)
    require(
        "WIKI_MEMORY_RESTORE_ATTESTATION_TOKEN:" in secret,
        "Helm Secret must contain the restore-attestation token",
        errors,
    )
    require("envFrom: [{secretRef:" in workloads, "Team workloads must consume their managed Secret", errors)
    require(
        "WIKI_MEMORY_TEAM_PLUGIN_TRUST_KEYS" in workloads
        and "WIKI_MEMORY_TEAM_APPROVED_PLUGIN_IDS" in workloads,
        "Helm workloads must provide the external plugin trust policy",
        errors,
    )
    require(".Values.postgresql.password | quote" not in storage, "Helm must not render PostgreSQL password into workload", errors)
    require(".Values.objectStore.secretKey | quote" not in storage, "Helm must not render MinIO password into workload", errors)
    require("runAsNonRoot: true" in storage and "capabilities: {drop: [ALL]}" in storage, "Helm storage must drop privileges", errors)
    require("automountServiceAccountToken: false" in workloads, "Helm workloads must not mount service-account tokens", errors)
    require("application-default-deny" in network_policy, "Helm must deny application traffic by default", errors)
    require("networkPolicy.apiIngress must explicitly" in validate, "Helm must require explicit API ingress peers", errors)
    require("networkPolicy.dnsCIDRs must explicitly" in validate, "Helm must require explicit DNS egress CIDRs", errors)
    for target in ("postgresCIDRs", "objectStoreCIDRs", "oidcCIDRs", "otlpCIDRs"):
        require(target in network_policy, f"Helm NetworkPolicy must model {target} egress", errors)
    require("minio-init" in storage and "minio-init" in network_policy, "MinIO initialization must retain its narrow storage access", errors)
    require("networkPolicy:" in ci_values and "apiIngress:" in ci_values, "Helm CI fixture must exercise the fail-closed policy", errors)
    require("internal: true" in internal_ci_values, "Helm internal CI fixture must exercise bundled storage", errors)
    require(
        "draft: true" in release_workflow
        and "actions/checkout@v7\n      - uses: actions/download-artifact@v8" in release_workflow
        and 'gh release edit "$GITHUB_REF_NAME" --draft=false' in release_workflow,
        "Release workflow must attach assets to a checked-out draft before immutable publication",
        errors,
    )

    if errors:
        print("deployment-validation-failed", file=sys.stderr)
        print("\n".join("- " + error for error in errors), file=sys.stderr)
        return 1
    print("deployment-config-valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
