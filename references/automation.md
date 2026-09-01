# Codex task templates

Onboarding may create heartbeat or scheduled Codex tasks only after the user confirms cadence, local run time, timezone, and result destination. Explicitly offer manual, daily, weekly, or custom synchronization. Use the prompts in `assets/automations/` and keep notification preferences outside the prompt.

The ingest task processes inboxes, refreshes QMD, and lints. The social task invokes the browser-first social workflow only after one successful interactive test sync. A background social task is best effort: browser authentication is not guaranteed to persist, so authentication or verification stops the task and requests interactive recovery.

Do not schedule a task for a connector the user disabled. Do not create a duplicate task when a matching task already exists.
