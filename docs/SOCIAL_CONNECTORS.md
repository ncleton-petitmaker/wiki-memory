# Social connector support

Wiki Memory supports saved-item capture for Instagram, LinkedIn, Reddit, X, and YouTube through the user-controlled Codex browser.

The collector intentionally has no cookie jar or account credential API. The user signs in interactively in the controlled browser and may let that browser retain its normal session. A browser run emits a normalized temporary JSON capture. The `source-social-browser` plugin exposes this capture through the same `SourceConnector` contract as database connectors; it validates the selected file, emits durable record/checkpoint messages, and never receives a social credential. The deterministic `social-import` command remains the daily convenience path and preserves the richer social Markdown layout.

New notes are organized locally as `01-Sources/items/<platform>/<collection>/`, with matching partitions for raw captures, revisions, and copied media. Instagram collections and YouTube playlist names are preserved when visible; items without a visible collection use `sans-collection`. This does not move or delete the original saved item on the platform.

Possible completion states:

- success with captured, revised, and duplicate counts;
- `needs-login`;
- `verification-required`;
- `rate-limited`;
- `layout-changed`;
- `access-denied`;
- `content-unavailable`.

Before scheduling, Wiki Memory explains the workflow, verifies dependencies, asks which platforms and collections to scan, confirms the destination vault and media policy, and completes one interactive test sync. It then asks whether the user wants a manual, daily, weekly, or custom cadence, including local time, timezone, and result destination.

Scheduled runs are best effort because browser authentication may not persist. Reauthentication is always interactive. The workflow does not bypass captchas, bot detection, rate limits, or platform access restrictions.
