from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .capture import _parse_frontmatter
from .config import CONFIG_NAME, REGISTRY_NAME, load_memory, load_registry, load_vault
from .installation import validate_installation_layout
from .layout import STIGNORE
from .dependencies import dependency_report


REQUIRED_SOURCE_FIELDS = {
    "id",
    "source_type",
    "captured_at",
    "connector",
    "content_hash",
    "vault",
    "epistemic_status",
    "revision",
    "raw",
    "media",
}


def _syncthing_ignore_is_safe(path: Path) -> bool:
    if not path.is_file():
        return False
    required = {line.strip() for line in STIGNORE.splitlines() if line.strip() and not line.startswith("//")}
    actual = {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("//")}
    return required.issubset(actual)


def lint_memory(root: Path) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    note_names: dict[str, list[str]] = {}
    markdown = [path for path in root.rglob("*.md") if ".git" not in path.parts]
    for path in markdown:
        note_names.setdefault(path.stem.lower(), []).append(str(path.relative_to(root)))
    links = re.compile(r"\[\[([^\]|#]+)")
    linked_stems: set[str] = set()
    for path in markdown:
        text = path.read_text(encoding="utf-8", errors="replace")
        for target in links.findall(text):
            stem = Path(target).stem.lower()
            linked_stems.add(stem)
            if stem not in note_names:
                warnings.append({"code": "broken-wikilink", "path": str(path.relative_to(root)), "detail": target})
            elif len(note_names[stem]) > 1 and "/" not in target:
                warnings.append({"code": "ambiguous-wikilink", "path": str(path.relative_to(root)), "detail": target})
    for entry in load_registry(root).get("vaults", []):
        vault_path, vault = load_vault(root, entry["slug"])
        source_items = vault_path / vault["folders"]["sources"] / "items"
        for path in source_items.rglob("*.md"):
            metadata, _ = _parse_frontmatter(path.read_text(encoding="utf-8"))
            missing = sorted(REQUIRED_SOURCE_FIELDS - set(metadata))
            if missing:
                errors.append({"code": "source-frontmatter", "path": str(path.relative_to(root)), "detail": ",".join(missing)})
            raw = metadata.get("raw")
            if raw and not (vault_path / raw).exists():
                errors.append({"code": "missing-raw", "path": str(path.relative_to(root)), "detail": str(raw)})
            if path.stem.lower() not in linked_stems:
                warnings.append({"code": "orphan-source", "path": str(path.relative_to(root)), "detail": "not linked"})
    return {"ok": not errors, "errors": errors, "warnings": warnings, "counts": {"notes": len(markdown)}}


def doctor_memory(root: Path) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, severity: str = "error") -> None:
        checks.append({"name": name, "ok": ok, "severity": severity, "detail": detail})

    add("memory-config", (root / CONFIG_NAME).is_file(), CONFIG_NAME)
    add("vault-registry", (root / REGISTRY_NAME).is_file(), REGISTRY_NAME)
    try:
        _, agent_root, memory_root = validate_installation_layout(root)
        add("installation-layout", True, "sibling Agent/ and Mémoire/ directories")
    except Exception as exc:
        agent_root, memory_root = root.parent / "Agent", root
        add("installation-layout", False, str(exc))
    try:
        registry = load_registry(root)
        for entry in registry.get("vaults", []):
            vault_path, vault = load_vault(root, entry["slug"])
            missing = [name for name in vault["folders"].values() if not (vault_path / name).is_dir()]
            add(f"vault:{entry['slug']}", not missing, "missing: " + ", ".join(missing) if missing else "layout complete")
    except Exception as exc:
        add("vault-layout", False, str(exc))
    try:
        sync = load_memory(root).get("sync", {})
    except Exception:
        sync = {}
    sync_enabled = bool(sync.get("enabled", False))
    if sync_enabled:
        for label, folder in (("agent", agent_root), ("memory", memory_root)):
            ignore_path = folder / ".stignore"
            ignore_ok = _syncthing_ignore_is_safe(ignore_path)
            add(
                f"syncthing-ignore:{label}",
                ignore_ok,
                "contains every required exclusion" if ignore_ok else f"copy required rules from syncthing.ignore.template to {folder.name}/.stignore on this device",
            )
        folder_ids = sync.get("folder_ids") or {}
        add(
            "syncthing-folders",
            set(folder_ids) == {"agent", "memory"},
            "separate Agent and Mémoire folder IDs" if set(folder_ids) == {"agent", "memory"} else "run syncthing-setup to register both folders",
        )
    else:
        add("syncthing-ignore:agent", True, "multi-device synchronization is disabled")
        add("syncthing-ignore:memory", True, "multi-device synchronization is disabled")
        add("syncthing-folders", True, "multi-device synchronization is disabled")
    versioning = bool(sync.get("versioning_confirmed", False))
    backup_detail = (
        "Syncthing is not a backup; confirm versioning or a separate backup."
        if sync_enabled
        else "Multi-device synchronization is disabled; confirm a separate backup."
    )
    add("backup-or-versioning", versioning, backup_detail, "warning")
    for dependency in dependency_report():
        optional_syncthing = dependency.name == "syncthing" and not sync_enabled
        severity = "error" if dependency.required or (dependency.name == "syncthing" and sync_enabled) else "warning"
        detail = (
            "optional; multi-device synchronization is disabled"
            if optional_syncthing
            else dependency.detail if dependency.installed else f"{dependency.detail} Official download: {dependency.official_url}"
        )
        add(
            f"dependency:{dependency.name}",
            dependency.installed or optional_syncthing,
            detail,
            severity,
        )
    lint = lint_memory(root) if (root / REGISTRY_NAME).is_file() else {"ok": False, "errors": []}
    add("lint", bool(lint["ok"]), f"{len(lint.get('errors', []))} errors")
    blocking = [item for item in checks if not item["ok"] and item["severity"] == "error"]
    return {"ok": not blocking, "checks": checks}


def scan_privacy(path: Path) -> dict[str, Any]:
    findings = []
    unix_home_roots = "/" + "Users/" + "|/" + "home/"
    private_key_marker = "-" * 5 + "BEGIN "
    forbidden_patterns = {
        "absolute-user-path": re.compile(r"(?:" + unix_home_roots + r")[^/\s]+/|[A-Za-z]:\\" + "Users" + r"\\[^\\\s]+\\"),
        "private-key": re.compile(private_key_marker + r"(?:RSA |EC |OPENSSH )?PRIVATE KEY" + "-" * 5),
        "generic-secret": re.compile(r"(?i)(?:api[_-]?key|access[_-]?token|client[_-]?secret)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}"),
        "browser-cookie": re.compile(r"(?i)(?:sessionid|auth_token|li_at)\s*[:=]\s*['\"]?[^\s'\"]{12,}"),
    }
    excluded = {".git", "node_modules", ".venv", "venv", "__pycache__"}
    for file in path.rglob("*"):
        if not file.is_file() or any(part in excluded for part in file.parts):
            continue
        if file.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".sqlite"}:
            continue
        try:
            text = file.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for name, pattern in forbidden_patterns.items():
            if pattern.search(text):
                findings.append({"code": name, "path": str(file.relative_to(path))})
    return {"ok": not findings, "findings": findings}
