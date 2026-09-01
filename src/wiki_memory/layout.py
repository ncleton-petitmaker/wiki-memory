from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import (
    CONFIG_NAME,
    REGISTRY_NAME,
    SCHEMA_VERSION,
    VAULT_CONFIG_NAME,
    MemoryError,
    load_data,
    load_registry,
    safe_child,
    slugify,
    utc_now,
    write_data,
)


FOLDER_PRESETS = {
    "fr": {
        "inbox": "00-Inbox",
        "sources": "01-Sources",
        "wiki": "02-Wiki",
        "outputs": "03-Synthèses",
        "journal": "04-Journal",
        "meta": "05-Meta",
        "assets": "assets",
    },
    "en": {
        "inbox": "00-Inbox",
        "sources": "01-Sources",
        "wiki": "02-Wiki",
        "outputs": "03-Outputs",
        "journal": "04-Journal",
        "meta": "05-Meta",
        "assets": "assets",
    },
}


GITIGNORE = """# Local-only and reproducible runtime state
.DS_Store
.env
.env.*
*.log
*.tmp
*.sqlite
*.sqlite3
__pycache__/
.venv/
venv/
node_modules/
.cache/
.qmd/
.wiki-memory-runtime/
browser-profile/
auth-state/
cookies/
**/.obsidian/workspace.json
**/.obsidian/workspace-mobile.json
"""


STIGNORE = """// Wiki Memory local-only state
.stfolder
.stversions
.git
.git/**
.env
.env.*
*.log
*.tmp
*.sqlite
*.sqlite3
__pycache__
__pycache__/**
.venv
.venv/**
venv
venv/**
node_modules
node_modules/**
.cache
.cache/**
.qmd
.qmd/**
.wiki-memory-runtime
.wiki-memory-runtime/**
browser-profile
browser-profile/**
auth-state
auth-state/**
cookies
cookies/**
.obsidian/workspace.json
.obsidian/workspace-mobile.json
*/.obsidian/workspace.json
*/.obsidian/workspace-mobile.json
"""


def _agents_markdown(language: str) -> str:
    if language.startswith("fr"):
        return """# Wiki Memory

Cette racine contient plusieurs vaults Markdown indépendants. Avant de classer une information, lire `memory.config.yaml`, `vaults.registry.yaml` et le `vault.yaml` de chaque candidat.

Règles :

1. Conserver les sources originales et leur provenance dans la couche Sources.
2. Ne jamais inventer un fait absent des sources. Distinguer `fact`, `inference`, `open_question` et `unverified`.
3. Enrichir la couche Wiki sans altérer silencieusement les sources immuables.
4. Utiliser des wikiliens relatifs et citer les fichiers sources dans les réponses.
5. Demander confirmation si plusieurs vaults conviennent.
6. Créer un nouveau vault seulement si l'objectif, l'audience, le cycle de vie ou la confidentialité exigent une frontière distincte.
7. Ne jamais stocker de cookies, jetons, profils navigateur, caches de modèles ou index QMD dans cette racine.
8. Pour tout nouveau fait, conserver ses deux temps : quand il est vrai et quand la mémoire l'a appris. Ne jamais inventer une date manquante.
9. Quand un fait change, conserver l'ancien, créer le nouveau et relier les deux avec `supersedes` et `superseded_by`.
"""
    return """# Wiki Memory

This root contains multiple independent Markdown vaults. Before routing information, read `memory.config.yaml`, `vaults.registry.yaml`, and each candidate's `vault.yaml`.

Rules:

1. Preserve original sources and provenance in the Sources layer.
2. Never invent facts absent from sources. Distinguish `fact`, `inference`, `open_question`, and `unverified`.
3. Refine the Wiki layer without silently changing immutable sources.
4. Use relative wikilinks and cite source files in answers.
5. Ask for confirmation when multiple vaults fit.
6. Create a vault only when purpose, audience, lifecycle, or confidentiality requires a separate boundary.
7. Never store cookies, tokens, browser profiles, model caches, or QMD indexes in this root.
8. For every new fact, preserve both timelines: when it is true and when the memory learned it. Never invent a missing date.
9. When a fact changes, preserve the old fact, create the new one, and connect them with `supersedes` and `superseded_by`.
"""


