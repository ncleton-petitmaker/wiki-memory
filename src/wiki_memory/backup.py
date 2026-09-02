from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any

from .config import MemoryError, ensure_root, safe_child, utc_now
from .engine import MemoryEngine
from .projections import MarkdownProjector


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def create_backup(root: Path, destination: Path) -> dict[str, Any]:
    root = ensure_root(root)
    engine = MemoryEngine(root)
    projector = engine.projections.projectors.get("projection.markdown")
    if isinstance(projector, MarkdownProjector) and projector.modified_files(root):
        raise MemoryError(
            "Markdown projections contain unreviewed edits; run markdown-edits and review them before backup."
        )
    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise MemoryError(f"Backup destination already exists: {destination}")
    with tempfile.TemporaryDirectory(prefix="wiki-memory-backup-") as temporary_directory:
        staging = Path(temporary_directory) / "memory"
        staging.mkdir()
        excluded_parts = {".git", ".obsidian", "__pycache__", ".cache", ".qmd"}
        device_local_files = {Path(".wiki-memory/team-session.json")}
        for source in root.rglob("*"):
            relative = source.relative_to(root)
            if any(part in excluded_parts for part in relative.parts):
                continue
            if relative in device_local_files:
                continue
            if source.is_symlink():
                raise MemoryError(f"Backup refuses symbolic links inside the memory root: {relative}")
            if relative in {
                Path(".wiki-memory/data/events.sqlite3"),
                Path(".wiki-memory/data/plugin-versions.sqlite3"),
            } or source.name.endswith(("-wal", "-shm")):
                continue
            target = staging / relative
            if source.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)

        database_target = staging / ".wiki-memory" / "data" / "events.sqlite3"
        database_target.parent.mkdir(parents=True, exist_ok=True)
        source_connection = engine.events.connect()
        target_connection = sqlite3.connect(database_target)
        try:
            source_connection.backup(target_connection)
        finally:
            target_connection.close()
            source_connection.close()

        # Plugin activation/migration versions are operational state, but they
        # still need a coherent snapshot: copying a live WAL database byte for
        # byte would be just as unsafe as copying the canonical ledger.
        plugin_state = root / ".wiki-memory" / "data" / "plugin-versions.sqlite3"
        if plugin_state.is_file():
            state_target = staging / ".wiki-memory" / "data" / "plugin-versions.sqlite3"
            state_source = sqlite3.connect(plugin_state)
            state_destination = sqlite3.connect(state_target)
            try:
                state_source.backup(state_destination)
            finally:
                state_destination.close()
                state_source.close()

        files = {
            path.relative_to(staging).as_posix(): {"sha256": _sha256(path), "size": path.stat().st_size}
            for path in staging.rglob("*")
            if path.is_file()
        }
        manifest = {
            "format": "wiki-memory-backup/v1",
            "createdAt": utc_now(),
            "schemaVersion": 2,
            "eventCount": engine.events.count(),
            "files": files,
        }
        (staging / "BACKUP-MANIFEST.json").write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        archive_descriptor, archive_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        os.close(archive_descriptor)
        temporary_archive = Path(archive_name)
        try:
            with tarfile.open(temporary_archive, "w:gz") as archive:
                archive.add(staging, arcname="memory", recursive=True)
            verification = verify_backup(temporary_archive)
            if not verification["ok"]:
                raise MemoryError("Backup verification failed: " + json.dumps(verification["errors"]))
            os.replace(temporary_archive, destination)
            if os.name != "nt":
                descriptor = os.open(destination.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            temporary_archive.unlink(missing_ok=True)
    return {"ok": True, "path": str(destination), "events": engine.events.count(), "files": len(files)}


def _safe_members(archive: tarfile.TarFile, target: Path) -> list[tarfile.TarInfo]:
    safe: list[tarfile.TarInfo] = []
    target = target.resolve()
    members = archive.getmembers()
    maximum_members = int(os.environ.get("WIKI_MEMORY_BACKUP_MAX_MEMBERS", "1000000"))
    maximum_bytes = int(os.environ.get("WIKI_MEMORY_BACKUP_MAX_BYTES", str(1024 * 1024 * 1024 * 1024)))
    if len(members) > maximum_members or sum(max(member.size, 0) for member in members) > maximum_bytes:
        raise MemoryError("Backup exceeds configured extraction limits.")
    for member in members:
        destination = (target / member.name).resolve()
        try:
            destination.relative_to(target)
        except ValueError as exc:
            raise MemoryError(f"Backup contains an unsafe path: {member.name}") from exc
        if not (member.isdir() or member.isreg()):
            raise MemoryError(f"Backup contains a non-file entry: {member.name}")
        safe.append(member)
    return safe


def verify_backup(archive_path: Path) -> dict[str, Any]:
    archive_path = archive_path.expanduser().resolve()
    errors: list[dict[str, str]] = []
    if not archive_path.is_file():
        return {"ok": False, "errors": [{"code": "missing", "detail": str(archive_path)}]}
    with tempfile.TemporaryDirectory(prefix="wiki-memory-verify-") as temporary_directory:
        target = Path(temporary_directory)
        try:
            with tarfile.open(archive_path, "r:gz") as archive:
                archive.extractall(target, members=_safe_members(archive, target))
            root = target / "memory"
            manifest_path = root / "BACKUP-MANIFEST.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("format") != "wiki-memory-backup/v1":
                errors.append({"code": "format", "detail": str(manifest.get("format"))})
            expected_files = set(str(item) for item in manifest.get("files", {}))
            actual_files = {
                path.relative_to(root).as_posix()
                for path in root.rglob("*")
                if path.is_file() and path != manifest_path
            }
            if actual_files != expected_files:
                errors.append(
                    {
                        "code": "file-set",
                        "detail": json.dumps(
                            {
                                "missing": sorted(expected_files - actual_files)[:20],
                                "unexpected": sorted(actual_files - expected_files)[:20],
                            },
                            sort_keys=True,
                        ),
                    }
                )
            for relative, expected in manifest.get("files", {}).items():
                path = safe_child(root, relative)
                if not path.is_file():
                    errors.append({"code": "missing-file", "detail": relative})
                elif _sha256(path) != expected.get("sha256"):
                    errors.append({"code": "hash", "detail": relative})
            database = root / ".wiki-memory" / "data" / "events.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
                count = int(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0])
            if integrity != "ok":
                errors.append({"code": "sqlite", "detail": integrity})
            if count != int(manifest.get("eventCount", -1)):
                errors.append({"code": "event-count", "detail": f"{count} != {manifest.get('eventCount')}"})
        except Exception as exc:
            errors.append({"code": "archive", "detail": str(exc)})
    return {"ok": not errors, "path": str(archive_path), "errors": errors}


