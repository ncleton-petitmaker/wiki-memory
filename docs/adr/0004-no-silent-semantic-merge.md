# ADR 0004 — No silent semantic merge

Status: accepted.

CRDTs can merge bytes but cannot decide which contradictory business fact is true. Shared knowledge edits carry an expected stream version. A stale base creates a curator-visible conflict proposal.

Consequences: append-only source streams remain simple; users resolve meaningful contradictions; offline operation is preserved; the product never presents a guessed merge as accepted knowledge.
