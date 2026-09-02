from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .capture import _parse_frontmatter
from .config import CONFIG_NAME, REGISTRY_NAME, load_memory, load_registry, load_vault
from .installation import validate_installation_layout
from .layout import STIGNORE
from .dependencies import dependency_report
from .temporal import (
    iter_temporal_notes,
    note_index,
    parse_temporal_date,
    resolve_wikilink,
    source_links,
    source_valid_from,
    subtract_calendar_months,
    supersession_findings,
    supersession_proposal,
    temporal_decision,
    validate_temporal_metadata,
)


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


def lint_memory(
    root: Path,
    contradiction_pairs: list[tuple[str, str]] | None = None,
    *,
    observed_at: str | None = None,
) -> dict[str, Any]:
    root = root.resolve()
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
    temporal_notes = list(iter_temporal_notes(root))
    for note in temporal_notes:
        errors.extend(validate_temporal_metadata(note))
    warnings.extend(supersession_findings(root, temporal_notes))

    proposals: list[dict[str, Any]] = []
    pairs: list[tuple[str, str]] = list(contradiction_pairs or [])
    indexed = note_index(temporal_notes)
    by_path = {note.path: note for note in temporal_notes}
    for newer in temporal_notes:
        if not newer.metadata.get("supersedes"):
            continue
        older_path = resolve_wikilink(
            root,
            newer.path,
            newer.metadata.get("supersedes"),
            by_stem=indexed,
        )
        older = by_path.get(older_path) if older_path is not None else None
        if older is None:
            continue
        incomplete = (
            older.metadata.get("superseded_by") in (None, "")
            or older.metadata.get("valid_until") in (None, "")
            or older.metadata.get("invalidated_at") in (None, "")
        )
        if incomplete:
            pairs.append((older.relative_path, newer.relative_path))

    seen_pairs: set[frozenset[str]] = set()
    for left, right in pairs:
        pair_key = frozenset((str(left), str(right)))
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)
        try:
            proposals.append(supersession_proposal(root, left, right, observed_at=observed_at))
        except (OSError, ValueError) as exc:
            errors.append({"code": "invalid-contradiction-pair", "path": str(left), "detail": str(exc)})
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "resolution_proposals": proposals,
        "counts": {"notes": len(markdown), "temporal_notes": len(temporal_notes)},
    }


def maintenance_report(
    root: Path,
    *,
    older_than_months: int,
    now: str | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    if older_than_months < 0:
        raise ValueError("older_than_months must be non-negative")
    reference = parse_temporal_date(now) if now else datetime.now(timezone.utc)
    if reference is None:  # pragma: no cover - guarded by the branch above
        reference = datetime.now(timezone.utc)
    cutoff = subtract_calendar_months(reference, older_than_months)
    notes = list(iter_temporal_notes(root))
    missing_valid_from = [
        {
            "path": note.relative_path,
            "kind": note.kind,
            "open_question": "When did this fact become true?",
        }
        for note in notes
        if note.metadata.get("valid_from") in (None, "")
    ]

    stale_current_facts: list[dict[str, Any]] = []
    for note in notes:
        visible, _ = temporal_decision(note.metadata, "current", reference)
        if not visible:
            continue
        dated_sources: list[tuple[Path, datetime, str]] = []
        for source_path in source_links(root, note):
            source_metadata, _ = _parse_frontmatter(source_path.read_text(encoding="utf-8", errors="replace"))
            source_date = source_valid_from(source_metadata)
            if source_date is None:
                continue
            parsed = parse_temporal_date(source_date)
            if parsed is not None:
                dated_sources.append((source_path, parsed, source_date))
        if not dated_sources:
            continue
        newest_source = max(dated_sources, key=lambda item: item[1])
        if newest_source[1] < cutoff:
            stale_current_facts.append(
                {
                    "path": note.relative_path,
                    "newest_source": newest_source[0].relative_to(root).as_posix(),
                    "source_date": newest_source[2],
                    "older_than_months": older_than_months,
                }
            )

    broken_chains = supersession_findings(root, notes)
    return {
        "ok": True,
        "read_only": True,
        "as_of": reference.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "older_than_months": older_than_months,
        "missing_valid_from": missing_valid_from,
        "stale_current_facts": stale_current_facts,
        "broken_supersession_chains": broken_chains,
        "counts": {
            "missing_valid_from": len(missing_valid_from),
            "stale_current_facts": len(stale_current_facts),
            "broken_supersession_chains": len(broken_chains),
        },
    }


def doctor_memory(root: Path) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, severity: str = "error") -> None:
        checks.append({"name": name, "ok": ok, "severity": severity, "detail": detail})

    add("memory-config", (root / CONFIG_NAME).is_file(), CONFIG_NAME)
    add("vault-registry", (root / REGISTRY_NAME).is_file(), REGISTRY_NAME)
    try:
        from .engine import MemoryEngine

        canonical = MemoryEngine(root).verify()
        add(
            "canonical-ledger",
            bool(canonical["ok"]),
            f"{canonical['ledger']['events']} immutable events; {len(canonical['corruptEvidence'])} corrupt evidence blobs",
        )
    except Exception as exc:
        add("canonical-ledger", False, str(exc))
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
        transport_ignore = memory_root / ".wiki-memory" / "data" / ".stignore"
        transport_text = transport_ignore.read_text(encoding="utf-8") if transport_ignore.is_file() else ""
        add(
            "syncthing-transport",
            "events.sqlite3" in transport_text and "outbox/**" in transport_text,
            "only immutable blobs and event packs are synchronized",
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
        add("syncthing-transport", True, "multi-device synchronization is disabled")
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

    def is_generated_runtime_path(file: Path) -> bool:
        # Virtual environments and temporary wheel smoke environments embed
        # their absolute creation path by design. They are ignored by Git and
        # must not turn a source-tree privacy check into a false positive.
        return any(part.startswith((".venv", ".build-")) for part in file.parts)

    for file in path.rglob("*"):
        if not file.is_file() or any(part in excluded for part in file.parts) or is_generated_runtime_path(file):
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
