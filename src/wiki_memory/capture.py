from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .config import MemoryError, content_hash, load_vault, platform_runtime_dir, slugify, utc_now
from .engine import MemoryEngine
from .events import EventActor, MemoryEvent, PluginRef
from .temporal import temporal_defaults_from_source, temporal_open_questions


TRACKING_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src"}
EPISTEMIC_STATUSES = {"fact", "inference", "open_question", "unverified"}
SOCIAL_CONNECTORS = {"instagram", "linkedin", "reddit", "x", "youtube"}


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise MemoryError(f"Invalid HTTP(S) URL: {url}")
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_KEYS
    ]
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def _frontmatter(metadata: dict[str, Any]) -> str:
    lines = ["---"]
    for key, value in metadata.items():
        if isinstance(value, list):
            if value:
                lines.append(f"{key}:")
                lines.extend(f"  - {json.dumps(item, ensure_ascii=False)}" for item in value)
            else:
                lines.append(f"{key}: []")
        elif value is None:
            lines.append(f"{key}: null")
        elif isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, (int, float)):
            lines.append(f"{key}: {value}")
        else:
            lines.append(f"{key}: {json.dumps(str(value), ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines)


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    closing = text.find("\n---\n", 4)
    if closing == -1:
        return {}, text
    raw = text[4:closing]
    try:
        import yaml

        metadata = yaml.safe_load(raw) or {}
    except Exception:
        metadata = {}
        current_list: str | None = None
        for line in raw.splitlines():
            if line.startswith("  - ") and current_list:
                try:
                    metadata[current_list].append(json.loads(line[4:]))
                except json.JSONDecodeError:
                    metadata[current_list].append(line[4:])
                continue
            if ":" not in line or line.startswith(" "):
                continue
            key, value = line.split(":", 1)
            value = value.strip()
            if not value:
                metadata[key] = []
                current_list = key
            else:
                current_list = None
                try:
                    metadata[key] = json.loads(value)
                except json.JSONDecodeError:
                    metadata[key] = value
    return metadata if isinstance(metadata, dict) else {}, text[closing + 5 :]


def _docling_convert(source: str, output: Path, root: Path) -> str:
    runtime = platform_runtime_dir()
    if Path(source).exists() and Path(source).suffix.lower() in {".md", ".markdown", ".txt"}:
        return Path(source).read_text(encoding="utf-8", errors="replace")
    python = runtime / ("venv/Scripts/python.exe" if __import__("os").name == "nt" else "venv/bin/python")
    if not python.is_file():
        raise MemoryError("Docling runtime is missing. Run scripts/bootstrap.py first.")
    completed = subprocess.run(
        [str(python), "-m", "wiki_memory.docling_bridge", source, str(output)],
        text=True,
        capture_output=True,
        timeout=1800,
        check=False,
    )
    if completed.returncode != 0:
        raise MemoryError(f"Docling conversion failed: {completed.stderr.strip() or completed.stdout.strip()}")
    return output.read_text(encoding="utf-8")


def _stable_id(origin: str) -> str:
    return content_hash(origin.encode("utf-8"))[:16]


def _social_partition(connector: str, collection: str | None) -> Path:
    if connector not in SOCIAL_CONNECTORS:
        return Path()
    label = (collection or "sans-collection").strip() or "sans-collection"
    try:
        collection_slug = slugify(label)
    except MemoryError:
        collection_slug = "collection-" + content_hash(label.encode("utf-8"))[:8]
    return Path(connector) / collection_slug


def capture_item(
    root: Path,
    vault_slug: str,
    *,
    source_type: str,
    source_url: str | None = None,
    source_file: Path | None = None,
    text: str | None = None,
    title: str | None = None,
    author: str | None = None,
    published_at: str | None = None,
    connector: str = "manual",
    collection: str | None = None,
    epistemic_status: str = "unverified",
    media: list[str] | None = None,
    raw_payload: dict[str, Any] | None = None,
    use_docling: bool = False,
    scope: str = "private",
    space_id: str = "local-owner",
    actor_id: str = "local-owner",
) -> dict[str, Any]:
    root = root.resolve()
    _, destination_vault = load_vault(root, vault_slug)  # validate before preserving data
    team_vault = dict(destination_vault.get("team") or {})
    if team_vault.get("read_only"):
        raise MemoryError(f"Shared vault {vault_slug} is read-only because Team is detached.")
    if scope == "private" and team_vault.get("managed"):
        raise MemoryError("Private captures cannot target a Team-managed vault.")
    if scope != "private":
        from .team import shared_vault_slug

        expected_vault = shared_vault_slug(space_id)
        if (
            vault_slug != expected_vault
            or not team_vault.get("managed")
            or team_vault.get("space_id") != space_id
        ):
            raise MemoryError(f"Shared captures for {space_id} must target the isolated vault {expected_vault}.")
    if not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*", connector):
        raise MemoryError(f"Invalid connector id: {connector}")
    if epistemic_status not in EPISTEMIC_STATUSES:
        raise MemoryError(f"Unsupported epistemic status: {epistemic_status}")
    if source_url:
        origin = canonicalize_url(source_url)
    elif source_file:
        source_file = source_file.expanduser().resolve()
        if not source_file.is_file():
            raise MemoryError(f"Source file does not exist: {source_file}")
        origin = source_file.name
    elif text is not None:
        origin = "text:" + content_hash(text.encode("utf-8"))
    else:
        raise MemoryError("Provide a URL, file, or text source.")

    item_id = _stable_id(origin)
    partition = _social_partition(connector, collection)
    engine = MemoryEngine(root)
    evidence_refs: list[str] = []
    media_refs: list[str] = []

    if source_file:
        evidence = engine.evidence.put_file(source_file)
        evidence_refs.append(evidence.reference)
        payload_hash = evidence.sha256
        if use_docling:
            with tempfile.TemporaryDirectory() as temp_dir:
                converted = Path(temp_dir) / "converted.md"
                body = _docling_convert(str(engine.evidence.path(evidence.reference)), converted, root)
        else:
            try:
                body = engine.evidence.path(evidence.reference).read_text(encoding="utf-8")
            except UnicodeDecodeError:
                body = f"Binary evidence preserved as `{evidence.reference}`."
        raw_reference = evidence.reference
    elif source_url:
        if text is None and use_docling:
            with tempfile.TemporaryDirectory() as temp_dir:
                converted = Path(temp_dir) / "converted.md"
                body = _docling_convert(origin, converted, root)
        else:
            body = text or ""
        payload = raw_payload or {
            "source_url": origin,
            "title": title,
            "author": author,
            "collection": collection,
            "text": body,
        }
        payload_bytes = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        evidence = engine.evidence.put_bytes(
            payload_bytes,
            media_type="application/json",
            original_name=f"{item_id}.json",
        )
        payload_hash = evidence.sha256
        raw_reference = evidence.reference
        evidence_refs.append(evidence.reference)
    else:
        body = text or ""
        evidence = engine.evidence.put_bytes(
            body.encode("utf-8"),
            media_type="text/plain; charset=utf-8",
            original_name=f"{item_id}.txt",
        )
        payload_hash = evidence.sha256
        raw_reference = evidence.reference
        evidence_refs.append(evidence.reference)

    for item in media or []:
        candidate = Path(item).expanduser().resolve()
        if candidate.is_file():
            media_evidence = engine.evidence.put_file(candidate)
            evidence_refs.append(media_evidence.reference)
            media_refs.append(media_evidence.reference)

    stream_id = f"source:{vault_slug}:{item_id}"
    idempotency_key = f"capture:{stream_id}:{payload_hash}"
    existing_idempotent = engine.events.get_by_idempotency_key(idempotency_key)
    captured_at = (
        str((existing_idempotent.payload.get("metadata") or {}).get("captured_at") or existing_idempotent.recorded_at)
        if existing_idempotent
        else utc_now()
    )
    metadata = {
        "id": item_id,
        "source_type": source_type,
        "source_url": origin if source_url else None,
        "origin": None if source_url else origin,
        "author": author,
        "published_at": published_at,
        "captured_at": captured_at,
        "connector": connector,
        "collection": (collection or "").strip() or None,
        "content_hash": payload_hash,
        "vault": vault_slug,
        "epistemic_status": epistemic_status,
        "raw": raw_reference,
        "media": media_refs,
    }
    heading = title or (source_file.stem if source_file else origin)
    current_version = engine.events.stream_version(stream_id)
    from .team import normalize_acl

    event = MemoryEvent(
        event_type=(
            existing_idempotent.event_type
            if existing_idempotent is not None
            else ("source.captured" if current_version == 0 else "source.revised")
        ),
        stream_id=stream_id,
        idempotency_key=idempotency_key,
        actor=EventActor(type="user" if connector == "manual" else "connector", id=actor_id if connector == "manual" else connector),
        plugin=PluginRef(id=f"source.{connector}", version="1.0.0"),
        scope=scope,  # type: ignore[arg-type]
        space_id=space_id,
        occurred_at=published_at,
        recorded_at=captured_at,
        evidence_refs=evidence_refs,
        acl=normalize_acl({}, owner=actor_id, space_id=space_id),
        payload={
            "sourceId": item_id,
            "vault": vault_slug,
            "partition": partition.as_posix() if str(partition) else "",
            "title": heading,
            "body": body.strip(),
            "metadata": metadata,
        },
    )
    persisted, created = engine.append(event, expected_stream_version=current_version)
    vault_path, vault = load_vault(root, vault_slug)
    item_path = vault_path / vault["folders"]["sources"] / "items" / partition / f"{item_id}.md"
    projected_metadata = dict(metadata)
    projected_metadata["recorded_at"] = persisted.recorded_at
    return {
        "status": ("captured" if persisted.stream_version == 1 else "revised") if created else "duplicate",
        "id": item_id,
        "event_id": persisted.event_id,
        "revision": persisted.stream_version,
        "path": str(item_path),
        "evidence_refs": persisted.evidence_refs,
        "fact_temporal_defaults": temporal_defaults_from_source(projected_metadata, recorded_at=persisted.recorded_at),
        "open_questions": temporal_open_questions(projected_metadata),
    }


def social_import(root: Path, vault_slug: str, input_path: Path) -> list[dict[str, Any]]:
    raw = json.loads(input_path.read_text(encoding="utf-8"))
    items = raw.get("items", []) if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise MemoryError("Social import must contain an array or an object with an items array.")
    results = []
    for item in items:
        if not isinstance(item, dict):
            raise MemoryError("Each social item must be an object.")
        connector = str(item.get("connector") or "").lower()
        if connector not in SOCIAL_CONNECTORS:
            raise MemoryError(f"Unsupported social connector: {connector}")
        results.append(
            capture_item(
                root,
                vault_slug,
                source_type="social_post" if connector != "youtube" else "video",
                source_url=item.get("source_url"),
                text=item.get("text") or item.get("transcript") or "",
                title=item.get("title"),
                author=item.get("author"),
                published_at=item.get("published_at"),
                connector=connector,
                collection=item.get("collection") or item.get("playlist"),
                epistemic_status="unverified",
                media=item.get("media") or [],
                raw_payload=item,
            )
        )
    return results


def karakeep_import(root: Path, vault_slug: str, input_path: Path) -> list[dict[str, Any]]:
    raw = json.loads(input_path.read_text(encoding="utf-8"))
    bookmarks = raw.get("bookmarks", raw.get("items", raw if isinstance(raw, list) else []))
    if not isinstance(bookmarks, list):
        raise MemoryError("Unsupported Karakeep export shape.")
    results = []
    for bookmark in bookmarks:
        if not isinstance(bookmark, dict):
            continue
        content = bookmark.get("content")
        content_url = content.get("url") if isinstance(content, dict) else None
        url = bookmark.get("url") or bookmark.get("sourceUrl") or content_url
        if not url:
            continue
        results.append(
            capture_item(
                root,
                vault_slug,
                source_type="bookmark",
                source_url=url,
                text=bookmark.get("description") or bookmark.get("textContent") or "",
                title=bookmark.get("title"),
                connector="karakeep",
                raw_payload=bookmark,
            )
        )
    return results
