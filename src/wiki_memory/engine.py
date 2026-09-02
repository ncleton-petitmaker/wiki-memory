from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

from .config import ensure_root
from .events import EventStore, MemoryEvent
from .evidence import EvidenceStore
from .projections import MarkdownProjector, ProjectionRegistry


EventHandler = Callable[[MemoryEvent], None]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: EventHandler) -> Callable[[], None]:
        self._handlers[event_type].append(handler)

        def unsubscribe() -> None:
            if handler in self._handlers[event_type]:
                self._handlers[event_type].remove(handler)

        return unsubscribe

    def emit(self, event: MemoryEvent) -> None:
        for handler in tuple(self._handlers.get(event.event_type, ())):
            handler(event)
        for handler in tuple(self._handlers.get("*", ())):
            handler(event)


class MemoryEngine:
    def __init__(self, root: Path, *, markdown_projection: bool = True):
        self.root = ensure_root(root)
        durable = self.root / ".wiki-memory" / "data"
        self.evidence = EvidenceStore(durable / "blobs" / "sha256")
        self.events = EventStore(durable / "events.sqlite3", evidence_exists=self.evidence.has)
        self.bus = EventBus()
        self.projections = ProjectionRegistry(
            self.root,
            self.events,
            evidence_verify=self.evidence.verify,
        )
        if markdown_projection:
            self.projections.register(MarkdownProjector())

    def append(
        self,
        event: MemoryEvent,
        *,
        expected_stream_version: int | None = None,
        project: bool = True,
        enqueue: bool | None = None,
    ) -> tuple[MemoryEvent, bool]:
        persisted, created = self.events.append(
            event,
            expected_stream_version=expected_stream_version,
            enqueue=enqueue,
        )
        if created:
            self.bus.emit(persisted)
        if project:
            self.projections.update_all()
        return persisted, created

    def rebuild(self, projection_id: str = "projection.markdown", *, force: bool = False) -> dict[str, Any]:
        result = self.projections.rebuild(projection_id, force=force)
        return {
            "ok": result.error is None,
            "projection": result.projection_id,
            "from": result.from_position,
            "to": result.to_position,
            "processed": result.processed,
            "rebuilt": result.rebuilt,
            "error": result.error,
        }

    def verify(self) -> dict[str, Any]:
        ledger = self.events.verify()
        # The ledger is authoritative. Starting from its references catches a
        # missing metadata sidecar just as reliably as a corrupt blob, rather
        # than only checking whatever metadata files happen to remain on disk.
        references = {
            reference
            for event in self.events.iter_events(0)
            for reference in event.evidence_refs
        }
        metadata_references = {metadata.reference for metadata in self.evidence.iter_metadata()}
        evidence_errors = sorted(
            reference
            for reference in references | metadata_references
            if not self.evidence.verify(reference)
        )
        return {
            "ok": ledger["ok"] and not evidence_errors and not self.events.projection_failures(),
            "ledger": ledger,
            "corruptEvidence": evidence_errors,
            "projectionFailures": self.events.projection_failures(),
        }
