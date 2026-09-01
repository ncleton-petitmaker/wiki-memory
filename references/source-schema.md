# Source record invariants

Every normalized source note must include a stable `id`, source type, source URL or non-sensitive origin, documented author/date, capture time, connector, SHA-256 content hash, target vault, epistemic status, integer revision, relative raw path, and relative media paths.

The current source note lives under the vault's Sources `items` directory. Changed content archives the previous note under Sources `revisions/<id>/`. Identical content is a duplicate and must not create another note.
