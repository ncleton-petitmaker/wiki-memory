from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import MemoryError, load_registry, load_vault, root_runtime_dir, write_data
from .temporal import iter_temporal_notes, parse_temporal_date, source_links, temporal_decision


def _qmd_binary(root: Path) -> Path | None:
    runtime = root_runtime_dir(root)
    binary = runtime.parents[1] / "qmd" / "node_modules" / ".bin" / ("qmd.cmd" if os.name == "nt" else "qmd")
    if binary.is_file():
        return binary
    found = shutil.which("qmd")
    return Path(found) if found else None


def _qmd_environment(root: Path) -> tuple[dict[str, str], Path]:
    runtime = root_runtime_dir(root)
    config_dir = runtime / "qmd-config"
    cache_dir = runtime / "qmd-cache"
    config_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["QMD_CONFIG_DIR"] = str(config_dir)
    env["XDG_CACHE_HOME"] = str(cache_dir)
    return env, config_dir


def configure_index(root: Path, *, embed: bool = True) -> dict[str, Any]:
    binary = _qmd_binary(root)
    if binary is None:
        raise MemoryError("QMD is missing. Run scripts/bootstrap.py first.")
    env, config_dir = _qmd_environment(root)
    collections = {}
    for entry in load_registry(root).get("vaults", []):
        vault_path, vault = load_vault(root, entry["slug"])
        collections[entry["slug"]] = {
            "path": str(vault_path),
            "pattern": "**/*.md",
            "context": {"/": str(vault.get("purpose", ""))},
        }
    write_data(config_dir / "index.yml", {"collections": collections})
    commands = [[str(binary), "update"]]
    if embed:
        commands.append([str(binary), "embed"])
    outputs = []
    for command in commands:
        completed = subprocess.run(command, env=env, text=True, capture_output=True, timeout=3600, check=False)
        if completed.returncode != 0:
            raise MemoryError(f"QMD failed: {completed.stderr.strip() or completed.stdout.strip()}")
        outputs.append(completed.stdout.strip())
    return {"collections": list(collections), "embedded": embed, "output": outputs}


def _fallback_search(root: Path, query: str, limit: int) -> list[dict[str, Any]]:
    terms = [term.lower() for term in re.findall(r"[\w-]+", query, flags=re.UNICODE) if len(term) > 2]
    results = []
    for path in root.rglob("*.md"):
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        lowered = text.lower()
        score = sum(lowered.count(term) for term in terms)
        if score:
            first = min((lowered.find(term) for term in terms if term in lowered), default=0)
            snippet = text[max(0, first - 120) : first + 360].replace("\n", " ").strip()
            results.append({"file": str(path.relative_to(root)), "score": score, "snippet": snippet})
    return sorted(results, key=lambda item: (-item["score"], item["file"]))[:limit]


def _natural_temporal_request(query: str) -> tuple[str, str | None]:
    lowered = query.lower().replace("’", "'")
    iso = re.search(r"\b\d{4}-\d{2}-\d{2}(?:[tT][0-9:.+-]+Z?)?\b", query)
    european = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", query)
    value: str | None = iso.group(0) if iso else None
    if value is None and european:
        value = f"{european.group(3)}-{int(european.group(2)):02d}-{int(european.group(1)):02d}"
    if value is None:
        return "current", None
    system_phrases = ("que savait la mémoire", "que savait la memoire", "what did the memory know")
    world_phrases = ("qu'était vrai", "qu'etait vrai", "what was true")
    if any(phrase in lowered for phrase in system_phrases):
        return "system", value
    if any(phrase in lowered for phrase in world_phrases):
        return "world", value
    return "current", None


def _temporal_mode(
    query: str,
    *,
    system_at: str | None,
    valid_at: str | None,
) -> tuple[str, datetime]:
    if system_at and valid_at:
        raise MemoryError("Use only one temporal axis: --system-at or --valid-at.")
    if system_at:
        mode, value = "system", system_at
    elif valid_at:
        mode, value = "world", valid_at
    else:
        mode, value = _natural_temporal_request(query)
    if mode == "current":
        return mode, datetime.now(timezone.utc)
    try:
        parsed = parse_temporal_date(value)
    except ValueError as exc:
        raise MemoryError(f"Invalid temporal query date: {value}") from exc
    if parsed is None:
        raise MemoryError("A temporal query needs an ISO 8601 date or timestamp.")
    return mode, parsed


def _result_path(result: dict[str, Any], root: Path) -> str | None:
    for key in ("file", "path", "document", "filename"):
        value = result.get(key)
        if not isinstance(value, str) or not value:
            continue
        candidate = value.replace("\\", "/")
        if candidate.startswith("qmd://"):
            candidate = candidate.split("/", 3)[-1]
        path = Path(candidate)
        if path.is_absolute():
            try:
                return path.resolve().relative_to(root.resolve()).as_posix()
            except ValueError:
                return None
        return candidate.lstrip("./")
    return None


def _payload_results(payload: Any) -> list[dict[str, Any]] | None:
    if isinstance(payload, list) and all(isinstance(item, dict) for item in payload):
        return payload
    if isinstance(payload, dict):
        for key in ("results", "matches", "documents"):
            value = payload.get(key)
            if isinstance(value, list) and all(isinstance(item, dict) for item in value):
                return value
    return None


