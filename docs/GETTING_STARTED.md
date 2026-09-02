# Getting started

This guide takes a new user from installation to a first source-grounded query.

## 1. Install the plugin

```bash
codex plugin marketplace add ncleton-petitmaker/wiki-memory
codex plugin add wiki-memory@petitmaker
```

Restart the ChatGPT desktop app or open Wiki Memory in a new Codex task. Plugin skills are loaded at task start.

## 2. Start onboarding

There is no command, skill name, or technical prompt to copy. Wiki Memory starts in French by asking:

> Veux-tu démarrer un échange pour que je comprenne mieux tes activités, que je puisse mieux t'aider et que nous structurions ta mémoire ensemble ?

If the user agrees, onboarding locates a Python 3.10+ launcher and runs the read-only dependency gate in the background. It checks:

- Python 3.10 or newer;
- Node.js 22 or newer, or Wiki Memory's isolated portable Node.js;
- Obsidian;
- Docling in the isolated Python runtime;
- QMD in the isolated Node runtime.

Syncthing is checked as an optional component but is not installed and cannot block onboarding unless the user later enables multi-device synchronization.

If an item is missing, onboarding shows the proposed package-manager command and official download page before requesting authorization. It never silently continues after a failed installation.

## 3. Design the memory

Once installation is ready, onboarding first asks whether the user has an organization in mind or wants a proposal based on what ChatGPT genuinely knows from available context. A generated proposal identifies known facts, assumptions, and unknowns so the user can correct it before continuing.

It also asks whether the user wants synchronization to another device. Choosing no keeps the installation fully local and skips Syncthing. Choosing yes starts a short explanation, permission-based installation, device pairing, two-folder configuration, and verification. The durable installation root always contains sibling `Agent/` and `Mémoire/` directories.

The interview is then progressive. It covers:

1. the user's own organization idea or the corrected ChatGPT-informed proposal;
2. preferred conversation and note languages;
3. what the memory should help remember, decide, create, or deliver;
4. current and expected source types;
5. audiences and confidentiality boundaries;
6. knowledge with distinct lifecycles or output formats;
7. preferred terminology, categories, and tags;
8. expected outputs;
9. enabled social connectors, selected collections or playlists, destination vaults, and platform/collection folder organization;
10. capture frequency, local run time, timezone, result destination, media retention, devices, and backups.

Onboarding presents the proposed vault boundaries, taxonomy, routing rules, schedules, and sync policy before writing files. Client vaults are never created unless the user explicitly needs client isolation.

For social sources, it also explains the benefit and limits, asks the user to sign in interactively in the controlled Codex browser, and completes a test sync before creating any recurring task. New items are stored under `01-Sources/items/<platform>/<collection>/`. The browser may retain its normal session, but Wiki Memory never requests or copies passwords, cookies, tokens, or browser profiles.

## 4. Open the vaults

Each registered vault is an independent Obsidian vault. Open the vault directory—not the memory container root—when you want separate Obsidian settings per knowledge boundary.

The generated `.obsidian/` directory contains only safe defaults. Device-local workspaces are ignored.

## 5. Capture a first source

Ask Codex naturally:

```text
Use $wiki-memory-capture to save this article in my memory: https://example.com/article
```

Or use the CLI printed by bootstrap:

```bash
wiki-memory capture /path/to/memory \
  --vault knowledge \
  --url https://example.com/article \
  --content "Captured article text"
```

For a document that requires conversion:

```bash
wiki-memory ingest /path/to/memory \
  --vault knowledge \
  --file /path/to/report.pdf
```

## 6. Index and query

```bash
wiki-memory index /path/to/memory
wiki-memory query /path/to/memory "What does the evidence say?"
wiki-memory query /path/to/memory "What did the memory know?" --system-at 2025-06-01
wiki-memory query /path/to/memory "What was true?" --valid-at 2025-06-01
```

The default view uses current facts. `--system-at` reconstructs what the memory knew at a date; `--valid-at` reconstructs what was true in the world at a date. The answer workflow must cite the source note or original path and disclose stale facts it excluded. If QMD is unavailable, Wiki Memory can fall back to text search, but `doctor` will still report QMD as missing.

Review temporal gaps without changing any note:

```bash
wiki-memory maintenance /path/to/memory --older-than-months 6
```

## 7. Configure Syncthing only when enabled

After the organization is confirmed, onboarding prepares the layout and initializes only the memory sibling:

```bash
wiki-memory prepare-installation /path/to/installation --agent-source /path/to/installed/plugin
wiki-memory init /path/to/installation/Mémoire --spec /path/to/onboarding.yaml
```

If the user opted in, onboarding then configures both sibling folders on the first device:

```bash
wiki-memory syncthing-setup /path/to/installation/Mémoire
wiki-memory syncthing-setup /path/to/installation/Mémoire --device-id OTHER-DEVICE-ID --device-name "Other device"
```

The other device accepts `Agent/` and the immutable transport share, mapped into an initialized memory at `Mémoire/.wiki-memory/data/`. Keep generated ignore files, wait for blobs and packs, import packs, then run verify and rebuild. Never map Syncthing directly to `events.sqlite3`.

Enable Syncthing file versioning on at least one device or maintain a separate backup. Then run:

```bash
wiki-memory doctor /path/to/installation/Mémoire
```

Skip this section entirely when multi-device synchronization is disabled.

## Updating

```bash
codex plugin marketplace upgrade petitmaker
codex plugin add wiki-memory@petitmaker
```

Start a new task after updating so Codex loads the new plugin version.

## Next steps

- Read [Architecture](ARCHITECTURE.md) before designing advanced vault boundaries.
- Read [Obsidian and Syncthing](OBSIDIAN_AND_SYNCTHING.md) before adding another device.
- Read [Social connectors](SOCIAL_CONNECTORS.md) before enabling browser collection.
- Use [Troubleshooting](TROUBLESHOOTING.md) when a dependency or index check fails.
