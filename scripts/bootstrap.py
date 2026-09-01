#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import webbrowser
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wiki_memory.config import platform_runtime_dir  # noqa: E402
from wiki_memory.dependencies import (  # noqa: E402
    app_install_command,
    dependency_report,
    find_node,
    find_obsidian,
    find_syncthing,
    portable_node_paths,
)


NODE_RELEASE = "latest-v24.x"


def run(command: list[str], *, dry_run: bool) -> None:
    print("+ " + " ".join(command))
    if dry_run:
        return
    subprocess.run(command, check=True)


def _node_archive_name(checksums: str) -> str:
    machine = platform.machine().lower()
    arch = "arm64" if machine in {"arm64", "aarch64"} else "x64"
    system = platform.system()
    suffix = {"Windows": f"win-{arch}.zip", "Darwin": f"darwin-{arch}.tar.xz", "Linux": f"linux-{arch}.tar.xz"}.get(system)
    if not suffix:
        raise RuntimeError(f"Portable Node.js is not supported automatically on {system}.")
    matches = [line.split()[1] for line in checksums.splitlines() if line.split() and line.split()[1].endswith(suffix)]
    if not matches:
        raise RuntimeError(f"No Node.js archive found for {system} {arch}.")
    return matches[0]


def install_portable_node(runtime: Path, *, dry_run: bool) -> tuple[str, str]:
    node_path, npm_path = portable_node_paths(runtime)
    if dry_run:
        print(f"+ download and verify Node.js {NODE_RELEASE} into {runtime / 'node'}")
        return str(node_path), str(npm_path)
    base_url = f"https://nodejs.org/dist/{NODE_RELEASE}"
    with urllib.request.urlopen(f"{base_url}/SHASUMS256.txt", timeout=60) as response:
        checksums = response.read().decode("utf-8")
    archive_name = _node_archive_name(checksums)
    expected = next(line.split()[0] for line in checksums.splitlines() if line.split()[1] == archive_name)
    with tempfile.TemporaryDirectory() as temp_name:
        archive = Path(temp_name) / archive_name
        print(f"+ download {base_url}/{archive_name}")
        with urllib.request.urlopen(f"{base_url}/{archive_name}", timeout=180) as response, archive.open("wb") as target:
            shutil.copyfileobj(response, target)
        actual = hashlib.sha256(archive.read_bytes()).hexdigest()
        if actual != expected:
            raise RuntimeError("Node.js archive checksum mismatch; installation stopped.")
        extracted = Path(temp_name) / "extracted"
        extracted.mkdir()
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(extracted)
        else:
            with tarfile.open(archive, "r:xz") as bundle:
                # The archive is fetched over HTTPS and verified against Node.js' published SHA-256 list.
                bundle.extractall(extracted)
        source = next(extracted.iterdir())
        destination = runtime / "node"
        if destination.exists():
            shutil.rmtree(destination)
        shutil.move(str(source), destination)
    return str(node_path), str(npm_path)