def _wiki_markdown(language: str) -> str:
    if language.startswith("fr"):
        return """# Architecture Wiki

La mémoire sépare les sources immuables du wiki vivant. Le flux normal est : capturer une source, la convertir sans perdre l'original, relier ses idées au wiki, produire des synthèses, puis contrôler les lacunes, contradictions et orphelins.

Les rôles `inbox`, `sources`, `wiki`, `outputs`, `journal`, `meta` et `assets` sont résolus via chaque `vault.yaml`. Les noms de dossiers peuvent donc changer sans casser les outils.

Toute affirmation doit rester traçable vers une source. Les interprétations sont distinguées des faits, et les questions ouvertes restent explicites.

```text
Source conservée ---> Fait sourcé et daté ---> Synthèse vérifiable
                           |
                           +-- vrai quand ?
                           +-- appris quand ?

Ancien fait conservé ---> Nouveau fait courant
```

Un fait remplacé n'est pas effacé. Il indique jusqu'à quand il était vrai, quand la mémoire a appris son remplacement et quel nouveau fait lui succède. Si une date manque, elle reste une question ouverte : la mémoire ne la devine pas.
"""
    return """# Wiki architecture

The memory separates immutable sources from a living Wiki. The normal flow is: capture a source, convert it without losing the original, connect its ideas to the Wiki, create outputs, then review gaps, contradictions, and orphans.

The `inbox`, `sources`, `wiki`, `outputs`, `journal`, `meta`, and `assets` roles are resolved through each `vault.yaml`, so folder names may change without breaking tools.

Every claim remains traceable to a source. Interpretations are distinguished from facts, and open questions remain explicit.

```text
Preserved source ---> Sourced, dated fact ---> Verifiable synthesis
                           |
                           +-- true when?
                           +-- learned when?

Preserved old fact ---> Current new fact
```

A superseded fact is not erased. It records when it stopped being true, when the memory learned about the change, and which fact replaced it. A missing date remains an open question; the memory never guesses it.
"""


