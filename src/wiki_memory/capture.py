from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .config import MemoryError, content_hash, load_vault, platform_runtime_dir, slugify, utc_now


TRACKING_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src"}
ALLOWED_CONNECTORS = {"instagram", "linkedin", "reddit", "x", "youtube", "karakeep", "web", "manual"}
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
) -> dict[str, Any]:
    root = root.resolve()
    vault_path, vault = load_vault(root, vault_slug)
    if connector not in ALLOWED_CONNECTORS:
        raise MemoryError(f"Unsupported connector: {connector}")
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

    folders = vault["folders"]
    source_root = vault_path / folders["sources"]
    item_id = _stable_id(origin)
    item_root = source_root / "items"
    existing_items = list(item_root.rglob(f"{item_id}.md"))
    if len(existing_items) > 1:
        raise MemoryError(f"Multiple source notes share id {item_id}; run Wiki Memory Doctor.")
    partition = _social_partition(connector, collection)
    item_path = existing_items[0] if existing_items else item_root / partition / f"{item_id}.md"
    item_path.parent.mkdir(parents=True, exist_ok=True)
    item_partition = item_path.relative_to(item_root).parent
    raw_dir = source_root / "raw" / item_partition / item_id
    raw_dir.mkdir(parents=True, exist_ok=True)
    copied_media: list[str] = []

    if source_file:
        raw_bytes = source_file.read_bytes()
        payload_hash = content_hash(raw_bytes)
        raw_target = raw_dir / f"{payload_hash[:12]}-{source_file.name}"
        if not raw_target.exists():
            shutil.copy2(source_file, raw_target)
        if use_docling:
            with tempfile.TemporaryDirectory() as temp_dir:
                converted = Path(temp_dir) / "converted.md"
                body = _docling_convert(str(raw_target), converted, root)
        else:
            try:
                body = raw_bytes.decode("utf-8")
            except UnicodeDecodeError:
                preserved = raw_target.relative_to(vault_path).as_posix()
                body = f"Binary source preserved at `{preserved}`."
        raw_reference = raw_target.relative_to(vault_path).as_posix()
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
        payload_hash = content_hash(payload_bytes)
        raw_target = raw_dir / f"{payload_hash[:12]}.json"
        if not raw_target.exists():
            raw_target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        raw_reference = raw_target.relative_to(vault_path).as_posix()
    else:
        body = text or ""
        payload_hash = content_hash(body.encode("utf-8"))
        raw_target = raw_dir / f"{payload_hash[:12]}.txt"
        if not raw_target.exists():
            raw_target.write_text(body, encoding="utf-8")
        raw_reference = raw_target.relative_to(vault_path).as_posix()

    for item in media or []:
        candidate = Path(item).expanduser().resolve()
        if candidate.is_file():
            media_hash = content_hash(candidate.read_bytes())[:12]
            target = vault_path / folders["assets"] / item_partition / f"{item_id}-{media_hash}-{candidate.name}"
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copy2(candidate, target)
            copied_media.append(target.relative_to(vault_path).as_posix())

    revision = 1
    if item_path.exists():
        previous_text = item_path.read_text(encoding="utf-8")
        previous, _ = _parse_frontmatter(previous_text)
        if previous.get("content_hash") == payload_hash:
            return {"status": "duplicate", "id": item_id, "path": str(item_path)}
        revision = int(previous.get("revision", 1)) + 1
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        revision_path = source_root / "revisions" / item_partition / item_id / f"{stamp}.md"
        revision_path.parent.mkdir(parents=True, exist_ok=True)
        revision_path.write_text(previous_text, encoding="utf-8")

    metadata = {
        "id": item_id,
        "source_type": source_type,
        "source_url": origin if source_url else None,
        "origin": None if source_url else origin,
        "author": author,
        "published_at": published_at,
        "captured_at": utc_now(),
        "connector": connector,
        "collection": (collection or "").strip() or None,
        "content_hash": payload_hash,
        "vault": vault_slug,
        "epistemic_status": epistemic_status,
        "revision": revision,
        "raw": raw_reference,
        "media": copied_media,
    }
    heading = title or (source_file.stem if source_file else origin)
    rendered = _frontmatter(metadata) + f"\n\n# {heading}\n\n{body.strip()}\n"
    item_path.write_text(rendered, encoding="utf-8")
    return {
        "status": "captured" if revision == 1 else "revised",
        "id": item_id,
        "revision": revision,
        "path": str(item_path),
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
