from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from .config import MemoryError, ensure_root
from .engine import MemoryEngine
from .plugins import PluginManager, PluginManifest, load_profile
from .plugin_signatures import team_signature_verifier_from_environment


PACKAGE_ROOT = Path(__file__).resolve().parent
CATALOG_ROOT = PACKAGE_ROOT / "plugin_catalog"
PROFILES_ROOT = PACKAGE_ROOT / "profile_catalog"


def verify_official_catalog(catalog_root: Path = CATALOG_ROOT) -> dict[str, dict[str, str]]:
    """Verify the bundled catalog before trusting any official plugin path.

    The catalog is itself included in the signed release checksum.  These
    hashes then turn accidental or post-install manifest drift into a
    fail-closed startup error rather than a silently trusted plugin change.
    """

    catalog_root = catalog_root.resolve()
    try:
        document = json.loads((catalog_root / "catalog.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MemoryError(f"Official plugin catalog is unreadable: {exc}") from exc
    if document.get("format") != "wiki-memory-plugin-catalog/v1" or not isinstance(document.get("plugins"), list):
        raise MemoryError("Official plugin catalog has an unsupported format.")
    expected: dict[str, dict[str, str]] = {}
    for item in document["plugins"]:
        if not isinstance(item, dict):
            raise MemoryError("Official plugin catalog contains an invalid entry.")
        plugin_id = str(item.get("id") or "")
        manifest_name = str(item.get("manifest") or "")
        digest = str(item.get("sha256") or "")
        version = str(item.get("version") or "")
        manifest_path = (catalog_root / manifest_name).resolve()
        try:
            manifest_path.relative_to(catalog_root)
        except ValueError as exc:
            raise MemoryError("Official plugin catalog manifest escapes its root.") from exc
        if not plugin_id or plugin_id in expected or not manifest_path.is_file() or len(digest) != 64:
            raise MemoryError("Official plugin catalog contains an invalid manifest entry.")
        if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != digest:
            raise MemoryError(f"Official plugin manifest hash mismatch: {plugin_id}")
        manifest = PluginManifest.load(manifest_path)
        if manifest.id != plugin_id or manifest.version != version:
            raise MemoryError(f"Official plugin manifest identity mismatch: {plugin_id}")
        expected[plugin_id] = {"manifest": manifest_name, "sha256": digest, "version": version}
    actual = {path.parent.name for path in catalog_root.glob("*/plugin.yaml")}
    if set(expected) != actual:
        raise MemoryError("Official plugin catalog does not cover every bundled manifest.")
    return expected


def profile_path(profile: str) -> Path:
    path = PROFILES_ROOT / f"{profile}.yaml"
    if not path.is_file():
        raise MemoryError(f"Unknown Wiki Memory profile: {profile}")
    return path


def build_profile(
    root: Path,
    profile: str = "solo",
    *,
    developer_mode: bool = False,
    secret_handles: dict[str, str] | None = None,
    config_overrides: dict[str, dict[str, Any]] | None = None,
    extra_plugin_manifests: list[Path] | None = None,
) -> tuple[MemoryEngine, PluginManager]:
    root = ensure_root(root)
    verified_catalog = verify_official_catalog()
    definition = load_profile(profile_path(profile))
    engine = MemoryEngine(root, markdown_projection=False)
    extra_plugin_manifests = extra_plugin_manifests or []
    profile_plugin_ids = {
        str(item if isinstance(item, str) else item.get("id"))
        for item in definition["plugins"]
    }
    official_manifest_paths = {
        (CATALOG_ROOT / entry["manifest"]).resolve(): plugin_id
        for plugin_id, entry in verified_catalog.items()
    }
    extra_manifests: list[PluginManifest] = []
    trusted_plugins = set(str(item) for item in definition.get("trustedPlugins", []))
    for path in extra_plugin_manifests:
        manifest = PluginManifest.load(path)
        if manifest.id in profile_plugin_ids or any(item.id == manifest.id for item in extra_manifests):
            raise MemoryError(f"Extra plugin duplicates an active profile plugin: {manifest.id}")
        # A bundled manifest may be enabled on demand without pretending it is
        # third-party. Its path/hash were verified by verify_official_catalog.
        if manifest.source_path in official_manifest_paths:
            trusted_plugins.add(manifest.id)
        extra_manifests.append(manifest)
    manager = PluginManager(
        trusted_plugins=trusted_plugins,
        require_signatures=bool(definition.get("requireSignatures", False)),
        developer_mode=developer_mode,
        signature_verifier=(
            team_signature_verifier_from_environment()
            if bool(definition.get("requireSignatures", False))
            else None
        ),
        state_database=root / ".wiki-memory" / "data" / "plugin-versions.sqlite3",
    )
    manager.services.register("events", "core", engine.events)
    manager.services.register("evidence", "core", engine.evidence)
    manager.services.register("projections", "core", engine.projections)
    manager.services.register("event-bus", "core", engine.bus)
    manager.services.register("memory-engine", "core", engine)
    for item in definition["plugins"]:
        if isinstance(item, str):
            plugin_id, config = item, {}
        else:
            plugin_id = str(item["id"])
            config = dict(item.get("config") or {})
        entry = verified_catalog.get(plugin_id)
        if entry is None:
            raise MemoryError(f"Profile references a plugin absent from the official catalog: {plugin_id}")
        manifest_path = CATALOG_ROOT / entry["manifest"]
        config.update((config_overrides or {}).get(plugin_id, {}))
        manager.add(PluginManifest.load(manifest_path), config)
    for manifest in extra_manifests:
        manager.add(manifest, dict((config_overrides or {}).get(manifest.id, {})))
    asyncio.run(manager.activate_all(secret_handles))
    return engine, manager


def profile_report(
    root: Path,
    profile: str = "solo",
    *,
    config_overrides: dict[str, dict[str, Any]] | None = None,
    secret_handles: dict[str, str] | None = None,
) -> dict[str, Any]:
    _, manager = build_profile(
        root,
        profile,
        config_overrides=config_overrides,
        secret_handles=secret_handles,
    )
    return {
        "profile": profile,
        "services": manager.services.snapshot(),
        "plugins": manager.report(),
        "ok": all(item["state"] == "active" for item in manager.report()),
    }
