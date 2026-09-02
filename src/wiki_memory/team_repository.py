from __future__ import annotations

from abc import ABC, abstractmethod
import hashlib
from typing import Any, Callable

from .config import MemoryError
from .events import MemoryEvent, canonical_json, idempotency_fingerprint


def _decode_postgres_value(value: Any) -> Any:
    """Normalize text returned as bytes by non-UTF-8 PostgreSQL clusters.

    Event data is canonical UTF-8 JSON. A database value that cannot be
    decoded is corruption at this boundary, not a reason to construct a
    different event (for example ``b'team'``) silently.
    """

    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MemoryError("PostgreSQL returned non-UTF-8 canonical event data.") from exc
    if isinstance(value, dict):
        return {_decode_postgres_value(key): _decode_postgres_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode_postgres_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_decode_postgres_value(item) for item in value)
    return value


TEAM_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memory_events (
    position BIGSERIAL PRIMARY KEY,
    event_id UUID NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    stream_id TEXT NOT NULL,
    stream_version BIGINT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    scope TEXT NOT NULL CHECK(scope IN ('team','organization')),
    space_id TEXT NOT NULL,
    actor_json JSONB NOT NULL,
    occurred_at TEXT,
    recorded_at TEXT NOT NULL,
    correlation_id TEXT,
    causation_id TEXT,
    plugin_json JSONB NOT NULL,
    evidence_refs_json JSONB NOT NULL,
    acl_json JSONB NOT NULL,
    payload_json JSONB NOT NULL,
    event_hash TEXT NOT NULL,
    UNIQUE(stream_id, stream_version)
);
CREATE INDEX IF NOT EXISTS memory_events_space_position ON memory_events(space_id, position);
CREATE INDEX IF NOT EXISTS memory_events_type_position ON memory_events(event_type, position);
CREATE INDEX IF NOT EXISTS memory_events_search ON memory_events USING GIN(to_tsvector('simple', payload_json::text));

CREATE OR REPLACE FUNCTION reject_canonical_event_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'canonical memory events are immutable';
END;
$$ LANGUAGE plpgsql;
DO $trigger$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname='memory_events_no_update' AND tgrelid='memory_events'::regclass
  ) THEN
    CREATE TRIGGER memory_events_no_update BEFORE UPDATE OR DELETE ON memory_events
    FOR EACH ROW EXECUTE FUNCTION reject_canonical_event_mutation();
  END IF;
END
$trigger$;

CREATE TABLE IF NOT EXISTS search_documents (
    event_id UUID PRIMARY KEY REFERENCES memory_events(event_id),
    stream_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    scope TEXT NOT NULL,
    space_id TEXT NOT NULL,
    acl_json JSONB NOT NULL,
    recorded_at TEXT NOT NULL,
    document TEXT NOT NULL,
    evidence_refs_json JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS search_documents_space ON search_documents(space_id, recorded_at);
CREATE INDEX IF NOT EXISTS search_documents_fts ON search_documents USING GIN(to_tsvector('simple', document));

CREATE TABLE IF NOT EXISTS memory_jobs (
    id BIGSERIAL PRIMARY KEY,
    job_type TEXT NOT NULL,
    payload_json JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_at TIMESTAMPTZ,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS memory_jobs_pending ON memory_jobs(status, available_at);

CREATE TABLE IF NOT EXISTS audit_events (
    id BIGSERIAL PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    target_id TEXT,
    space_id TEXT,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb
);
DO $trigger$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname='audit_events_no_update' AND tgrelid='audit_events'::regclass
  ) THEN
    CREATE TRIGGER audit_events_no_update BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION reject_canonical_event_mutation();
  END IF;
END
$trigger$;

CREATE TABLE IF NOT EXISTS restore_verifications (
    id BIGSERIAL PRIMARY KEY,
    verified_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('success','failed')),
    backup_id TEXT,
    event_count BIGINT,
    evidence_count BIGINT,
    detail_json JSONB NOT NULL DEFAULT '{}'::jsonb
);
DO $trigger$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname='restore_verifications_no_update' AND tgrelid='restore_verifications'::regclass
  ) THEN
    CREATE TRIGGER restore_verifications_no_update BEFORE UPDATE OR DELETE ON restore_verifications
    FOR EACH ROW EXECUTE FUNCTION reject_canonical_event_mutation();
  END IF;
