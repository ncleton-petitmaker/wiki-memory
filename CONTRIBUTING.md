# Contributing to Wiki Memory

Thank you for helping build a durable, local-first memory system. Contributions are welcome when they preserve user ownership, source traceability, and safe defaults.

## Before starting

- Search existing [issues](https://github.com/ncleton-petitmaker/wiki-memory/issues) and [discussions](https://github.com/ncleton-petitmaker/wiki-memory/discussions).
- Use a discussion for an early idea or design question.
- Use an issue for a reproducible bug or scoped feature.
- Open a draft pull request early for changes that affect file formats, routing, security boundaries, or multiple platforms.

## Non-negotiable project rules

1. Markdown and original files remain the durable source of truth.
2. Runtime state, models, indexes, credentials, and browser profiles stay outside vaults.
3. Source captures are immutable; interpretations belong in the wiki layer.
4. Folder names are resolved through `vault.yaml`, never hard-coded to one language.
5. No client taxonomy is created by default.
6. All fixtures, examples, screenshots, and test data must be synthetic.
7. Browser automation must stop on captchas, verification, rate limits, or access controls.
8. Syncthing compatibility must not depend on symlinks.

## Development setup

```bash
git clone https://github.com/ncleton-petitmaker/wiki-memory.git
cd wiki-memory
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

On Windows, create the environment with `py -3 -m venv .venv` and activate it with `.venv\Scripts\activate`.

Install the full runtime only when testing Docling or QMD:

```bash
python3 scripts/bootstrap.py --yes
```

## Tests

Run before every pull request:

```bash
python -m unittest discover -s tests -v
python -m compileall -q src scripts tests
python scripts/privacy_scan.py .
```

Also validate every changed skill with Codex's skill validator and validate the plugin manifest with the plugin validator when those tools are available.

Test changes proportionally:

- routing: existing, ambiguous, and new-vault cases;
- ingestion: duplicate and meaningful-revision cases;
- paths: spaces, Unicode, macOS, Linux, and Windows conventions;
- social capture: normalized synthetic payloads and every typed stop state;
- sync: `.stignore`, local Obsidian state, and backup warnings;
- security: no personal paths, tokens, cookies, or non-synthetic fixtures.

## Pull requests

Keep pull requests focused. Explain the user problem, chosen behavior, compatibility impact, tests performed, and privacy considerations. Update documentation and `CHANGELOG.md` when user-visible behavior changes.

## Commit messages

Use short, imperative subjects, for example:

```text
Add ambiguity test for vault routing
Stop social sync on verification pages
Document Syncthing versioning recovery
```

Do not open a public issue for a vulnerability or privacy leak. Follow [SECURITY.md](SECURITY.md). By participating, you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
