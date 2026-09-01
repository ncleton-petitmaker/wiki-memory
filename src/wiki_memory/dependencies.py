from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from .config import platform_runtime_dir


PYTHON_URL = "https://www.python.org/downloads/"
NODE_URL = "https://nodejs.org/en/download"
OBSIDIAN_URL = "https://obsidian.md/download"
SYNCTHING_URL = "https://syncthing.net/downloads/"
DOCLING_URL = "https://github.com/docling-project/docling"
QMD_URL = "https://github.com/tobi/qmd"


@dataclass(frozen=True)
class Dependency:
    name: str
    installed: bool
    required: bool
    detail: str
    official_url: str
    install_command: list[str] | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _run_version(command: list[str]) -> str | None:
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return (completed.stdout or completed.stderr).strip().splitlines()[0]


def version_tuple(value: str) -> tuple[int, ...]:
    clean = value.strip().lstrip("v")
    parts: list[int] = []
    for part in clean.split("."):
        digits = "".join(char for char in part if char.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def portable_node_paths(runtime: Path | None = None) -> tuple[Path, Path]:
    base = (runtime or platform_runtime_dir()) / "node"
    if os.name == "nt":
        return base / "node.exe", base / "npm.cmd"
    return base / "bin" / "node", base / "bin" / "npm"


def find_node(runtime: Path | None = None) -> tuple[str | None, str | None, str | None]:
    portable_node, portable_npm = portable_node_paths(runtime)
    candidates = [(str(portable_node), str(portable_npm)), (shutil.which("node"), shutil.which("npm.cmd" if os.name == "nt" else "npm"))]
    for node, npm in candidates:
        if not node or not npm or not Path(node).is_file() or not Path(npm).is_file():
            continue
        version = _run_version([node, "--version"])
        if version and version_tuple(version) >= (22,):
            return node, npm, version
    return None, None, None


def find_obsidian(system: str | None = None, home: Path | None = None) -> str | None:
    system = system or platform.system()
    home = home or Path.home()
    if system == "Darwin":
        for candidate in (Path("/Applications/Obsidian.app"), home / "Applications/Obsidian.app"):
            if candidate.is_dir():
                return str(candidate)
    elif system == "Windows":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local"))
        for candidate in (local / "Obsidian/Obsidian.exe", home / "AppData/Local/Obsidian/Obsidian.exe"):
            if candidate.is_file():
                return str(candidate)
    else:
        binary = shutil.which("obsidian")
        if binary:
            return binary
        if shutil.which("flatpak"):
            completed = subprocess.run(
                ["flatpak", "info", "md.obsidian.Obsidian"], capture_output=True, text=True, check=False
            )
            if completed.returncode == 0:
                return "flatpak:md.obsidian.Obsidian"
        for candidate in (Path("/opt/Obsidian/obsidian"), home / ".local/bin/obsidian"):
            if candidate.exists():
                return str(candidate)
    return None


def find_syncthing() -> tuple[str | None, str | None]:
    binary = shutil.which("syncthing")
    if binary:
        return binary, _run_version([binary, "--version"])
    if platform.system() == "Darwin":
        app_binary = Path("/Applications/Syncthing.app/Contents/Resources/syncthing/syncthing")
        if app_binary.is_file():
            return str(app_binary), _run_version([str(app_binary), "--version"])
    return None, None


def app_install_command(name: str, system: str | None = None, which: Callable[[str], str | None] = shutil.which) -> list[str] | None:
    system = system or platform.system()
    if system == "Darwin" and which("brew"):
        return [which("brew") or "brew", "install", "--cask", "obsidian"] if name == "obsidian" else [which("brew") or "brew", "install", "syncthing"]
    if system == "Windows" and which("winget"):
        package = "Obsidian.Obsidian" if name == "obsidian" else "Syncthing.Syncthing"
        return [which("winget") or "winget", "install", "--id", package, "--exact", "--accept-package-agreements", "--accept-source-agreements"]
    if system == "Linux":
        if name == "obsidian" and which("flatpak"):
            return [which("flatpak") or "flatpak", "install", "--user", "-y", "flathub", "md.obsidian.Obsidian"]
        if name == "syncthing":
            if which("apt-get"):
                prefix = [] if hasattr(os, "geteuid") and os.geteuid() == 0 else ([which("sudo") or "sudo"] if which("sudo") else [])
                return prefix + [which("apt-get") or "apt-get", "install", "-y", "syncthing"]
            if which("dnf"):
                prefix = [] if hasattr(os, "geteuid") and os.geteuid() == 0 else ([which("sudo") or "sudo"] if which("sudo") else [])
                return prefix + [which("dnf") or "dnf", "install", "-y", "syncthing"]
            if which("pacman"):
                prefix = [] if hasattr(os, "geteuid") and os.geteuid() == 0 else ([which("sudo") or "sudo"] if which("sudo") else [])
                return prefix + [which("pacman") or "pacman", "-S", "--noconfirm", "syncthing"]
    return None


def dependency_report(runtime: Path | None = None) -> list[Dependency]:
    runtime = runtime or platform_runtime_dir()
    node, npm, node_version = find_node(runtime)
    obsidian = find_obsidian()
    syncthing, syncthing_version = find_syncthing()
    python_ok = sys.version_info >= (3, 10)
    venv_python = runtime / ("venv/Scripts/python.exe" if os.name == "nt" else "venv/bin/python")
    docling_ok = bool(venv_python.is_file() and _run_version([str(venv_python), "-c", "import docling; print('installed')"]))
    qmd = runtime / "qmd/node_modules/.bin" / ("qmd.cmd" if os.name == "nt" else "qmd")
    return [
        Dependency(
            "python",
            python_ok,
            True,
            platform.python_version() if python_ok else "Python 3.10 or newer is required before bootstrap can run.",
            PYTHON_URL,
        ),
        Dependency(
            "node",
            bool(node and npm),
            True,
            f"{node_version} ({node})" if node else "Node.js 22+ will be installed in Wiki Memory's isolated runtime.",
            NODE_URL,
        ),
        Dependency(
            "obsidian",
            bool(obsidian),
            True,
            obsidian or "Obsidian is not installed.",
            OBSIDIAN_URL,
            app_install_command("obsidian"),
        ),
        Dependency(
            "syncthing",
            bool(syncthing),
            False,
            f"{syncthing_version or 'installed'} ({syncthing})" if syncthing else "Syncthing is not installed.",
            SYNCTHING_URL,
            app_install_command("syncthing"),
        ),
        Dependency(
            "docling",
            docling_ok,
            True,
            str(venv_python) if docling_ok else "Docling is missing from the isolated Wiki Memory runtime.",
            DOCLING_URL,
        ),
        Dependency(
            "qmd",
            qmd.is_file(),
            True,
            str(qmd) if qmd.is_file() else "QMD is missing from the isolated Wiki Memory runtime.",
            QMD_URL,
        ),
    ]
