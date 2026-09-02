from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from contextlib import closing
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable

import jsonschema

from .config import MemoryError, load_data
from .contracts import SourceConnector


PLUGIN_API_VERSION = "wiki-memory/v1"
PLUGIN_SDK_VERSION = "1.0.0"
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:[-+][0-9A-Za-z.-]+)?$")


def _semver_core(value: str) -> tuple[int, int, int]:
    match = SEMVER.fullmatch(value)
    if match is None:
        raise MemoryError(f"Invalid semantic version: {value}")
    return tuple(int(match.group(index)) for index in (1, 2, 3))


class PluginState(str, Enum):
    DISCOVERED = "discovered"
    PENDING = "pending"
    STARTING = "starting"
    ACTIVE = "active"
    DRAINING = "draining"
    STOPPED = "stopped"
    FAILED = "failed"
    QUARANTINED = "quarantined"


@dataclass(frozen=True)
class PluginPermissions:
    filesystem: tuple[str, ...] = ()
    network: tuple[str, ...] = ()
    secrets: tuple[str, ...] = ()
    subprocess: bool = False
    data_classes: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "PluginPermissions":
        value = value or {}
        return cls(
            filesystem=tuple(str(item) for item in value.get("filesystem", [])),
            network=tuple(str(item) for item in value.get("network", [])),
            secrets=tuple(str(item) for item in value.get("secrets", [])),
            subprocess=bool(value.get("subprocess", False)),
            data_classes=tuple(str(item) for item in value.get("dataClasses", value.get("data_classes", []))),
        )


@dataclass(frozen=True)
class PluginMigration:
    from_version: str
    to_version: str
    entrypoint: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PluginMigration":
        migration = cls(
            from_version=str(value.get("fromVersion", "")),
            to_version=str(value.get("toVersion", "")),
            entrypoint=str(value.get("entrypoint", "")),
        )
        if not SEMVER.fullmatch(migration.from_version) or not SEMVER.fullmatch(migration.to_version):
            raise MemoryError("Plugin migrations require semantic fromVersion and toVersion values.")
        if _semver_core(migration.to_version) <= _semver_core(migration.from_version):
            raise MemoryError("Plugin migration toVersion must be newer than fromVersion.")
        module, separator, attribute = migration.entrypoint.partition(":")
        if not module or not separator or not attribute:
            raise MemoryError("Plugin migration entrypoint must be module:function.")
        return migration


@dataclass(frozen=True)
class PluginManifest:
    api_version: str
    id: str
    version: str
    minimum_sdk_version: str
    runtime: str
    provides: tuple[str, ...]
    requires: tuple[str, ...]
    entrypoint: str | None
    permissions: PluginPermissions
    command: tuple[str, ...] = ()
    image: str | None = None
    config_schema: str | None = None
    migrations: tuple[PluginMigration, ...] = ()
    health_check: str | None = None
    stop_timeout_seconds: int = 15
    signature: dict[str, Any] | None = None
    source_path: Path | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any], source_path: Path | None = None) -> "PluginManifest":
        raw_migrations = value.get("migrations", [])
        if not isinstance(raw_migrations, list) or any(not isinstance(item, dict) for item in raw_migrations):
            raise MemoryError("Plugin migrations must be an array of migration objects.")
        manifest = cls(
            api_version=str(value.get("apiVersion", "")),
            id=str(value.get("id", "")),
            version=str(value.get("version", "")),
            minimum_sdk_version=str(value.get("minimumSdkVersion", "")),
            runtime=str(value.get("runtime", "python")),
            provides=tuple(str(item) for item in value.get("provides", [])),
            requires=tuple(str(item) for item in value.get("requires", [])),
            entrypoint=value.get("entrypoint"),
            command=tuple(str(item) for item in value.get("command", [])),
            image=str(value["image"]) if value.get("image") is not None else None,
            permissions=PluginPermissions.from_dict(value.get("permissions")),
            config_schema=value.get("configSchema"),
            migrations=tuple(PluginMigration.from_dict(item) for item in raw_migrations),
            health_check=value.get("healthCheck"),
            stop_timeout_seconds=int(value.get("stopTimeoutSeconds", 15)),
            signature=value.get("signature"),
            source_path=source_path,
        )
        manifest.validate()
        return manifest

    @classmethod
    def load(cls, path: Path) -> "PluginManifest":
        path = path.resolve()
        return cls.from_dict(load_data(path), source_path=path)

    def validate(self) -> None:
        if self.api_version != PLUGIN_API_VERSION:
            raise MemoryError(f"Unsupported plugin apiVersion: {self.api_version}")
        if not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*", self.id):
            raise MemoryError(f"Invalid plugin id: {self.id}")
        if not SEMVER.fullmatch(self.version):
            raise MemoryError(f"Invalid plugin version: {self.version}")
        if not SEMVER.fullmatch(self.minimum_sdk_version):
            raise MemoryError(f"Invalid minimumSdkVersion for {self.id}: {self.minimum_sdk_version}")
        if _semver_core(self.minimum_sdk_version) > _semver_core(PLUGIN_SDK_VERSION):
            raise MemoryError(
                f"Plugin {self.id} requires SDK {self.minimum_sdk_version}; Core provides {PLUGIN_SDK_VERSION}."
            )
        if self.runtime not in {"python", "executable", "oci"}:
            raise MemoryError(f"Unsupported plugin runtime: {self.runtime}")
        if self.runtime == "python" and not self.entrypoint:
            raise MemoryError(f"Python plugin {self.id} requires an entrypoint.")
        if self.runtime == "executable":
            if not self.command:
                raise MemoryError(f"Executable plugin {self.id} requires a command array.")
            if not Path(self.command[0]).is_absolute():
                raise MemoryError(f"Executable plugin {self.id} command must start with an absolute path.")
        if self.runtime == "oci":
            if not self.image or "@sha256:" not in self.image:
                raise MemoryError(f"OCI plugin {self.id} requires an image pinned by sha256 digest.")
        if len(set(self.provides)) != len(self.provides) or len(set(self.requires)) != len(self.requires):
            raise MemoryError(f"Plugin {self.id} has duplicate capabilities.")
        if len({migration.from_version for migration in self.migrations}) != len(self.migrations):
            raise MemoryError(f"Plugin {self.id} has multiple migrations from the same version.")
        if self.stop_timeout_seconds < 1 or self.stop_timeout_seconds > 300:
            raise MemoryError(f"Invalid stop timeout for plugin {self.id}.")

    def validate_config(self, config: dict[str, Any]) -> None:
        if not self.config_schema:
            raise MemoryError(f"Plugin {self.id} does not declare configSchema.")
        if self.source_path is None:
            raise MemoryError(f"Plugin {self.id} config schema cannot be resolved without a manifest path.")
        schema_path = (self.source_path.parent / self.config_schema).resolve()
        try:
            schema_path.relative_to(self.source_path.parent.parent.resolve())
        except ValueError as exc:
            raise MemoryError(f"Plugin {self.id} configSchema escapes its catalog root.") from exc
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MemoryError(f"Cannot load config schema for {self.id}: {exc}") from exc
        try:
            jsonschema.Draft202012Validator.check_schema(schema)
            errors = sorted(
                jsonschema.Draft202012Validator(schema).iter_errors(config),
                key=lambda error: tuple(str(item) for item in error.absolute_path),
            )
        except jsonschema.SchemaError as exc:
            raise MemoryError(f"Plugin {self.id} has an invalid config schema.") from exc
        if errors:
            first = errors[0]
            location = ".".join(str(item) for item in first.absolute_path) or "<root>"
            # Validation messages can echo a secret-bearing instance. Report only
            # the location and failed schema keyword.
            raise MemoryError(
                f"Plugin {self.id} config is invalid at {location} ({first.validator})."
            )


