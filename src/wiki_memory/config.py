from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - bootstrap installs PyYAML
    yaml = None


CONFIG_NAME = "memory.config.yaml"
REGISTRY_NAME = "vaults.registry.yaml"
VAULT_CONFIG_NAME = "vault.yaml"
SCHEMA_VERSION = 1


class MemoryError(RuntimeError):
    """Raised for invalid or unsafe Wiki Memory operations."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_value).strip("-").lower()
    if not slug:
        raise MemoryError("The value cannot be converted to a safe slug.")
    return slug[:64]


def ensure_root(path: str | Path) -> Path:
    root = Path(path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise MemoryError(f"Memory root does not exist: {root}")
    if not (root / CONFIG_NAME).is_file():
        raise MemoryError(f"Not a Wiki Memory root: {root}")
    return root


def safe_child(root: Path, relative: str | Path) -> Path:
    child = (root / relative).resolve()
    try:
        child.relative_to(root.resolve())
    except ValueError as exc:
        raise MemoryError(f"Path escapes memory root: {relative}") from exc
    return child


def load_data(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise MemoryError(f"Missing configuration: {path}")
    raw = path.read_text(encoding="utf-8")
    try:
        if yaml is not None:
            data = yaml.safe_load(raw)
        else:
            data = json.loads(raw)
    except Exception as exc:
        raise MemoryError(f"Invalid YAML/JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise MemoryError(f"Expected an object in {path}")
    return data


def write_data(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if yaml is not None:
        rendered = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100)
    else:
        rendered = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    path.write_text(rendered, encoding="utf-8")


def load_memory(root: Path) -> dict[str, Any]:
    return load_data(root / CONFIG_NAME)


def load_registry(root: Path) -> dict[str, Any]:
    return load_data(root / REGISTRY_NAME)


def load_vault(root: Path, slug: str) -> tuple[Path, dict[str, Any]]:
    registry = load_registry(root)
    entry = next((item for item in registry.get("vaults", []) if item.get("slug") == slug), None)
    if entry is None:
        raise MemoryError(f"Unknown vault: {slug}")
    vault_path = safe_child(root, entry["path"])
    return vault_path, load_data(vault_path / VAULT_CONFIG_NAME)


def content_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def platform_runtime_dir() -> Path:
    override = os.environ.get("WIKI_MEMORY_RUNTIME")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "WikiMemory"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "WikiMemory"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "wiki-memory"


def root_runtime_dir(root: Path) -> Path:
    fingerprint = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:12]
    return platform_runtime_dir() / "memories" / fingerprint