def init_memory(root: Path, spec: dict[str, Any]) -> dict[str, Any]:
    root = root.expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        raise MemoryError(f"Target directory is not empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    language = str(spec.get("language") or "en").lower()
    sync_enabled = bool(spec.get("sync_enabled", False))
    now = utc_now()
    config = {
        "schema_version": SCHEMA_VERSION,
        "name": spec.get("name") or "Wiki Memory",
        "language": language,
        "created_at": now,
        "vault_registry": REGISTRY_NAME,
        "routing": {
            "ask_on_ambiguity": True,
            "new_vault_boundaries": ["purpose", "audience", "lifecycle", "confidentiality"],
            "client_isolation": bool(spec.get("client_isolation", False)),
        },
        "connectors": spec.get("connectors", {}),
        "schedules": spec.get("schedules", {}),
        "sync": {
            "enabled": sync_enabled,
            "provider": "syncthing" if sync_enabled else None,
            "include_content_and_media": True,
            "ignore_template": "syncthing.ignore.template" if sync_enabled else None,
            "versioning_confirmed": bool(spec.get("versioning_confirmed", False)),
        },
        "runtime": {"storage": "os-user-data-directory", "inside_memory_root": False},
    }
    registry = {"schema_version": SCHEMA_VERSION, "updated_at": now, "vaults": []}
    write_data(root / CONFIG_NAME, config)
    write_data(root / REGISTRY_NAME, registry)
    (root / "AGENTS.md").write_text(_agents_markdown(language), encoding="utf-8")
    (root / "WIKI.md").write_text(_wiki_markdown(language), encoding="utf-8")
    (root / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    if sync_enabled:
        (root / ".stignore").write_text(STIGNORE, encoding="utf-8")
        (root / "syncthing.ignore.template").write_text(STIGNORE, encoding="utf-8")
    (root / "README.md").write_text(
        "# " + str(config["name"]) + "\n\nOpen each registered vault independently in Obsidian.\n",
        encoding="utf-8",
    )
    created = []
    for vault_spec in spec.get("vaults", []):
        created.append(create_vault(root, vault_spec))
    return {"root": str(root), "vaults": created, "config": config}


def create_vault(root: Path, spec: dict[str, Any]) -> dict[str, Any]:
    root = root.resolve()
    config = load_data(root / CONFIG_NAME)
    registry = load_registry(root)
    title = str(spec.get("title") or spec.get("name") or "Memory")
    slug = slugify(str(spec.get("slug") or title))
    if any(item.get("slug") == slug for item in registry.get("vaults", [])):
        raise MemoryError(f"Vault already exists: {slug}")
    vault_path = safe_child(root, slug)
    if vault_path.exists():
        raise MemoryError(f"Path already exists: {vault_path}")
    language = str(spec.get("language") or config.get("language") or "en").lower()
    folders = dict(FOLDER_PRESETS.get(language.split("-")[0], FOLDER_PRESETS["en"]))
    folders.update(spec.get("folders") or {})
    if len(set(folders.values())) != len(folders):
        raise MemoryError("Vault folder names must be unique.")
    for relative in folders.values():
        safe_child(vault_path, relative).mkdir(parents=True, exist_ok=True)
    source_root = vault_path / folders["sources"]
    for child in ("raw", "items", "revisions"):
        (source_root / child).mkdir(parents=True, exist_ok=True)
    (vault_path / ".obsidian").mkdir(parents=True, exist_ok=True)
    (vault_path / ".obsidian" / ".keep").write_text("", encoding="utf-8")
    for filename in ("gaps.md", "contradictions.md", "orphans.md"):
        (vault_path / folders["meta"] / filename).write_text(f"# {filename[:-3].title()}\n", encoding="utf-8")
    vault_config = {
        "schema_version": SCHEMA_VERSION,
        "slug": slug,
        "title": title,
        "language": language,
        "purpose": spec.get("purpose") or "General knowledge",
        "audience": spec.get("audience") or ["owner"],
        "confidentiality": spec.get("confidentiality") or "private",
        "lifecycle": spec.get("lifecycle") or "ongoing",
        "routing": {
            "include": spec.get("routing", {}).get("include", []),
            "exclude": spec.get("routing", {}).get("exclude", []),
            "keywords": spec.get("routing", {}).get("keywords", []),
        },
        "taxonomy": spec.get("taxonomy") or {"categories": [], "tags": []},
        "deliverables": spec.get("deliverables") or [],
        "folders": folders,
        "created_at": utc_now(),
    }
    write_data(vault_path / VAULT_CONFIG_NAME, vault_config)
    (vault_path / "AGENTS.md").write_text(
        f"# {title}\n\nPurpose: {vault_config['purpose']}\n\nFollow the root AGENTS.md and this vault's `vault.yaml`.\n",
        encoding="utf-8",
    )
    (vault_path / "Index.md").write_text(f"# {title}\n\n## Wiki\n\n## Sources\n", encoding="utf-8")
    entry = {
        "slug": slug,
        "title": title,
        "path": slug,
        "purpose": vault_config["purpose"],
        "audience": vault_config["audience"],
        "confidentiality": vault_config["confidentiality"],
        "lifecycle": vault_config["lifecycle"],
        "created_at": vault_config["created_at"],
    }
    registry.setdefault("vaults", []).append(entry)
    registry["updated_at"] = utc_now()
    write_data(root / REGISTRY_NAME, registry)
    return entry
