from __future__ import annotations

import base64
import json
import os
from typing import Any, Callable

from .plugins import PluginManifest


def signing_payload(manifest: PluginManifest) -> bytes:
    """Return the stable, content-only payload signed by a plugin author."""

    value: dict[str, Any] = {
        "apiVersion": manifest.api_version,
        "id": manifest.id,
        "version": manifest.version,
        "minimumSdkVersion": manifest.minimum_sdk_version,
        "runtime": manifest.runtime,
        "provides": list(manifest.provides),
        "requires": list(manifest.requires),
        "permissions": {
            "filesystem": list(manifest.permissions.filesystem),
            "network": list(manifest.permissions.network),
            "secrets": list(manifest.permissions.secrets),
            "subprocess": manifest.permissions.subprocess,
            "dataClasses": list(manifest.permissions.data_classes),
        },
        "configSchema": manifest.config_schema,
        "migrations": [
            {"fromVersion": item.from_version, "toVersion": item.to_version, "entrypoint": item.entrypoint}
            for item in manifest.migrations
        ],
        "healthCheck": manifest.health_check,
        "stopTimeoutSeconds": manifest.stop_timeout_seconds,
    }
    if manifest.entrypoint is not None:
        value["entrypoint"] = manifest.entrypoint
    if manifest.command:
        value["command"] = list(manifest.command)
    if manifest.image is not None:
        value["image"] = manifest.image
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def team_signature_verifier_from_environment() -> Callable[[PluginManifest], bool]:
    """Build the Team allowlist + Ed25519 verifier without exposing key material.

    ``WIKI_MEMORY_TEAM_PLUGIN_TRUST_KEYS`` is a JSON object mapping key IDs to
    base64 raw Ed25519 public keys. ``WIKI_MEMORY_TEAM_APPROVED_PLUGIN_IDS`` is
    a JSON array of exact plugin IDs. An external Team plugin needs both an
    approved ID and a valid manifest signature; malformed configuration fails
    closed by simply verifying nothing.
    """

    try:
        raw_keys = json.loads(os.environ.get("WIKI_MEMORY_TEAM_PLUGIN_TRUST_KEYS", "{}"))
        approved = json.loads(os.environ.get("WIKI_MEMORY_TEAM_APPROVED_PLUGIN_IDS", "[]"))
        if not isinstance(raw_keys, dict) or not isinstance(approved, list):
            raise ValueError("invalid policy shape")
        keys = {str(key): str(value) for key, value in raw_keys.items()}
        approved_ids = {str(value) for value in approved}
    except (json.JSONDecodeError, ValueError, TypeError):
        keys, approved_ids = {}, set()

    def verify(manifest: PluginManifest) -> bool:
        if manifest.id not in approved_ids or not isinstance(manifest.signature, dict):
            return False
        signature = manifest.signature
        if signature.get("algorithm") != "ed25519":
            return False
        key_id = str(signature.get("keyId") or "")
        encoded_key, encoded_signature = keys.get(key_id), signature.get("value")
        if not encoded_key or not isinstance(encoded_signature, str):
            return False
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

            public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(encoded_key, validate=True))
            public_key.verify(base64.b64decode(encoded_signature, validate=True), signing_payload(manifest))
            return True
        except Exception:
            return False

    return verify
