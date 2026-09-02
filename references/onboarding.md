# Adaptive onboarding interview

On first launch, greet the user in French and ask whether they want to start an exchange so Wiki Memory can understand their activities, help them better, and structure their memory with them. Do not expose a skill name, command, prompt to copy, or installation checklist. If they accept, complete the mandatory dependency gate in the onboarding skill before starting this interview. Classification onboarding starts only after Python, Node.js, Obsidian, Docling, and QMD are installed or the user has explicitly waived a non-core application. Syncthing is not part of this mandatory gate.

Once that gate passes, ask whether the user already has an organization in mind or wants an initial proposal based on what ChatGPT genuinely knows about them from available conversation, memory, project context, and user-provided sources. If they have an idea, collect it in their own words first. If they want a proposal, present a concise draft, distinguish known facts from assumptions and unknowns, and invite corrections before asking the remaining questions. Never fabricate personal context.

Before asking that organization question, show the plain-language ASCII memory graph from the onboarding skill. Explain that Sources preserve evidence, Wiki notes carry traceable facts, and Syntheses turn those facts into verifiable answers. Make clear that old facts remain visible when they are replaced and that missing dates become open questions rather than guesses. Use everyday vocabulary and define any unavoidable term.

Also ask whether the user wants synchronization to another device. Make clear that solo remains complete without it. If declined, do not install Syncthing. If accepted, explain peer-to-peer transport versus backup, install with permission, create sibling `Agent/` and `Mémoire/`, then share `Agent/` and `Mémoire/.wiki-memory/data/` separately. The transport folder carries immutable blobs and packs, never live SQLite. Pair the device, map transport into an initialized remote memory, import, verify, rebuild, and confirm both shares are current.

Collect enough information to produce an onboarding spec without assuming that the user needs clients, projects, or social media.

1. Record the user's chosen starting point: their own organization idea or a ChatGPT-informed proposal.
2. Confirm the conversation language and the language or languages used inside notes.
3. Ask what the memory should help the user remember, decide, create, or deliver.
4. Inventory current and expected sources: files, URLs, web pages, email exports, images, audio, video, and social saves.
5. Identify audiences and confidentiality boundaries. Ask whether any person, client, employer, health topic, or regulated data requires isolation.
6. Ask which bodies of knowledge have distinct lifecycles or output formats.
7. Ask for preferred terminology, classification axes, tags, and examples of ambiguous content.
8. Ask which outputs matter: answers, research notes, articles, training materials, project briefs, client deliverables, or other artifacts.
9. If social saves are wanted, first explain the local copy, provenance, search, deduplication, platform/collection filing, and background-login limits. Then ask which social connectors are enabled, what saved collection or playlist each should read, and which vault receives it.
10. Ask capture frequency (manual, daily, weekly, or custom), local run time and timezone, result destination, media retention, and storage constraints.
11. Record whether multi-device synchronization is disabled or, if enabled, the target devices and backup/versioning plan.
12. Present the proposed vaults, taxonomy, routing boundaries, schedules, and optional sync policy for confirmation before creating files.

Do not offer a client vault unless the user's answers make it relevant. If client isolation is enabled, use one vault per client and never put one client's source in another client's vault.

Write the confirmed answers as JSON or YAML matching `schemas/onboarding.schema.json`, run `wiki-memory prepare-installation <installation-root> --agent-source <plugin-directory>`, then run `wiki-memory init <installation-root>/Mémoire --spec <spec>`.

After initialization and Doctor, complete an interactive browser sign-in and test sync for every enabled social connector before creating its confirmed recurring task. Credentials stay in the browser's own session and are never requested in chat or copied into Wiki Memory.