def _filter_temporal_results(
    root: Path,
    results: list[dict[str, Any]],
    *,
    mode: str,
    at: datetime,
    limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from .capture import _parse_frontmatter

    notes = list(iter_temporal_notes(root))
    exact = {note.relative_path: note for note in notes}
    source_roots: list[Path] = []
    for entry in load_registry(root).get("vaults", []):
        vault_path, vault = load_vault(root, entry["slug"])
        source_roots.append((vault_path / vault["folders"]["sources"] / "items").resolve())
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for result in results:
        relative = _result_path(result, root)
        note = exact.get(relative or "")
        if note is None and relative:
            matches = [candidate for path, candidate in exact.items() if path.endswith("/" + relative)]
            note = matches[0] if len(matches) == 1 else None
        if note is None:
            enriched = dict(result)
            candidate = (root / relative).resolve() if relative else None
            is_source = False
            if candidate is not None and candidate.is_file():
                for source_root in source_roots:
                    try:
                        candidate.relative_to(source_root)
                        is_source = True
                        break
                    except ValueError:
                        continue
            if is_source and mode == "system" and candidate is not None:
                metadata, _ = _parse_frontmatter(candidate.read_text(encoding="utf-8", errors="replace"))
                try:
                    captured_at = parse_temporal_date(metadata.get("captured_at"))
                except ValueError:
                    captured_at = None
                if captured_at is None or captured_at > at:
                    continue
            enriched.setdefault("kind", "source" if is_source else "context")
            if is_source:
                enriched["evidence_only"] = True
            included.append(enriched)
            continue
        visible, reason = temporal_decision(note.metadata, mode, at)
        if not visible:
            excluded.append(
                {
                    "file": note.relative_path,
                    "reason": reason,
                    "valid_from": note.metadata.get("valid_from"),
                    "valid_until": note.metadata.get("valid_until"),
                    "recorded_at": note.metadata.get("recorded_at"),
                    "invalidated_at": note.metadata.get("invalidated_at"),
                }
            )
            continue
        enriched = dict(result)
        enriched["file"] = note.relative_path
        enriched["kind"] = note.kind
        enriched["temporal_status"] = reason or ("current" if mode == "current" else "active-at-date")
        enriched["sources"] = [path.relative_to(root).as_posix() for path in source_links(root, note)]
        included.append(enriched)
    return included[:limit], excluded


def query_memory(
    root: Path,
    query: str,
    limit: int = 10,
    *,
    system_at: str | None = None,
    valid_at: str | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    mode, at = _temporal_mode(query, system_at=system_at, valid_at=valid_at)
    candidate_limit = max(limit * 5, limit)
    binary = _qmd_binary(root)
    if binary is None:
        raw_results = _fallback_search(root, query, candidate_limit)
        results, excluded = _filter_temporal_results(root, raw_results, mode=mode, at=at, limit=limit)
        return {
            "engine": "text-fallback",
            "temporal": {"mode": mode, "as_of": at.replace(microsecond=0).isoformat().replace("+00:00", "Z")},
            "results": results,
            "excluded_stale_facts": excluded,
        }
    env, config_dir = _qmd_environment(root)
    if not (config_dir / "index.yml").is_file():
        raw_results = _fallback_search(root, query, candidate_limit)
        results, excluded = _filter_temporal_results(root, raw_results, mode=mode, at=at, limit=limit)
        return {
            "engine": "text-fallback",
            "warning": "QMD index is not configured for this memory; run `wiki-memory index`.",
            "temporal": {"mode": mode, "as_of": at.replace(microsecond=0).isoformat().replace("+00:00", "Z")},
            "results": results,
            "excluded_stale_facts": excluded,
        }
    completed = subprocess.run(
        [str(binary), "query", "--json", "-n", str(candidate_limit), query],
        env=env,
        text=True,
        capture_output=True,
        timeout=600,
        check=False,
    )
    if completed.returncode != 0:
        raw_results = _fallback_search(root, query, candidate_limit)
        results, excluded = _filter_temporal_results(root, raw_results, mode=mode, at=at, limit=limit)
        return {
            "engine": "text-fallback",
            "warning": completed.stderr.strip() or completed.stdout.strip(),
            "temporal": {"mode": mode, "as_of": at.replace(microsecond=0).isoformat().replace("+00:00", "Z")},
            "results": results,
            "excluded_stale_facts": excluded,
        }
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        payload = completed.stdout
    raw_results = _payload_results(payload)
    if raw_results is None:
        fallback = _fallback_search(root, query, candidate_limit)
        results, excluded = _filter_temporal_results(root, fallback, mode=mode, at=at, limit=limit)
        return {
            "engine": "text-fallback",
            "warning": "QMD returned an unknown JSON shape; used temporally filtered text search instead.",
            "temporal": {"mode": mode, "as_of": at.replace(microsecond=0).isoformat().replace("+00:00", "Z")},
            "results": results,
            "excluded_stale_facts": excluded,
        }
    results, excluded = _filter_temporal_results(root, raw_results, mode=mode, at=at, limit=limit)
    return {
        "engine": "qmd",
        "temporal": {"mode": mode, "as_of": at.replace(microsecond=0).isoformat().replace("+00:00", "Z")},
        "results": results,
        "excluded_stale_facts": excluded,
    }
