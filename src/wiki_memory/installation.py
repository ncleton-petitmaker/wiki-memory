from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .config import MemoryError


AGENT_DIRECTORY = "Agent"
MEMORY_DIRECTORY = "Mémoire"

_IGNORED_NAMES = {
    ".DS_Store",
    ".cache",
    ".git",
    ".qmd",
    ".stfolder",
    ".stversions",
    ".venv",
    ".wiki-memory-runtime",
    "__pycache__",
    "auth-state",
    "browser-profile",
    "cookies",
    "credentials",
    "node_modules",
    "secrets",
    "venv",
}
_IGNORED_SUFFIXES = {".key", ".log", ".pem", ".sqlite", ".sqlite3", ".tmp"}


def installation_paths(installation_root: Path) -> tuple[Path, Path]:
    root = installation_root.expanduser().resolve()
    return root / AGENT_DIRECTORY, root / MEMORY_DIRECTORY


def validate_installation_layout(memory_root: Path, agent_root: Path | None = None) -> tuple[Path, Path, Path]:
    memory = memory_root.expanduser().resolve()
    expected_agent, expected_memory = installation_paths(memory.parent)
    agent = agent_root.expanduser().resolve() if agent_root else expected_agent
    if memory != expected_memory:
        raise MemoryError(f"The memory folder must be the installation root's '{MEMORY_DIRECTORY}' directory: {expected_memory}")
    if agent != expected_agent:
        raise MemoryError(f"The agent folder must be the memory folder's sibling '{AGENT_DIRECTORY}' directory: {expected_agent}")
    manifest = agent / ".codex-plugin" / "plugin.json"
    if not manifest.is_file():
        raise MemoryError(f"Wiki Memory agent files are missing from: {agent}")
    return memory.parent, agent, memory


def _ignore_agent_copy(_: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        if name in _IGNORED_NAMES or name.startswith(".env") or Path(name).suffix in _IGNORED_SUFFIXES:
            ignored.add(name)
    return ignored


def prepare_installation(installation_root: Path, agent_source: Path) -> dict[str, Any]:
    root = installation_root.expanduser().resolve()
    source = agent_source.expanduser().resolve()
    source_manifest = source / ".codex-plugin" / "plugin.json"
    if not source_manifest.is_file():
        raise MemoryError(f"The agent source is not a Wiki Memory plugin directory: {source}")
    if source != root / AGENT_DIRECTORY:
        try:
            root.relative_to(source)
        except ValueError:
            pass
        else:
            raise MemoryError("The installation root cannot be located inside the agent source directory.")
    if root.exists() and not root.is_dir():
        raise MemoryError(f"Installation root is not a directory: {root}")

    agent, memory = installation_paths(root)
    root.mkdir(parents=True, exist_ok=True)
    copied = False
    if source != agent:
        if agent.exists() and any(agent.iterdir()):
            if not (agent / ".codex-plugin" / "plugin.json").is_file():
                raise MemoryError(f"Agent target is not empty and does not contain Wiki Memory: {agent}")
        else:
            shutil.copytree(source, agent, dirs_exist_ok=True, ignore=_ignore_agent_copy)
            copied = True
    memory.mkdir(parents=True, exist_ok=True)
    validate_installation_layout(memory, agent)
    return {
        "ok": True,
        "installation_root": str(root),
        "agent": str(agent),
        "memory": str(memory),
        "agent_copied": copied,
    }
