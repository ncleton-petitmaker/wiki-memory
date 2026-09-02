from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .capture import capture_item, karakeep_import, social_import
from .backup import create_backup, restore_backup, verify_backup
from .config import MemoryError, ensure_root, load_data
from .engine import MemoryEngine
from .installation import prepare_installation
from .layout import create_vault, init_memory
from .quality import doctor_memory, lint_memory, maintenance_report, scan_privacy
from .router import recommend_vault
from .search import configure_index, query_memory
from .profiles import profile_report
from .profiles import build_profile
from .replication import export_event_pack, import_event_pack
from .sync import configure_syncthing
from . import __version__


def _json_file(path: str) -> dict[str, Any]:
    data = load_data(Path(path).expanduser().resolve())
    return data


def _print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _object_file(path: str, *, label: str) -> dict[str, Any]:
    value = _json_file(path)
    if not isinstance(value, dict):
        raise MemoryError(f"{label} must be a JSON/YAML object.")
    return value


def _secret_environment(values: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    """Return actual secret values for activation and opaque handles for checks."""

    actual: dict[str, str] = {}
    handles: dict[str, str] = {}
    for item in values:
        name, separator, environment = item.partition("=")
        name, environment = name.strip(), environment.strip()
        if not separator or not name or not environment:
            raise MemoryError("--secret-env must use SECRET_NAME=ENVIRONMENT_VARIABLE.")
        if name in actual:
            raise MemoryError(f"--secret-env declares {name} more than once.")
        value = os.environ.get(environment)
        if not value:
            raise MemoryError(f"Environment variable {environment} is not set for declared secret {name}.")
        actual[name] = value
        handles[name] = f"env:{environment}"
    return actual, handles


def _source_plugin(root: Path, args: argparse.Namespace):
    """Activate a source plugin and return its portable source contract.

    An extra manifest is deliberately opt-in.  A third-party Python plugin
    additionally needs --developer-mode in solo; executable/OCI plugins keep
    the isolated host boundary.  The Team profile retains its signature gate.
    """

    config = _object_file(args.config, label="Connector config")
    manifest_path = Path(args.manifest).expanduser().resolve() if args.manifest else None
    plugin_id = str(args.plugin or "").strip()
    extra: list[Path] = []
    if manifest_path:
        from .plugins import PluginManifest

        manifest = PluginManifest.load(manifest_path)
        if plugin_id and plugin_id != manifest.id:
            raise MemoryError("--plugin does not match the id declared by --manifest.")
        plugin_id = manifest.id
        extra = [manifest_path]
    if not plugin_id:
        raise MemoryError("Specify --plugin for an installed source or --manifest for an explicit plugin manifest.")
    secrets, secret_handles = _secret_environment(args.secret_env)
    profile_overrides: dict[str, dict[str, Any]] = {}
    if args.profile_config:
        raw_profile_overrides = _object_file(args.profile_config, label="Profile config")
        for configured_plugin_id, value in raw_profile_overrides.items():
            if not isinstance(value, dict):
                raise MemoryError("Profile config must map every plugin id to an object.")
            profile_overrides[str(configured_plugin_id)] = dict(value)
    profile_overrides[plugin_id] = config
    engine, manager = build_profile(
        root,
        args.profile,
        developer_mode=bool(args.developer_mode),
        secret_handles=secrets,
        config_overrides=profile_overrides,
        extra_plugin_manifests=extra,
    )
    fiber = manager.fibers.get(plugin_id)
    if fiber is None or fiber.state.value != "active":
        asyncio.run(manager.stop_all())
        message = fiber.message if fiber else "not loaded by the selected profile"
        raise MemoryError(f"Source plugin {plugin_id} is not active: {message}")
    source_capabilities = [item for item in fiber.manifest.provides if item.startswith("source.")]
    capability = str(args.capability or "").strip()
    if capability:
        if capability not in source_capabilities:
            asyncio.run(manager.stop_all())
            raise MemoryError(f"Plugin {plugin_id} does not provide source capability {capability}.")
    elif len(source_capabilities) == 1:
        capability = source_capabilities[0]
    else:
        asyncio.run(manager.stop_all())
        raise MemoryError(f"Plugin {plugin_id} provides multiple/no source capabilities; pass --capability explicitly.")
    from .contracts import SourceConnector

    connector = manager.services.get(capability)
    if not isinstance(connector, SourceConnector):
        asyncio.run(manager.stop_all())
        raise MemoryError(f"Plugin {plugin_id} capability {capability} is not a SourceConnector.")
    return engine, manager, connector, config, secret_handles, fiber.manifest.version, plugin_id, capability


def _add_connector_arguments(parser: argparse.ArgumentParser, *, sync: bool = False) -> None:
    parser.add_argument("root")
    parser.add_argument("--plugin", help="Source plugin id already enabled by the selected profile")
    parser.add_argument("--manifest", help="Explicit third-party or optional official plugin.yaml")
    parser.add_argument("--capability", help="Source capability when a plugin provides more than one")
    parser.add_argument("--config", required=True, help="Plugin configuration object (never put secrets here)")
    parser.add_argument("--profile", default="solo", choices=["solo", "team-client", "team-server"])
    parser.add_argument("--profile-config", help="Configuration object keyed by the other plugins in the selected profile")
    parser.add_argument("--developer-mode", action="store_true", help="Explicitly allow an untrusted in-process Python plugin in solo")
    parser.add_argument("--secret-env", action="append", default=[], metavar="SECRET=ENV", help="Pass one declared secret from an environment variable")
    if sync:
        parser.add_argument("--selection", required=True, help="Selected connector streams as an object with streams")
        parser.add_argument("--vault", required=True)
        parser.add_argument("--instance", required=True, help="Stable connector installation id")
        parser.add_argument("--scope", choices=["private", "team", "organization"], default="private")
        parser.add_argument("--space", default="local-owner")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wiki-memory",
        description="Local-first event-ledger memory engine with plugin projections",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Create a memory root from an onboarding spec")
    init.add_argument("root")
    init.add_argument("--spec", required=True)

    prepare = sub.add_parser("prepare-installation", help="Create sibling Agent and Mémoire folders")
    prepare.add_argument("installation_root")
    prepare.add_argument("--agent-source", required=True, help="Installed Wiki Memory plugin directory")

    vault = sub.add_parser("create-vault", help="Create and register an independent vault")
    vault.add_argument("root")
    vault.add_argument("--spec", required=True)

    route = sub.add_parser("recommend-vault", help="Rank existing vaults for a request")
    route.add_argument("root")
    route.add_argument("--request", required=True)

    for name in ("capture", "ingest"):
        capture = sub.add_parser(name, help=f"{name.title()} a source")
        capture.add_argument("root")
        capture.add_argument("--vault", required=True)
        source = capture.add_mutually_exclusive_group(required=True)
        source.add_argument("--file")
        source.add_argument("--url")
        source.add_argument("--text")
        capture.add_argument("--title")
        capture.add_argument("--author")
        capture.add_argument("--published-at")
        capture.add_argument("--connector", default="manual")
        capture.add_argument("--source-type", default="document")
        capture.add_argument("--status", default="unverified", choices=["fact", "inference", "open_question", "unverified"])
        capture.add_argument("--content", help="Extracted text for a URL")
        capture.add_argument("--media", action="append", default=[])
        capture.add_argument("--docling", action="store_true", help="Use Docling even for capture")

    social = sub.add_parser("social-import", help="Import normalized browser-captured social items")
    social.add_argument("root")
    social.add_argument("--vault", required=True)
    social.add_argument("--input", required=True)

    karakeep = sub.add_parser("karakeep-import", help="Import a Karakeep JSON export")
    karakeep.add_argument("root")
    karakeep.add_argument("--vault", required=True)
    karakeep.add_argument("--input", required=True)

    index = sub.add_parser("index", help="Configure and refresh the local QMD index")
    index.add_argument("root")
    index.add_argument("--no-embed", action="store_true")

    query = sub.add_parser("query", help="Search the memory locally")
    query.add_argument("root")
    query.add_argument("question")
    query.add_argument("--limit", type=int, default=10)
    temporal_axis = query.add_mutually_exclusive_group()
    temporal_axis.add_argument("--system-at", help="What the memory knew at an ISO 8601 date")
    temporal_axis.add_argument("--valid-at", help="What was true in the world at an ISO 8601 date")

    lint = sub.add_parser("lint", help="Check memory integrity")
    lint.add_argument("root")
    lint.add_argument(
        "--contradiction",
        nargs=2,
        action="append",
        default=[],
        metavar=("FACT_A", "FACT_B"),
        help="Propose, but never apply, a temporal resolution for two contradictory fact notes",
    )

    maintenance = sub.add_parser("maintenance", help="List temporal facts that need review")
    maintenance.add_argument("root")
    maintenance.add_argument("--older-than-months", type=int, default=6)

    doctor = sub.add_parser("doctor", help="Check dependencies, layout, and sync safety")
    doctor.add_argument("root")

    syncthing = sub.add_parser("syncthing-setup", help="Configure opted-in Agent and Mémoire Syncthing folders")
    syncthing.add_argument("root")
    syncthing.add_argument("--agent-root", help="Agent directory; defaults to the memory folder's sibling Agent directory")
    syncthing.add_argument("--device-id", help="Syncthing device ID of the other device")
    syncthing.add_argument("--device-name", help="Friendly name for the other device")

    privacy = sub.add_parser("privacy-scan", help="Scan a repository for likely secrets and personal paths")
    privacy.add_argument("path", nargs="?", default=".")

    verify = sub.add_parser("verify", help="Verify the canonical event ledger and evidence hashes")
    verify.add_argument("root")

    rebuild = sub.add_parser("rebuild", help="Rebuild a derived projection from canonical events")
    rebuild.add_argument("root")
    rebuild.add_argument("--projection", default="projection.markdown")
    rebuild.add_argument("--force", action="store_true", help="Discard unreviewed projection edits")

    markdown_edits = sub.add_parser("markdown-edits", help="Turn modified projection files into sourced proposals")
    markdown_edits.add_argument("root")
    markdown_edits.add_argument("--actor", default="local-owner")

    markdown_review = sub.add_parser("markdown-edit-review", help="Accept or reject a private Markdown edit proposal")
    markdown_review.add_argument("root")
    markdown_review.add_argument("proposal_event_id")
    markdown_review.add_argument("decision", choices=["accept", "reject"])
    markdown_review.add_argument("--actor", default="local-owner")
    markdown_review.add_argument("--reason")

    events = sub.add_parser("events", help="Read canonical events after a cursor")
    events.add_argument("root")
    events.add_argument("--cursor", type=int, default=0)
    events.add_argument("--limit", type=int, default=100)

    backup = sub.add_parser("backup", help="Create and verify a consistent local backup")
    backup.add_argument("root")
    backup.add_argument("destination")

    backup_verify = sub.add_parser("backup-verify", help="Verify a Wiki Memory backup archive")
    backup_verify.add_argument("archive")

    backup_restore = sub.add_parser("backup-restore", help="Restore a verified backup into an empty directory")
    backup_restore.add_argument("archive")
    backup_restore.add_argument("target")

    pack_export = sub.add_parser("event-pack-export", help="Export immutable events for safe file synchronization")
    pack_export.add_argument("root")
    pack_export.add_argument("--cursor", type=int, default=0)
    pack_export.add_argument("--destination")

    pack_import = sub.add_parser("event-pack-import", help="Validate and import an immutable event pack")
    pack_import.add_argument("root")
    pack_import.add_argument("pack")

    profile = sub.add_parser("profile-doctor", help="Resolve a plugin profile and report every lifecycle state")
    profile.add_argument("root")
    profile.add_argument("--profile", default="solo", choices=["solo", "team-client", "team-server"])
    profile.add_argument("--config", help="JSON/YAML object keyed by plugin id")

    audio = sub.add_parser("audio-ingest", help="Preserve and transcribe an MP3, M4A, or WAV file")
    audio.add_argument("root")
    audio.add_argument("file")
    audio.add_argument("--vault", required=True)
    audio.add_argument("--provider", required=True, choices=["mistral", "local"])
    audio.add_argument("--title")
    audio.add_argument("--language")
    audio.add_argument("--no-diarize", action="store_true")
    audio.add_argument("--timestamp-granularity", choices=["segment", "word"], default="segment")
    audio.add_argument("--context-bias", action="append", default=[])
    audio.add_argument("--mistral-model", default="voxtral-mini-latest")
    audio.add_argument("--mistral-base-url")
    audio.add_argument("--whisper-model")
    audio.add_argument("--whisper-binary")

    postgres_check = sub.add_parser("postgres-check", help="Verify that a source PostgreSQL account is read-only")
    postgres_check.add_argument("root")
    postgres_check.add_argument("--config", required=True)

    postgres_discover = sub.add_parser("postgres-discover", help="Discover allowed PostgreSQL schemas and streams")
    postgres_discover.add_argument("root")
    postgres_discover.add_argument("--config", required=True)

    postgres_sync = sub.add_parser("postgres-sync", help="Ingest selected PostgreSQL streams with durable checkpoints")
    postgres_sync.add_argument("root")
    postgres_sync.add_argument("--config", required=True)
    postgres_sync.add_argument("--selection", required=True)
    postgres_sync.add_argument("--vault", required=True)
    postgres_sync.add_argument("--instance", required=True)
    postgres_sync.add_argument("--scope", choices=["private", "team", "organization"], default="private")
    postgres_sync.add_argument("--space", default="local-owner")

    connector_check = sub.add_parser("connector-check", help="Check any SourceConnector plugin without ingesting data")
    _add_connector_arguments(connector_check)
    connector_discover = sub.add_parser("connector-discover", help="Discover streams exposed by any SourceConnector plugin")
    _add_connector_arguments(connector_discover)
    connector_sync = sub.add_parser("connector-sync", help="Durably ingest any SourceConnector plugin through the canonical runtime")
    _add_connector_arguments(connector_sync, sync=True)

    team_sync = sub.add_parser("team-sync", help="Push the shared outbox and pull authorized Team events")
    team_sync.add_argument("root")
    team_sync.add_argument("--server", required=True)
    team_sync.add_argument("--cursor", type=int, help="Override the durable replication cursor")

    team_detach = sub.add_parser("team-detach", help="Leave private memory intact and make shared vaults read-only")
    team_detach.add_argument("root")

    team_serve = sub.add_parser("team-serve", help="Run the self-hosted Team API from environment configuration")
    team_serve.add_argument("--host", default="127.0.0.1")
    team_serve.add_argument("--port", type=int, default=8787)

    sub.add_parser("team-preflight", help="Verify non-secret Team readiness gates from environment configuration")

    local_serve = sub.add_parser("serve", help="Run the authenticated loopback HTTP API")
    local_serve.add_argument("root")
    local_serve.add_argument("--host", default="127.0.0.1")
    local_serve.add_argument("--port", type=int, default=8765)

    mcp_serve = sub.add_parser("mcp-serve", help="Run the agent-agnostic MCP stdio gateway")
    mcp_serve.add_argument("root")
    mcp_serve.add_argument("--actor", default="local-owner")
    mcp_serve.add_argument("--no-review", action="store_true")
    return parser


def run(args: argparse.Namespace) -> Any:
    if args.command == "prepare-installation":
        return prepare_installation(Path(args.installation_root), Path(args.agent_source))
    if args.command == "init":
        return init_memory(Path(args.root), _json_file(args.spec))
    if args.command == "privacy-scan":
        return scan_privacy(Path(args.path).expanduser().resolve())
    if args.command == "backup-verify":
        return verify_backup(Path(args.archive))
    if args.command == "backup-restore":
        return restore_backup(Path(args.archive), Path(args.target))
    if args.command == "team-serve":
        try:
            import uvicorn
        except ImportError as exc:
            raise MemoryError("team-serve requires the 'server' optional dependencies.") from exc
        uvicorn.run("wiki_memory.team_server:app_from_environment", factory=True, host=args.host, port=args.port)
        return {"ok": True}
    if args.command == "team-preflight":
        from .team_preflight import team_preflight

        return team_preflight()
    if args.command == "mcp-serve":
        from .mcp_gateway import serve_mcp

        serve_mcp(Path(args.root), actor_id=args.actor, include_review=not args.no_review)
        return {"ok": True}
    root = ensure_root(args.root)
    if args.command == "serve":
        try:
            import uvicorn
        except ImportError as exc:
            raise MemoryError("serve requires the 'server' optional dependencies.") from exc
        from .local_api import create_local_app, local_api_token

        if args.host not in {"127.0.0.1", "::1", "localhost"}:
            raise MemoryError("The solo API may only listen on loopback.")
        token = local_api_token(root)
        print(json.dumps({"local_api_token": token, "warning": "Keep this token private."}), file=sys.stderr)
        uvicorn.run(create_local_app(root, token), host=args.host, port=args.port)
        return {"ok": True}
    if args.command == "create-vault":
        return create_vault(root, _json_file(args.spec))
    if args.command == "recommend-vault":
        return recommend_vault(root, _json_file(args.request))
    if args.command in {"capture", "ingest"}:
        return capture_item(
            root,
            args.vault,
            source_type=args.source_type,
            source_url=args.url,
            source_file=Path(args.file) if args.file else None,
            text=args.content if args.url else args.text,
            title=args.title,
            author=args.author,
            published_at=args.published_at,
            connector=args.connector,
            epistemic_status=args.status,
            media=args.media,
            use_docling=args.command == "ingest" or args.docling,
        )
    if args.command == "social-import":
        return {"items": social_import(root, args.vault, Path(args.input).expanduser().resolve())}
    if args.command == "karakeep-import":
        return {"items": karakeep_import(root, args.vault, Path(args.input).expanduser().resolve())}
    if args.command == "index":
        return configure_index(root, embed=not args.no_embed)
    if args.command == "query":
        return query_memory(
            root,
            args.question,
            args.limit,
            system_at=args.system_at,
            valid_at=args.valid_at,
        )
    if args.command == "lint":
        return lint_memory(root, [tuple(pair) for pair in args.contradiction])
    if args.command == "maintenance":
        if args.older_than_months < 0:
            raise MemoryError("--older-than-months must be non-negative.")
        return maintenance_report(root, older_than_months=args.older_than_months)
    if args.command == "doctor":
        return doctor_memory(root)
    if args.command == "syncthing-setup":
        return configure_syncthing(
            root,
            agent_root=Path(args.agent_root) if args.agent_root else None,
            remote_device_id=args.device_id,
            remote_device_name=args.device_name,
        )
    if args.command == "verify":
        return MemoryEngine(root).verify()
    if args.command == "rebuild":
        return MemoryEngine(root).rebuild(args.projection, force=args.force)
    if args.command == "markdown-edits":
        from .operations import capture_projection_edits

        return capture_projection_edits(MemoryEngine(root), actor_id=args.actor)
    if args.command == "markdown-edit-review":
        from .operations import review_projection_edit

        return review_projection_edit(
            MemoryEngine(root),
            proposal_event_id=args.proposal_event_id,
            actor_id=args.actor,
            decision=args.decision,
            reason=args.reason,
        )
    if args.command == "events":
        engine = MemoryEngine(root)
        return {
            "cursor": args.cursor,
            "events": [event.to_dict() for event in engine.events.iter_events(args.cursor, limit=args.limit)],
        }
    if args.command == "backup":
        return create_backup(root, Path(args.destination))
    if args.command == "event-pack-export":
        return export_event_pack(
            root,
            cursor=args.cursor,
            destination=Path(args.destination) if args.destination else None,
        )
    if args.command == "event-pack-import":
        return import_event_pack(root, Path(args.pack))
    if args.command == "profile-doctor":
        overrides = _json_file(args.config) if args.config else {}
        configured_secrets = {
            name: value
            for name, value in {
                "TEAM_ACCESS_TOKEN": os.environ.get("WIKI_MEMORY_TEAM_TOKEN"),
                "MISTRAL_API_KEY": os.environ.get("MISTRAL_API_KEY"),
                "POSTGRES_DSN": os.environ.get("WIKI_MEMORY_POSTGRES_DSN"),
            }.items()
            if value
        }
        return profile_report(root, args.profile, config_overrides=overrides, secret_handles=configured_secrets)
    if args.command == "audio-ingest":
        from .audio import AudioIngestor, MistralTranscriber, WhisperCppTranscriber

        engine = MemoryEngine(root)
        if args.provider == "mistral":
            key = os.environ.get("MISTRAL_API_KEY")
            if not key:
                raise MemoryError("Set MISTRAL_API_KEY in the process environment; it is never stored in the memory.")
            provider = MistralTranscriber(key, model=args.mistral_model, base_url=args.mistral_base_url)
        else:
            if not args.whisper_model:
                raise MemoryError("--whisper-model is required for the local provider.")
            provider = WhisperCppTranscriber(Path(args.whisper_model), binary=args.whisper_binary)
        return asyncio.run(
            AudioIngestor(engine, provider).ingest(
                Path(args.file),
                vault=args.vault,
                title=args.title,
                language=args.language,
                diarize=not args.no_diarize,
                timestamp_granularity=args.timestamp_granularity,
                context_bias=args.context_bias,
            )
        )
    if args.command in {"connector-check", "connector-discover", "connector-sync"}:
        from .contracts import SourceSelection
        from .ingestion import SourceIngestionRuntime

        engine, manager, connector, config, secret_handles, version, _, _ = _source_plugin(root, args)
        try:
            if args.command == "connector-check":
                return asdict(asyncio.run(connector.check(config, secret_handles)))
            if args.command == "connector-discover":
                return asdict(asyncio.run(connector.discover(config)))
            selection_value = _object_file(args.selection, label="Connector selection")
            streams = selection_value.get("streams")
            if not isinstance(streams, dict):
                raise MemoryError("Connector selection requires an object-valued streams field.")
            result = asyncio.run(
                SourceIngestionRuntime(engine).run(
                    connector,
                    connector_instance_id=args.instance,
                    selection=SourceSelection(streams=dict(streams)),
                    vault=args.vault,
                    scope=args.scope,
                    space_id=args.space,
                    plugin_version=version,
                )
            )
            return asdict(result)
        finally:
            asyncio.run(manager.stop_all())
    if args.command in {"postgres-check", "postgres-discover", "postgres-sync"}:
        from .contracts import SourceSelection
        from .ingestion import SourceIngestionRuntime
        from .postgres_source import PostgresSourceConnector

        dsn = os.environ.get("WIKI_MEMORY_POSTGRES_DSN")
        if not dsn:
            raise MemoryError("Set WIKI_MEMORY_POSTGRES_DSN in the process environment; it is never stored in memory.")
        config = _json_file(args.config)
        connector = PostgresSourceConnector(dsn, allowlist=config)
        if args.command == "postgres-check":
            return asdict(asyncio.run(connector.check(config, {})))
        if args.command == "postgres-discover":
            return asdict(asyncio.run(connector.discover(config)))
        selection_value = _json_file(args.selection)
        selection = SourceSelection(streams=dict(selection_value.get("streams") or {}))
        result = asyncio.run(
            SourceIngestionRuntime(MemoryEngine(root)).run(
                connector,
                connector_instance_id=args.instance,
                selection=selection,
                vault=args.vault,
                scope=args.scope,
                space_id=args.space,
            )
        )
        return asdict(result)
    if args.command == "team-sync":
        from .team import TeamClient

        token = os.environ.get("WIKI_MEMORY_TEAM_TOKEN")
        if not token:
            raise MemoryError("Set WIKI_MEMORY_TEAM_TOKEN in the process environment.")
        return TeamClient(MemoryEngine(root), args.server, lambda: token).sync(pull_cursor=args.cursor)
    if args.command == "team-detach":
        from .team import detach_team

        return detach_team(root)
    raise MemoryError(f"Unknown command: {args.command}")


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        payload = run(args)
        _print(payload)
        if isinstance(payload, dict) and payload.get("ok") is False:
            raise SystemExit(1)
    except MemoryError as exc:
        _print({"ok": False, "error": str(exc)})
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
