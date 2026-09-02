#!/usr/bin/env python3
"""Exercise the solo durability boundary with real process termination.

This is intentionally a release-gate harness rather than a benchmark. A worker
only records an acknowledgement after the evidence blob has been fsynced and the
event transaction has committed. The parent repeatedly terminates workers, then
opens the ledger afresh and proves that every acknowledged event and its evidence
survived. Extra unacknowledged events are acceptable: at-least-once delivery
must never turn them into an acknowledgement lie.

Run against a disposable initialized memory:

    uv run python scripts/crash_campaign.py --root /tmp/wiki-memory-crash --rounds 100
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


# The harness also works when called directly by a source checkout, without
# relying on an editable install being present in the invoking Python runtime.
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
# ``scripts/wiki_memory.py`` is a CLI shim. Put ``src`` before the script
# directory even when an editable install has already appended it elsewhere.
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from wiki_memory.engine import MemoryEngine
from wiki_memory.events import EventActor, MemoryEvent, PluginRef


def _append_acknowledgement(path: Path, event_id: str, reference: str) -> None:
    """Persist an external acknowledgement after the canonical transaction."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({"eventId": event_id, "evidenceRef": reference}, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _mark_ready(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="ascii", newline="\n") as handle:
        handle.write("ready\n")
        handle.flush()
        os.fsync(handle.fileno())


def worker(
    root: Path,
    acknowledgement_log: Path,
    ready_path: Path | None,
    start: int,
    count: int,
    delay: float,
) -> None:
    engine = MemoryEngine(root)
    if ready_path is not None:
        _mark_ready(ready_path)
    for offset in range(count):
        sequence = start + offset
        evidence = engine.evidence.put_bytes(
            f"crash-campaign evidence {sequence}".encode("utf-8"),
            media_type="text/plain",
            original_name=f"crash-{sequence}.txt",
        )
        event = MemoryEvent(
            event_type="test.crash-campaign",
            stream_id=f"crash-campaign:{sequence}",
            idempotency_key=f"crash-campaign:{sequence}",
            actor=EventActor("system", "crash-campaign"),
            plugin=PluginRef("crash-campaign", "1.0.0"),
            evidence_refs=[evidence.reference],
            payload={"sequence": sequence},
        )
        persisted, _ = engine.append(event, project=False)
        _append_acknowledgement(acknowledgement_log, persisted.event_id, evidence.reference)
        # A deterministic interval gives the parent enough opportunities to
        # terminate between blob creation, commit, and acknowledgement.
        if delay:
            time.sleep(delay)


def _read_acknowledgements(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    acknowledgements: list[dict[str, str]] = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
                event_id = str(value["eventId"])
                evidence_ref = str(value["evidenceRef"])
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"Invalid acknowledgement at line {number}") from exc
            acknowledgements.append({"eventId": event_id, "evidenceRef": evidence_ref})
    return acknowledgements


