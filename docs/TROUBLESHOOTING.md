# Troubleshooting

Start with the dependency gate and Doctor. Keep their JSON output when opening an issue, after removing paths or content you do not want to share.

```bash
python3 scripts/bootstrap.py --check
wiki-memory doctor /path/to/memory
```

On Windows, use `py -3` for the bootstrap command.

## The onboarding skill does not appear

1. Confirm the marketplace and plugin are installed:

   ```bash
   codex plugin marketplace list
   codex plugin list
   ```

2. Refresh the marketplace and reinstall:

   ```bash
   codex plugin marketplace upgrade petitmaker
   codex plugin add wiki-memory@petitmaker
   ```

3. Start a new task or restart the ChatGPT desktop app. Skills are loaded at task start.

## Python is missing or too old

Install Python 3.10 or newer from [python.org](https://www.python.org/downloads/) or through a trusted operating-system package manager. Then rerun `bootstrap.py --check` with `python3`, `python`, or `py -3`.

## Node.js is missing

Bootstrap installs a verified portable Node.js in Wiki Memory's runtime when no compatible system Node.js is available. If that download fails, check network access to [nodejs.org](https://nodejs.org/) and rerun the bootstrap.

## Obsidian or optional Syncthing could not be installed automatically

Bootstrap installs Obsidian as part of the core setup. It installs Syncthing only after multi-device synchronization is enabled with `--with-syncthing`. It uses supported package managers where available and otherwise opens official pages:

- [Obsidian downloads](https://obsidian.md/download)
- [Syncthing downloads](https://syncthing.net/downloads/)

Complete the platform installer, then rerun `bootstrap.py --check`. Wiki Memory does not download application installers from mirrors.

## Docling fails on a document

- Confirm `dependency:docling` passes in Doctor.
- Try a local file path instead of a remote URL.
- Confirm the source is not encrypted or password protected.
- Keep the original file; a conversion failure must not delete it.
- Include the file type and sanitized error in a bug report. Do not attach confidential documents.

## QMD returns no results

```bash
wiki-memory index /path/to/memory
wiki-memory query /path/to/memory "distinctive phrase"
```

Use `--no-embed` to test exact indexing separately from model downloads. QMD indexes and models are outside the vault and can be rebuilt.

## Social sync says `needs-login`

Sign in interactively in the browser selected for the run, then restart the capture. Wiki Memory does not copy or persist the browser's credentials.

Other explicit stop states are `verification-required`, `rate-limited`, `layout-changed`, `access-denied`, and `content-unavailable`. Do not treat them as empty successful imports.

## `.stignore` is reported as different

This check applies only when synchronization is enabled. Syncthing does not synchronize `.stignore`. In both sibling folders, `Agent/` and `Mémoire/`, compare the local file with `syncthing.ignore.template`, preserve any intentional device-specific additions, and rerun Doctor against `Mémoire/`.

## A file was deleted on another device

Syncthing mirrors deletions. Recover from Syncthing versioning or a separate backup. Wiki Memory cannot reconstruct an original that no longer exists on any device.

## Before sharing diagnostics

```bash
wiki-memory privacy-scan /path/to/diagnostic-folder
```

Never post vault contents, `.env` files, browser profiles, cookies, tokens, or personal absolute paths in a public issue.
