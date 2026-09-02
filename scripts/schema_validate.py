#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wiki_memory.config import load_data


def main() -> None:
    try:
        import jsonschema
    except ImportError as exc:
        raise SystemExit("Install the dev extra to validate JSON schemas.") from exc
    errors: list[str] = []
    for path in sorted((ROOT / "schemas").glob("*.json")):
        try:
            jsonschema.Draft202012Validator.check_schema(json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:
            errors.append(f"{path.relative_to(ROOT)}: {exc}")
    manifest_schema = json.loads((ROOT / "schemas/plugin-manifest.schema.json").read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(manifest_schema)
    for path in sorted((ROOT / "src/wiki_memory/plugin_catalog").glob("*/plugin.yaml")):
        manifest = load_data(path)
        for error in validator.iter_errors(manifest):
            errors.append(f"{path.relative_to(ROOT)}: {error.message}")
        config_schema = manifest.get("configSchema")
        if isinstance(config_schema, str):
            schema_path = (path.parent / config_schema).resolve()
            try:
                schema_path.relative_to(path.parent.parent.resolve())
                jsonschema.Draft202012Validator.check_schema(
                    json.loads(schema_path.read_text(encoding="utf-8"))
                )
            except Exception as exc:
                errors.append(f"{path.relative_to(ROOT)} configSchema: {exc}")
    if errors:
        print("\n".join(errors))
        raise SystemExit(1)
    print("schemas-valid")


if __name__ == "__main__":
    main()
