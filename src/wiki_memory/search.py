from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .config import MemoryError, load_registry, load_vault, root_runtime_dir, write_data


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


def query_memory(root: Path, query: str, limit: int = 10) -> dict[str, Any]:
    binary = _qmd_binary(root)
    if binary is None:
        return {"engine": "text-fallback", "results": _fallback_search(root, query, limit)}
    env, config_dir = _qmd_environment(root)
    if not (config_dir / "index.yml").is_file():
        return {
            "engine": "text-fallback",
            "warning": "QMD index is not configured for this memory; run `wiki-memory index`.",
            "results": _fallback_search(root, query, limit),
        }
    completed = subprocess.run(
        [str(binary), "query", "--json", "-n", str(limit), query],
        env=env,
        text=True,
        capture_output=True,
        timeout=600,
        check=False,
    )
    if completed.returncode != 0:
        return {
            "engine": "text-fallback",
            "warning": completed.stderr.strip() or completed.stdout.strip(),
            "results": _fallback_search(root, query, limit),
        }
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        payload = completed.stdout
    return {"engine": "qmd", "results": payload}