END
$trigger$;

CREATE TABLE IF NOT EXISTS replication_clients (
    client_id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    last_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
    pull_cursor BIGINT NOT NULL CHECK(pull_cursor >= 0),
    outbox_pending BIGINT NOT NULL CHECK(outbox_pending >= 0)
);
CREATE INDEX IF NOT EXISTS replication_clients_last_seen ON replication_clients(last_seen);
"""


# Search is a current-state projection. These sets are deliberately shared by
# the worker and an operator-triggered rebuild: reconstructing must not invent
# a second interpretation of which canonical events are visible.
PROJECTABLE_EVENT_TYPES = (
    "source.captured",
    "source.revised",
    "source.deleted",
    "source.audio.captured",
    "source.published",
    "transcription.created",
    "assertion.accepted",
    "assertion.retracted",
    "assertion.superseded",
)
ACTIVE_SEARCH_EVENT_TYPES = (
    "source.captured",
    "source.revised",
    "source.audio.captured",
    "source.published",
    "transcription.created",
    "assertion.accepted",
)
REMOVES_SEARCH_DOCUMENT = ("source.deleted", "assertion.retracted", "assertion.superseded")


class TeamRepository(ABC):
    @abstractmethod
    def initialize(self) -> None: ...

    @abstractmethod
    def append(self, event: MemoryEvent, *, expected_stream_version: int | None = None) -> tuple[MemoryEvent, bool]: ...

    @abstractmethod
    def list_events(
        self,
        cursor: int,
        limit: int,
        spaces: set[str],
        organization: bool,
        *,
        principal_id: str | None = None,
        groups: set[str] | None = None,
        all_access: bool = False,
    ) -> list[MemoryEvent]: ...

    @abstractmethod
    def get_event(self, event_id: str) -> MemoryEvent | None: ...

    @abstractmethod
    def get_by_idempotency_key(self, key: str) -> MemoryEvent | None: ...

    @abstractmethod
    def proposal_is_resolved(self, proposal_id: str) -> bool: ...

    @abstractmethod
    def latest_stream_event(self, stream_id: str) -> MemoryEvent | None: ...

    @abstractmethod
    def events_referencing_blob(self, digest: str) -> list[MemoryEvent]: ...

    @abstractmethod
    def search(
        self,
        query: str,
        limit: int,
        spaces: set[str],
        organization: bool,
        *,
        principal_id: str | None = None,
        groups: set[str] | None = None,
        all_access: bool = False,
    ) -> list[dict[str, Any]]: ...

    @abstractmethod
    def audit(self, actor_id: str, action: str, target_id: str | None, space_id: str | None, metadata: dict[str, Any]) -> None: ...

    @abstractmethod
    def list_audit(self, cursor: int, limit: int) -> list[dict[str, Any]]: ...

    @abstractmethod
    def record_restore_verification(
        self,
        actor_id: str,
        *,
        status: str,
        backup_id: str | None,
        event_count: int | None,
        evidence_count: int | None,
        detail: dict[str, Any],
    ) -> dict[str, Any]: ...

    @abstractmethod
    def report_replication_client(
        self, client_id: str, actor_id: str, pull_cursor: int, outbox_pending: int
    ) -> None: ...

    @abstractmethod
    def run_jobs_once(
        self,
        limit: int = 100,
        *,
        evidence_verify: Callable[[str], bool] | None = None,
    ) -> dict[str, int]: ...

    @abstractmethod
    def operational_metrics(self) -> dict[str, float]: ...

    @abstractmethod
    def healthcheck(self) -> None: ...

    @abstractmethod
    def rebuild_search_projection(
        self,
        *,
        evidence_verify: Callable[[str], bool] | None = None,
    ) -> dict[str, int]: ...

    @abstractmethod
    def verify_integrity(self, blob_exists: Callable[[str], bool], *, evidence_limit: int = 0) -> dict[str, Any]: ...


class PostgresTeamRepository(TeamRepository):
    def __init__(self, dsn: str):
        self.dsn = dsn

    def _connect(self):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise MemoryError("Team server requires the 'server' optional dependencies.") from exc
        # Some legacy PostgreSQL clusters advertise SQL_ASCII. Without an
        # explicit client encoding psycopg returns text columns as bytes,
        # which must never leak across the canonical event boundary.
        return psycopg.connect(self.dsn, row_factory=dict_row, client_encoding="UTF8")

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(TEAM_SCHEMA_SQL)

    @staticmethod
    def _access_filter(
        spaces: set[str],
        organization: bool,
        *,
        principal_id: str | None,
        groups: set[str] | None,
        all_access: bool,
    ) -> tuple[str, list[Any]]:
        if all_access:
            return "TRUE", []
        if principal_id is None:
            clauses: list[str] = []
            params: list[Any] = []
            if spaces:
                clauses.append("space_id = ANY(%s)")
                params.append(sorted(spaces))
            if organization:
                clauses.append("scope = 'organization'")
            return ("(" + " OR ".join(clauses) + ")", params) if clauses else ("FALSE", [])
        clauses = ["acl_json->'owners' ? %s", "acl_json->'readers' ? %s"]
        params = [principal_id, principal_id]
        if groups:
            clauses.append("acl_json->'groups' ?| %s::text[]")
            params.append(sorted(groups))
        if spaces:
            clauses.append(
                "(COALESCE(acl_json->>'audience','explicit')='space' "
                "AND space_id = ANY(%s) AND acl_json->'spaces' ? space_id)"
            )
            params.append(sorted(spaces))
        if organization:
            clauses.append("(COALESCE(acl_json->>'audience','explicit')='organization' AND scope='organization')")
        return "(" + " OR ".join(clauses) + ")", params

    @staticmethod
    def _row_to_event(row: dict[str, Any]) -> MemoryEvent:
        row = _decode_postgres_value(row)
        return MemoryEvent.from_dict(
            {
                "eventId": str(row["event_id"]),
                "eventType": row["event_type"],
                "schemaVersion": row["schema_version"],
                "streamId": row["stream_id"],
                "streamVersion": row["stream_version"],
                "idempotencyKey": row["idempotency_key"],
                "scope": row["scope"],
                "spaceId": row["space_id"],
                "actor": row["actor_json"],
                "occurredAt": row["occurred_at"],
                "recordedAt": row["recorded_at"],
                "correlationId": row["correlation_id"],
                "causationId": row["causation_id"],
                "plugin": row["plugin_json"],
                "evidenceRefs": row["evidence_refs_json"],
                "acl": row["acl_json"],
                "payload": row["payload_json"],
                "position": row["position"],
                "eventHash": row["event_hash"],
            }
        )

    def append(self, event: MemoryEvent, *, expected_stream_version: int | None = None) -> tuple[MemoryEvent, bool]:
        event.validate()
        if event.scope == "private":
            raise MemoryError("Private events are never accepted by Team.")
        from .events import _event_hash

        with self._connect() as connection:
            with connection.transaction():
                # Serialize retries by idempotency key before inspecting the row.
                # Without this lock, two concurrent first deliveries can both miss
                # the duplicate and one fails on the UNIQUE constraint after commit.
                connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (event.idempotency_key,))
                duplicate = connection.execute(
                    "SELECT * FROM memory_events WHERE idempotency_key=%s",
                    (event.idempotency_key,),
                ).fetchone()
                if duplicate:
                    existing = self._row_to_event(duplicate)
                    if idempotency_fingerprint(existing) != idempotency_fingerprint(event):
                        raise MemoryError(f"Idempotency key was reused with different content: {event.idempotency_key}")
                    return existing, False
                connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (event.stream_id,))
                current = connection.execute(
                    "SELECT COALESCE(MAX(stream_version), 0) AS version FROM memory_events WHERE stream_id=%s",
                    (event.stream_id,),
                ).fetchone()["version"]
                if expected_stream_version is not None and current != expected_stream_version:
                    raise MemoryError(
                        f"Stream version conflict for {event.stream_id}: expected {expected_stream_version}, current {current}."
                    )
                if event.stream_version not in {0, current + 1}:
                    raise MemoryError(
                        f"Stream version conflict for {event.stream_id}: expected {current + 1}, got {event.stream_version}."
                    )
                event.stream_version = current + 1
                event.event_hash = _event_hash(event)
                row = connection.execute(
                    """
                    INSERT INTO memory_events(
                      event_id,event_type,schema_version,stream_id,stream_version,idempotency_key,
                      scope,space_id,actor_json,occurred_at,recorded_at,correlation_id,causation_id,
                      plugin_json,evidence_refs_json,acl_json,payload_json,event_hash
                    ) VALUES (
                      %s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s
                    ) RETURNING *
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
                        canonical_json(event.to_dict()["actor"]),
                        event.occurred_at,
                        event.recorded_at,
                        event.correlation_id,
                        event.causation_id,
                        canonical_json(event.to_dict()["plugin"]),
                        canonical_json(event.evidence_refs),
                        canonical_json(event.acl),
                        canonical_json(event.payload),
                        event.event_hash,
                    ),
                ).fetchone()
                connection.execute(
                    "INSERT INTO memory_jobs(job_type,payload_json) VALUES ('project-event', %s::jsonb)",
                    (canonical_json({"eventId": str(row["event_id"])}),),
                )
                return self._row_to_event(row), True

    def list_events(
        self,
        cursor: int,
        limit: int,
        spaces: set[str],
        organization: bool,
        *,
        principal_id: str | None = None,
        groups: set[str] | None = None,
        all_access: bool = False,
    ) -> list[MemoryEvent]:
        access_sql, access_params = self._access_filter(
            spaces,
            organization,
            principal_id=principal_id,
            groups=groups,
            all_access=all_access,
        )
        clauses = ["position > %s", access_sql]
        params: list[Any] = [cursor, *access_params]
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM memory_events WHERE " + " AND ".join(clauses) + " ORDER BY position LIMIT %s",
                params,
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def get_event(self, event_id: str) -> MemoryEvent | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM memory_events WHERE event_id=%s", (event_id,)).fetchone()
        return self._row_to_event(row) if row else None

    def get_by_idempotency_key(self, key: str) -> MemoryEvent | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM memory_events WHERE idempotency_key=%s", (key,)).fetchone()
        return self._row_to_event(row) if row else None

    def proposal_is_resolved(self, proposal_id: str) -> bool:
        """Whether an ACL-bound publication has its public terminal event."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT EXISTS(
                    SELECT 1 FROM memory_events
                    WHERE causation_id=%s AND event_type='source.published'
                ) AS resolved
                """,
                (proposal_id,),
            ).fetchone()
        return bool(row["resolved"])

    def latest_stream_event(self, stream_id: str) -> MemoryEvent | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM memory_events WHERE stream_id=%s ORDER BY stream_version DESC LIMIT 1",
                (stream_id,),
            ).fetchone()
        return self._row_to_event(row) if row else None

    def events_referencing_blob(self, digest: str) -> list[MemoryEvent]:
        reference = f"sha256:{digest.lower()}"
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM memory_events WHERE evidence_refs_json ? %s ORDER BY position DESC",
                (reference,),
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def search(
        self,
        query: str,
        limit: int,
        spaces: set[str],
        organization: bool,
        *,
        principal_id: str | None = None,
        groups: set[str] | None = None,
        all_access: bool = False,
    ) -> list[dict[str, Any]]:
        access_sql, access_params = self._access_filter(
            spaces,
            organization,
            principal_id=principal_id,
            groups=groups,
            all_access=all_access,
        )
        params: list[Any] = [query, *access_params]
        params.extend([query, limit])
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT event_id,event_type,scope,space_id,acl_json,recorded_at,document,evidence_refs_json,
                       ts_rank(to_tsvector('simple', document), plainto_tsquery('simple', %s)) AS score
                FROM search_documents
                WHERE {access_sql}
                  AND to_tsvector('simple', document) @@ plainto_tsquery('simple', %s)
                ORDER BY score DESC, recorded_at DESC LIMIT %s
                """,
                params,
            ).fetchall()
        return [
            {
                "eventId": str(row["event_id"]),
                "eventType": row["event_type"],
                "scope": row["scope"],
                "spaceId": row["space_id"],
                "acl": row["acl_json"],
                "recordedAt": row["recorded_at"],
                "snippet": row["document"][:800],
                "evidenceRefs": row["evidence_refs_json"],
                "score": float(row["score"]),
            }
            for row in rows
        ]

    def audit(self, actor_id: str, action: str, target_id: str | None, space_id: str | None, metadata: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_events(actor_id,action,target_id,space_id,metadata_json) VALUES (%s,%s,%s,%s,%s::jsonb)",
                (actor_id, action, target_id, space_id, canonical_json(metadata)),
            )

    def list_audit(self, cursor: int, limit: int) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id,occurred_at,actor_id,action,target_id,space_id,metadata_json
                FROM audit_events WHERE id>%s ORDER BY id LIMIT %s
                """,
                (cursor, limit),
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "occurredAt": row["occurred_at"].isoformat(),
                "actorId": row["actor_id"],
                "action": row["action"],
                "targetId": row["target_id"],
                "spaceId": row["space_id"],
                "metadata": row["metadata_json"],
            }
            for row in rows
        ]

    def record_restore_verification(
        self,
        actor_id: str,
        *,
        status: str,
        backup_id: str | None,
        event_count: int | None,
        evidence_count: int | None,
        detail: dict[str, Any],
    ) -> dict[str, Any]:
        if status not in {"success", "failed"}:
            raise MemoryError("Restore verification status must be success or failed.")
        with self._connect() as connection:
            row = connection.execute(
                """
                INSERT INTO restore_verifications(actor_id,status,backup_id,event_count,evidence_count,detail_json)
                VALUES (%s,%s,%s,%s,%s,%s::jsonb)
                RETURNING id,verified_at,status
                """,
                (actor_id, status, backup_id, event_count, evidence_count, canonical_json(detail)),
            ).fetchone()
        return {"id": int(row["id"]), "verifiedAt": row["verified_at"].isoformat(), "status": row["status"]}

    def report_replication_client(
        self, client_id: str, actor_id: str, pull_cursor: int, outbox_pending: int
    ) -> None:
        if pull_cursor < 0 or outbox_pending < 0:
            raise MemoryError("Replication cursor and outbox count must be non-negative.")
        with self._connect() as connection:
            row = connection.execute(
                """
                INSERT INTO replication_clients(client_id,actor_id,last_seen,pull_cursor,outbox_pending)
                VALUES (%s,%s,now(),%s,%s)
                ON CONFLICT(client_id) DO UPDATE SET
                  last_seen=excluded.last_seen,
                  pull_cursor=excluded.pull_cursor,outbox_pending=excluded.outbox_pending
                WHERE replication_clients.actor_id=excluded.actor_id
                RETURNING client_id
                """,
                (client_id, actor_id, pull_cursor, outbox_pending),
            ).fetchone()
            if row is None:
                raise MemoryError("Replication client fingerprint is already bound to another identity.")

    def run_jobs_once(
        self,
        limit: int = 100,
        *,
        evidence_verify: Callable[[str], bool] | None = None,
    ) -> dict[str, int]:
        completed = failed = 0
        with self._connect() as connection:
            with connection.transaction():
                jobs = connection.execute(
                    """
                    WITH candidates AS (
                      SELECT id FROM memory_jobs
                      WHERE (
                        status IN ('pending','retry') AND available_at <= now()
                      ) OR (
                        status='running' AND locked_at < now() - interval '5 minutes'
                      )
                      ORDER BY id FOR UPDATE SKIP LOCKED LIMIT %s
                    )
                    UPDATE memory_jobs j SET status='running',locked_at=now(),attempts=attempts+1
                    FROM candidates c WHERE j.id=c.id
                    RETURNING j.*
                    """,
                    (limit,),
                ).fetchall()
            for job in jobs:
                try:
                    event_id = str(job["payload_json"]["eventId"])
                    with connection.transaction():
                        event = connection.execute(
                            "SELECT * FROM memory_events WHERE event_id=%s",
                            (event_id,),
                        ).fetchone()
                        if event is None:
                            raise MemoryError(f"Projection event not found: {event_id}")
                        latest_projection = connection.execute(
                            """
                            SELECT event_id FROM memory_events
                            WHERE stream_id=%s AND event_type = ANY(%s)
                            ORDER BY stream_version DESC LIMIT 1
                            """,
                            (event["stream_id"], list(PROJECTABLE_EVENT_TYPES)),
                        ).fetchone()
                        if latest_projection and latest_projection["event_id"] != event["event_id"]:
                            connection.execute(
                                "UPDATE memory_jobs SET status='completed',last_error=NULL WHERE id=%s",
                                (job["id"],),
                            )
                            completed += 1
                            continue
                        references = event["evidence_refs_json"]
                        if evidence_verify is not None and not all(
                            evidence_verify(str(reference).split(":", 1)[1])
                            for reference in references
                        ):
                            # The durable event remains canonical, but a
                            # derivative cannot outlive an unreadable proof.
                            # Delete the current stream document before
                            # retrying, so a prior revision cannot answer a
                            # question while the current evidence is broken.
                            connection.execute(
                                "DELETE FROM search_documents WHERE stream_id=%s",
                                (event["stream_id"],),
                            )
                            connection.execute(
                                """
                                UPDATE memory_jobs SET
                                  status=CASE WHEN attempts>=5 THEN 'dead' ELSE 'retry' END,
                                  available_at=now() + (interval '1 second' * LEAST(300, power(2, attempts))),
                                  last_error='search projection withheld: evidence verification failed'
                                WHERE id=%s
                                """,
                                (job["id"],),
                            )
                            failed += 1
                            continue
                        if event["event_type"] in REMOVES_SEARCH_DOCUMENT:
                            connection.execute("DELETE FROM search_documents WHERE stream_id=%s", (event["stream_id"],))
                        elif event["event_type"] in ACTIVE_SEARCH_EVENT_TYPES:
                            # Search is a current-state projection. Retaining older
                            # revisions would let stale knowledge outrank its replacement.
                            connection.execute(
                                "DELETE FROM search_documents WHERE stream_id=%s",
                                (event["stream_id"],),
                            )
                            connection.execute(
                                """
                                INSERT INTO search_documents(
                                  event_id,stream_id,event_type,scope,space_id,acl_json,recorded_at,document,evidence_refs_json
                                ) VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s::jsonb)
                                ON CONFLICT(event_id) DO NOTHING
                                """,
                                (
                                    event["event_id"],
                                    event["stream_id"],
                                    event["event_type"],
                                    event["scope"],
                                    event["space_id"],
                                    canonical_json(event["acl_json"]),
                                    event["recorded_at"],
                                    canonical_json(event["payload_json"]),
                                    canonical_json(event["evidence_refs_json"]),
                                ),
                            )
                        connection.execute("UPDATE memory_jobs SET status='completed',last_error=NULL WHERE id=%s", (job["id"],))
                    completed += 1
                except Exception as exc:
                    with connection.transaction():
                        connection.execute(
                            """
                            UPDATE memory_jobs SET status=CASE WHEN attempts>=5 THEN 'dead' ELSE 'retry' END,
                              available_at=now() + (interval '1 second' * LEAST(300, power(2, attempts))),
                              last_error=%s WHERE id=%s
                            """,
                            (str(exc), job["id"]),
                        )
                    failed += 1
        return {"claimed": len(jobs), "completed": completed, "failed": failed}

    def rebuild_search_projection(
        self,
        *,
        evidence_verify: Callable[[str], bool] | None = None,
    ) -> dict[str, int]:
        """Atomically rebuild the Team search projection from canonical events."""

        with self._connect() as connection:
            with connection.transaction():
                connection.execute("DELETE FROM search_documents")
                rows = connection.execute(
                    """
                    WITH latest AS (
                      SELECT DISTINCT ON (stream_id) *
                      FROM memory_events
                      WHERE event_type = ANY(%s)
                      ORDER BY stream_id, stream_version DESC
                    )
                    SELECT * FROM latest WHERE event_type = ANY(%s)
                    ORDER BY position
                    """,
                    (list(PROJECTABLE_EVENT_TYPES), list(ACTIVE_SEARCH_EVENT_TYPES)),
                ).fetchall()
                withheld = 0
                for event in rows:
                    if evidence_verify is not None and not all(
                        evidence_verify(str(reference).split(":", 1)[1])
                        for reference in event["evidence_refs_json"]
                    ):
                        withheld += 1
                        continue
                    connection.execute(
                        """
                        INSERT INTO search_documents(
                          event_id,stream_id,event_type,scope,space_id,acl_json,recorded_at,document,evidence_refs_json
                        ) VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s::jsonb)
                        """,
                        (
                            event["event_id"],
                            event["stream_id"],
                            event["event_type"],
                            event["scope"],
                            event["space_id"],
                            canonical_json(event["acl_json"]),
                            event["recorded_at"],
                            canonical_json(event["payload_json"]),
                            canonical_json(event["evidence_refs_json"]),
                        ),
                    )
        return {"streams": len(rows), "documents": len(rows) - withheld, "withheldUnverifiableEvidence": withheld}

    def verify_integrity(self, blob_exists: Callable[[str], bool], *, evidence_limit: int = 0) -> dict[str, Any]:
        """Verify a restored Team ledger without returning sensitive content.

        ``evidence_limit=0`` is the safe default: every referenced blob is
        checked. A positive limit supports an explicitly documented sampling
        policy for very large disaster-recovery rehearsals.
        """

        from .events import _event_hash

        errors: list[dict[str, str]] = []
        evidence: set[str] = set()
        versions: dict[str, int] = {}
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM memory_events ORDER BY position").fetchall()
            for raw in rows:
                event_id = str(raw["event_id"])
                try:
                    event = self._row_to_event(raw)
                    expected_version = versions.get(event.stream_id, 0) + 1
                    if event.stream_version != expected_version:
                        errors.append({"eventId": event_id, "error": "non-contiguous stream version"})
                    versions[event.stream_id] = event.stream_version
                    if event.event_hash != _event_hash(event):
                        errors.append({"eventId": event_id, "error": "canonical event hash mismatch"})
                    evidence.update(reference.split(":", 1)[1] for reference in event.evidence_refs)
                except Exception as exc:
                    errors.append({"eventId": event_id, "error": f"invalid canonical event: {exc}"})
            projected = int(connection.execute("SELECT COUNT(*) AS value FROM search_documents").fetchone()["value"])
            orphaned = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS value FROM search_documents d
                    LEFT JOIN memory_events e ON e.event_id=d.event_id
                    WHERE e.event_id IS NULL
                    """
                ).fetchone()["value"]
            )
        # SHA-256 ranks give a reproducible, content-independent sample rather
        # than privileging a lexical digest prefix when an operator explicitly
        # chooses not to verify every object.
        checked = sorted(evidence, key=lambda digest: hashlib.sha256(digest.encode()).digest())
        if evidence_limit > 0:
            checked = checked[:evidence_limit]
        missing = [digest for digest in checked if not blob_exists(digest)]
        if orphaned:
            errors.append({"eventId": "", "error": f"{orphaned} orphaned search documents"})
        return {
            "ok": not errors and not missing,
            "events": len(rows),
            "streams": len(versions),
            "evidenceReferences": len(evidence),
            "evidenceChecked": len(checked),
            "missingEvidence": missing,
            "searchDocuments": projected,
            "errors": errors,
        }

    def operational_metrics(self) -> dict[str, float]:
        with self._connect() as connection:
            events = int(connection.execute("SELECT COUNT(*) AS value FROM memory_events").fetchone()["value"])
            positions = connection.execute(
                "SELECT COALESCE(MAX(position),0) AS latest FROM memory_events"
            ).fetchone()
            job_rows = connection.execute(
                "SELECT status,COUNT(*) AS value FROM memory_jobs GROUP BY status"
            ).fetchall()
            oldest_pending = connection.execute(
                """
                SELECT COALESCE(EXTRACT(EPOCH FROM now()-MIN(available_at)),0) AS value
                FROM memory_jobs WHERE status IN ('pending','retry','running')
                """
            ).fetchone()["value"]
            pending_proposals = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS value FROM memory_events p
                    WHERE p.event_type IN ('assertion.proposed','projection.edit.proposed','source.publication.proposed')
                      AND NOT EXISTS (
                        SELECT 1 FROM memory_events r WHERE r.stream_id=p.stream_id
                          AND r.stream_version>p.stream_version
                          AND r.event_type IN ('assertion.accepted','assertion.rejected','assertion.retracted',
                                               'projection.edit.accepted','projection.edit.rejected',
                                               'source.published','source.publication.rejected')
                      )
                      AND NOT EXISTS (
                        SELECT 1 FROM memory_events r WHERE r.causation_id=p.event_id::text
                          AND r.event_type='source.published'
                      )
                    """
                ).fetchone()["value"]
            )
            restore_success_age = connection.execute(
                """
                SELECT EXTRACT(EPOCH FROM now()-MAX(verified_at)) AS value
                FROM restore_verifications WHERE status='success'
                """
            ).fetchone()["value"]
            restore_failure_age = connection.execute(
                """
                SELECT EXTRACT(EPOCH FROM now()-MAX(verified_at)) AS value
                FROM restore_verifications WHERE status='failed'
                """
            ).fetchone()["value"]
            replication = connection.execute(
                """
                SELECT COUNT(*) AS clients,
                       COALESCE(MAX(GREATEST(0, %s-pull_cursor)),0) AS max_lag,
                       COALESCE(SUM(outbox_pending),0) AS outbox_pending
                FROM replication_clients WHERE last_seen > now() - interval '24 hours'
                """,
                (int(positions["latest"]),),
            ).fetchone()
        metrics = {
            "wiki_memory_events_total": float(events),
            "wiki_memory_global_position": float(positions["latest"]),
            "wiki_memory_proposals_pending": float(pending_proposals),
            "wiki_memory_jobs_oldest_seconds": float(oldest_pending or 0),
            # -1 is intentionally distinct from 0: no restore has ever been
            # attested, which must alert rather than look freshly verified.
            "wiki_memory_restore_last_success_age_seconds": float(restore_success_age if restore_success_age is not None else -1),
            "wiki_memory_restore_last_failure_age_seconds": float(restore_failure_age if restore_failure_age is not None else -1),
            "wiki_memory_replication_clients_active": float(replication["clients"]),
            "wiki_memory_replication_max_lag_events": float(replication["max_lag"]),
            "wiki_memory_replication_outbox_pending": float(replication["outbox_pending"]),
        }
        for row in job_rows:
            metrics[f"wiki_memory_jobs_{row['status']}"] = float(row["value"])
        return metrics

    def healthcheck(self) -> None:
        """Perform the smallest useful database readiness check without a payload."""

        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
        except Exception as exc:
            raise MemoryError("Team database health check failed.") from exc
