#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wiki_memory.config import MemoryError, load_data
from wiki_memory.plugins import PluginManager, PluginManifest, PluginState
from wiki_memory.profiles import verify_official_catalog


def validate_catalog(catalog: Path) -> dict[str, object]:
    manifests = sorted(catalog.glob("*/plugin.yaml"))
    errors: list[dict[str, str]] = []
    ids: set[str] = set()
    capabilities: dict[str, str] = {}
    for path in manifests:
        try:
            raw = load_data(path)
            manifest = PluginManifest.from_dict(raw, path)
            if manifest.id in ids:
                raise MemoryError(f"duplicate id {manifest.id}")
            ids.add(manifest.id)
            for capability in manifest.provides:
                capabilities.setdefault(capability, manifest.id)
            schema_path = (path.parent / str(manifest.config_schema)).resolve()
            json.loads(schema_path.read_text(encoding="utf-8"))
            # Validate the empty/default configuration when the schema permits it.
            required = json.loads(schema_path.read_text(encoding="utf-8")).get("required", [])
            if not required:
                manifest.validate_config({})
        except Exception as exc:
            errors.append({"manifest": str(path.relative_to(ROOT)), "error": str(exc)})
    for path in manifests:
        try:
            manifest = PluginManifest.load(path)
            missing = [item for item in manifest.requires if item not in capabilities and item not in {
                "events", "evidence", "projections", "event-bus", "memory-engine"
            }]
            if missing:
                errors.append({"manifest": str(path.relative_to(ROOT)), "error": "unprovided capabilities: " + ", ".join(missing)})
        except Exception:
            pass
    return {"ok": not errors, "manifests": len(manifests), "errors": errors}


def lifecycle_probe() -> dict[str, object]:
    manager = PluginManager(trusted_plugins={"missing-provider"})
    manifest = PluginManifest.from_dict(
        {
            "apiVersion": "wiki-memory/v1", "id": "missing-provider", "version": "1.0.0",
            "minimumSdkVersion": "1.0.0",
            "runtime": "python", "entrypoint": "wiki_memory.builtin_plugins:backup_local",
            "provides": ["probe"], "requires": ["not.available"],
            "permissions": {}, "configSchema": "config.schema.json"
        },
        ROOT / "src/wiki_memory/plugin_catalog/transcriber-mistral/plugin.yaml",
    )
    manager.add(manifest, {})
    asyncio.run(manager.activate_all())
    state = manager.fibers[manifest.id].state
    return {"ok": state == PluginState.PENDING, "state": state.value, "message": manager.fibers[manifest.id].message}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("catalog", nargs="?", default=str(ROOT / "src/wiki_memory/plugin_catalog"))
    args = parser.parse_args()
    catalog_path = Path(args.catalog)
    try:
        official_catalog = {"ok": True, "plugins": len(verify_official_catalog(catalog_path))}
    except MemoryError as exc:
        official_catalog = {"ok": False, "error": str(exc)}
    result = {"catalog": validate_catalog(catalog_path), "officialCatalog": official_catalog, "lifecycle": lifecycle_probe()}
    result["ok"] = bool(result["catalog"]["ok"] and result["officialCatalog"]["ok"] and result["lifecycle"]["ok"])
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
