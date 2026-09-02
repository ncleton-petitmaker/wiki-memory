#!/usr/bin/env python3
"""Generate or verify the deterministic official plugin catalog."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wiki_memory.plugins import PluginManifest


def generated_catalog(catalog_root: Path) -> dict[str, object]:
    plugins: list[dict[str, str]] = []
    for path in sorted(catalog_root.glob("*/plugin.yaml")):
        manifest = PluginManifest.load(path)
        plugins.append(
            {
                "id": manifest.id,
                "version": manifest.version,
                "manifest": path.relative_to(catalog_root).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    return {"format": "wiki-memory-plugin-catalog/v1", "plugins": plugins}


def canonical_json(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate or verify the Wiki Memory plugin catalog")
    parser.add_argument("--catalog", type=Path, default=ROOT / "src/wiki_memory/plugin_catalog")
    parser.add_argument("--write", action="store_true", help="write catalog.json instead of checking it")
    args = parser.parse_args()
    catalog_root = args.catalog.resolve()
    target = catalog_root / "catalog.json"
    expected = generated_catalog(catalog_root)
    if args.write:
        target.write_text(canonical_json(expected), encoding="utf-8")
        print(f"catalog-written {target}")
        return 0
    try:
        actual = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"catalog-invalid {exc}", file=sys.stderr)
        return 1
    if actual != expected:
        print("catalog-drift: run python scripts/plugin_catalog.py --write", file=sys.stderr)
        return 1
    print(f"catalog-valid plugins={len(expected['plugins'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
