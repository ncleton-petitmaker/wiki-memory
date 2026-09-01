---
name: wiki-memory-onboarding
description: Welcome a user on first launch, set up a new adaptable Wiki Memory root, or redesign its vault boundaries, including core dependency installation and optional multi-device synchronization with Syncthing. Use whenever Wiki Memory is opened for the first time or the user asks to start, install, create, initialize, configure, synchronize, or restructure their memory system.
---

# Wiki Memory onboarding

Read [the onboarding interview](../../references/onboarding.md) and [the architecture contract](../../references/architecture.md) completely.

## First-launch welcome

On the first launch, the first visible response must be in French and contain only a short welcome followed by this question:

> Veux-tu démarrer un échange pour que je comprenne mieux tes activités, que je puisse mieux t'aider et que nous structurions ta mémoire ensemble ?

Do not mention a skill name, `$wiki-memory-onboarding`, a command, a task to open, a prompt to copy, dependencies, or installation details in this first response. Do not ask any classification question yet. If the user declines, stop without running checks or creating files. If the user accepts, continue in French by default and run the read-only dependency check before the first classification question.

## Mandatory dependency gate

After the user accepts the first-launch welcome, do this before asking classification questions or creating files on every first setup. Do not wait for `wiki-memory doctor` to fail.

1. Locate this installed plugin directory from this `SKILL.md`; never assume the current working directory is the plugin.
2. Select an available Python 3.10+ launcher (`python3`, `python`, or `py -3`). If none exists, report [Python's official download](https://www.python.org/downloads/) and, with permission, use the supported OS package manager or official installer before continuing.
3. Run `<python-launcher> scripts/bootstrap.py --check` from the plugin directory. The check is read-only. Keep successful technical details in the background; summarize them in French only when they help the user.
4. If a required core dependency is missing, explain it in plain French, include only the package-manager commands or official links that are actually needed, and ask once for permission to install it. Syncthing is optional and its absence must never block this gate.
5. After permission, run `<python-launcher> scripts/bootstrap.py --yes --open-links`. It installs or verifies Obsidian, installs a portable Node.js when Node 22+ is unavailable, and installs Wiki Memory, Docling, and QMD in the operating system's user-data directory. It must not install Syncthing at this stage. Never place runtimes, models, indexes, cookies, or credentials in a vault.
6. If automatic installation is unavailable, the bootstrap opens only the official page needed for the selected missing component and reports `needs-user`. Guide the user through the official installer, then rerun `--check`. Do not continue until all required core checks pass.

Onboarding must not silently claim success after a package-manager, network, or permission error. Preserve the bootstrap's status and recovery link in the response, translated or summarized in the conversation language. Never tell the user to invoke a named skill or copy a technical prompt.

## Explain the memory in plain language

After the dependency gate passes and before asking how the memory should be organized, explain the operating model to a non-technical user. Keep the explanation short, concrete, and in the conversation language. Show this localized ASCII graph (translate labels when the conversation is not in French):

```text
CE QUE VOUS DONNEZ
fichier · mail · réunion · page web
              |
              v
+------------------------------+
| 01-SOURCES                   |
| La preuve d'origine, gardée |
+------------------------------+
              |
              | "ce fait vient d'ici"
              v
+------------------------------+
| 02-WIKI                      |
| Faits courts + lien source   |
| + quand c'était vrai       |
| + quand la mémoire l'a su   |
+------------------------------+
              |
              v
+------------------------------+
| 03-SYNTHÈSES                 |
| Réponses et livrables       |
| que l'on peut vérifier       |
+------------------------------+

Si un fait change :

[ancien fait, conservé] ---> [nouveau fait, courant]
         "remplacé par"          "remplace"
```

Explain the three consequences in ordinary language:

- an answer can be traced back to the source that supports it;
- a changed fact is kept as history instead of being silently erased;
- when a date or proof is missing, the memory shows an open question instead of guessing.

Define "source", "fact", "current", and "verification" if the user may not know them. Avoid database, graph, schema, vector, embedding, frontmatter, and bi-temporal jargon unless the user asks. Invite questions, but do not turn this explanation into a technical lesson or block the interview when the user is ready to continue.

## Durable installation layout

Use one user-selected installation root with exactly these two durable sibling directories at its top level:

```text
<installation-root>/
├── Agent/
└── Mémoire/
```

`Agent/` contains the public Wiki Memory plugin files. `Mémoire/` is the only target for the generated memory root and all its vaults. Never place the memory inside `Agent/`, or the agent inside `Mémoire/`. Runtimes, Python or Node environments, models, indexes, browser profiles, cookies, credentials, and caches remain in the operating system's user-data directory, outside both synchronized folders.

Do not create these directories before the user confirms the proposed memory organization and storage location. Immediately before initialization, run `wiki-memory prepare-installation <installation-root> --agent-source <plugin-directory>`. This copies only public agent files when the installed plugin is not already in `Agent/`, excludes local secrets and reproducible state, creates the empty `Mémoire/` sibling, and refuses an unrelated non-empty `Agent/`. Then run `wiki-memory init <installation-root>/Mémoire --spec <spec>`. Do not initialize any differently named memory root during onboarding.

## Choose the starting point

As soon as every required dependency is installed, or the user has explicitly waived an optional application, ask this question before the detailed interview:

> Maintenant que tout est installé, as-tu déjà une idée de la façon dont ta mémoire devrait être organisée, ou préfères-tu que je te fasse une proposition à partir de ce que ChatGPT sait déjà de toi ?

If the user already has an idea, let them describe it freely before asking follow-up questions. Treat it as the starting hypothesis, preserve their terminology, and ask only for missing decisions or boundaries.

If the user requests a proposal, use only information actually available in the current conversation, ChatGPT memory, selected project context, or sources the user has provided. Draft a concise initial organization with the evidence or rationale behind each major boundary. Clearly label assumptions and unknowns; never invent personal facts or imply access to information that is not present. Let the user correct the proposal before continuing.

## Offer optional multi-device synchronization

After the core dependency gate passes, ask explicitly:

> Souhaites-tu synchroniser ta mémoire sur un autre appareil ? C'est facultatif : ta mémoire fonctionnera entièrement sur cet appareil si tu réponds non.

If the user declines, set `sync_enabled` to `false`, do not install Syncthing, do not create Syncthing configuration files, and do not report its absence as an error.

If the user accepts, first explain in plain French that Syncthing keeps the selected memory folder synchronized directly between the user's devices, without turning it into hosted cloud storage; changes on either device propagate to the other, and synchronization is not a backup. Explain that Syncthing must be installed on both devices and that file versioning or a separate backup remains recommended.

Then ask permission to install the optional application. After permission, run `<python-launcher> scripts/bootstrap.py --yes --with-syncthing --open-links`, start the supported Syncthing application or user service, and verify that its CLI responds before continuing. Ask which other device should receive the memory and help install and start Syncthing there. The remote Syncthing device ID is pairing information, not a password; never request private keys, API keys, or configuration files.

Set `sync_enabled` to `true` in the confirmed onboarding spec. After `wiki-memory init` creates `<installation-root>/Mémoire` and its vaults, run `wiki-memory syncthing-setup <installation-root>/Mémoire` on the first device. This must register `Agent/` and `Mémoire/` as two separate Syncthing folders; never combine them into one share. Once the user supplies the other device's Syncthing ID, rerun it with `--device-id <id>` and an optional `--device-name`. Help the user accept both shared folders on the other device and map them to sibling destinations named `Agent/` and `Mémoire/` under that device's chosen installation root. Do not claim synchronization is complete until both folders are configured on both devices and Syncthing reports them as connected or up to date. Preserve `.stignore` in each folder on each device and confirm a backup or versioning policy.

Interview progressively. Do not create files until the user confirms the proposed vault boundaries, taxonomy, folder language, enabled connectors, schedules, media policy, optional synchronization choice, and backup plan. Avoid asking for answers discoverable from an existing memory root.

When the user wants social saves, explain before activation that Wiki Memory can copy new saved posts and selected YouTube playlists into a local searchable memory, preserve provenance, deduplicate revisions, and organize notes by platform and collection. Explain that it does not move or delete the originals on Instagram, YouTube, or another platform. Ask which platforms and collections to read, which vault receives them, whether media is retained, and whether the scan is manual, daily, weekly, or custom. For a schedule, confirm local time, timezone, and result destination.

Create a temporary onboarding spec matching `schemas/onboarding.schema.json`, prepare the two-folder installation layout, run `wiki-memory init` against its `Mémoire/` directory, then run `wiki-memory doctor`. The final doctor report must include Python, Node.js, Obsidian, Docling, QMD, layout, and backup readiness. Include Syncthing and ignore-rule verification for both `Agent/` and `Mémoire/` only when synchronization is enabled. If the target is non-empty, stop rather than merging implicitly. Offer the router and migration workflow for an existing memory.

After a successful doctor run, offer to open each generated vault in Obsidian. Mention Syncthing follow-up only when the user enabled multi-device synchronization.

For every enabled social connector, open its saved-items page in the controlled Codex browser and ask the user to sign in there when needed. Never request credentials in chat or copy authentication state into the memory. Run one interactive test sync, verify the platform/collection folders and counts, and only then create the confirmed recurring task. If the browser capability is missing, explain the required supported integration and ask permission before installing or enabling it.

When scheduling tasks, read [automation guidance](../../references/automation.md). Never imply that browser authentication is guaranteed in background runs.