def run_campaign(
    root: Path,
    *,
    rounds: int = 100,
    events_per_worker: int = 20,
    delay: float = 0.01,
    seed: int = 7,
) -> dict[str, Any]:
    """Kill workers at randomized points and prove every durable acknowledgement.

    ``root`` must be an initialized, disposable memory. The campaign appends
    test events, so it is deliberately never wired into ordinary user commands.
    """

    if rounds < 1 or events_per_worker < 1 or delay < 0:
        raise ValueError("rounds and events_per_worker must be positive; delay cannot be negative")
    root = root.expanduser().resolve()
    # Opening the engine here is both an initialization check and a recovery
    # check before the first child is launched.
    MemoryEngine(root)
    acknowledgement_log = root.parent / f".{root.name}.crash-campaign-acks.jsonl"
    acknowledgement_log.unlink(missing_ok=True)
    generator = random.Random(seed)
    terminated = 0
    # Random early interruption is excellent at exploring write boundaries but
    # can, on a very slow traced/interpreted runtime, kill every worker before
    # it has emitted an acknowledgement. One round therefore waits for a real
    # durable acknowledgement and then still force-terminates that worker. The
    # campaign always proves the acknowledgement boundary rather than merely
    # proving that interrupted startup is harmless.
    acknowledgement_round = rounds // 2
    for round_index in range(rounds):
        ready_path = root.parent / f".{root.name}.crash-campaign-{round_index}.ready"
        ready_path.unlink(missing_ok=True)
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            "--root",
            str(root),
            "--acknowledgements",
            str(acknowledgement_log),
            "--ready",
            str(ready_path),
            "--start",
            str(round_index * events_per_worker),
            "--count",
            str(events_per_worker),
            "--delay",
            str(delay),
        ]
        process = subprocess.Popen(command, cwd=REPOSITORY_ROOT)
        deadline = time.monotonic() + 10
        while not ready_path.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.001)
        if not ready_path.exists():
            process.kill()
            process.wait(timeout=10)
            raise RuntimeError(f"Crash worker {round_index} did not initialize")
        if round_index == acknowledgement_round:
            acknowledged_before = len(_read_acknowledgements(acknowledgement_log))
            acknowledgement_deadline = time.monotonic() + 10
            while (
                len(_read_acknowledgements(acknowledgement_log)) <= acknowledged_before
                and process.poll() is None
                and time.monotonic() < acknowledgement_deadline
            ):
                time.sleep(0.001)
            if len(_read_acknowledgements(acknowledgement_log)) <= acknowledged_before:
                process.kill()
                process.wait(timeout=10)
                raise RuntimeError(f"Crash worker {round_index} did not acknowledge")
        else:
            # Do not rely on a fixed timing relationship with SQLite. Every
            # other round samples a distinct interruption point across its
            # short worker window.
            time.sleep(generator.uniform(delay / 4 if delay else 0.001, max(delay * 1.5, 0.004)))
        if process.poll() is None:
            process.kill()  # SIGKILL on POSIX; forced termination on Windows.
            terminated += 1
        process.wait(timeout=10)
        ready_path.unlink(missing_ok=True)

    recovered = MemoryEngine(root)
    ledger = recovered.verify()
    if not ledger["ok"]:
        raise RuntimeError(f"Ledger did not recover cleanly: {ledger}")
    acknowledgements = _read_acknowledgements(acknowledgement_log)
    for acknowledgement in acknowledgements:
        event = recovered.events.get(acknowledgement["eventId"])
        if event is None:
            raise RuntimeError(f"Acknowledged event is absent after crash recovery: {acknowledgement['eventId']}")
        if acknowledgement["evidenceRef"] not in event.evidence_refs:
            raise RuntimeError(f"Acknowledged event lost its evidence reference: {event.event_id}")
        if not recovered.evidence.verify(acknowledgement["evidenceRef"]):
            raise RuntimeError(f"Acknowledged evidence is corrupt: {acknowledgement['evidenceRef']}")
    rebuild = recovered.rebuild(force=True)
    if not rebuild["ok"]:
        raise RuntimeError(f"Projection rebuild failed after crash recovery: {rebuild}")
    return {
        "ok": True,
        "rounds": rounds,
        "terminatedWorkers": terminated,
        "acknowledgedEvents": len(acknowledgements),
        "ledgerEvents": recovered.events.count(),
        "rebuild": rebuild,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Wiki Memory kill -9 durability campaign")
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--rounds", type=int, default=100)
    parser.add_argument("--events-per-worker", type=int, default=20)
    parser.add_argument("--delay", type=float, default=0.01)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--acknowledgements", type=Path)
    parser.add_argument("--ready", type=Path)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--count", type=int, default=1)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.worker:
        if args.acknowledgements is None:
            raise SystemExit("--acknowledgements is required for a worker")
        worker(args.root, args.acknowledgements, args.ready, args.start, args.count, args.delay)
        return 0
    result = run_campaign(
        args.root,
        rounds=args.rounds,
        events_per_worker=args.events_per_worker,
        delay=args.delay,
        seed=args.seed,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