def install_apps(*, dry_run: bool, open_links: bool, include_syncthing: bool = False) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    applications = [("obsidian", lambda: (find_obsidian(), None), "https://obsidian.md/download")]
    if include_syncthing:
        applications.append(("syncthing", find_syncthing, "https://syncthing.net/downloads/"))
    for name, finder, url in applications:
        location, _ = finder()
        if location:
            results.append({"name": name, "status": "already-installed", "detail": location, "official_url": url})
            continue
        command = app_install_command(name)
        if command:
            try:
                run(command, dry_run=dry_run)
                status = "planned" if dry_run else "installed"
                results.append({"name": name, "status": status, "detail": " ".join(command), "official_url": url})
                continue
            except subprocess.CalledProcessError as exc:
                results.append({"name": name, "status": "needs-user", "detail": f"automatic installer failed ({exc.returncode})", "official_url": url})
        else:
            results.append({"name": name, "status": "needs-user", "detail": "no supported package manager found", "official_url": url})
        if open_links and not dry_run:
            webbrowser.open(url)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Check and install Wiki Memory, Docling, QMD, and Obsidian")
    parser.add_argument("--check", action="store_true", help="Only report dependency status; change nothing")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--yes", action="store_true", help="Authorize supported system package managers to install selected applications")
    parser.add_argument("--skip-apps", action="store_true", help="Do not require or install Obsidian")
    parser.add_argument("--with-syncthing", action="store_true", help="Install optional Syncthing after the user chooses multi-device synchronization")
    parser.add_argument("--open-links", action="store_true", help="Open official download pages when automatic installation is unavailable")
    args = parser.parse_args()
    if args.skip_apps and args.with_syncthing:
        parser.error("--skip-apps and --with-syncthing cannot be used together")
    if sys.version_info < (3, 10):
        print(json.dumps({"ok": False, "dependencies": [item.to_dict() for item in dependency_report()]}, indent=2))
        raise SystemExit(2)

    runtime = platform_runtime_dir()
    if args.check:
        dependencies = dependency_report(runtime)
        required = [item for item in dependencies if item.required and (not args.skip_apps or item.name != "obsidian")]
        payload = {"ok": all(item.installed for item in required), "runtime": str(runtime), "dependencies": [item.to_dict() for item in dependencies]}
        print(json.dumps(payload, indent=2))
        raise SystemExit(0 if payload["ok"] else 1)

    node, npm, node_version = find_node(runtime)
    if not node or not npm:
        node, npm = install_portable_node(runtime, dry_run=args.dry_run)
        node_version = "planned portable Node.js" if args.dry_run else subprocess.check_output([node, "--version"], text=True).strip()

    venv = runtime / "venv"
    qmd = runtime / "qmd"
    runtime.mkdir(parents=True, exist_ok=True)
    run([sys.executable, "-m", "venv", str(venv)], dry_run=args.dry_run)
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    run([str(python), "-m", "pip", "install", "--upgrade", "pip"], dry_run=args.dry_run)
    run([str(python), "-m", "pip", "install", str(ROOT), "docling"], dry_run=args.dry_run)
    run([npm, "install", "--prefix", str(qmd), "@tobilu/qmd"], dry_run=args.dry_run)
    app_results: list[dict[str, object]] = []
    if not args.skip_apps:
        if args.yes or args.dry_run:
            app_results = install_apps(
                dry_run=args.dry_run,
                open_links=args.open_links,
                include_syncthing=args.with_syncthing,
            )
        else:
            selected_apps = {"obsidian"}
            if args.with_syncthing:
                selected_apps.add("syncthing")
            app_results = [
                {
                    "name": item.name,
                    "status": "already-installed" if item.installed else "needs-authorization",
                    "detail": item.detail,
                    "official_url": item.official_url,
                    "install_command": item.install_command,
                }
                for item in dependency_report(runtime)
                if item.name in selected_apps
            ]
    metadata = {
        "ok": True,
        "plugin_root": str(ROOT),
        "python": str(python),
        "cli": str(venv / ("Scripts/wiki-memory.exe" if os.name == "nt" else "bin/wiki-memory")),
        "qmd": str(qmd / "node_modules" / ".bin" / ("qmd.cmd" if os.name == "nt" else "qmd")),
        "node_version": node_version,
        "applications": app_results,
    }
    if not args.dry_run:
        dependencies = dependency_report(runtime)
        required = [item for item in dependencies if item.required and (not args.skip_apps or item.name != "obsidian")]
        metadata["dependencies"] = [item.to_dict() for item in dependencies]
        metadata["ok"] = all(item.installed for item in required)
        (runtime / "runtime.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metadata, indent=2))
    missing_apps = [item for item in app_results if item["status"] in {"needs-user", "needs-authorization"}]
    if missing_apps and not args.dry_run:
        print("A selected application still needs attention. Re-run with --yes --open-links.", file=sys.stderr)
        raise SystemExit(1)
    if not metadata["ok"] and not args.dry_run:
        print("One or more required dependencies failed verification. Review the official links above.", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
