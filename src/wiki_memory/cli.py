from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .capture import capture_item, karakeep_import, social_import
from .config import MemoryError, ensure_root, load_data
from .installation import prepare_installation
from .layout import create_vault, init_memory
from .quality import doctor_memory, lint_memory, scan_privacy
from .router import recommend_vault
from .search import configure_index, query_memory
from .sync import configure_syncthing
from . import __version__


def _json_file(path: str) -> dict[str, Any]:
    data = load_data(Path(path).expanduser().resolve())
    return data


def _print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wiki-memory", description="Local-first Markdown memory engine")
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

    lint = sub.add_parser("lint", help="Check memory integrity")
    lint.add_argument("root")

    doctor = sub.add_parser("doctor", help="Check dependencies, layout, and sync safety")
    doctor.add_argument("root")

    syncthing = sub.add_parser("syncthing-setup", help="Configure opted-in Agent and Mémoire Syncthing folders")
    syncthing.add_argument("root")
    syncthing.add_argument("--agent-root", help="Agent directory; defaults to the memory folder's sibling Agent directory")
    syncthing.add_argument("--device-id", help="Syncthing device ID of the other device")
    syncthing.add_argument("--device-name", help="Friendly name for the other device")

    privacy = sub.add_parser("privacy-scan", help="Scan a repository for likely secrets and personal paths")
    privacy.add_argument("path", nargs="?", default=".")
    return parser


def run(args: argparse.Namespace) -> Any:
    if args.command == "prepare-installation":
        return prepare_installation(Path(args.installation_root), Path(args.agent_source))
    if args.command == "init":
        return init_memory(Path(args.root), _json_file(args.spec))
    if args.command == "privacy-scan":
        return scan_privacy(Path(args.path).expanduser().resolve())
    root = ensure_root(args.root)
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
        return query_memory(root, args.question, args.limit)
    if args.command == "lint":
        return lint_memory(root)
    if args.command == "doctor":
        return doctor_memory(root)
    if args.command == "syncthing-setup":
        return configure_syncthing(
            root,
            agent_root=Path(args.agent_root) if args.agent_root else None,
            remote_device_id=args.device_id,
            remote_device_name=args.device_name,
        )
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
