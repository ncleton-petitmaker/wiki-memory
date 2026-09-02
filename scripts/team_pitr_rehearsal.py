#!/usr/bin/env python3
"""Run an isolated PostgreSQL WAL-recovery rehearsal for Wiki Memory Team.

The rehearsal creates only synthetic events and a local FileObjectStore.  It
proves that a base backup plus archived WAL can recover an event committed
after the backup, then invokes the same Team restore verifier used by an
operator.  It is intentionally not a claim that an operator's managed
PostgreSQL/S3 production retention policy has been configured correctly.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wiki_memory.events import EventActor, MemoryEvent, PluginRef
from wiki_memory.object_store import FileObjectStore
from wiki_memory.team import normalize_acl
from wiki_memory.team_repository import PostgresTeamRepository


def require(binary: str) -> str:
    path = shutil.which(binary)
    if path is None:
        raise RuntimeError(f"{binary} is required for the PostgreSQL PITR rehearsal.")
    return path


def available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as handle:
        handle.bind(("127.0.0.1", 0))
        return int(handle.getsockname()[1])


def command(arguments: list[str], *, environment: dict[str, str] | None = None) -> None:
    try:
        completed = subprocess.run(
            arguments,
            text=True,
            capture_output=True,
            env=environment,
            check=False,
            timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"PostgreSQL rehearsal command timed out: {arguments[0]}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown command failure"
        raise RuntimeError(f"PostgreSQL rehearsal command failed: {detail}")


def stop(pg_ctl: str, cluster: Path) -> None:
    if (cluster / "postmaster.pid").is_file():
        subprocess.run([pg_ctl, "-D", str(cluster), "-m", "fast", "-w", "stop"], capture_output=True, check=False)


def wait_for(condition, *, label: str, seconds: float = 30.0) -> None:
    deadline = time.monotonic() + seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if condition():
                return
        except Exception as exc:  # PostgreSQL may still be applying WAL
            last_error = exc
        time.sleep(0.1)
    suffix = f": {last_error}" if last_error else ""
    raise RuntimeError(f"Timed out waiting for {label}{suffix}")


def append_event(repository: PostgresTeamRepository, *, number: int, evidence_ref: str, owner: str, space_id: str) -> None:
    event = MemoryEvent(
        event_type="source.captured",
        stream_id=f"pitr-rehearsal:{number}",
        idempotency_key=f"pitr-rehearsal:{number}",
        actor=EventActor("system", owner),
        plugin=PluginRef("pitr-rehearsal", "1.0.0"),
        scope="team",
        space_id=space_id,
        acl=normalize_acl({}, owner=owner, space_id=space_id),
        evidence_refs=[evidence_ref],
        payload={"body": f"Synthetic PITR rehearsal event {number}."},
    )
    persisted, created = repository.append(event, expected_stream_version=0)
    if not created or persisted.stream_version != 1:
        raise RuntimeError("Synthetic PITR event was not durably appended.")


def config_literal(path: Path) -> str:
    # tempfile paths contain no quotes today, but PostgreSQL configuration
    # remains correct even if a platform changes that implementation detail.
    return str(path).replace("'", "''")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a synthetic PostgreSQL WAL-recovery rehearsal for Team")
    parser.add_argument("--keep", action="store_true", help="preserve the synthetic rehearsal directory for inspection")
    args = parser.parse_args()

    initdb = require("initdb")
    pg_ctl = require("pg_ctl")
    pg_basebackup = require("pg_basebackup")
    createdb = require("createdb")

    root = Path(tempfile.mkdtemp(prefix="wiki-memory-team-pitr-"))
    primary = root / "primary"
    restored = root / "restored"
    archive = root / "wal-archive"
    socket_dir = root / "socket"
    evidence_root = root / "objects"
    archive.mkdir()
    socket_dir.mkdir()
    primary_port = available_port()
    restored_port = available_port()
    database = "wiki_memory_pitr"
    username = getpass.getuser()
    primary_dsn = f"postgresql://127.0.0.1:{primary_port}/{database}"
    restored_dsn = f"postgresql://127.0.0.1:{restored_port}/{database}"

    try:
        command([initdb, "-D", str(primary), "-A", "trust", "--no-locale", "--encoding=UTF8"])
        with (primary / "postgresql.conf").open("a", encoding="utf-8") as configuration:
            configuration.write("\nwal_level = replica\nmax_wal_senders = 5\narchive_mode = on\n")
            configuration.write(
                "archive_command = 'test ! -f "
                + config_literal(archive)
                + "/%f && cp %p "
                + config_literal(archive)
                + "/%f'\n"
            )
        # ``pg_ctl -l`` is essential here: otherwise the detached postmaster
        # inherits subprocess's captured stderr pipe and makes ``pg_ctl -w``
        # appear to hang even after the server is ready.
        command(
            [
                pg_ctl,
                "-D",
                str(primary),
                "-l",
                str(root / "primary.log"),
                "-o",
                f"-k {socket_dir} -p {primary_port}",
                "-w",
                "start",
            ]
        )
        command([createdb, "-h", str(socket_dir), "-p", str(primary_port), database])

        store = FileObjectStore(evidence_root)
        proof = root / "synthetic-proof.txt"
        proof.write_text("synthetic PITR evidence\n", encoding="utf-8")
        digest = hashlib.sha256(proof.read_bytes()).hexdigest()
        store.put_file(digest, proof, "text/plain")
        evidence_ref = f"sha256:{digest}"
        owner = "pitr-operator"
        space_id = "pitr-space"
        repository = PostgresTeamRepository(primary_dsn)
        repository.initialize()
        append_event(repository, number=1, evidence_ref=evidence_ref, owner=owner, space_id=space_id)

        backup = root / "base-backup"
        command(
            [
                pg_basebackup,
                "-D",
                str(backup),
                "-h",
                str(socket_dir),
                "-p",
                str(primary_port),
                "-U",
                username,
                "-X",
                "none",
            ]
        )
        append_event(repository, number=2, evidence_ref=evidence_ref, owner=owner, space_id=space_id)
        # Close the current WAL segment so the post-backup commit becomes
        # available to the restore command before the primary is stopped.
        import psycopg

        with psycopg.connect(primary_dsn, autocommit=True) as connection:
            connection.execute("SELECT pg_switch_wal()")
        wait_for(lambda: any(archive.iterdir()), label="WAL archive")
        stop(pg_ctl, primary)

        shutil.copytree(backup, restored)
        (restored / "recovery.signal").touch()
        with (restored / "postgresql.conf").open("a", encoding="utf-8") as configuration:
            configuration.write(
                "\nrestore_command = 'cp " + config_literal(archive) + "/%f %p'\n"
            )
        command(
            [
                pg_ctl,
                "-D",
                str(restored),
                "-l",
                str(root / "restored.log"),
                "-o",
                f"-k {socket_dir} -p {restored_port}",
                "-w",
                "start",
            ]
        )

        def recovered_events() -> bool:
            import psycopg

            with psycopg.connect(restored_dsn) as connection:
                count = connection.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0]
            return int(count) == 2

        wait_for(recovered_events, label="post-backup event WAL replay")
        # A restore with no upstream primary can promote itself once every
        # available archived segment has been replayed.  Promote only when it
        # remains in recovery; both branches prove the same recovered state.
        with psycopg.connect(restored_dsn) as connection:
            in_recovery = bool(connection.execute("SELECT pg_is_in_recovery()").fetchone()[0])
        if in_recovery:
            command([pg_ctl, "-D", str(restored), "-w", "promote"])

        environment = os.environ | {
            "DATABASE_URL": restored_dsn,
            "TEAM_FILE_OBJECT_STORE": str(evidence_root),
        }
        verifier = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "team_restore_verify.py")],
            text=True,
            capture_output=True,
            env=environment,
            check=False,
        )
        if verifier.returncode != 0:
            raise RuntimeError(f"Team restore verifier failed: {verifier.stderr.strip() or verifier.stdout.strip()}")
        report = json.loads(verifier.stdout)
        if not report.get("ok") or int((report.get("rebuild") or {}).get("documents", 0)) != 2:
            raise RuntimeError("Recovered Team ledger did not contain both synthetic events.")
        output = {"ok": True, "events": 2, "evidenceMode": report["evidenceMode"]}
        if args.keep:
            output["directory"] = str(root)
        print(json.dumps(output, sort_keys=True))
        return 0
    finally:
        stop(pg_ctl, primary)
        stop(pg_ctl, restored)
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
