from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, AsyncIterator

from .config import MemoryError, utc_now
from .contracts import (
    CheckResult,
    ConnectorCapabilities,
    ConnectorSpec,
    SourceCatalog,
    SourceConnector,
    SourceMessage,
    SourceSelection,
    SourceStream,
)
from .events import canonical_json


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return {"encoding": "hex", "value": value.hex()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return str(value)


class PostgresSourceConnector(SourceConnector):
    def __init__(self, dsn: str, *, batch_size: int = 500, allowlist: dict[str, Any] | None = None):
        self.dsn = dsn
        self.batch_size = batch_size
        self.allowlist = dict(allowlist or {})

    def _connect(self):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise MemoryError("PostgreSQL ingestion requires the 'postgres' optional dependencies.") from exc
        return psycopg.connect(self.dsn, row_factory=dict_row, autocommit=False)

    async def spec(self) -> ConnectorSpec:
        return ConnectorSpec(
            id="source-postgres",
            display_name="PostgreSQL",
            config_schema={
                "type": "object",
                "required": ["schemas", "tables", "columns"],
                "properties": {
                    "schemas": {"type": "array", "items": {"type": "string"}},
                    "tables": {"type": "array", "items": {"type": "string"}},
                    "columns": {"type": "object"},
                    "batchSize": {"type": "integer", "minimum": 1, "maximum": 10000},
                },
                "additionalProperties": False,
            },
            capabilities=ConnectorCapabilities(
                backfill=True,
                incremental=True,
                hard_deletes=False,
                schema_changes=True,
            ),
        )

    async def check(self, config: dict[str, Any], secret_handles: dict[str, str]) -> CheckResult:
        try:
            self._validate_allowlist_config(config)
        except MemoryError as exc:
            return CheckResult(False, str(exc))
        try:
            with self._connect() as connection:
                identity = connection.execute(
                    """
                    SELECT current_user AS username, r.rolsuper, r.rolcreaterole, r.rolcreatedb, r.rolreplication
                    FROM pg_roles r WHERE r.rolname=current_user
                    """
                ).fetchone()
                dangerous_role = any(
                    bool(identity[key]) for key in ("rolsuper", "rolcreaterole", "rolcreatedb", "rolreplication")
                )
                write_grants = connection.execute(
                    """
                    SELECT n.nspname AS table_schema,c.relname AS table_name
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE c.relkind IN ('r','p','v','m','f')
                      AND n.nspname NOT IN ('pg_catalog','information_schema')
                      AND n.nspname NOT LIKE 'pg_toast%'
                      AND (
                        has_table_privilege(c.oid,'INSERT')
                        OR has_table_privilege(c.oid,'UPDATE')
                        OR has_table_privilege(c.oid,'DELETE')
                        OR has_table_privilege(c.oid,'TRUNCATE')
                        OR has_table_privilege(c.oid,'REFERENCES')
                        OR has_table_privilege(c.oid,'TRIGGER')
                      )
                    LIMIT 20
                    """
                ).fetchall()
                connection.rollback()
            if dangerous_role or write_grants:
                return CheckResult(
                    False,
                    "The PostgreSQL connector account is not strictly read-only.",
                    {"role": identity, "writeGrants": write_grants},
                )
            return CheckResult(True, "Connection succeeded with a read-only role.", {"user": identity["username"]})
        except Exception as exc:
            return CheckResult(False, f"PostgreSQL connection failed: {exc}")

    async def discover(self, config: dict[str, Any]) -> SourceCatalog:
        self._validate_allowlist_config(config)
        self.allowlist = dict(config)
        schemas = [str(item) for item in config.get("schemas", [])]
        if not schemas:
            raise MemoryError("PostgreSQL discovery requires at least one allowed schema.")
        tables_filter = {str(item) for item in config.get("tables", [])}
        columns_filter = {
            str(table): {str(column) for column in columns}
            for table, columns in dict(config.get("columns") or {}).items()
        }
        with self._connect() as connection:
            columns = connection.execute(
                """
                SELECT table_schema,table_name,column_name,data_type,is_nullable,ordinal_position
                FROM information_schema.columns
                WHERE table_schema = ANY(%s)
                ORDER BY table_schema,table_name,ordinal_position
                """,
                (schemas,),
            ).fetchall()
            primary_keys = connection.execute(
                """
                SELECT tc.table_schema,tc.table_name,kcu.column_name,kcu.ordinal_position
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON tc.constraint_name=kcu.constraint_name AND tc.table_schema=kcu.table_schema
                WHERE tc.constraint_type='PRIMARY KEY' AND tc.table_schema = ANY(%s)
                ORDER BY tc.table_schema,tc.table_name,kcu.ordinal_position
                """,
                (schemas,),
            ).fetchall()
            connection.rollback()
        grouped: dict[str, list[dict[str, Any]]] = {}
        keys: dict[str, list[str]] = {}
        for row in columns:
            name = f"{row['table_schema']}.{row['table_name']}"
            if tables_filter and name not in tables_filter:
                continue
            if row["column_name"] not in columns_filter.get(name, set()):
                continue
            grouped.setdefault(name, []).append(row)
        for row in primary_keys:
            name = f"{row['table_schema']}.{row['table_name']}"
            keys.setdefault(name, []).append(row["column_name"])
        streams = []
        for name, items in grouped.items():
            properties = {
                row["column_name"]: {"type": ["string", "number", "integer", "boolean", "object", "array", "null"]}
                for row in items
            }
            streams.append(
                SourceStream(
                    name=name,
                    schema={"type": "object", "properties": properties, "additionalProperties": False},
                    primary_key=tuple(keys.get(name, ())),
                    capabilities=ConnectorCapabilities(backfill=True, incremental=True, schema_changes=True),
                )
            )
        return SourceCatalog(tuple(streams))

    @staticmethod
    def _validate_allowlist_config(config: dict[str, Any]) -> None:
        schemas = {str(item) for item in config.get("schemas", [])}
        tables = {str(item) for item in config.get("tables", [])}
        columns = dict(config.get("columns") or {})
        if not schemas or not tables:
            raise MemoryError("PostgreSQL requires explicit non-empty schema and table allowlists.")
        invalid_tables = sorted(table for table in tables if "." not in table or table.split(".", 1)[0] not in schemas)
        if invalid_tables:
            raise MemoryError("PostgreSQL tables are outside the schema allowlist: " + ", ".join(invalid_tables))
        missing_columns = sorted(table for table in tables if not columns.get(table))
        if missing_columns:
            raise MemoryError("PostgreSQL tables require explicit column allowlists: " + ", ".join(missing_columns))
        extra_column_sets = sorted(set(str(table) for table in columns) - tables)
        if extra_column_sets:
            raise MemoryError(
                "PostgreSQL column allowlists reference unlisted tables: "
                + ", ".join(extra_column_sets)
            )

    def _validate_selection(self, stream_name: str, options: dict[str, Any]) -> None:
        allowed_schemas = {str(item) for item in self.allowlist.get("schemas", [])}
        allowed_tables = {str(item) for item in self.allowlist.get("tables", [])}
        allowed_columns_by_table = dict(self.allowlist.get("columns") or {})
        if "." not in stream_name:
            raise MemoryError(f"PostgreSQL stream must be schema.table: {stream_name}")
        schema_name, _ = stream_name.split(".", 1)
        if schema_name not in allowed_schemas or (allowed_tables and stream_name not in allowed_tables):
            raise MemoryError(f"PostgreSQL stream is outside the configured allowlist: {stream_name}")
        configured_columns = {str(item) for item in allowed_columns_by_table.get(stream_name, [])}
        if not configured_columns:
            raise MemoryError(f"PostgreSQL stream has no explicit column allowlist: {stream_name}")
        requested = {str(item) for item in options.get("columns", [])}
        selected_columns = set(requested)
        required_selected = {str(item) for item in options.get("primaryKey", [])}
        requested.update(required_selected)
        if options.get("updatedAt"):
            required_selected.add(str(options["updatedAt"]))
            requested.add(str(options["updatedAt"]))
        outside = sorted(requested - configured_columns)
        if outside:
            raise MemoryError(f"PostgreSQL columns are outside the allowlist for {stream_name}: {', '.join(outside)}")
        missing_from_select = sorted(required_selected - selected_columns)
        if missing_from_select:
            raise MemoryError(
                f"PostgreSQL cursor/primary-key columns must be selected for {stream_name}: "
                + ", ".join(missing_from_select)
            )

    async def read(
        self,
        selection: SourceSelection,
        cursor: Any | None,
        signal: Any | None = None,
    ) -> AsyncIterator[SourceMessage]:
        try:
            from psycopg import sql
        except ImportError as exc:
            raise MemoryError("PostgreSQL ingestion requires the 'postgres' optional dependencies.") from exc
        cursor_by_stream = cursor if isinstance(cursor, dict) else {}
        with self._connect() as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            for stream_name, options in selection.streams.items():
                self._validate_selection(stream_name, options)
                schema_name, table_name = stream_name.split(".", 1)
                columns = [str(item) for item in options.get("columns", [])]
                primary_key = [str(item) for item in options.get("primaryKey", [])]
                updated_at = options.get("updatedAt")
                if not columns or not primary_key:
                    raise MemoryError(f"PostgreSQL stream {stream_name} requires columns and primaryKey.")
                selected_identifiers = [sql.Identifier(column) for column in columns]
                order_columns = ([str(updated_at)] if updated_at else []) + primary_key
                query = sql.SQL("SELECT {} FROM {}.{}").format(
                    sql.SQL(",").join(selected_identifiers),
                    sql.Identifier(schema_name),
                    sql.Identifier(table_name),
                )
                params: list[Any] = []
                stream_cursor = cursor_by_stream.get(stream_name) if isinstance(cursor_by_stream, dict) else None
                if stream_cursor:
                    overlap_seconds = int(options.get("overlapSeconds", 60 if updated_at else 0))
                    if updated_at and overlap_seconds > 0:
                        raw_value = stream_cursor.get(str(updated_at))
                        if raw_value is None:
                            raise MemoryError(f"Incomplete cursor for {stream_name}.")
                        parsed = datetime.fromisoformat(str(raw_value).replace("Z", "+00:00"))
                        query += sql.SQL(" WHERE {} >= %s").format(sql.Identifier(str(updated_at)))
                        params.append(parsed - timedelta(seconds=overlap_seconds))
                    else:
                        cursor_columns = order_columns
                        cursor_values = [stream_cursor.get(column) for column in cursor_columns]
                        if any(value is None for value in cursor_values):
                            raise MemoryError(f"Incomplete cursor for {stream_name}.")
                        query += sql.SQL(" WHERE ({}) > ({})").format(
                            sql.SQL(",").join(sql.Identifier(column) for column in cursor_columns),
                            sql.SQL(",").join(sql.Placeholder() for _ in cursor_columns),
                        )
                        params.extend(cursor_values)
                query += sql.SQL(" ORDER BY {}").format(
                    sql.SQL(",").join(sql.Identifier(column) for column in order_columns)
                )
                cursor_name = "wiki_memory_" + hashlib.sha256(stream_name.encode()).hexdigest()[:12]
                with connection.cursor(name=cursor_name) as server_cursor:
                    server_cursor.itersize = self.batch_size
                    server_cursor.execute(query, params)
                    emitted = 0
                    last_cursor: dict[str, Any] | None = None
                    for raw_row in server_cursor:
                        row = {str(key): _json_value(value) for key, value in raw_row.items()}
                        identity = {key: row.get(key) for key in primary_key}
                        source_id = canonical_json(identity)
                        source_version = hashlib.sha256(canonical_json(row).encode("utf-8")).hexdigest()
                        last_cursor = {column: row.get(column) for column in order_columns}
                        yield SourceMessage(
                            type="record",
                            stream=stream_name,
                            emitted_at=utc_now(),
                            source_id=source_id,
                            source_version=source_version,
                            occurred_at=(
                                str(row.get(updated_at))
                                if updated_at and row.get(updated_at)
                                else None
                            ),
                            payload=row,
                        )
                        emitted += 1
                        if emitted % self.batch_size == 0:
                            yield SourceMessage(
                                type="checkpoint",
                                stream=stream_name,
                                emitted_at=utc_now(),
                                cursor=last_cursor,
                            )
                if last_cursor is not None:
                    yield SourceMessage(
                        type="checkpoint",
                        stream=stream_name,
                        emitted_at=utc_now(),
                        cursor=last_cursor,
                    )
            connection.rollback()


class DebeziumMessageAdapter:
    """Translate Debezium envelopes into the canonical source protocol.

    Transport and offset persistence stay in the isolated Debezium provider. This
    adapter deliberately does not run arbitrary SQL or manage replication slots.
    """

    def parse(self, envelope: dict[str, Any], stream: str) -> SourceMessage:
        payload = envelope.get("payload", envelope)
        operation = payload.get("op")
        source = payload.get("source") or {}
        before = payload.get("before")
        after = payload.get("after")
        identity_source = after or before or {}
        primary_key = envelope.get("key") or payload.get("key") or identity_source
        source_id = canonical_json(_json_value(primary_key))
        lsn = source.get("lsn") or source.get("sequence") or payload.get("ts_ms")
        occurred = None
        if payload.get("ts_ms"):
            occurred = datetime.fromtimestamp(payload["ts_ms"] / 1000, tz=timezone.utc).isoformat()
        if operation == "d":
            return SourceMessage(
                type="delete",
                stream=stream,
                emitted_at=utc_now(),
                source_id=source_id,
                source_version=str(lsn),
                occurred_at=occurred,
                cursor={"lsn": lsn},
            )
        if operation in {"c", "u", "r"}:
            normalized = _json_value(after or {})
            return SourceMessage(
                type="record",
                stream=stream,
                emitted_at=utc_now(),
                source_id=source_id,
                source_version=str(lsn or hashlib.sha256(canonical_json(normalized).encode()).hexdigest()),
                occurred_at=occurred,
                payload=normalized,
                cursor={"lsn": lsn},
            )
        raise MemoryError(f"Unsupported Debezium operation: {operation}")
