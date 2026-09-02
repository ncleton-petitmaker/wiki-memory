from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import BinaryIO, Iterator

from .config import MemoryError


class ObjectStore(ABC):
    @abstractmethod
    def has(self, digest: str) -> bool: ...

    @abstractmethod
    def put_file(self, digest: str, path: Path, media_type: str = "application/octet-stream") -> None: ...

    @abstractmethod
    def open(self, digest: str) -> BinaryIO: ...

    def versioning_status(self) -> bool | None:
        """Return bucket versioning state when the backend can prove it.

        ``None`` intentionally means "not a versioned object-store backend",
        not success. Team production preflight treats it as an unmet gate.
        """

        return None

    def verify(self, digest: str) -> bool:
        """Hash the stored object before treating it as usable evidence."""

        digest = digest.lower()
        if not self.has(digest):
            return False
        actual = hashlib.sha256()
        try:
            handle = self.open(digest)
            try:
                while block := handle.read(1024 * 1024):
                    actual.update(block)
            finally:
                handle.close()
        except OSError:
            return False
        return actual.hexdigest() == digest


def verify_digest(path: Path, expected: str) -> None:
    actual = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            actual.update(block)
    if actual.hexdigest() != expected:
        raise MemoryError(f"Blob checksum mismatch: expected {expected}, got {actual.hexdigest()}")


def stream_and_close(handle: BinaryIO, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
    """Stream a local/S3 body while guaranteeing descriptor release."""

    try:
        while chunk := handle.read(chunk_size):
            yield chunk
    finally:
        handle.close()


class FileObjectStore(ObjectStore):
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, digest: str) -> Path:
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest.lower()):
            raise MemoryError("Invalid SHA-256 digest.")
        # A validated hexadecimal digest is intrinsically a safe relative
        # address.  Construct it directly: generic path resolution on Windows
        # can misclassify a not-yet-created content-addressed child.
        return self.root / digest[:2] / digest[2:4] / digest

    def has(self, digest: str) -> bool:
        return self._path(digest).is_file()

    def put_file(self, digest: str, path: Path, media_type: str = "application/octet-stream") -> None:
        digest = digest.lower()
        verify_digest(path, digest)
        target = self._path(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        replacing_corrupt_target = False
        if target.exists():
            try:
                verify_digest(target, digest)
                return
            except MemoryError:
                # The key is canonical but storage can still suffer bit rot.
                # The caller supplied independently verified bytes, so replace
                # atomically instead of perpetuating the corrupt object.
                replacing_corrupt_target = True
        # A digest may be submitted by several connectors at once.  A fixed
        # ``.tmp`` name makes otherwise harmless concurrent uploads race.  A
        # unique file in the destination directory preserves atomic replace
        # semantics and keeps a completed object on the same filesystem.
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{digest}.", suffix=".tmp", dir=target.parent)
        temporary = Path(temporary_name)
        try:
            # Open the staging descriptor first: if the source disappears
            # between its first verification and this copy, the descriptor is
            # still closed by the context manager.
            with os.fdopen(descriptor, "wb") as destination, path.open("rb") as source:
                shutil.copyfileobj(source, destination)
                destination.flush()
                os.fsync(destination.fileno())
            # Verify the exact bytes that will become canonical.  This also
            # protects against a source file changing after its initial hash.
            verify_digest(temporary, digest)
            if replacing_corrupt_target:
                # Repair is the only path allowed to replace a canonical name.
                # A reader can transiently lock it on Windows; another repairer
                # may also win while we wait, so verify after each failed try.
                for attempt in range(8):
                    try:
                        os.replace(temporary, target)
                        break
                    except PermissionError:
                        try:
                            verify_digest(target, digest)
                            break
                        except (MemoryError, OSError):
                            if attempt == 7:
                                raise
                            time.sleep(min(0.01 * (2**attempt), 0.25))
            else:
                # Publish without replacing an existing canonical blob.
                # ``replace`` is safe on POSIX but Windows rejects it while a
                # concurrent reader hashes the already-published object. A
                # same-filesystem hard link is atomic and create-only: one
                # uploader wins; the others verify the winner.
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    verify_digest(target, digest)
            if os.name != "nt":
                directory_descriptor = os.open(target.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
        finally:
            if temporary.exists():
                temporary.unlink()

    def open(self, digest: str) -> BinaryIO:
        path = self._path(digest.lower())
        if not path.is_file():
            raise MemoryError(f"Missing blob: {digest}")
        return path.open("rb")


class S3ObjectStore(ObjectStore):
    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str | None = None,
        region: str | None = None,
        prefix: str = "blobs/sha256",
    ) -> None:
        try:
            import boto3
        except ImportError as exc:
            raise MemoryError("S3 storage requires the 'server' optional dependencies.") from exc
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.client = boto3.client("s3", endpoint_url=endpoint_url, region_name=region)

    def _key(self, digest: str) -> str:
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest.lower()):
            raise MemoryError("Invalid SHA-256 digest.")
        return f"{self.prefix}/{digest[:2]}/{digest[2:4]}/{digest}"

    def has(self, digest: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(digest.lower()))
            return True
        except Exception as exc:
            response = getattr(exc, "response", {})
            status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status == 404:
                return False
            raise MemoryError(f"Object storage health check failed: {exc}") from exc

    def put_file(self, digest: str, path: Path, media_type: str = "application/octet-stream") -> None:
        digest = digest.lower()
        verify_digest(path, digest)
        if self.has(digest):
            if self.verify(digest):
                return
            # A corrupted object must be replaced by the verified source,
            # never silently accepted merely because its key exists.
        self.client.upload_file(
            str(path),
            self.bucket,
            self._key(digest),
            ExtraArgs={"ContentType": media_type, "Metadata": {"sha256": digest}},
        )
        if not self.verify(digest):
            raise MemoryError(f"Object store did not persist blob {digest}.")

    def open(self, digest: str) -> BinaryIO:
        response = self.client.get_object(Bucket=self.bucket, Key=self._key(digest.lower()))
        return response["Body"]

    def versioning_status(self) -> bool:
        try:
            response = self.client.get_bucket_versioning(Bucket=self.bucket)
        except Exception as exc:
            raise MemoryError("Object storage versioning status could not be verified.") from exc
        return response.get("Status") == "Enabled"
