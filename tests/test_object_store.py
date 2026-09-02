from __future__ import annotations

import hashlib
import tempfile
import threading
import unittest
from pathlib import Path

from wiki_memory.object_store import FileObjectStore


class FileObjectStoreTests(unittest.TestCase):
    def test_concurrent_put_of_identical_evidence_is_atomic(self) -> None:
        """Concurrent sources must not share or lose the staging file."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.txt"
            source.write_bytes(b"canonical evidence\n" * 2048)
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            store = FileObjectStore(root / "objects")
            barrier = threading.Barrier(12)
            failures: list[BaseException] = []

            def upload() -> None:
                try:
                    barrier.wait()
                    store.put_file(digest, source, "text/plain")
                except BaseException as exc:  # asserted after every thread joins
                    failures.append(exc)

            threads = [threading.Thread(target=upload) for _ in range(12)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(failures, [])
            self.assertTrue(store.verify(digest))
            self.assertEqual(list((root / "objects").rglob("*.tmp")), [])

    def test_verified_upload_repairs_a_corrupt_canonical_blob(self) -> None:
        """Create-only publication must not weaken the bit-rot repair path."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.txt"
            source.write_bytes(b"canonical evidence\n")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            store = FileObjectStore(root / "objects")
            store.put_file(digest, source, "text/plain")
            store._path(digest).write_bytes(b"corrupt")

            self.assertFalse(store.verify(digest))
            store.put_file(digest, source, "text/plain")

            self.assertTrue(store.verify(digest))