Cleanup = Callable[[], Any | Awaitable[Any]]


class EffectScope:
    def __init__(self) -> None:
        self._cleanups: list[Cleanup] = []

    def add(self, cleanup: Cleanup) -> Cleanup:
        if not callable(cleanup):
            raise MemoryError("Plugin effects must register a callable cleanup.")
        self._cleanups.append(cleanup)
        return cleanup

    async def dispose(self) -> list[str]:
        errors: list[str] = []
        for cleanup in reversed(self._cleanups):
            try:
                if inspect.iscoroutinefunction(cleanup):
                    result = cleanup()
                else:
                    # A third-party synchronous cleanup must not block the event
                    # loop and defeat the manager's shutdown deadline.
                    result = await asyncio.to_thread(cleanup)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # cleanup must continue after one failure
                errors.append(str(exc))
        self._cleanups.clear()
        return errors


class ServiceRegistry:
    def __init__(self) -> None:
        self._services: dict[str, tuple[str, Any]] = {}
        self._lock = threading.RLock()

    def register(self, capability: str, provider_id: str, service: Any) -> Cleanup:
        with self._lock:
            if capability in self._services:
                previous = self._services[capability][0]
                raise MemoryError(f"Capability {capability} already provided by {previous}.")
            self._services[capability] = (provider_id, service)

        def unregister() -> None:
            with self._lock:
                current = self._services.get(capability)
                if current and current[0] == provider_id:
                    del self._services[capability]

        return unregister

    def has(self, capability: str) -> bool:
        with self._lock:
            return capability in self._services

    def get(self, capability: str) -> Any:
        with self._lock:
            if capability not in self._services:
                raise MemoryError(f"Missing required service: {capability}")
            return self._services[capability][1]

    def provider(self, capability: str) -> str | None:
        with self._lock:
            return self._services.get(capability, (None, None))[0]

    def snapshot(self) -> dict[str, str]:
        with self._lock:
            return {capability: provider for capability, (provider, _) in self._services.items()}

    def replace_provider(
        self,
        old_provider: str,
        new_provider: str,
        replacements: dict[str, Any],
    ) -> list[Cleanup]:
        """Swap a complete capability set while holding one registry lock."""

        with self._lock:
            old_capabilities = {
                capability for capability, (provider, _) in self._services.items() if provider == old_provider
            }
            if old_capabilities != set(replacements):
                raise MemoryError("Plugin upgrade must replace exactly the old provider capabilities.")
            if not old_capabilities:
                raise MemoryError(f"No active capabilities are provided by {old_provider}.")
            for capability, service in replacements.items():
                self._services[capability] = (new_provider, service)

        cleanups: list[Cleanup] = []
        for capability in replacements:
            def unregister(capability: str = capability) -> None:
                with self._lock:
                    current = self._services.get(capability)
                    if current and current[0] == new_provider:
                        del self._services[capability]

            cleanups.append(unregister)
        return cleanups


