from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import MemoryError, slugify
from .contracts import SourceConnector, SourceSelection
from .engine import MemoryEngine
from .events import EventActor, MemoryEvent, PluginRef, canonical_json


@dataclass(frozen=True)
class IngestionResult:
    records: int
    deletes: int
    checkpoints: int
    warnings: tuple[str, ...]
    last_position: int


class SourceIngestionRuntime:
    def __init__(self, engine: MemoryEngine):
        self.engine = engine

    async def run(
        self,
        connector: SourceConnector,
        *,
        connector_instance_id: str,
        selection: SourceSelection,
        vault: str,
        scope: str = "private",
        space_id: str = "local-owner",
        acl: dict[str, Any] | None = None,
        plugin_version: str = "1.0.0",
    ) -> IngestionResult:
        from .team import ensure_team_vault, normalize_acl

        spec = await connector.spec()
        if scope != "private":
            vault = ensure_team_vault(self.engine.root, space_id)
        resolved_acl = normalize_acl(acl, owner=connector_instance_id, space_id=space_id)
        records = deletes = checkpoints = 0
        warnings: list[str] = []
        last_position = 0
        cursors = {
            stream: self.engine.events.connector_checkpoint(connector_instance_id, stream)
            for stream in selection.streams
        }
        # A connector receives its complete cursor object and may emit per-stream
        # checkpoints. Checkpoints are committed only after prior event appends.
        async for message in connector.read(selection, cursors):
            if message.type not in {"record", "delete", "checkpoint", "schema-change", "warning"}:
                raise MemoryError(f"Connector {spec.id} emitted an unsupported message type: {message.type}")
            if not message.stream.strip():
                raise MemoryError(f"Connector {spec.id} emitted a message without a stream.")
            if message.type == "warning":
                warnings.append(message.warning or "unspecified connector warning")
                continue
            if message.type == "checkpoint":
                if message.cursor is None:
                    raise MemoryError(f"Connector {spec.id} emitted a checkpoint without a cursor.")
                self.engine.events.set_connector_checkpoint(
                    connector_instance_id,
                    message.stream,
                    message.cursor,
                    last_position,
                )
                checkpoints += 1
                continue
            if message.type == "schema-change":
                schema_hash = hashlib.sha256(canonical_json(message.schema or {}).encode("utf-8")).hexdigest()
                event = MemoryEvent(
                    event_type="source.schema.changed",
                    stream_id=f"connector:{connector_instance_id}:{message.stream}:schema",
                    idempotency_key=f"schema:{connector_instance_id}:{message.stream}:{schema_hash}",
                    actor=EventActor(type="connector", id=connector_instance_id),
                    plugin=PluginRef(id=spec.id, version=plugin_version),
                    scope=scope,  # type: ignore[arg-type]
                    space_id=space_id,
                    acl=resolved_acl,
                    payload={"stream": message.stream, "schema": message.schema or {}},
                )
                persisted, _ = self.engine.append(event)
                last_position = int(persisted.position or last_position)
                continue
            if not message.source_id:
                raise MemoryError(f"Connector {spec.id} emitted {message.type} without source_id.")
            stream_id = f"source:{vault}:{connector_instance_id}:{message.stream}:{message.source_id}"
            partition = Path(slugify(spec.id)) / slugify(message.stream)
            current = self.engine.events.stream_version(stream_id)
            if message.type == "delete":
                event_type = "source.deleted"
                idempotency = f"delete:{stream_id}:{message.source_version or canonical_json(message.cursor)}"
                payload = {
                    "sourceId": hashlib.sha256(stream_id.encode("utf-8")).hexdigest()[:16],
                    "vault": vault,
                    "partition": partition.as_posix(),
                    "stream": message.stream,
                    "nativeSourceId": message.source_id,
                }
                evidence_refs: list[str] = []
            else:
                raw = canonical_json(message.payload or {}).encode("utf-8")
                evidence = self.engine.evidence.put_bytes(
                    raw,
                    media_type="application/json",
                    original_name=f"{slugify(message.stream)}-{hashlib.sha256(message.source_id.encode()).hexdigest()[:12]}.json",
                )
                evidence_refs = [evidence.reference, *message.evidence]
                version = message.source_version or evidence.sha256
                idempotency = f"record:{stream_id}:{version}"
                existing = self.engine.events.get_by_idempotency_key(idempotency)
                event_type = (
                    existing.event_type
                    if existing is not None
                    else ("source.captured" if current == 0 else "source.revised")
                )
                source_id = hashlib.sha256(stream_id.encode("utf-8")).hexdigest()[:16]
                payload = {
                    "sourceId": source_id,
                    "vault": vault,
                    "partition": partition.as_posix(),
                    "title": f"{message.stream} — {message.source_id}",
                    "body": "```json\n" + json.dumps(message.payload or {}, ensure_ascii=False, indent=2, default=str) + "\n```",
                    "metadata": {
                        "source_type": "database_record",
                        "connector": spec.id,
                        "connector_instance": connector_instance_id,
                        "source_stream": message.stream,
                        "source_id": message.source_id,
                        "source_version": version,
                        "content_hash": evidence.sha256,
                        "epistemic_status": "unverified",
                    },
                }
            event = MemoryEvent(
                event_type=event_type,
                stream_id=stream_id,
                idempotency_key=idempotency,
                actor=EventActor(type="connector", id=connector_instance_id),
                plugin=PluginRef(id=spec.id, version=plugin_version),
                scope=scope,  # type: ignore[arg-type]
                space_id=space_id,
                occurred_at=message.occurred_at,
                evidence_refs=evidence_refs,
                acl=resolved_acl,
                payload=payload,
            )
            persisted, created = self.engine.append(event, expected_stream_version=current)
            last_position = int(persisted.position or last_position)
            if created:
                if message.type == "delete":
                    deletes += 1
                else:
                    records += 1
        return IngestionResult(records, deletes, checkpoints, tuple(warnings), last_position)
