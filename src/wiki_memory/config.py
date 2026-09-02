from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
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
SCHEMA_VERSION = 2


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
    config = load_data(root / CONFIG_NAME)
    version = int(config.get("schema_version", 0))
    if version != SCHEMA_VERSION:
        raise MemoryError(
            f"Unsupported Wiki Memory schema {version}; this V1 engine requires schema {SCHEMA_VERSION}. "
            "Create a new memory root. Existing roots are never modified automatically."
        )
    return root


def safe_child(root: Path, relative: str | Path) -> Path:
    """Resolve a user-controlled child without permitting traversal or symlink escape.

    ``Path.relative_to`` has historically disagreed with Windows drive/case
    normalization for a not-yet-created nested path.  Compare normalized OS
    paths instead, after resolving existing symlinks, so a valid content
    address works on Windows while a drive, ``..`` segment, or symlink escape
    remains fail-closed everywhere.
    """

    root = root.resolve()
    requested = Path(relative)
    if requested.is_absolute() or ".." in requested.parts:
        raise MemoryError(f"Path escapes memory root: {relative}")
    child = (root / requested).resolve()
    normalized_root = os.path.normcase(os.path.normpath(str(root)))
    normalized_child = os.path.normcase(os.path.normpath(str(child)))
    try:
        common = os.path.normcase(os.path.normpath(os.path.commonpath([normalized_root, normalized_child])))
    except ValueError as exc:  # different drives on Windows
        raise MemoryError(f"Path escapes memory root: {relative}") from exc
    if common != normalized_root:
        raise MemoryError(f"Path escapes memory root: {relative}")
    return child


def load_data(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise MemoryError(f"Missing configuration: {path}")
    raw = path.read_text(encoding="utf-8")
    try:
        if yaml is not None:
            data = yaml.safe_load(raw)
        else:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = _safe_minimal_yaml(raw)
    except Exception as exc:
        raise MemoryError(f"Invalid YAML/JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise MemoryError(f"Expected an object in {path}")
    return data


def _yaml_scalar(value: str) -> Any:
    value = value.strip()
    if value == "":
        return None
    if value in {"null", "~"}:
        return None
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if value.startswith("[") and value.endswith("]"):
        body = value[1:-1].strip()
        return [] if not body else [_yaml_scalar(item) for item in body.split(",")]
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        return value


def _safe_minimal_yaml(raw: str) -> Any:
    """Parse the non-ambiguous YAML subset used by bundled manifests.

    User-authored YAML still requires PyYAML. This fallback deliberately rejects
    anchors, tags, multiline scalars, duplicate keys, and mixed list/map blocks.
    """

    prepared: list[tuple[int, str]] = []
    for number, line in enumerate(raw.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "\t" in line[: len(line) - len(line.lstrip())]:
            raise ValueError(f"Tabs are not allowed in YAML indentation (line {number}).")
        text = line.lstrip(" ")
        if any(token in text for token in ("!!", "&", "*")) or text in {"|", ">"}:
            raise ValueError(f"Unsupported YAML feature on line {number}.")
        prepared.append((len(line) - len(text), text))
    if not prepared:
        return {}

    def parse_block(index: int, indent: int) -> tuple[Any, int]:
        if prepared[index][0] != indent:
            raise ValueError("Invalid YAML indentation.")
        list_mode = prepared[index][1].startswith("- ")
        container: Any = [] if list_mode else {}
        while index < len(prepared):
            current_indent, text = prepared[index]
            if current_indent < indent:
                break
            if current_indent > indent:
                raise ValueError("Unexpected YAML indentation.")
            if list_mode:
                if not text.startswith("- "):
                    raise ValueError("Cannot mix YAML lists and mappings in one block.")
                item_text = text[2:].strip()
                if not item_text:
                    if index + 1 >= len(prepared) or prepared[index + 1][0] <= indent:
                        raise ValueError("Empty YAML list item.")
                    item, index = parse_block(index + 1, prepared[index + 1][0])
                    container.append(item)
                    continue
                if ":" in item_text:
                    key, value = item_text.split(":", 1)
                    item: dict[str, Any] = {key.strip(): _yaml_scalar(value)}
                    index += 1
                    if index < len(prepared) and prepared[index][0] > indent:
                        extra, index = parse_block(index, prepared[index][0])
                        if not isinstance(extra, dict):
                            raise ValueError("YAML list mapping extension must be an object.")
                        for extra_key, extra_value in extra.items():
                            if extra_key in item:
                                raise ValueError(f"Duplicate YAML key: {extra_key}")
                            item[extra_key] = extra_value
                    container.append(item)
                    continue
                container.append(_yaml_scalar(item_text))
                index += 1
                continue
            if text.startswith("- ") or ":" not in text:
                raise ValueError("Expected a YAML mapping entry.")
            key, value = text.split(":", 1)
            key = key.strip()
            if key in container:
                raise ValueError(f"Duplicate YAML key: {key}")
            index += 1
            if value.strip():
                container[key] = _yaml_scalar(value)
            elif index < len(prepared) and prepared[index][0] > indent:
                container[key], index = parse_block(index, prepared[index][0])
            else:
                container[key] = {}
        return container, index

    parsed, final_index = parse_block(0, prepared[0][0])
    if final_index != len(prepared):
        raise ValueError("Could not parse complete YAML document.")
    return parsed


def write_data(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if yaml is not None:
        rendered = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100)
    else:
        rendered = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


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
