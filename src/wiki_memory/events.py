from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Literal

from .config import MemoryError


Scope = Literal["private", "team", "organization"]
ActorType = Literal["user", "connector", "system"]


class ClosingSQLiteConnection(sqlite3.Connection):
    """sqlite3 context manager that also closes the file descriptor."""

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc, traceback))
        finally:
            self.close()


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def uuid7() -> str:
    """Generate an RFC 9562 UUIDv7 without requiring Python 3.14."""

    timestamp_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    random_a = secrets.randbits(12)
    random_b = secrets.randbits(62)
    value = timestamp_ms << 80
    value |= 0x7 << 76
    value |= random_a << 64
    value |= 0b10 << 62
    value |= random_b
    return str(uuid.UUID(int=value))


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _event_hash(event: "MemoryEvent") -> str:
    payload = event.to_dict()
    payload.pop("position", None)
    payload.pop("eventHash", None)
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def idempotency_fingerprint(event: "MemoryEvent") -> str:
    semantic = {
        "eventType": event.event_type,
        "streamId": event.stream_id,
        "scope": event.scope,
        "spaceId": event.space_id,
        "actor": asdict(event.actor),
        "plugin": asdict(event.plugin),
        "evidenceRefs": event.evidence_refs,
        "acl": event.acl,
        "payload": event.payload,
        "occurredAt": event.occurred_at,
        "correlationId": event.correlation_id,
        "causationId": event.causation_id,
    }
    return hashlib.sha256(canonical_json(semantic).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EventActor:
    type: ActorType
    id: str

    def validate(self) -> None:
        if self.type not in {"user", "connector", "system"}:
            raise MemoryError(f"Invalid actor type: {self.type}")
        if not self.id.strip():
            raise MemoryError("Event actor id cannot be empty.")


@dataclass(frozen=True)
class PluginRef:
    id: str
    version: str

    def validate(self) -> None:
        if not self.id.strip() or not self.version.strip():
            raise MemoryError("Event plugin id and version are required.")


@dataclass
class MemoryEvent:
    event_type: str
    stream_id: str
    idempotency_key: str
    actor: EventActor
    plugin: PluginRef
    payload: dict[str, Any]
    event_id: str = field(default_factory=uuid7)
    schema_version: int = 1
    stream_version: int = 0
    scope: Scope = "private"
    space_id: str = "local-owner"
    occurred_at: str | None = None
    recorded_at: str = field(default_factory=utc_now)
    correlation_id: str | None = None
    causation_id: str | None = None
    evidence_refs: list[str] = field(default_factory=list)
    acl: dict[str, Any] = field(default_factory=lambda: {"owners": ["local-owner"], "readers": []})
    position: int | None = None
    event_hash: str | None = None

    def validate(self) -> None:
        try:
            parsed_id = uuid.UUID(self.event_id)
        except ValueError as exc:
            raise MemoryError(f"Invalid event UUID: {self.event_id}") from exc
        if parsed_id.version != 7:
            raise MemoryError("Canonical event IDs must be UUIDv7.")
        if not self.event_type.strip() or not self.stream_id.strip():
            raise MemoryError("Event type and stream id are required.")
        if not self.idempotency_key.strip():
            raise MemoryError("Event idempotency key is required.")
        if self.scope not in {"private", "team", "organization"}:
            raise MemoryError(f"Invalid event scope: {self.scope}")
        if self.scope == "private" and self.space_id != "local-owner":
            raise MemoryError("Private events must remain in the local-owner space.")
        if not isinstance(self.payload, dict) or not isinstance(self.acl, dict):
            raise MemoryError("Event payload and ACL must be objects.")
        if not isinstance(self.evidence_refs, list):
            raise MemoryError("Event evidenceRefs must be an array.")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise MemoryError("Event evidenceRefs cannot contain duplicates.")
        from .evidence import parse_reference

        for reference in self.evidence_refs:
            parse_reference(reference)
        if self.event_type == "assertion.accepted" and not self.evidence_refs:
            # This is a ledger invariant, not merely a Team UI policy. The
            # sole exception makes human procedural memory explicit and keeps
            # its human author visible instead of inventing a source.
            procedural_human = self.payload.get("kind") == "procedural" and self.actor.type == "user"
            if not procedural_human:
                raise MemoryError("Accepted assertions require evidence unless explicitly human procedural memory.")
        if self.schema_version != 1 or self.stream_version < 0:
            raise MemoryError("Unsupported event schema or negative stream version.")
        for label, timestamp in (("occurredAt", self.occurred_at), ("recordedAt", self.recorded_at)):
            if timestamp is None:
                continue
            try:
                datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError as exc:
                raise MemoryError(f"Invalid {label} timestamp: {timestamp}") from exc
        self.actor.validate()
        self.plugin.validate()

    def to_dict(self) -> dict[str, Any]:
        return {
            "eventId": self.event_id,
            "eventType": self.event_type,
            "schemaVersion": self.schema_version,
            "streamId": self.stream_id,
            "streamVersion": self.stream_version,
            "idempotencyKey": self.idempotency_key,
            "scope": self.scope,
            "spaceId": self.space_id,
            "actor": asdict(self.actor),
            "occurredAt": self.occurred_at,
            "recordedAt": self.recorded_at,
            "correlationId": self.correlation_id,
            "causationId": self.causation_id,
            "plugin": asdict(self.plugin),
            "evidenceRefs": list(self.evidence_refs),
            "acl": self.acl,
            "payload": self.payload,
            "position": self.position,
            "eventHash": self.event_hash,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MemoryEvent":
        """Deserialize an untrusted event without leaking implementation errors.

        HTTP replication accepts JSON from clients and imported packs can be
        corrupted.  Every malformed shape must become ``MemoryError`` so the
        API returns its controlled client error rather than a Python
        ``TypeError`` or ``AttributeError``.
        """

        if not isinstance(value, dict):
            raise MemoryError("Event must be an object.")
        try:
            event = cls(
                event_id=str(value.get("eventId") or uuid7()),
                event_type=str(value["eventType"]),
                schema_version=int(value.get("schemaVersion", 1)),
                stream_id=str(value["streamId"]),
                stream_version=int(value.get("streamVersion", 0)),
                idempotency_key=str(value["idempotencyKey"]),
                scope=str(value.get("scope", "private")),  # type: ignore[arg-type]
                space_id=str(value.get("spaceId", "local-owner")),
                actor=EventActor(**value.get("actor", {"type": "system", "id": "wiki-memory"})),
                occurred_at=value.get("occurredAt"),
                recorded_at=str(value.get("recordedAt") or utc_now()),
                correlation_id=value.get("correlationId"),
                causation_id=value.get("causationId"),
                plugin=PluginRef(**value.get("plugin", {"id": "core", "version": "1.0.0"})),
                evidence_refs=[str(item) for item in value.get("evidenceRefs", [])],
                acl=dict(value.get("acl") or {}),
                payload=dict(value.get("payload") or {}),
                position=int(value["position"]) if value.get("position") is not None else None,
                event_hash=value.get("eventHash"),
            )
            event.validate()
        except MemoryError:
            raise
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise MemoryError("Invalid event object.") from exc
        return event


class EventStore:
    """Transactional append-only event ledger.

    The only supported mutations outside migrations are inserts and projection/outbox
    checkpoints. Database triggers reject UPDATE and DELETE on canonical events.
    """

    def __init__(self, database: Path, evidence_exists: Callable[[str], bool] | None = None):
        self.database = database.resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._evidence_exists = evidence_exists
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database,
            timeout=30,
            isolation_level=None,
            factory=ClosingSQLiteConnection,
        )
        connection.row_factory = sqlite3.Row
        # Set the busy handler before negotiating WAL. Several independent
        # capture processes can legitimately open a brand-new memory at once;
        # the first WAL upgrade needs an exclusive database lock. Retrying only
        # that harmless setup race is safer than rejecting a durable capture
        # before it reaches a ledger transaction.
        connection.execute("PRAGMA busy_timeout=30000")
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
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    position INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    stream_id TEXT NOT NULL,
                    stream_version INTEGER NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    scope TEXT NOT NULL CHECK(scope IN ('private','team','organization')),
                    space_id TEXT NOT NULL,
                    actor_json TEXT NOT NULL,
                    occurred_at TEXT,
                    recorded_at TEXT NOT NULL,
                    correlation_id TEXT,
                    causation_id TEXT,
                    plugin_json TEXT NOT NULL,
                    evidence_refs_json TEXT NOT NULL,
                    acl_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    UNIQUE(stream_id, stream_version)
                );
                CREATE INDEX IF NOT EXISTS idx_events_stream ON events(stream_id, stream_version);
                CREATE INDEX IF NOT EXISTS idx_events_scope_position ON events(scope, position);
                CREATE INDEX IF NOT EXISTS idx_events_type_position ON events(event_type, position);

                CREATE TABLE IF NOT EXISTS event_evidence (
                    event_id TEXT NOT NULL REFERENCES events(event_id),
                    reference TEXT NOT NULL,
                    PRIMARY KEY(event_id, reference)
                );
                CREATE INDEX IF NOT EXISTS idx_event_evidence_reference
                    ON event_evidence(reference, event_id);

                CREATE TRIGGER IF NOT EXISTS event_evidence_no_update
                BEFORE UPDATE ON event_evidence BEGIN
                    SELECT RAISE(ABORT, 'canonical event evidence is immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS event_evidence_no_delete
                BEFORE DELETE ON event_evidence BEGIN
                    SELECT RAISE(ABORT, 'canonical event evidence is immutable');
                END;

                CREATE TABLE IF NOT EXISTS store_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TRIGGER IF NOT EXISTS events_no_update
                BEFORE UPDATE ON events BEGIN
                    SELECT RAISE(ABORT, 'canonical events are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS events_no_delete
                BEFORE DELETE ON events BEGIN
                    SELECT RAISE(ABORT, 'canonical events are immutable');
                END;

                CREATE TABLE IF NOT EXISTS projection_checkpoints (
                    projection_id TEXT PRIMARY KEY,
                    plugin_version TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS connector_checkpoints (
                    connector_instance_id TEXT NOT NULL,
                    stream TEXT NOT NULL,
                    cursor_json TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(connector_instance_id, stream)
                );
                CREATE TABLE IF NOT EXISTS projection_failures (
                    projection_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    error TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(projection_id, position)
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    event_id TEXT PRIMARY KEY REFERENCES events(event_id),
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    remote_position INTEGER,
                    updated_at TEXT NOT NULL
                );
                """
            )
            # event_evidence is a normalized lookup of immutable event JSON. A
            # one-time, transactionally marked rebuild supports alpha databases
            # created before this index without an O(events) scan on every open.
            marker = connection.execute(
                "SELECT 1 FROM store_metadata WHERE key='event-evidence-index-version' AND value='1'"
            ).fetchone()
            if marker is None:
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    marker = connection.execute(
                        "SELECT 1 FROM store_metadata WHERE key='event-evidence-index-version' AND value='1'"
                    ).fetchone()
                    if marker is None:
                        rows = connection.execute(
                            "SELECT event_id,evidence_refs_json FROM events"
                        ).fetchall()
                        for row in rows:
                            connection.executemany(
                                "INSERT OR IGNORE INTO event_evidence(event_id,reference) VALUES (?,?)",
                                (
                                    (row["event_id"], str(reference))
                                    for reference in json.loads(row["evidence_refs_json"])
                                ),
                            )
                        connection.execute(
                            """
                            INSERT INTO store_metadata(key,value)
                            VALUES ('event-evidence-index-version','1')
                            ON CONFLICT(key) DO UPDATE SET value=excluded.value
                            """
                        )
                    connection.execute("COMMIT")
                except Exception:
                    if connection.in_transaction:
                        connection.execute("ROLLBACK")
                    raise

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> MemoryEvent:
        return MemoryEvent.from_dict(
            {
                "eventId": row["event_id"],
                "eventType": row["event_type"],
                "schemaVersion": row["schema_version"],
                "streamId": row["stream_id"],
                "streamVersion": row["stream_version"],
                "idempotencyKey": row["idempotency_key"],
                "scope": row["scope"],
                "spaceId": row["space_id"],
                "actor": json.loads(row["actor_json"]),
                "occurredAt": row["occurred_at"],
                "recordedAt": row["recorded_at"],
                "correlationId": row["correlation_id"],
                "causationId": row["causation_id"],
                "plugin": json.loads(row["plugin_json"]),
                "evidenceRefs": json.loads(row["evidence_refs_json"]),
                "acl": json.loads(row["acl_json"]),
                "payload": json.loads(row["payload_json"]),
                "position": row["position"],
                "eventHash": row["event_hash"],
            }
        )

    def append(
        self,
        event: MemoryEvent,
        *,
        expected_stream_version: int | None = None,
        enqueue: bool | None = None,
    ) -> tuple[MemoryEvent, bool]:
        event.validate()
        if self._evidence_exists:
            missing = [reference for reference in event.evidence_refs if not self._evidence_exists(reference)]
            if missing:
                raise MemoryError("Event references missing evidence: " + ", ".join(missing))
        with self.connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                duplicate = connection.execute(
                    "SELECT * FROM events WHERE idempotency_key = ?",
                    (event.idempotency_key,),
                ).fetchone()
                if duplicate is not None:
                    existing = self._row_to_event(duplicate)
                    if idempotency_fingerprint(existing) != idempotency_fingerprint(event):
                        raise MemoryError(f"Idempotency key was reused with different content: {event.idempotency_key}")
                    connection.execute("COMMIT")
                    return existing, False
                current = connection.execute(
                    "SELECT COALESCE(MAX(stream_version), 0) AS version FROM events WHERE stream_id = ?",
                    (event.stream_id,),
                ).fetchone()["version"]
                if expected_stream_version is not None and current != expected_stream_version:
                    raise MemoryError(
                        f"Stream version conflict for {event.stream_id}: expected {expected_stream_version}, current {current}."
                    )
                if event.stream_version not in {0, current + 1}:
                    raise MemoryError(
                        f"Invalid next stream version for {event.stream_id}: {event.stream_version}; expected {current + 1}."
                    )
                event.stream_version = current + 1
                event.event_hash = _event_hash(event)
                cursor = connection.execute(
                    """
                    INSERT INTO events (
                        event_id,event_type,schema_version,stream_id,stream_version,idempotency_key,
                        scope,space_id,actor_json,occurred_at,recorded_at,correlation_id,causation_id,
                        plugin_json,evidence_refs_json,acl_json,payload_json,event_hash
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        event.event_id,
                        event.event_type,
                        event.schema_version,
                        event.stream_id,
                        event.stream_version,
                        event.idempotency_key,
                        event.scope,
                        event.space_id,
                        canonical_json(asdict(event.actor)),
                        event.occurred_at,
                        event.recorded_at,
                        event.correlation_id,
                        event.causation_id,
                        canonical_json(asdict(event.plugin)),
                        canonical_json(event.evidence_refs),
                        canonical_json(event.acl),
                        canonical_json(event.payload),
                        event.event_hash,
                    ),
                )
                event.position = int(cursor.lastrowid)
                connection.executemany(
                    "INSERT INTO event_evidence(event_id,reference) VALUES (?,?)",
                    ((event.event_id, reference) for reference in event.evidence_refs),
                )
                should_enqueue = enqueue if enqueue is not None else event.scope != "private"
                if should_enqueue:
                    connection.execute(
                        "INSERT INTO outbox(event_id,status,updated_at) VALUES (?, 'pending', ?)",
                        (event.event_id, utc_now()),
                    )
                connection.execute("COMMIT")
                return event, True
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def append_batch(
        self,
        events: Iterable[MemoryEvent],
        *,
        expected_versions: dict[str, int] | None = None,
    ) -> list[tuple[MemoryEvent, bool]]:
        # Each append is independently durable and idempotent. Connectors advance their
        # checkpoint only after all prior messages have been accepted.
        return [
            self.append(event, expected_stream_version=(expected_versions or {}).get(event.stream_id))
            for event in events
        ]

    def get(self, event_id: str) -> MemoryEvent | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
        return self._row_to_event(row) if row else None

    def get_by_idempotency_key(self, key: str) -> MemoryEvent | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM events WHERE idempotency_key = ?", (key,)).fetchone()
        return self._row_to_event(row) if row else None

    def events_referencing_evidence(self, reference: str) -> list[MemoryEvent]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT e.* FROM event_evidence r
                JOIN events e ON e.event_id=r.event_id
                WHERE r.reference=? ORDER BY e.position DESC
                """,
                (reference,),
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def stream_version(self, stream_id: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(stream_version), 0) AS version FROM events WHERE stream_id = ?",
                (stream_id,),
            ).fetchone()
        return int(row["version"])

    def latest_stream_event(self, stream_id: str) -> MemoryEvent | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM events WHERE stream_id = ? ORDER BY stream_version DESC LIMIT 1",
                (stream_id,),
            ).fetchone()
        return self._row_to_event(row) if row else None

    def iter_events(
        self,
        cursor: int = 0,
        *,
        limit: int | None = None,
        scopes: set[str] | None = None,
        event_types: set[str] | None = None,
    ) -> Iterator[MemoryEvent]:
        clauses = ["position > ?"]
        params: list[Any] = [cursor]
        if scopes:
            placeholders = ",".join("?" for _ in scopes)
            clauses.append(f"scope IN ({placeholders})")
            params.extend(sorted(scopes))
        if event_types:
            placeholders = ",".join("?" for _ in event_types)
            clauses.append(f"event_type IN ({placeholders})")
            params.extend(sorted(event_types))
        sql = "SELECT * FROM events WHERE " + " AND ".join(clauses) + " ORDER BY position"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        yield from (self._row_to_event(row) for row in rows)

    def count(self) -> int:
        with self.connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def latest_position(self) -> int:
        with self.connect() as connection:
            return int(connection.execute("SELECT COALESCE(MAX(position), 0) FROM events").fetchone()[0])

    def projection_checkpoint(self, projection_id: str) -> tuple[int, str] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT position, plugin_version FROM projection_checkpoints WHERE projection_id = ?",
                (projection_id,),
            ).fetchone()
        return (int(row["position"]), str(row["plugin_version"])) if row else None

    def set_projection_checkpoint(self, projection_id: str, position: int, plugin_version: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO projection_checkpoints(projection_id,plugin_version,position,updated_at)
                VALUES (?,?,?,?)
                ON CONFLICT(projection_id) DO UPDATE SET
                    plugin_version=excluded.plugin_version,
                    position=excluded.position,
                    updated_at=excluded.updated_at
                """,
                (projection_id, plugin_version, position, utc_now()),
            )
            connection.execute(
                "DELETE FROM projection_failures WHERE projection_id=? AND position<=?",
                (projection_id, position),
            )

    def record_projection_failure(self, projection_id: str, position: int, error: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO projection_failures(projection_id,position,error,updated_at) VALUES (?,?,?,?)
                ON CONFLICT(projection_id,position) DO UPDATE SET error=excluded.error,updated_at=excluded.updated_at
                """,
                (projection_id, position, error[:2000], utc_now()),
            )

    def projection_failures(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT projection_id,position,error,updated_at FROM projection_failures ORDER BY position"
            ).fetchall()
        return [dict(row) for row in rows]

    def set_connector_checkpoint(
        self,
        connector_instance_id: str,
        stream: str,
        cursor: Any,
        position: int,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO connector_checkpoints(connector_instance_id,stream,cursor_json,position,updated_at)
                VALUES (?,?,?,?,?)
                ON CONFLICT(connector_instance_id,stream) DO UPDATE SET
                    cursor_json=excluded.cursor_json,
                    position=excluded.position,
                    updated_at=excluded.updated_at
                """,
                (connector_instance_id, stream, canonical_json(cursor), position, utc_now()),
            )

    def connector_checkpoint(self, connector_instance_id: str, stream: str) -> Any | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT cursor_json FROM connector_checkpoints WHERE connector_instance_id=? AND stream=?",
                (connector_instance_id, stream),
            ).fetchone()
        return json.loads(row["cursor_json"]) if row else None

    def pending_outbox(self, limit: int = 100) -> list[MemoryEvent]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT e.* FROM outbox o JOIN events e ON e.event_id=o.event_id
                WHERE o.status IN ('pending','retry') ORDER BY e.position LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def outbox_status_counts(self) -> dict[str, int]:
        with self.connect() as connection:
            rows = connection.execute("SELECT status,COUNT(*) AS value FROM outbox GROUP BY status").fetchall()
        return {str(row["status"]): int(row["value"]) for row in rows}

    def mark_outbox(
        self,
        event_id: str,
        status: Literal["pending", "retry", "accepted", "rejected"],
        *,
        remote_position: int | None = None,
        error: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE outbox SET status=?, attempts=attempts+1, last_error=?, remote_position=?, updated_at=?
                WHERE event_id=?
                """,
                (status, error, remote_position, utc_now(), event_id),
            )

    def verify(self) -> dict[str, Any]:
        errors: list[dict[str, Any]] = []
        previous_versions: dict[str, int] = {}
        expected_evidence_links: set[tuple[str, str]] = set()
        for event in self.iter_events():
            expected = previous_versions.get(event.stream_id, 0) + 1
            if event.stream_version != expected:
                errors.append(
                    {
                        "code": "stream-gap",
                        "eventId": event.event_id,
                        "expected": expected,
                        "actual": event.stream_version,
                    }
                )
            if event.event_hash != _event_hash(event):
                errors.append({"code": "event-hash", "eventId": event.event_id})
            if self._evidence_exists:
                for reference in event.evidence_refs:
                    if not self._evidence_exists(reference):
                        errors.append({"code": "missing-evidence", "eventId": event.event_id, "reference": reference})
            expected_evidence_links.update((event.event_id, reference) for reference in event.evidence_refs)
            previous_versions[event.stream_id] = event.stream_version
        with self.connect() as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            actual_evidence_links = {
                (str(row["event_id"]), str(row["reference"]))
                for row in connection.execute(
                    "SELECT event_id,reference FROM event_evidence"
                ).fetchall()
            }
        if integrity != "ok":
            errors.append({"code": "sqlite-integrity", "detail": integrity})
        if actual_evidence_links != expected_evidence_links:
            errors.append(
                {
                    "code": "event-evidence-index",
                    "missing": len(expected_evidence_links - actual_evidence_links),
                    "unexpected": len(actual_evidence_links - expected_evidence_links),
                }
            )
        return {"ok": not errors, "events": self.count(), "errors": errors}
