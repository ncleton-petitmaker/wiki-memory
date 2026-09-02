# Obsidian and optional Syncthing

The onboarding dependency gate requires Obsidian but only reports Syncthing as an optional component. A local Wiki Memory works without Syncthing. Run the core gate with:

```bash
python3 scripts/bootstrap.py --check
python3 scripts/bootstrap.py --yes --open-links
```

Only after the user chooses multi-device synchronization, install Syncthing with:

```bash
python3 scripts/bootstrap.py --yes --with-syncthing --open-links
```

On Windows, use `py -3` in place of `python3`.

Supported automatic paths are Homebrew on macOS, WinGet on Windows, Flatpak for Obsidian on Linux, and common Linux package managers for Syncthing. If an explicitly selected installation route is unavailable, bootstrap opens the corresponding official [Obsidian download](https://obsidian.md/download) or [Syncthing download](https://syncthing.net/downloads/) page and stops with `needs-user`; it never downloads an installer from an unofficial mirror.

Open each registered vault directory independently in Obsidian. Wiki Memory uses standard Markdown, YAML frontmatter, relative paths, and wikilinks; it does not require filesystem symlinks.

The durable installation root contains sibling `Agent/` and `Mémoire/` directories. When synchronization is enabled, Wiki Memory registers `Agent/` and `Mémoire/.wiki-memory/data/` as separate Syncthing folders. The latter transports immutable blobs and event packs only; its ignore file excludes the live SQLite ledger and device outbox. Markdown is rebuilt independently on each device.

```bash
wiki-memory syncthing-setup /path/to/installation/Mémoire
wiki-memory syncthing-setup /path/to/installation/Mémoire --device-id OTHER-DEVICE-ID --device-name "Other device"
```

The second device accepts the agent folder and maps the transport folder into an initialized memory's `.wiki-memory/data/`. Import arriving packs only after their blobs are present, then rebuild projections. Do not claim setup complete until both transport folders are up to date and an import/verify succeeds.

Syncthing does not synchronize `.stignore` itself. On every device:

1. preserve the generated ignore files in `Agent/`, `Mémoire/`, and `.wiki-memory/data/`;
2. preserve any device-specific additions;
3. run `wiki-memory doctor <installation-root>/Mémoire`;
4. enable Syncthing file versioning on at least one device or maintain a separate backup.

The generated rules exclude browser/auth state, secrets, logs, caches, environments, packages, QMD state, local Obsidian workspace files, SQLite, and outbox state. Never point Syncthing at `events.sqlite3` directly.
