from __future__ import annotations

import hashlib
from typing import Any

from .config import MemoryError, load_vault
from .engine import MemoryEngine
from .events import EventActor, MemoryEvent, PluginRef, canonical_json, uuid7
from .team import Principal, ProposalService, Role, ensure_team_vault, normalize_acl, shared_vault_slug
from .projections import MarkdownProjector


def publication_preview(
    engine: MemoryEngine,
    event_id: str,
    *,
    destination_scope: str,
    destination_space: str,
    principal_id: str | None = None,
) -> dict[str, Any]:
    if destination_scope not in {"team", "organization"}:
        raise MemoryError("A publication destination must be team or organization scope.")
    if not destination_space.strip():
        raise MemoryError("A publication destination space is required.")
    source = engine.events.get(event_id)
    if source is None:
        raise MemoryError(f"Unknown event: {event_id}")
    if source.scope != "private":
        raise MemoryError("Only private local events use the explicit publication workflow.")
    publishing_actor = principal_id or source.actor.id
    organization_promotion = destination_scope == "organization"
    event_type = "source.publication.proposed" if organization_promotion else "source.published"
    publication_acl = normalize_acl(
        {"audience": "organization"} if organization_promotion else {},
        owner=publishing_actor,
        space_id=destination_space,
    )
    # A proposal is visible only to the destination Team space.  Its desired
    # organization ACL remains an intent in the proposal and takes effect only
    # when a curator appends ``source.published``.
    proposal_acl = normalize_acl({}, owner=publishing_actor, space_id=destination_space)
    published_payload = {
        **source.payload,
        "vault": shared_vault_slug(destination_space),
        "partition": f"shared/{source.payload.get('sourceId', source.event_id)}",
        "publishedFromEventId": source.event_id,
        "publishedFromFingerprint": source.event_hash,
        "localPathsRemoved": True,
        "status": "proposed" if destination_scope == "organization" else "published",
        "reviewRequired": destination_scope == "organization",
        "reviewReasons": ["organization-promotion"] if destination_scope == "organization" else [],
    }
    if organization_promotion:
        published_payload["publicationTarget"] = {
            "scope": "organization",
            "acl": publication_acl,
        }
    exact = {
        "sourceEventId": source.event_id,
        "eventType": event_type,
        "streamId": f"publication:{destination_space}:{source.event_id}",
        "destinationScope": destination_scope,
        "eventScope": "team" if organization_promotion else destination_scope,
        "destinationSpace": destination_space,
        "actor": {"type": "user", "id": publishing_actor},
        "plugin": {"id": "team-contribution", "version": "1.0.0"},
        "evidenceRefs": source.evidence_refs,
        "acl": proposal_acl if organization_promotion else publication_acl,
        "payload": published_payload,
        "causationId": source.event_id,
    }
    preview_hash = hashlib.sha256(canonical_json(exact).encode("utf-8")).hexdigest()
    return {**exact, "previewHash": preview_hash}


def publish_private_event(
    engine: MemoryEngine,
    event_id: str,
    *,
    principal_id: str,
    destination_scope: str,
    destination_space: str,
    preview_hash: str,
) -> dict[str, Any]:
    preview = publication_preview(
        engine,
        event_id,
        destination_scope=destination_scope,
        destination_space=destination_space,
        principal_id=principal_id,
    )
    if preview_hash != preview["previewHash"]:
        raise MemoryError("Publication preview changed or was not confirmed.")
    source = engine.events.get(event_id)
    assert source is not None
    ensure_team_vault(engine.root, destination_space)
    published = MemoryEvent(
        event_type=str(preview["eventType"]),
        stream_id=f"publication:{destination_space}:{source.event_id}",
        idempotency_key=f"publish:{source.event_id}:{destination_scope}:{destination_space}:{preview_hash}",
        actor=EventActor(type="user", id=principal_id),
        plugin=PluginRef(id="team-contribution", version="1.0.0"),
        scope=str(preview["eventScope"]),  # type: ignore[arg-type]
        space_id=destination_space,
        evidence_refs=source.evidence_refs,
        acl=dict(preview["acl"]),
        payload=dict(preview["payload"]),
        causation_id=source.event_id,
    )
    persisted, created = engine.append(published, expected_stream_version=0)
    return {"created": created, "event": persisted.to_dict()}


