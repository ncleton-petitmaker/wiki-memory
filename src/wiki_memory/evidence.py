from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO, Iterator

from .config import MemoryError, utc_now


@dataclass(frozen=True)
class EvidenceMetadata:
    reference: str
    sha256: str
    size: int
    media_type: str
    original_name: str | None
    created_at: str


def parse_reference(reference: str) -> str:
    if not reference.startswith("sha256:"):
        raise MemoryError(f"Unsupported evidence reference: {reference}")
    digest = reference.split(":", 1)[1].lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise MemoryError(f"Invalid SHA-256 evidence reference: {reference}")
    return digest


class EvidenceStore:
    """Immutable content-addressed evidence storage."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _blob_path(self, digest: str) -> Path:
        # Digest validation makes these path components safe on every
        # platform; avoid generic path resolution for a child that may not
        # exist yet on Windows.
        return self.root / digest[:2] / digest[2:4] / digest

    def _metadata_path(self, digest: str) -> Path:
        return self._blob_path(digest).with_suffix(".metadata.json")

    def has(self, reference: str) -> bool:
        try:
            digest = parse_reference(reference)
            blob = self._blob_path(digest)
            metadata_path = self._metadata_path(digest)
            if not blob.is_file() or not metadata_path.is_file():
                return False
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            return (
                metadata.get("reference") == reference
                and metadata.get("sha256") == digest
                and int(metadata.get("size", -1)) == blob.stat().st_size
            )
        except (MemoryError, OSError, TypeError, ValueError, json.JSONDecodeError):
            return False

    def _verify_blob(self, digest: str) -> bool:
        """Verify bytes without requiring metadata during interrupted recovery."""

        actual = hashlib.sha256()
        try:
            with self._blob_path(digest).open("rb") as handle:
                while block := handle.read(1024 * 1024):
                    actual.update(block)
        except OSError:
            return False
        return actual.hexdigest() == digest

    def put_bytes(
        self,
        content: bytes,
        *,
        media_type: str = "application/octet-stream",
        original_name: str | None = None,
    ) -> EvidenceMetadata:
        return self.put_stream(iter((content,)), media_type=media_type, original_name=original_name)

    def put_file(
        self,
        source: Path,
        *,
        media_type: str | None = None,
        original_name: str | None = None,
    ) -> EvidenceMetadata:
        source = source.expanduser().resolve()
        if not source.is_file():
            raise MemoryError(f"Evidence file does not exist: {source}")

        def chunks() -> Iterator[bytes]:
            with source.open("rb") as handle:
                while block := handle.read(1024 * 1024):
                    yield block

        detected = media_type or mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        return self.put_stream(chunks(), media_type=detected, original_name=original_name or source.name)

    def put_stream(
        self,
        chunks: Iterator[bytes],
        *,
        media_type: str,
        original_name: str | None,
    ) -> EvidenceMetadata:
        digest = hashlib.sha256()
        size = 0
        self.root.mkdir(parents=True, exist_ok=True)
        file_descriptor, temporary_name = tempfile.mkstemp(prefix="evidence-", dir=self.root)
        try:
            with os.fdopen(file_descriptor, "wb") as handle:
                for block in chunks:
                    if not isinstance(block, bytes):
                        raise MemoryError("Evidence streams must yield bytes.")
                    digest.update(block)
                    size += len(block)
                    handle.write(block)
                handle.flush()
                os.fsync(handle.fileno())
            hexadecimal = digest.hexdigest()
            target = self._blob_path(hexadecimal)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                Path(temporary_name).unlink(missing_ok=True)
                # A kill after the atomic blob rename but before the metadata
                # rename leaves a safe orphan. Replaying identical input repairs
                # metadata below; it must not reject the sound blob as corrupt.
                if target.stat().st_size != size or not self._verify_blob(hexadecimal):
                    raise MemoryError(f"Evidence hash collision or corrupted blob: {hexadecimal}")
            else:
                os.replace(temporary_name, target)
                self._fsync_directory(target.parent)
            metadata = EvidenceMetadata(
                reference=f"sha256:{hexadecimal}",
                sha256=hexadecimal,
                size=size,
                media_type=media_type,
                original_name=original_name,
                created_at=utc_now(),
            )
            metadata_path = self._metadata_path(hexadecimal)
            if not metadata_path.exists():
                metadata_fd, metadata_name = tempfile.mkstemp(prefix="metadata-", dir=metadata_path.parent)
                try:
                    with os.fdopen(metadata_fd, "w", encoding="utf-8") as handle:
                        handle.write(json.dumps(asdict(metadata), ensure_ascii=False, sort_keys=True, indent=2) + "\n")
                        handle.flush()
                        os.fsync(handle.fileno())
                    # Concurrent writers produce equivalent metadata except createdAt;
                    # the first durable record wins and is never rewritten.
                    try:
                        os.link(metadata_name, metadata_path)
                    except FileExistsError:
                        pass
                    else:
                        self._fsync_directory(metadata_path.parent)
                finally:
                    Path(metadata_name).unlink(missing_ok=True)
            return self.metadata(metadata.reference)
        finally:
            Path(temporary_name).unlink(missing_ok=True)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def metadata(self, reference: str) -> EvidenceMetadata:
        digest = parse_reference(reference)
        path = self._metadata_path(digest)
        if not path.is_file() or not self._blob_path(digest).is_file():
            raise MemoryError(f"Missing evidence: {reference}")
        value = json.loads(path.read_text(encoding="utf-8"))
        return EvidenceMetadata(**value)

    def path(self, reference: str) -> Path:
        digest = parse_reference(reference)
        path = self._blob_path(digest)
        if not path.is_file():
            raise MemoryError(f"Missing evidence: {reference}")
        return path

    def open(self, reference: str) -> BinaryIO:
        return self.path(reference).open("rb")

    def verify(self, reference: str) -> bool:
        digest = parse_reference(reference)
        try:
            metadata = self.metadata(reference)
            blob = self._blob_path(digest)
        except (MemoryError, OSError, TypeError, json.JSONDecodeError):
            return False
        return (
            metadata.reference == reference
            and metadata.sha256 == digest
            and metadata.size == blob.stat().st_size
            and self._verify_blob(digest)
        )

    def iter_metadata(self) -> Iterator[EvidenceMetadata]:
        for path in sorted(self.root.rglob("*.metadata.json")):
            try:
                yield EvidenceMetadata(**json.loads(path.read_text(encoding="utf-8")))
            except (OSError, TypeError, json.JSONDecodeError):
                continue