class StagedServiceRegistry(ServiceRegistry):
    """Registry used to prove a replacement before it becomes visible."""

    def __init__(self, live: ServiceRegistry) -> None:
        super().__init__()
        self._live = live

    def has(self, capability: str) -> bool:
        return super().has(capability) or self._live.has(capability)

    def get(self, capability: str) -> Any:
        if super().has(capability):
            return super().get(capability)
        return self._live.get(capability)

    def provider(self, capability: str) -> str | None:
        return super().provider(capability) or self._live.provider(capability)

    def staged_services(self, provider_id: str) -> dict[str, Any]:
        with self._lock:
            return {
                capability: service
                for capability, (provider, service) in self._services.items()
                if provider == provider_id
            }


class PluginContext:
    def __init__(
        self,
        manifest: PluginManifest,
        services: ServiceRegistry,
        effects: EffectScope,
        config: dict[str, Any],
        secret_handles: dict[str, str],
        provider_id: str | None = None,
    ) -> None:
        self.manifest = manifest
        self.services = services
        self.effects = effects
        self.config = config
        self.secret_handles = secret_handles
        self.provider_id = provider_id or manifest.id

    def require(self, capability: str) -> Any:
        if capability not in self.manifest.requires:
            raise MemoryError(f"Plugin {self.manifest.id} did not declare required capability {capability}.")
        return self.services.get(capability)

    def provide(self, capability: str, service: Any) -> None:
        if capability not in self.manifest.provides:
            raise MemoryError(f"Plugin {self.manifest.id} did not declare provided capability {capability}.")
        self.effects.add(self.services.register(capability, self.provider_id, service))

    def effect(self, acquire: Callable[[], Cleanup | tuple[Any, Cleanup]]) -> Any | None:
        result = acquire()
        if isinstance(result, tuple):
            value, cleanup = result
            self.effects.add(cleanup)
            return value
        self.effects.add(result)
        return None

    def secret(self, name: str) -> str:
        if name not in self.manifest.permissions.secrets:
            raise MemoryError(f"Plugin {self.manifest.id} did not declare secret {name}.")
        if name not in self.secret_handles:
            raise MemoryError(f"Secret handle not configured: {name}")
        return self.secret_handles[name]


@dataclass
class PluginFiber:
    manifest: PluginManifest
    config: dict[str, Any]
    state: PluginState = PluginState.DISCOVERED
    message: str | None = None
    effects: EffectScope = field(default_factory=EffectScope)
    instance: Any = None
    previous_version: str | None = None