def restore_backup(archive_path: Path, target: Path) -> dict[str, Any]:
    verification = verify_backup(archive_path)
    if not verification["ok"]:
        raise MemoryError("Cannot restore an invalid backup.")
    target = target.expanduser().resolve()
    if target.exists() and any(target.iterdir()):
        raise MemoryError(f"Restore target is not empty: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{target.name}.wiki-memory-restore-", dir=target.parent
    ) as temporary_directory:
        temporary = Path(temporary_directory)
        with tarfile.open(archive_path.expanduser().resolve(), "r:gz") as archive:
            archive.extractall(temporary, members=_safe_members(archive, temporary))
        restored = temporary / "memory"
        (restored / "BACKUP-MANIFEST.json").unlink(missing_ok=True)
        engine = MemoryEngine(restored)
        ledger = engine.events.verify()
        corrupt_evidence = [
            metadata.reference
            for metadata in engine.evidence.iter_metadata()
            if not engine.evidence.verify(metadata.reference)
        ]
        if not ledger["ok"] or corrupt_evidence:
            raise MemoryError("Restored memory failed canonical ledger or evidence verification.")
        rebuilt = engine.rebuild()
        if not rebuilt["ok"]:
            raise MemoryError(f"Restored memory projection rebuild failed: {rebuilt['error']}")
        if not engine.verify()["ok"]:
            raise MemoryError("Restored memory failed final verification after rebuild.")
        restored_event_count = engine.events.count()
        if target.exists():
            target.rmdir()  # Already checked empty; never remove user content.
        os.replace(restored, target)
    return {"ok": True, "target": str(target), "events": restored_event_count}
