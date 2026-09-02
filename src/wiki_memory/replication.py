from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .config import MemoryError, ensure_root, utc_now
from .engine import MemoryEngine
from .events import EventActor, MemoryEvent, PluginRef, canonical_json, idempotency_fingerprint


PACK_FORMAT = "wiki-memory-event-pack/v1"


def export_event_pack(
    root: Path,
    *,
    cursor: int = 0,
    destination: Path | None = None,
    scopes: set[str] | None = None,
) -> dict[str, Any]:
    root = ensure_root(root)
    engine = MemoryEngine(root)
    events = [event.to_dict() for event in engine.events.iter_events(cursor, scopes=scopes)]
    if not events:
        return {"ok": True, "created": False, "cursor": cursor, "events": 0}
    serialized_events = canonical_json(events)
    digest = hashlib.sha256(serialized_events.encode("utf-8")).hexdigest()
    pack = {
        "format": PACK_FORMAT,
        "createdAt": utc_now(),
        "fromPosition": int(events[0]["position"]),
        "toPosition": int(events[-1]["position"]),
        "eventCount": len(events),
        "eventsSha256": digest,
        "events": events,
    }
    if destination is None:
        destination = (
            root
            / ".wiki-memory"
            / "data"
            / "exports"
            / "local"
            / f"{pack['fromPosition']:012d}-{pack['toPosition']:012d}-{digest[:12]}.json"
        )
    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        existing = json.loads(destination.read_text(encoding="utf-8"))
        if existing.get("eventsSha256") != digest:
            raise MemoryError(f"Refusing to replace a different event pack: {destination}")
        return {"ok": True, "created": False, "path": str(destination), "cursor": pack["toPosition"], "events": len(events)}
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(pack, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        if os.name != "nt":
            descriptor = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)
    return {"ok": True, "created": True, "path": str(destination), "cursor": pack["toPosition"], "events": len(events)}


def validate_event_pack(path: Path) -> dict[str, Any]:
    value = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    if value.get("format") != PACK_FORMAT or not isinstance(value.get("events"), list):
        raise MemoryError(f"Unsupported event pack: {path}")
    actual = hashlib.sha256(canonical_json(value["events"]).encode("utf-8")).hexdigest()
    if actual != value.get("eventsSha256"):
        raise MemoryError(f"Event pack checksum mismatch: {path}")
    if len(value["events"]) != int(value.get("eventCount", -1)):
        raise MemoryError(f"Event pack count mismatch: {path}")
    return value


def import_event_pack(root: Path, path: Path) -> dict[str, Any]:
    root = ensure_root(root)
    engine = MemoryEngine(root)
    pack = validate_event_pack(path)
    imported = duplicates = conflicts = 0
    for raw in pack["events"]:
        incoming = MemoryEvent.from_dict(raw)
        existing = engine.events.get_by_idempotency_key(incoming.idempotency_key)
        if existing:
            if idempotency_fingerprint(existing) != idempotency_fingerprint(incoming):
                raise MemoryError(f"Event pack reuses idempotency key with different content: {incoming.idempotency_key}")
            duplicates += 1
            continue
        current = engine.events.stream_version(incoming.stream_id)
        if incoming.stream_version != current + 1:
            conflict = MemoryEvent(
                event_type="replication.conflict.detected",
                stream_id=f"replication-conflict:{incoming.event_id}",
                idempotency_key=f"replication-conflict:{incoming.event_id}",
                actor=EventActor(type="system", id="sync.event-pack"),
                plugin=PluginRef(id="sync.event-pack", version="1.0.0"),
                scope=incoming.scope,
                space_id=incoming.space_id,
                evidence_refs=incoming.evidence_refs,
                acl=incoming.acl,
                payload={
                    "reason": "stream-version",
                    "incomingEventId": incoming.event_id,
                    "incomingEventType": incoming.event_type,
                    "incomingStreamId": incoming.stream_id,
                    "incomingStreamVersion": incoming.stream_version,
                    "incomingEventHash": incoming.event_hash,
                    "currentStreamVersion": current,
                },
            )
            engine.append(conflict, enqueue=False)
            conflicts += 1
            continue
        engine.append(incoming, expected_stream_version=current, enqueue=False)
        imported += 1
    return {"ok": conflicts == 0, "imported": imported, "duplicates": duplicates, "conflicts": conflicts}