class PluginVersionStore:
    """Durable plugin-version ledger, deliberately separate from memory events.

    Canonical user knowledge is never migrated in place. This store records
    plugin activation/migration state so an interrupted upgrade replays the
    same idempotent migration rather than silently advancing its version.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path.resolve() if path else None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS plugin_versions (
                        plugin_id TEXT PRIMARY KEY,
                        version TEXT NOT NULL,
                        activated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS plugin_migrations (
                        plugin_id TEXT NOT NULL,
                        from_version TEXT NOT NULL,
                        to_version TEXT NOT NULL,
                        applied_at TEXT NOT NULL,
                        PRIMARY KEY(plugin_id, from_version, to_version)
                    );
                    """
                )

    def _connect(self) -> sqlite3.Connection:
        if self.path is None:
            raise MemoryError("Plugin version store is not persistent.")
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
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
        return connection

    def version(self, plugin_id: str) -> str | None:
        if self.path is None:
            return None
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT version FROM plugin_versions WHERE plugin_id=?", (plugin_id,)).fetchone()
        return str(row[0]) if row else None

    def record(self, plugin_id: str, version: str, migrations: list[PluginMigration]) -> None:
        if self.path is None:
            return
        from .config import utc_now

        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for migration in migrations:
                    connection.execute(
                        "INSERT OR IGNORE INTO plugin_migrations(plugin_id,from_version,to_version,applied_at) VALUES (?,?,?,?)",
                        (plugin_id, migration.from_version, migration.to_version, utc_now()),
                    )
                connection.execute(
                    "INSERT INTO plugin_versions(plugin_id,version,activated_at) VALUES (?,?,?) ON CONFLICT(plugin_id) DO UPDATE SET version=excluded.version,activated_at=excluded.activated_at",
                    (plugin_id, version, utc_now()),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise


PLUGIN_HOST_PROTOCOL = "wiki-memory-plugin-host/v1"


class RemotePluginService:
    """Capability-scoped RPC facade for an isolated plugin process.

    The facade intentionally exposes no Core object. An isolated connector can
    only receive its declared config/secrets and issue explicit protocol calls.
    A consumer must opt into the remote contract by calling ``call``; existing
    in-process SDK services never accidentally cross this trust boundary.
    """

    def __init__(self, host: "IsolatedPluginHost", capability: str) -> None:
        self._host = host
        self.capability = capability

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        return await self._host.call(self.capability, method, params or {})


class RemoteSourceConnector(SourceConnector):
    """SourceConnector-compatible facade over a capability-scoped host RPC.

    Isolated runtimes cannot receive Core's ledger objects. They implement the
    same four source methods over RPC. ``read`` returns one bounded batch (or
    ``{"messages": [...]}``) per invocation; Core still persists every
    evidence/event/checkpoint independently.
    """

    _MAX_MESSAGES = 10_000
    _MAX_READ_BATCHES = 1_000

    def __init__(self, service: RemotePluginService) -> None:
        self._service = service

    @staticmethod
    def _capabilities(value: Any):
        from .contracts import ConnectorCapabilities

        raw = value if isinstance(value, dict) else {}
        allowed = {key: bool(raw[key]) for key in ConnectorCapabilities.__dataclass_fields__ if key in raw}
        return ConnectorCapabilities(**allowed)

    async def spec(self):
        from .contracts import ConnectorSpec

        value = await self._service.call("spec")
        if not isinstance(value, dict):
            raise MemoryError("Isolated source connector returned an invalid spec.")
        return ConnectorSpec(
            id=str(value.get("id") or ""),
            display_name=str(value.get("displayName") or value.get("display_name") or ""),
            config_schema=dict(value.get("configSchema") or value.get("config_schema") or {}),
            capabilities=self._capabilities(value.get("capabilities")),
            documentation_url=str(value["documentationUrl"]) if value.get("documentationUrl") else None,
        )

    async def check(self, config: dict[str, Any], secret_handles: dict[str, str]):
        from .contracts import CheckResult

        value = await self._service.call("check", {"config": config, "secretHandles": secret_handles})
        if not isinstance(value, dict):
            raise MemoryError("Isolated source connector returned an invalid check result.")
        return CheckResult(
            ok=value.get("ok") is True,
            message=str(value.get("message") or ""),
            details=dict(value.get("details") or {}),
        )

    async def discover(self, config: dict[str, Any]):
        from .contracts import SourceCatalog, SourceStream

        value = await self._service.call("discover", {"config": config})
        streams = value.get("streams") if isinstance(value, dict) else None
        if not isinstance(streams, list):
            raise MemoryError("Isolated source connector returned an invalid catalog.")
        parsed = []
        for stream in streams:
            if not isinstance(stream, dict):
                raise MemoryError("Isolated source connector catalog contains an invalid stream.")
            parsed.append(
                SourceStream(
                    name=str(stream.get("name") or ""),
                    schema=dict(stream.get("schema") or {}),
                    primary_key=tuple(str(item) for item in stream.get("primaryKey", stream.get("primary_key", []))),
                    default_cursor=tuple(str(item) for item in stream.get("defaultCursor", stream.get("default_cursor", []))),
                    capabilities=self._capabilities(stream.get("capabilities")),
                )
            )
        return SourceCatalog(tuple(parsed))

    async def read(self, selection, cursor, signal=None):
        from .contracts import SourceMessage

        read_cursor = cursor
        for _ in range(self._MAX_READ_BATCHES):
            value = await self._service.call(
                "read",
                {"selection": {"streams": selection.streams}, "cursor": read_cursor},
            )
            messages = value.get("messages") if isinstance(value, dict) else value
            done = value.get("done", True) if isinstance(value, dict) else True
            if not isinstance(messages, list) or len(messages) > self._MAX_MESSAGES or not isinstance(done, bool):
                raise MemoryError("Isolated source connector returned an invalid or oversized read batch.")
            checkpoint_cursors: dict[str, Any] = {}
            for item in messages:
                if getattr(signal, "is_set", lambda: False)():
                    return
                if not isinstance(item, dict):
                    raise MemoryError("Isolated source connector returned an invalid source message.")
                message_type = str(item.get("type") or "")
                stream = str(item.get("stream") or "").strip()
                emitted_at = str(item.get("emittedAt") or item.get("emitted_at") or "").strip()
                if not stream or not emitted_at:
                    raise MemoryError("Isolated source connector source messages require stream and emittedAt.")
                payload = item.get("payload")
                if payload is not None and not isinstance(payload, dict):
                    raise MemoryError("Isolated source connector source-message payload must be an object.")
                evidence = item.get("evidence", [])
                if not isinstance(evidence, list) or any(not isinstance(ref, str) for ref in evidence):
                    raise MemoryError("Isolated source connector evidence references must be strings.")
                if message_type == "checkpoint" and item.get("cursor") is not None:
                    checkpoint_cursors[stream] = item["cursor"]
                yield SourceMessage(
                    type=message_type,
                    stream=stream,
                    emitted_at=emitted_at,
                    source_id=str(item["sourceId"]) if item.get("sourceId") is not None else None,
                    source_version=str(item["sourceVersion"]) if item.get("sourceVersion") is not None else None,
                    occurred_at=str(item["occurredAt"]) if item.get("occurredAt") is not None else None,
                    payload=payload,
                    cursor=item.get("cursor"),
                    schema=dict(item["schema"]) if isinstance(item.get("schema"), dict) else None,
                    warning=str(item["warning"]) if item.get("warning") is not None else None,
                    evidence=tuple(evidence),
                )
            if done:
                return
            if not checkpoint_cursors:
                raise MemoryError("Isolated source connector requested another read batch without a checkpoint.")
            updated_cursor = dict(read_cursor) if isinstance(read_cursor, dict) else {}
            updated_cursor.update(checkpoint_cursors)
            read_cursor = updated_cursor
        raise MemoryError("Isolated source connector exceeded the 1,000-batch read limit.")


class IsolatedPluginHost:
    """Small NDJSON host for executable and OCI plugins.

    Protocol messages are deliberately capability-neutral. Startup receives
    ``start`` and must answer ``ready`` with exactly the capabilities declared
    by its signed manifest. Calls are serialized over stdio so child plugins
    cannot access Core memory objects, event stores, or undeclared secrets.
    """

    def __init__(self, process: subprocess.Popen[bytes], runtime_dir: Path) -> None:
        self.process = process
        self.runtime_dir = runtime_dir
        self._lock = threading.Lock()
        self._sequence = 0
        self._closed = False

    @staticmethod
    def _safe_environment(runtime_dir: Path) -> dict[str, str]:
        return {
            "PATH": os.defpath,
            "HOME": str(runtime_dir),
            "TMPDIR": str(runtime_dir),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        }

    @classmethod
    async def start(
        cls,
        manifest: PluginManifest,
        config: dict[str, Any],
        secrets: dict[str, str],
    ) -> "IsolatedPluginHost":
        return await asyncio.to_thread(cls._start_sync, manifest, config, secrets)

    @classmethod
    def _start_sync(
        cls,
        manifest: PluginManifest,
        config: dict[str, Any],
        secrets: dict[str, str],
    ) -> "IsolatedPluginHost":
        runtime_dir = Path(tempfile.mkdtemp(prefix=f"wiki-memory-plugin-{manifest.id}-"))
        process: subprocess.Popen[bytes] | None = None
        try:
            if manifest.runtime == "executable":
                command = list(manifest.command)
            elif manifest.runtime == "oci":
                runtime = os.environ.get("WIKI_MEMORY_OCI_RUNTIME", "docker")
                executable = shutil.which(runtime)
                if executable is None:
                    raise MemoryError(f"OCI runtime {runtime!r} is not installed.")
                command = [
                    executable,
                    "run",
                    "--rm",
                    "--interactive",
                    "--read-only",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges",
                    "--tmpfs=/tmp:rw,noexec,nosuid,size=64m",
                    "--mount",
                    f"type=bind,src={runtime_dir},dst=/runtime,rw",
                    "--workdir=/runtime",
                    "--env=HOME=/runtime",
                    "--env=TMPDIR=/runtime",
                ]
                if not manifest.permissions.network:
                    command.append("--network=none")
                command.append(str(manifest.image))
                command.extend(manifest.command)
            else:  # guarded by manifest validation; keeps this boundary fail closed
                raise MemoryError(f"Runtime {manifest.runtime} cannot use the isolated host.")
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=runtime_dir,
                env=cls._safe_environment(runtime_dir) if manifest.runtime == "executable" else None,
            )
            host = cls(process, runtime_dir)
            host._send_sync(
                {
                    "protocol": PLUGIN_HOST_PROTOCOL,
                    "type": "start",
                    "plugin": {"id": manifest.id, "version": manifest.version},
                    "config": config,
                    "secrets": secrets,
                    "permissions": {
                        "filesystem": list(manifest.permissions.filesystem),
                        "network": list(manifest.permissions.network),
                        "subprocess": manifest.permissions.subprocess,
                        "dataClasses": list(manifest.permissions.data_classes),
                    },
                }
            )
            ready = host._receive_sync(timeout=manifest.stop_timeout_seconds)
            if ready.get("protocol") != PLUGIN_HOST_PROTOCOL or ready.get("type") != "ready":
                raise MemoryError("Isolated plugin did not complete a valid ready handshake.")
            if set(ready.get("provides") or []) != set(manifest.provides):
                raise MemoryError("Isolated plugin ready capabilities do not match its manifest.")
            return host
        except Exception:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                for handle in (process.stdin, process.stdout):
                    if handle is not None:
                        handle.close()
            shutil.rmtree(runtime_dir, ignore_errors=True)
            raise

    def _send_sync(self, value: dict[str, Any]) -> None:
        if self.process.stdin is None:
            raise MemoryError("Isolated plugin stdin is unavailable.")
        try:
            encoded = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise MemoryError("Isolated plugin message is not JSON-serializable.") from exc
        self.process.stdin.write(encoded)
        self.process.stdin.flush()

    def _receive_sync(self, *, timeout: float) -> dict[str, Any]:
        if self.process.stdout is None:
            raise MemoryError("Isolated plugin stdout is unavailable.")
        # ``readline`` is intentionally used in the worker thread. A bounded
        # timeout prevents a malicious plugin from keeping the host forever.
        result: list[bytes] = []
        failure: list[BaseException] = []

        def read_line() -> None:
            try:
                result.append(self.process.stdout.readline())
            except BaseException as exc:  # propagate I/O failure below
                failure.append(exc)

        reader = threading.Thread(target=read_line, daemon=True)
        reader.start()
        reader.join(timeout)
        if reader.is_alive():
            raise MemoryError("Isolated plugin response timed out.")
        if failure:
            raise MemoryError("Isolated plugin stdout failed.") from failure[0]
        line = result[0] if result else b""
        if not line:
            raise MemoryError("Isolated plugin exited before responding.")
        if len(line) > 1024 * 1024:
            raise MemoryError("Isolated plugin protocol message exceeds 1 MiB.")
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MemoryError("Isolated plugin returned invalid protocol JSON.") from exc
        if not isinstance(value, dict):
            raise MemoryError("Isolated plugin protocol messages must be objects.")
        return value

    async def call(self, capability: str, method: str, params: dict[str, Any]) -> Any:
        if self._closed:
            raise MemoryError("Isolated plugin is stopped.")
        if not method or not isinstance(params, dict):
            raise MemoryError("Plugin RPC requires a method and object params.")
        return await asyncio.to_thread(self._call_sync, capability, method, params)

    def _call_sync(self, capability: str, method: str, params: dict[str, Any]) -> Any:
        with self._lock:
            self._sequence += 1
            request_id = self._sequence
            self._send_sync(
                {
                    "protocol": PLUGIN_HOST_PROTOCOL,
                    "type": "call",
                    "id": request_id,
                    "capability": capability,
                    "method": method,
                    "params": params,
                }
            )
            response = self._receive_sync(timeout=30)
        if response.get("protocol") != PLUGIN_HOST_PROTOCOL or response.get("type") != "result":
            raise MemoryError("Isolated plugin returned an invalid RPC response.")
        if response.get("id") != request_id:
            raise MemoryError("Isolated plugin returned a mismatched RPC response.")
        if response.get("ok") is not True:
            raise MemoryError("Isolated plugin rejected the RPC call.")
        return response.get("result")

    async def migrate(self, migration: PluginMigration, *, timeout_seconds: int) -> None:
        """Run one idempotent plugin-owned migration through the isolated host.

        The child receives no Core objects or canonical storage handles: its
        only writable location remains its private runtime directory.  The
        version ledger is updated by Core only after the whole plugin has
        subsequently passed activation and health checks.
        """

        if self._closed:
            raise MemoryError("Isolated plugin is stopped.")
        await asyncio.to_thread(self._migrate_sync, migration, timeout_seconds)

    def _migrate_sync(self, migration: PluginMigration, timeout_seconds: int) -> None:
        with self._lock:
            self._sequence += 1
            request_id = self._sequence
            self._send_sync(
                {
                    "protocol": PLUGIN_HOST_PROTOCOL,
                    "type": "migrate",
                    "id": request_id,
                    "entrypoint": migration.entrypoint,
                    "fromVersion": migration.from_version,
                    "toVersion": migration.to_version,
                }
            )
            response = self._receive_sync(timeout=timeout_seconds)
        if response.get("protocol") != PLUGIN_HOST_PROTOCOL or response.get("type") != "migration-result":
            raise MemoryError("Isolated plugin returned an invalid migration response.")
        if response.get("id") != request_id:
            raise MemoryError("Isolated plugin returned a mismatched migration response.")
        if response.get("ok") is not True:
            raise MemoryError("Isolated plugin rejected the migration.")

    async def close(self) -> None:
        await asyncio.to_thread(self._close_sync)

    def _close_sync(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self.process.poll() is None:
                with self._lock:
                    self._send_sync({"protocol": PLUGIN_HOST_PROTOCOL, "type": "stop"})
                    try:
                        self.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait()
        finally:
            for handle in (self.process.stdin, self.process.stdout):
                if handle is not None:
                    handle.close()
            shutil.rmtree(self.runtime_dir, ignore_errors=True)


class PluginManager:
    def __init__(
        self,
        *,
        trusted_plugins: set[str] | None = None,
        require_signatures: bool = False,
        developer_mode: bool = False,
        signature_verifier: Callable[[PluginManifest], bool] | None = None,
        state_database: Path | None = None,
    ) -> None:
        self.services = ServiceRegistry()
        self.fibers: dict[str, PluginFiber] = {}
        self.trusted_plugins = trusted_plugins or set()
        self.require_signatures = require_signatures
        self.developer_mode = developer_mode
        self.signature_verifier = signature_verifier
        self.version_store = PluginVersionStore(state_database)

    @staticmethod
    def _provider_plugin_id(provider: str | None) -> str | None:
        return provider.split("@", 1)[0] if provider else None

    def add(self, manifest: PluginManifest, config: dict[str, Any] | None = None) -> None:
        if manifest.id in self.fibers:
            raise MemoryError(f"Duplicate plugin id: {manifest.id}")
        verified_signature = bool(self.signature_verifier and self.signature_verifier(manifest))
        resolved_config = config or {}
        manifest.validate_config(resolved_config)
        if self.require_signatures and manifest.id not in self.trusted_plugins and not verified_signature:
            state = PluginState.QUARANTINED
            message = "Plugin is not in the trusted catalog and has no verified signature."
        elif manifest.runtime == "python" and manifest.id not in self.trusted_plugins and not self.developer_mode:
            state = PluginState.QUARANTINED
            message = "Untrusted Python plugins require explicit developer mode or an isolated runtime."
        else:
            state = PluginState.DISCOVERED
            message = None
        previous_version = self.version_store.version(manifest.id)
        if previous_version and _semver_core(previous_version) > _semver_core(manifest.version):
            raise MemoryError(
                f"Plugin downgrade is not supported for {manifest.id}: {previous_version} -> {manifest.version}."
            )
        self.fibers[manifest.id] = PluginFiber(
            manifest,
            resolved_config,
            state=state,
            message=message,
            previous_version=previous_version,
        )

    def add_manifest(self, path: Path, config: dict[str, Any] | None = None) -> None:
        self.add(PluginManifest.load(path), config)

    async def activate_all(self, secret_handles: dict[str, str] | None = None) -> None:
        secret_handles = secret_handles or {}
        while True:
            progress = False
            for fiber in self.fibers.values():
                if fiber.state not in {PluginState.DISCOVERED, PluginState.PENDING}:
                    continue
                missing = [capability for capability in fiber.manifest.requires if not self.services.has(capability)]
                if missing:
                    fiber.state = PluginState.PENDING
                    fiber.message = "Missing services: " + ", ".join(missing)
                    continue
                await self._activate(fiber, secret_handles)
                progress = True
            if not progress:
                break

    async def _activate(
        self,
        fiber: PluginFiber,
        secret_handles: dict[str, str],
        *,
        services: ServiceRegistry | None = None,
        record_version: bool = True,
        provider_id: str | None = None,
    ) -> None:
        fiber.state = PluginState.STARTING
        fiber.message = None
        active_services = services or self.services
        allowed_secrets = {
            name: secret_handles[name]
            for name in fiber.manifest.permissions.secrets
            if name in secret_handles
        }
        context = PluginContext(
            fiber.manifest,
            active_services,
            fiber.effects,
            fiber.config,
            allowed_secrets,
            provider_id,
        )
        try:
            migration_chain = self._migration_chain(fiber)
            if fiber.manifest.runtime != "python":
                host = await IsolatedPluginHost.start(fiber.manifest, fiber.config, allowed_secrets)
                fiber.instance = host
                # Register the host cleanup first: service unregistration then
                # process termination are both performed in LIFO order.
                fiber.effects.add(host.close)
                for migration in migration_chain:
                    await host.migrate(migration, timeout_seconds=fiber.manifest.stop_timeout_seconds)
                applied_migrations = migration_chain
                for capability in fiber.manifest.provides:
                    service = RemotePluginService(host, capability)
                    # Source capabilities have a public, portable contract.
                    # Generic ingestion does not need to know whether the
                    # connector is Python, executable, or OCI.
                    if capability.startswith("source."):
                        service = RemoteSourceConnector(service)
                    context.provide(capability, service)
            else:
                applied_migrations = await self._run_python_migrations(migration_chain, context)
                assert fiber.manifest.entrypoint
                module_name, separator, attribute = fiber.manifest.entrypoint.partition(":")
                if not separator:
                    raise MemoryError(f"Invalid Python entrypoint: {fiber.manifest.entrypoint}")
                target = getattr(importlib.import_module(module_name), attribute)
                fiber.instance = target
                result = target(context, fiber.config)
                if inspect.isawaitable(result):
                    result = await result
                if callable(result):
                    fiber.effects.add(result)
            if fiber.manifest.health_check == "services":
                # During an upgrade the candidate is activated in a staging
                # registry.  Checking the live registry here would let the
                # old provider mask a candidate that failed to publish one of
                # its declared capabilities.
                missing_services = [item for item in fiber.manifest.provides if not active_services.has(item)]
                if missing_services:
                    raise MemoryError("Health check missing provided services: " + ", ".join(missing_services))
            elif fiber.manifest.health_check:
                module_name, separator, attribute = fiber.manifest.health_check.partition(":")
                if not separator:
                    raise MemoryError(f"Invalid healthCheck: {fiber.manifest.health_check}")
                check = getattr(importlib.import_module(module_name), attribute)
                health = check(context, fiber.instance)
                if inspect.isawaitable(health):
                    health = await health
                if health is not True:
                    raise MemoryError(f"Plugin health check failed: {health}")
            fiber.state = PluginState.ACTIVE
            if record_version:
                self.version_store.record(fiber.manifest.id, fiber.manifest.version, applied_migrations)
        except Exception as exc:
            try:
                cleanup_errors = await asyncio.wait_for(
                    fiber.effects.dispose(), timeout=fiber.manifest.stop_timeout_seconds
                )
            except TimeoutError:
                cleanup_errors = [
                    f"cleanup exceeded {fiber.manifest.stop_timeout_seconds} seconds"
                ]
            fiber.state = PluginState.FAILED
            suffix = f" Cleanup errors: {'; '.join(cleanup_errors)}" if cleanup_errors else ""
            fiber.message = str(exc) + suffix

    async def upgrade(
        self,
        plugin_id: str,
        manifest: PluginManifest,
        config: dict[str, Any] | None = None,
        secret_handles: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Drain and replace one live plugin without exposing a half-started one.

        The candidate starts against a staging registry first. Only a healthy
        candidate can replace the old provider's complete capability set. A
        capability consumer is drained before the swap and reactivated after it;
        a failed candidate leaves the original graph untouched.
        """

        if plugin_id not in self.fibers:
            raise MemoryError(f"Unknown plugin: {plugin_id}")
        current = self.fibers[plugin_id]
        if current.state != PluginState.ACTIVE:
            raise MemoryError(f"Plugin {plugin_id} is not active and cannot be upgraded.")
        if manifest.id != plugin_id:
            raise MemoryError("Replacement manifest id must match the active plugin.")
        if _semver_core(manifest.version) <= _semver_core(current.manifest.version):
            raise MemoryError("Plugin upgrades must move to a newer version.")
        if tuple(manifest.provides) != tuple(current.manifest.provides):
            raise MemoryError("Live plugin upgrades cannot add or remove capabilities.")
        resolved_config = config or {}
        manifest.validate_config(resolved_config)
        candidate = PluginFiber(
            manifest,
            resolved_config,
            previous_version=current.manifest.version,
        )
        staged = StagedServiceRegistry(self.services)
        candidate_provider = f"{plugin_id}@{manifest.version}"
        await self._activate(
            candidate,
            secret_handles or {},
            services=staged,
            record_version=False,
            provider_id=candidate_provider,
        )
        if candidate.state != PluginState.ACTIVE:
            raise MemoryError("Replacement plugin did not become healthy: " + (candidate.message or "unknown error"))

        dependents = [
            fiber
            for fiber in self.fibers.values()
            if fiber.manifest.id != plugin_id
            and fiber.state == PluginState.ACTIVE
            and any(
                self._provider_plugin_id(self.services.provider(required)) == plugin_id
                for required in fiber.manifest.requires
            )
        ]
        for dependent in dependents:
            await self.stop(dependent.manifest.id)

        current.state = PluginState.DRAINING
        live_cleanups = self.services.replace_provider(
            plugin_id,
            candidate_provider,
            staged.staged_services(candidate_provider),
        )
        # New live cleanups must execute before the candidate's staging-only
        # registrations, preserving normal LIFO provider teardown semantics.
        for cleanup in live_cleanups:
            candidate.effects.add(cleanup)
        try:
            cleanup_errors = await asyncio.wait_for(
                current.effects.dispose(), timeout=current.manifest.stop_timeout_seconds
            )
        except TimeoutError:
            cleanup_errors = [f"cleanup exceeded {current.manifest.stop_timeout_seconds} seconds"]
        current.state = PluginState.STOPPED if not cleanup_errors else PluginState.FAILED
        current.message = "; ".join(cleanup_errors) if cleanup_errors else None
        self.fibers[plugin_id] = candidate
        self.version_store.record(plugin_id, manifest.version, [])

        for dependent in dependents:
            if dependent.state == PluginState.STOPPED:
                dependent.state = PluginState.DISCOVERED
                dependent.message = None
        await self.activate_all(secret_handles)
        return {
            "ok": all(dependent.state == PluginState.ACTIVE for dependent in dependents),
            "plugin": plugin_id,
            "fromVersion": current.manifest.version,
            "toVersion": manifest.version,
            "drainedDependents": [dependent.manifest.id for dependent in dependents],
            "oldCleanupErrors": cleanup_errors,
        }

    def _migration_chain(self, fiber: PluginFiber) -> list[PluginMigration]:
        """Resolve, but do not record, a complete forward-only migration path."""

        previous = fiber.previous_version
        if previous is None or previous == fiber.manifest.version:
            return []
        migrations_by_from = {migration.from_version: migration for migration in fiber.manifest.migrations}
        chain: list[PluginMigration] = []
        current = previous
        while current != fiber.manifest.version:
            migration = migrations_by_from.get(current)
            if migration is None or _semver_core(migration.to_version) > _semver_core(fiber.manifest.version):
                raise MemoryError(
                    f"Plugin {fiber.manifest.id} has no complete migration path from {previous} to {fiber.manifest.version}."
                )
            chain.append(migration)
            current = migration.to_version
        return chain

    async def _run_python_migrations(
        self,
        chain: list[PluginMigration],
        context: PluginContext,
    ) -> list[PluginMigration]:
        """Run a complete forward-only migration chain before activation.

        Migrations can only prepare plugin-owned state/projections. They receive
        no escape hatch to mutate canonical events, and version recording occurs
        only after activation and health checks succeed. If a process dies in
        between, the identical chain is deliberately replayed.
        """

        for migration in chain:
            module_name, _, attribute = migration.entrypoint.partition(":")
            handler = getattr(importlib.import_module(module_name), attribute)
            result = handler(context, migration.from_version, migration.to_version)
            if inspect.isawaitable(result):
                result = await result
            if callable(result):
                context.effects.add(result)
        return chain

    async def stop(self, plugin_id: str) -> None:
        fiber = self.fibers[plugin_id]
        dependents = [
            other
            for other in self.fibers.values()
            if other.state == PluginState.ACTIVE
            and any(
                self._provider_plugin_id(self.services.provider(required)) == plugin_id
                for required in other.manifest.requires
            )
        ]
        for dependent in dependents:
            await self.stop(dependent.manifest.id)
        if fiber.state != PluginState.ACTIVE:
            return
        fiber.state = PluginState.DRAINING
        try:
            errors = await asyncio.wait_for(
                fiber.effects.dispose(), timeout=fiber.manifest.stop_timeout_seconds
            )
        except TimeoutError:
            fiber.state = PluginState.FAILED
            fiber.message = (
                f"Plugin did not stop within {fiber.manifest.stop_timeout_seconds} seconds."
            )
            return
        fiber.state = PluginState.STOPPED if not errors else PluginState.FAILED
        fiber.message = "; ".join(errors) if errors else None
        for pending in self.fibers.values():
            if pending.state == PluginState.ACTIVE and any(
                not self.services.has(required) for required in pending.manifest.requires
            ):
                pending.state = PluginState.PENDING
                pending.message = "A required provider stopped."

    async def stop_all(self) -> None:
        for plugin_id in reversed(list(self.fibers)):
            await self.stop(plugin_id)

    def report(self) -> list[dict[str, Any]]:
        return [
            {
                "id": fiber.manifest.id,
                "version": fiber.manifest.version,
                "state": fiber.state.value,
                "message": fiber.message,
                "provides": list(fiber.manifest.provides),
                "requires": list(fiber.manifest.requires),
            }
            for fiber in self.fibers.values()
        ]


def load_profile(path: Path) -> dict[str, Any]:
    profile = load_data(path)
    if profile.get("apiVersion") != PLUGIN_API_VERSION:
        raise MemoryError(f"Unsupported profile apiVersion in {path}")
    plugins = profile.get("plugins")
    if not isinstance(plugins, list):
        raise MemoryError(f"Profile {path} must contain a plugins array.")
    return profile
