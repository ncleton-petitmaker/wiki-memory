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

The durable installation root contains sibling `Agent/` and `Mémoire/` directories. When synchronization is enabled, Wiki Memory registers both as separate Syncthing folders. `Mémoire/` includes raw sources, normalized source notes, media, Wiki notes, outputs, journals, and configuration. `Agent/` includes the public agent files. Runtime state stays outside both:

```bash
wiki-memory syncthing-setup /path/to/installation/Mémoire
wiki-memory syncthing-setup /path/to/installation/Mémoire --device-id OTHER-DEVICE-ID --device-name "Other device"
```

The second device must accept both shared folders and select sibling local destinations named `Agent/` and `Mémoire/`. Do not claim setup is complete until both devices show both folders as connected or up to date.

Syncthing does not synchronize `.stignore` itself. On every device:

1. in both `Agent/` and `Mémoire/`, copy `syncthing.ignore.template` to `.stignore`;
2. preserve any device-specific additions;
3. run `wiki-memory doctor <installation-root>/Mémoire`;
4. enable Syncthing file versioning on at least one device or maintain a separate backup.

The generated ignore rules exist only for sync-enabled memories and exclude browser profiles, authentication state, cookies, environment variables, logs, caches, Python environments, Node packages, SQLite indexes, QMD state, and local Obsidian workspace files.
