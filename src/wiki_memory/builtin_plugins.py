from __future__ import annotations

from typing import Any

from .backup import create_backup, restore_backup, verify_backup
from .plugins import PluginContext
from .projections import MarkdownProjector


def projection_markdown(ctx: PluginContext, config: dict[str, Any]) -> None:
    registry = ctx.require("projections")
    projector = MarkdownProjector()
    registry.register(projector)
    ctx.effects.add(lambda: registry.unregister(projector.id))
    ctx.provide("projection.markdown", projector)


def backup_local(ctx: PluginContext, config: dict[str, Any]) -> None:
    service = {
        "create": create_backup,
        "verify": verify_backup,
        "restore": restore_backup,
    }
    ctx.provide("backup.local", service)


def parser_docling(ctx: PluginContext, config: dict[str, Any]) -> None:
    from .capture import _docling_convert

    ctx.provide("parser.document", _docling_convert)


def search_qmd(ctx: PluginContext, config: dict[str, Any]) -> None:
    from .search import configure_index, query_memory

    ctx.provide("search", {"configure": configure_index, "query": query_memory})


def sync_syncthing(ctx: PluginContext, config: dict[str, Any]) -> None:
    from .replication import export_event_pack, import_event_pack
    from .sync import configure_syncthing

    ctx.provide(
        "sync",
        {"configure": configure_syncthing, "export": export_event_pack, "import": import_event_pack},
    )


def source_social_browser(ctx: PluginContext, config: dict[str, Any]) -> None:
    from .social_source import SocialBrowserConnector

    ctx.provide("source.social", SocialBrowserConnector(config.get("inputPath")))


def transcriber_mistral(ctx: PluginContext, config: dict[str, Any]) -> None:
    from .audio import MistralTranscriber

    provider = MistralTranscriber(
        ctx.secret("MISTRAL_API_KEY"),
        model=str(config.get("model") or "voxtral-mini-latest"),
        base_url=config.get("baseUrl"),
    )
    ctx.provide("transcription", provider)


def transcriber_local(ctx: PluginContext, config: dict[str, Any]) -> None:
    from pathlib import Path
    from .audio import WhisperCppTranscriber

    model_path = config.get("modelPath")
    if not model_path:
        raise ValueError("transcriber-local requires modelPath")
    ctx.provide("transcription", WhisperCppTranscriber(Path(model_path), binary=config.get("binary")))


def source_audio(ctx: PluginContext, config: dict[str, Any]) -> None:
    from .audio import AudioIngestor, FFmpegMediaDecoder

    engine = ctx.require("memory-engine")
    transcriber = ctx.require("transcription")
    ingestor = AudioIngestor(
        engine,
        transcriber,
        FFmpegMediaDecoder(ffmpeg=config.get("ffmpeg"), ffprobe=config.get("ffprobe")),
    )
    ctx.provide("source.audio", ingestor)


def source_postgres(ctx: PluginContext, config: dict[str, Any]) -> None:
    from .postgres_source import PostgresSourceConnector

    connector = PostgresSourceConnector(
        ctx.secret("POSTGRES_DSN"),
        batch_size=int(config.get("batchSize", 500)),
        allowlist=dict(config),
    )
    ctx.provide("source.postgres", connector)


def team_client(ctx: PluginContext, config: dict[str, Any]) -> None:
    from .team import ProposalService, TeamClient

    server_url = str(config.get("serverUrl") or "").strip()
    if not server_url:
        raise ValueError("team-client requires serverUrl")
    engine = ctx.require("memory-engine")
    token = ctx.secret("TEAM_ACCESS_TOKEN")
    ctx.provide("identity.oidc", {"accessTokenConfigured": True})
    ctx.provide("spaces.shared", {"serverUrl": server_url})
    ctx.provide("authorization", {"mode": "server"})
    ctx.provide("replication", TeamClient(engine, server_url, lambda: token))
    ctx.provide("review", {"mode": "server-only", "mcpVisible": False})
    ctx.provide("audit", {"mode": "event-ledger"})
    ctx.provide("team-contribution", {"proposals": ProposalService(engine)})


def team_server(ctx: PluginContext, config: dict[str, Any]) -> None:
    from .team_server import create_app

    ctx.provide("team-server", {"createApp": create_app})
    ctx.provide("identity.oidc", {"mode": "oidc"})
    ctx.provide("spaces.shared", {"mode": "postgres"})
    ctx.provide("authorization", {"mode": "rbac-acl"})
    ctx.provide("review", {"mode": "risk-based"})
    ctx.provide("audit", {"mode": "append-only"})
    ctx.provide("team-search", {"mode": "postgres-fts"})
    ctx.provide("team-console", {"path": "team_console/index.html"})


def gateway_mcp(ctx: PluginContext, config: dict[str, Any]) -> None:
    from .mcp_gateway import MCPGateway

    engine = ctx.require("memory-engine")
    ctx.provide(
        "gateway.mcp",
        MCPGateway(
            engine.root,
            actor_id=str(config.get("actorId") or "local-owner"),
            include_review=bool(config.get("includeReview", True)),
        ),
    )
