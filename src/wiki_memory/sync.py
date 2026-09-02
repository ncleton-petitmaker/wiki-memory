from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Callable

from .config import MemoryError, load_memory, slugify, write_data
from .dependencies import find_syncthing
from .installation import validate_installation_layout
from .layout import STIGNORE


DEVICE_ID = re.compile(r"^[A-Z2-7]{7}(?:-[A-Z2-7]{7}){7}$")


def syncthing_folder_id(root: Path, name: str) -> str:
    fingerprint = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:8]
    return f"wiki-memory-{slugify(name)[:32]}-{fingerprint}"


def _run(
    command: list[str],
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> subprocess.CompletedProcess[str]:
    try:
        return runner(command, capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", None) or str(exc)
        raise MemoryError(f"Syncthing command failed. Ensure the Syncthing application or service is running: {detail.strip()}") from exc


def _keys(binary: str, parts: list[str], runner: Callable[..., subprocess.CompletedProcess[str]]) -> set[str]:
    completed = _run([binary, "cli", "config", *parts, "list"], runner)
    return {line.strip() for line in completed.stdout.splitlines() if line.strip()}


def _ensure_folder(
    binary: str,
    folder_id: str,
    label: str,
    path: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    if folder_id not in _keys(binary, ["folders"], runner):
        _run(
            [
                binary,
                "cli",
                "config",
                "folders",
                "add",
                "--id",
                folder_id,
                "--label",
                label,
                "--path",
                str(path),
                "--type",
                "sendreceive",
            ],
            runner,
        )


def _verify_folder(
    binary: str,
    folder_id: str,
    expected_path: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    try:
        folder = json.loads(_run([binary, "cli", "config", "folders", folder_id, "dump-json"], runner).stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise MemoryError("Syncthing returned an invalid folder configuration.") from exc
    configured_path = Path(str(folder.get("path", ""))).expanduser().resolve()
    if configured_path != expected_path:
        raise MemoryError(f"Syncthing folder {folder_id} points to a different path: {configured_path}")


def configure_syncthing(
    root: Path,
    *,
    agent_root: Path | None = None,
    remote_device_id: str | None = None,
    remote_device_name: str | None = None,
    syncthing_binary: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, object]:
    root = root.resolve()
    _, agent, memory = validate_installation_layout(root, agent_root)
    config = load_memory(root)
    sync = dict(config.get("sync") or {})
    if not sync.get("enabled") or sync.get("provider") != "syncthing":
        raise MemoryError("Syncthing setup requires the user's explicit choice to enable multi-device synchronization.")

    binary = syncthing_binary
    if binary is None:
        binary, _ = find_syncthing()
    if not binary:
        raise MemoryError("Syncthing is not installed. Install it only after the user enables multi-device synchronization.")

    for folder in (agent, memory):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "syncthing.ignore.template").write_text(STIGNORE, encoding="utf-8")
        ignore_path = folder / ".stignore"
        if not ignore_path.exists():
            ignore_path.write_text(STIGNORE, encoding="utf-8")

    transport = memory / ".wiki-memory" / "data"
    transport.mkdir(parents=True, exist_ok=True)
    transport_ignore = """// Only immutable transport artifacts cross devices
events.sqlite3
events.sqlite3-*
outbox
outbox/**
projections
projections/**
"""
    (transport / ".stignore").write_text(transport_ignore, encoding="utf-8")

    name = str(config.get("name") or "Wiki Memory")
    folders = {
        "agent": {
            "id": syncthing_folder_id(agent, "agent"),
            "label": "Wiki Memory — Agent",
            "path": agent,
        },
        "memory": {
            "id": syncthing_folder_id(transport, name),
            "label": f"Wiki Memory — Transport — {name}",
            "path": transport,
        },
    }
    for item in folders.values():
        _ensure_folder(binary, str(item["id"]), str(item["label"]), Path(item["path"]), runner)

    device_id = remote_device_id.upper() if remote_device_id else None
    if device_id:
        if not DEVICE_ID.fullmatch(device_id):
            raise MemoryError("Invalid Syncthing device ID.")
        device_ids = _keys(binary, ["devices"], runner)
        if device_id not in device_ids:
            command = [binary, "cli", "config", "devices", "add", "--device-id", device_id]
            if remote_device_name:
                command.extend(["--name", remote_device_name])
            _run(command, runner)
        for item in folders.values():
            folder_id = str(item["id"])
            shared_devices = _keys(binary, ["folders", folder_id, "devices"], runner)
            if device_id not in shared_devices:
                _run(
                    [
                        binary,
                        "cli",
                        "config",
                        "folders",
                        folder_id,
                        "devices",
                        "add",
                        "--device-id",
                        device_id,
                    ],
                    runner,
                )

    for item in folders.values():
        _verify_folder(binary, str(item["id"]), Path(item["path"]), runner)

    sync.update(
        {
            "enabled": True,
            "provider": "syncthing",
            "folder_id": str(folders["memory"]["id"]),
            "folder_ids": {key: str(value["id"]) for key, value in folders.items()},
            "configured_on_this_device": True,
            "ignore_template": "syncthing.ignore.template",
        }
    )
    config["sync"] = sync
    write_data(root / "memory.config.yaml", config)
    return {
        "ok": True,
        "folder_id": str(folders["memory"]["id"]),
        "path": str(transport),
        "folders": {
            key: {"id": str(value["id"]), "path": str(value["path"])} for key, value in folders.items()
        },
        "remote_device_configured": bool(device_id),
        "next_step": "Accept both shared folders on the other device." if device_id else "Add the other device before sharing both folders.",
    }