def propose_assertion(
    engine: MemoryEngine,
    *,
    actor_id: str,
    scope: str,
    space_id: str,
    assertion: dict[str, Any],
    evidence_refs: list[str],
) -> dict[str, Any]:
    if scope == "private":
        if space_id != "local-owner":
            raise MemoryError("Private proposals must remain in the local-owner space.")
        vault_slug = str(assertion.get("vault") or "").strip()
        if not vault_slug:
            raise MemoryError("Private proposals require a destination vault.")
        _, vault = load_vault(engine.root, vault_slug)
        if (vault.get("team") or {}).get("managed"):
            raise MemoryError("Private proposals cannot target a Team-managed vault.")
        assertion_id = str(assertion.get("assertionId") or uuid7())
        event = MemoryEvent(
            event_type="assertion.proposed",
            stream_id=f"assertion:local-owner:{assertion_id}",
            idempotency_key=str(assertion.get("idempotencyKey") or f"proposal:{assertion_id}:{uuid7()}"),
            actor=EventActor(type="user", id=actor_id),
            plugin=PluginRef(id="gateway-mcp", version="1.0.0"),
            evidence_refs=evidence_refs,
            payload={**assertion, "assertionId": assertion_id, "status": "proposed"},
        )
        persisted, created = engine.append(event)
        return {"created": created, "event": persisted.to_dict()}
    principal = Principal(
        actor_id,
        frozenset({Role.CONTRIBUTOR}),
        frozenset({space_id}),
    )
    return ProposalService(engine).propose(
        principal=principal,
        space_id=space_id,
        scope=scope,
        assertion=assertion,
        evidence_refs=evidence_refs,
        acl=normalize_acl({}, owner=actor_id, space_id=space_id),
    )


def review_local_proposal(
    engine: MemoryEngine,
    *,
    actor_id: str,
    proposal_event_id: str,
    decision: str,
    reason: str | None = None,
) -> dict[str, Any]:
    principal = Principal(actor_id, frozenset({Role.ADMIN, Role.CURATOR}), frozenset({"local-owner"}))
    return ProposalService(engine).review(
        principal=principal,
        proposal_event_id=proposal_event_id,
        decision=decision,
        reason=reason,
    )


def capture_projection_edits(engine: MemoryEngine, *, actor_id: str = "local-owner") -> dict[str, Any]:
    projector = engine.projections.projectors.get("projection.markdown")
    if not isinstance(projector, MarkdownProjector):
        raise MemoryError("Markdown projection is not active.")
    proposals: list[dict[str, Any]] = []
    for edit in projector.modified_files(engine.root):
        path = engine.root / edit["path"]
        evidence = engine.evidence.put_file(path, media_type="text/markdown", original_name=path.name)
        scope = str(edit.get("scope") or "private")
        space_id = str(edit.get("spaceId") or "local-owner")
        event = MemoryEvent(
            event_type="projection.edit.proposed",
            stream_id=f"projection-edit:{edit['path']}",
            idempotency_key=f"projection-edit:{edit['path']}:{edit['actualSha256']}",
            actor=EventActor(type="user", id=actor_id),
            plugin=PluginRef(id="projection-markdown", version=projector.version),
            scope=scope,  # type: ignore[arg-type]
            space_id=space_id,
            evidence_refs=[evidence.reference],
            acl=normalize_acl({}, owner=actor_id, space_id=space_id),
            payload={
                "kind": "projection-edit",
                "path": edit["path"],
                "expectedSha256": edit["expectedSha256"],
                "editedSha256": edit["actualSha256"],
                "basedOnEventId": edit.get("eventId"),
                "status": "proposed",
            },
        )
        persisted, created = engine.append(event)
        proposals.append({"created": created, "event": persisted.to_dict()})
    return {"ok": True, "modified": len(proposals), "proposals": proposals}


def review_projection_edit(
    engine: MemoryEngine,
    *,
    proposal_event_id: str,
    actor_id: str,
    decision: str,
    reason: str | None = None,
) -> dict[str, Any]:
    proposal = engine.events.get(proposal_event_id)
    if proposal is None or proposal.event_type != "projection.edit.proposed":
        raise MemoryError(f"Unknown projection edit proposal: {proposal_event_id}")
    if proposal.scope != "private":
        raise MemoryError("Shared projection edits must be reviewed by a Team curator.")
    if decision not in {"accept", "reject"}:
        raise MemoryError("Projection edits can only be accepted or rejected.")
    latest = engine.events.latest_stream_event(proposal.stream_id)
    review_key = f"projection-edit-review:{proposal.event_id}:{decision}"
    if latest is not None and latest.idempotency_key == review_key:
        return {"created": False, "event": latest.to_dict()}
    if latest is None or latest.event_type != "projection.edit.proposed":
        raise MemoryError("Projection edit has already been reviewed.")
    event = MemoryEvent(
        event_type=f"projection.edit.{'accepted' if decision == 'accept' else 'rejected'}",
        stream_id=proposal.stream_id,
        idempotency_key=review_key,
        actor=EventActor(type="user", id=actor_id),
        plugin=PluginRef(id="projection-markdown", version="1.0.0"),
        evidence_refs=proposal.evidence_refs,
        acl=proposal.acl,
        payload={**proposal.payload, "status": decision, "reviewReason": reason},
        causation_id=proposal.event_id,
    )
    persisted, created = engine.append(event, expected_stream_version=latest.stream_version, project=decision == "accept")
    if decision == "reject":
        engine.rebuild(force=True)
    return {"created": created, "event": persisted.to_dict()}
