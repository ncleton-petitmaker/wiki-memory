# Browser-first social capture

The social workflow uses the browser surface selected by the user or the available Codex Browser plugin. The user signs in interactively and may let that browser retain its normal session. Wiki Memory does not read or export cookies, local storage, profiles, passwords, or session files. It does not use standalone Playwright, unofficial credential stores, or third-party scraping services.

## Common flow

1. Open the platform's user-facing saved-items surface in the selected browser.
2. If signed out, ask the user to sign in in that browser and stop the run.
3. If a captcha, verification flow, automation warning, rate limit, or access restriction appears, stop and report a typed status. Never bypass it.
4. Read only the visible saved-item list. Paginate or scroll conservatively and stop when the last imported canonical URL is reached or no new items appear.
5. For every item, capture the canonical public URL, title, author, visible publication date, visible collection or playlist name, visible text or transcript, and references to media explicitly available for download.
6. Write normalized JSON matching `schemas/social-capture.schema.json` to a temporary local file, import it with `wiki-memory social-import`, then delete the temporary file.
7. Refresh QMD and report counts for captured, revised, duplicate, and blocked items.

New notes are stored under `Sources/items/<platform>/<collection>/`; raw captures, revisions, and copied media follow the same platform/collection partition. Use `sans-collection` only when the platform exposes no collection. This is a local memory classification: the workflow does not move, relabel, or delete the original saved item on the platform.

## Connector targets

- Instagram: Saved items and user-selected collections.
- LinkedIn: Saved posts.
- Reddit: Saved items; include other user-selected lists only when onboarding enables them.
- X: Bookmarks.
- YouTube: User-selected playlists, including Watch later only when the browser exposes it.

Page structure changes frequently. Prefer semantic labels and visible content over brittle selectors. A missing expected collection is `layout-changed`, not an empty successful sync.

Allowed stop statuses: `needs-login`, `verification-required`, `rate-limited`, `layout-changed`, `access-denied`, and `content-unavailable`.
