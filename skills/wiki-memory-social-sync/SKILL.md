---
name: wiki-memory-social-sync
description: Collect a user's saved items from Instagram, LinkedIn, Reddit, X, or YouTube through their controlled browser and import new items into Wiki Memory. Use for manual or scheduled social synchronization, never for bypassing access controls.
---

# Wiki Memory social sync

Read [browser-first connector guidance](../../references/social-connectors.md) completely. Use the available Codex Browser or the browser family explicitly selected by the user; follow that browser skill's setup and authentication rules.

## First activation contract

Before the first social run, explain in the user's language what the workflow does and why it is useful: it copies newly saved items into the user's local, searchable, source-grounded memory; preserves the visible URL, title, author, date, text or transcript, and allowed media; deduplicates and versions changes; and files each item by platform and collection or playlist. Also explain that it does not move or delete the original item on the social platform and that unattended access can stop when a platform requests login or verification.

Run the onboarding bootstrap dependency check if it has not already passed. If a supported runtime or browser capability is missing, describe exactly what is needed and ask permission before installing or enabling it. Never claim that a dependency or browser integration is ready without verifying it.

Ask which platforms, saved collections or playlists, destination vaults, media policy, and local folder organization the user wants. Explicitly ask whether synchronization should be manual, daily, weekly, or another cadence; for a schedule, also confirm local time, timezone, and result destination. Do not create an automation until the user confirms those choices, and do not create a duplicate.

Open each selected platform in the controlled browser. If signed out, ask the user to sign in interactively and allow the browser to retain its normal session if they want future runs to reuse it. Never ask the user to paste a password, cookie, session token, or exported browser profile into the conversation or vault. Complete one interactive test sync before enabling an unattended schedule.

Inspect `memory.config.yaml` and process only enabled connectors and configured collections. Use `$wiki-memory-router` unless the social destination vault is explicit.

Capture visible saved items into normalized temporary JSON matching `schemas/social-capture.schema.json`, including the visible `collection` or playlist name for every item. Then run `wiki-memory social-import`. The importer files new social notes under `Sources/items/<platform>/<collection>/`, using `sans-collection` only when the source exposes no collection. Delete the temporary capture after a successful import. Run `wiki-memory index` and report captured, revised, duplicate, blocked, and unclassified counts by platform and collection.

Stop on sign-in, verification, captcha, rate limit, access denial, or an unrecognized layout. Never inspect or export cookies, local storage, passwords, profiles, or session files. Never switch to standalone Playwright or a scraping service to bypass the controlled browser.

For scheduled use, read [automation guidance](../../references/automation.md), use the social-sync automation prompt, and treat browser authentication as best effort. A scheduled run that needs login must request interactive recovery instead of silently succeeding.
