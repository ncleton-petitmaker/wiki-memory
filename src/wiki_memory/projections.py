from __future__ import annotations

import json
import hashlib
import mimetypes
import os
import shutil
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .config import MemoryError, load_vault, root_runtime_dir, safe_child
from .events import EventStore, MemoryEvent
from .evidence import parse_reference


class Projector(Protocol):
    id: str
    version: str

    def apply(self, root: Path, event: MemoryEvent) -> list[Path]: ...

    def reset(self, root: Path, *, force: bool = False) -> None: ...


class ClosingProjectionConnection(sqlite3.Connection):
    """SQLite context manager that commits/rolls back and closes its descriptor."""

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc, traceback))
        finally:
            self.close()


def render_frontmatter(metadata: dict[str, Any]) -> str:
    lines = ["---"]
    for key, value in metadata.items():
        if isinstance(value, list):
            if value:
                lines.append(f"{key}:")
                lines.extend(f"  - {json.dumps(item, ensure_ascii=False)}" for item in value)
            else:
                lines.append(f"{key}: []")
        elif isinstance(value, dict):
            lines.append(f"{key}: {json.dumps(value, ensure_ascii=False, sort_keys=True)}")
        elif value is None:
            lines.append(f"{key}: null")
        elif isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, (int, float)):
            lines.append(f"{key}: {value}")
        else:
            lines.append(f"{key}: {json.dumps(str(value), ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines)


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.wiki-memory-", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
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


@contextmanager
def projection_lock(root: Path, projection_id: str, timeout_seconds: float = 30.0):
    """Serialize filesystem projection writes across threads and processes."""

    lock_path = root_runtime_dir(root) / "locks" / f"{projection_id}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    if lock_path.stat().st_size == 0:
        handle.write(b"0")
        handle.flush()
    deadline = time.monotonic() + timeout_seconds
    acquired = False
    try:
        while not acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    raise MemoryError(
                        f"Timed out waiting for projection lock: {projection_id}"
                    )
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


class MarkdownProjector:
    id = "projection.markdown"
    version = "1.0.0"

    def __init__(self) -> None:
        self._state_name = "markdown-generated.sqlite3"

    def _state_path(self, root: Path) -> Path:
        return root / ".wiki-memory" / "projections" / self._state_name

    def state_available(self, root: Path) -> bool:
        """Whether the per-file hashes expected by a checkpoint still exist."""

        return self._state_path(root).is_file()

    def initialize_state(self, root: Path) -> None:
        """Create an empty state database even when an event projects no file."""

        with self._connect_state(root):
            pass

    def _connect_state(self, root: Path) -> sqlite3.Connection:
        """Open rebuildable per-file projection state.

        This must remain outside the canonical ledger.  A JSON object rewritten
        after every projected event turns an otherwise append-only capture path
        quadratic as the vault grows.  SQLite gives the same deterministic
        state, an atomic per-file upsert, and efficient review/reset scans.
        """

        state = self._state_path(root)
        state.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(state, timeout=30, factory=ClosingProjectionConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        # The projection state is rebuildable, but a capture must not fail
        # merely because another first capture is enabling WAL for this tiny
        # sidecar database. Mirror the ledger's bounded setup retry.
        for attempt in range(12):
            try:
                mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
                if mode != "wal":
                    connection.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 11:
                    connection.close()
                    raise
                time.sleep(min(0.01 * (2**attempt), 0.25))
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS generated_files (
                relative_path TEXT PRIMARY KEY,
                sha256 TEXT NOT NULL,
                event_id TEXT NOT NULL,
                scope TEXT NOT NULL,
                space_id TEXT NOT NULL
            )
            """
        )
        return connection

    def _state_for_path(self, root: Path, path: Path) -> dict[str, Any]:
        relative = path.resolve().relative_to(root.resolve()).as_posix()
        with self._connect_state(root) as connection:
            row = connection.execute(
                "SELECT sha256,event_id,scope,space_id FROM generated_files WHERE relative_path=?", (relative,)
            ).fetchone()
        return dict(row) if row else {}

    @staticmethod
    def _hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while block := handle.read(1024 * 1024):
                digest.update(block)
        return digest.hexdigest()

    def modified_files(self, root: Path) -> list[dict[str, Any]]:
        modified: list[dict[str, Any]] = []
        with self._connect_state(root) as connection:
            states = [dict(row) for row in connection.execute("SELECT * FROM generated_files")]
        for state in states:
            relative = str(state.pop("relative_path"))
            path = safe_child(root, relative)
            expected = state.get("sha256")
            if expected and path.is_file():
                actual = self._hash(path)
                if actual != expected:
                    modified.append({"path": relative, "expectedSha256": expected, "actualSha256": actual, **state})
        return modified

    def _is_modified(self, root: Path, path: Path) -> bool:
        state = self._state_for_path(root, path)
        return bool(state.get("sha256") and path.is_file() and self._hash(path) != state["sha256"])

    def _remember(self, root: Path, paths: list[Path], event: MemoryEvent) -> None:
        rows = [
            (
                path.resolve().relative_to(root.resolve()).as_posix(),
                self._hash(path),
                event.event_id,
                event.scope,
                event.space_id,
            )
            for path in paths
            if path.is_file()
        ]
        if not rows:
            return
        with self._connect_state(root) as connection:
            connection.executemany(
                """
                INSERT INTO generated_files(relative_path,sha256,event_id,scope,space_id)
                VALUES (?,?,?,?,?)
                ON CONFLICT(relative_path) DO UPDATE SET
                    sha256=excluded.sha256,
                    event_id=excluded.event_id,
                    scope=excluded.scope,
                    space_id=excluded.space_id
                """,
                rows,
            )

    def _write(self, root: Path, path: Path, content: str, event: MemoryEvent) -> Path:
        state = self._state_for_path(root, path)
        expected = state.get("sha256")
        if expected and path.is_file() and self._hash(path) != expected:
            pending = self._state_path(root).parent / "pending" / f"{event.event_id}-{path.name}"
            atomic_write(pending, content)
            return pending
        atomic_write(path, content)
        return path

    def _source_path(self, root: Path, payload: dict[str, Any]) -> tuple[Path, Path, dict[str, Any]]:
        vault_slug = str(payload["vault"])
        vault_path, vault = load_vault(root, vault_slug)
        source_root = vault_path / vault["folders"]["sources"]
        partition = Path(str(payload.get("partition") or ""))
        if partition.is_absolute() or ".." in partition.parts:
            raise MemoryError("Source projection partition must be relative.")
        source_id = str(payload["sourceId"])
        item_path = safe_child(source_root / "items", partition / f"{source_id}.md")
        return source_root, item_path, vault

    def apply(self, root: Path, event: MemoryEvent) -> list[Path]:
        created: list[Path] = []
        if event.event_type in {"source.captured", "source.revised", "source.audio.captured", "source.published"}:
            payload = event.payload
            source_root, item_path, vault = self._source_path(root, payload)
            item_modified = self._is_modified(root, item_path)
            if item_path.exists() and event.stream_version > 1 and not item_modified:
                partition = item_path.relative_to(source_root / "items").parent
                revision_path = safe_child(
                    source_root / "revisions",
                    partition / str(payload["sourceId"]) / f"{event.stream_version - 1:08d}.md",
                )
                if not revision_path.exists():
                    atomic_write(revision_path, item_path.read_text(encoding="utf-8"))
                created.append(revision_path)
            metadata = dict(payload.get("metadata") or {})
            partition = item_path.relative_to(source_root / "items").parent
            projected_raw: str | None = None
            projected_media: list[str] = []
            for index, reference in enumerate(event.evidence_refs):
                digest = parse_reference(reference)
                blob = root / ".wiki-memory" / "data" / "blobs" / "sha256" / digest[:2] / digest[2:4] / digest
                metadata_path = blob.with_suffix(".metadata.json")
                evidence_metadata: dict[str, Any] = {}
                if metadata_path.is_file():
                    evidence_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                original_name = str(evidence_metadata.get("original_name") or "")
                suffix = Path(original_name).suffix
                if not suffix:
                    suffix = mimetypes.guess_extension(str(evidence_metadata.get("media_type") or "")) or ".bin"
                if index == 0:
                    evidence_path = safe_child(
                        source_root / "raw",
                        partition / str(payload["sourceId"]) / f"{digest[:12]}{suffix}",
                    )
                else:
                    vault_path, _ = load_vault(root, str(payload["vault"]))
                    evidence_path = safe_child(
                        vault_path / vault["folders"]["assets"],
                        partition / f"{payload['sourceId']}-{digest[:12]}{suffix}",
                    )
                evidence_path.parent.mkdir(parents=True, exist_ok=True)
                if not evidence_path.exists():
                    try:
                        os.link(blob, evidence_path)
                    except OSError:
                        shutil.copy2(blob, evidence_path)
                created.append(evidence_path)
                vault_path, _ = load_vault(root, str(payload["vault"]))
                relative_evidence = evidence_path.relative_to(vault_path).as_posix()
                if index == 0:
                    projected_raw = relative_evidence
                else:
                    projected_media.append(relative_evidence)
            metadata.update(
                {
                    "id": payload["sourceId"],
                    "event_id": event.event_id,
                    "stream_id": event.stream_id,
                    "stream_version": event.stream_version,
                    "scope": event.scope,
                    "space_id": event.space_id,
                    "acl": event.acl,
                    "recorded_at": event.recorded_at,
                    "occurred_at": event.occurred_at,
                    "evidence_refs": event.evidence_refs,
                    "raw": projected_raw,
                    "media": projected_media,
                    "projection": f"{self.id}@{self.version}",
                }
            )
            title = str(payload.get("title") or payload["sourceId"])
            body = str(payload.get("body") or "").strip()
            projected_path = self._write(
                root,
                item_path,
                render_frontmatter(metadata) + f"\n\n# {title}\n\n{body}\n",
                event,
            )
            created.append(projected_path)
        elif event.event_type == "source.deleted":
            _, item_path, _ = self._source_path(root, event.payload)
            if item_path.exists():
                if self._is_modified(root, item_path):
                    marker = self._state_path(root).parent / "pending" / f"{event.event_id}-{item_path.name}.delete"
                    atomic_write(marker, json.dumps({"eventId": event.event_id, "path": str(item_path)}) + "\n")
                    created.append(marker)
                else:
                    item_path.unlink()
        elif event.event_type == "transcription.created":
            payload = event.payload
            source_root, item_path, _ = self._source_path(root, payload)
            metadata = dict(payload.get("metadata") or {})
            metadata.update(
                {
                    "id": payload["sourceId"],
                    "event_id": event.event_id,
                    "stream_id": event.stream_id,
                    "stream_version": event.stream_version,
                    "recorded_at": event.recorded_at,
                    "scope": event.scope,
                    "space_id": event.space_id,
                    "acl": event.acl,
                    "evidence_refs": event.evidence_refs,
                    "transcription": payload.get("transcription"),
                    "projection": f"{self.id}@{self.version}",
                }
            )
            if item_path.exists() and not self._is_modified(root, item_path):
                partition = item_path.relative_to(source_root / "items").parent
                revision_path = safe_child(
                    source_root / "revisions",
                    partition / str(payload["sourceId"]) / f"{event.stream_version - 1:08d}.md",
                )
                atomic_write(revision_path, item_path.read_text(encoding="utf-8"))
                created.append(revision_path)
            title = str(payload.get("title") or payload["sourceId"])
            created.append(
                self._write(
                    root,
                    item_path,
                    render_frontmatter(metadata) + f"\n\n# {title}\n\n{str(payload.get('body') or '').strip()}\n",
                    event,
                )
            )
        elif event.event_type == "assertion.accepted":
            payload = event.payload
            vault_path, vault = load_vault(root, str(payload["vault"]))
            relative = Path(str(payload.get("path") or f"{payload['assertionId']}.md"))
            if relative.is_absolute() or ".." in relative.parts:
                raise MemoryError("Assertion projection path must be relative.")
            path = safe_child(vault_path / vault["folders"]["wiki"], relative)
            metadata = dict(payload.get("metadata") or {})
            metadata.update(
                {
                    "assertion_id": payload["assertionId"],
                    "event_id": event.event_id,
                    "scope": event.scope,
                    "space_id": event.space_id,
                    "acl": event.acl,
                    "epistemic_status": payload.get("epistemicStatus", "fact"),
                    "recorded_at": event.recorded_at,
                    "valid_from": payload.get("validFrom"),
                    "valid_until": payload.get("validUntil"),
                    "evidence_refs": event.evidence_refs,
                    "projection": f"{self.id}@{self.version}",
                }
            )
            title = str(payload.get("title") or payload["assertionId"])
            body = str(payload.get("body") or "").strip()
            created.append(
                self._write(root, path, render_frontmatter(metadata) + f"\n\n# {title}\n\n{body}\n", event)
            )
        elif event.event_type == "projection.edit.accepted":
            relative = str(event.payload["path"])
            path = safe_child(root, relative)
            if not event.evidence_refs:
                raise MemoryError("Accepted projection edits require evidence.")
            digest = parse_reference(event.evidence_refs[0])
            blob = root / ".wiki-memory" / "data" / "blobs" / "sha256" / digest[:2] / digest[2:4] / digest
            atomic_write(path, blob.read_text(encoding="utf-8"))
            created.append(path)
        elif event.event_type in {"assertion.retracted", "assertion.superseded"}:
            payload = event.payload
            if payload.get("vault") and payload.get("assertionId"):
                vault_path, vault = load_vault(root, str(payload["vault"]))
                relative = Path(str(payload.get("path") or f"{payload['assertionId']}.md"))
                if relative.is_absolute() or ".." in relative.parts:
                    raise MemoryError("Assertion projection path must be relative.")
                path = safe_child(vault_path / vault["folders"]["wiki"], relative)
                if path.is_file():
                    from .capture import _parse_frontmatter

                    metadata, body = _parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
                    metadata["invalidated_at"] = event.recorded_at
                    metadata["epistemic_status"] = (
                        "superseded" if event.event_type == "assertion.superseded" else "retracted"
                    )
                    if payload.get("supersededBy"):
                        metadata["superseded_by"] = payload["supersededBy"]
                    created.append(self._write(root, path, render_frontmatter(metadata) + "\n" + body, event))
        if created:
            self._remember(root, created, event)
        return created

    def reset(self, root: Path, *, force: bool = False) -> None:
        modified = self.modified_files(root)
        if modified and not force:
            raise MemoryError("Markdown projection contains unreviewed local edits; run markdown-edits before rebuilding.")
        with self._connect_state(root) as connection:
            paths = [str(row["relative_path"]) for row in connection.execute("SELECT relative_path FROM generated_files")]
        for relative in paths:
            path = safe_child(root, str(relative))
            if path.is_file():
                path.unlink()
        state = self._state_path(root)
        for suffix in ("", "-wal", "-shm"):
            Path(str(state) + suffix).unlink(missing_ok=True)


@dataclass
class ProjectionResult:
    projection_id: str
    from_position: int
    to_position: int
    processed: int
    rebuilt: bool
    error: str | None = None


class ProjectionRegistry:
    def __init__(self, root: Path, events: EventStore):
        self.root = root.resolve()
        self.events = events
        self.projectors: dict[str, Projector] = {}

    def register(self, projector: Projector) -> None:
        if projector.id in self.projectors:
            raise MemoryError(f"Projection already registered: {projector.id}")
        self.projectors[projector.id] = projector

    def unregister(self, projection_id: str) -> None:
        self.projectors.pop(projection_id, None)

    def _update_unlocked(self, projection_id: str) -> ProjectionResult:
        projector = self.projectors[projection_id]
        checkpoint = self.events.projection_checkpoint(projection_id)
        if (
            checkpoint
            and isinstance(projector, MarkdownProjector)
            and not projector.state_available(self.root)
        ):
            error = (
                "Markdown projection state is missing; refusing to overwrite possible local edits. "
                "Run an explicitly reviewed rebuild with --force."
            )
            self.events.record_projection_failure(projection_id, max(checkpoint[0] + 1, 1), error)
            return ProjectionResult(projection_id, checkpoint[0], checkpoint[0], 0, False, error)
        if checkpoint is None and isinstance(projector, MarkdownProjector):
            # A proposal or other non-Markdown event can be the first event in
            # the ledger.  Record an empty state before checkpointing it so
            # later generated files are not mistaken for a lost state index.
            projector.initialize_state(self.root)
        if checkpoint and checkpoint[1] != projector.version:
            error = f"Projection {projection_id} changed from {checkpoint[1]} to {projector.version}; rebuild is required."
            self.events.record_projection_failure(projection_id, max(checkpoint[0] + 1, 1), error)
            return ProjectionResult(projection_id, checkpoint[0], checkpoint[0], 0, False, error)
        start = checkpoint[0] if checkpoint else 0
        processed = 0
        end = start
        for event in self.events.iter_events(start):
            try:
                projector.apply(self.root, event)
            except Exception as exc:
                self.events.record_projection_failure(projection_id, int(event.position or 0), str(exc))
                return ProjectionResult(projection_id, start, end, processed, False, str(exc))
            end = int(event.position or end)
            self.events.set_projection_checkpoint(projection_id, end, projector.version)
            processed += 1
        return ProjectionResult(projection_id, start, end, processed, False)

    def update(self, projection_id: str) -> ProjectionResult:
        with projection_lock(self.root, projection_id):
            return self._update_unlocked(projection_id)

    def update_all(self) -> list[ProjectionResult]:
        return [self.update(projection_id) for projection_id in self.projectors]

    def rebuild(self, projection_id: str, *, force: bool = False) -> ProjectionResult:
        with projection_lock(self.root, projection_id):
            projector = self.projectors[projection_id]
            checkpoint = self.events.projection_checkpoint(projection_id)
            if (
                checkpoint
                and isinstance(projector, MarkdownProjector)
                and not projector.state_available(self.root)
                and not force
            ):
                raise MemoryError(
                    "Markdown projection state is missing; rebuild --force is required to avoid overwriting local edits."
                )
            projector.reset(self.root, force=force)
            # Checkpoint updates are mutable projection state, not canonical events.
            with self.events.connect() as connection:
                connection.execute("DELETE FROM projection_checkpoints WHERE projection_id = ?", (projection_id,))
                connection.execute("DELETE FROM projection_failures WHERE projection_id = ?", (projection_id,))
            result = self._update_unlocked(projection_id)
            result.rebuilt = True
            return result


def generated_projection_hash(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()
