from __future__ import annotations

import json
import hashlib
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from .config import (
    MemoryError,
    load_registry,
    load_vault,
    root_runtime_dir,
    slugify,
    utc_now,
    write_data,
)
from .engine import MemoryEngine
from .events import EventActor, MemoryEvent, PluginRef, idempotency_fingerprint, uuid7
from .layout import create_vault


class Role(str, Enum):
    ADMIN = "admin"
    CURATOR = "curator"
    CONTRIBUTOR = "contributor"
    READER = "reader"
    SERVICE = "service"


@dataclass(frozen=True)
class Principal:
    id: str
    roles: frozenset[Role]
    spaces: frozenset[str]
    groups: frozenset[str] = frozenset()
    kind: str = "user"

    def has_any_role(self, *roles: Role) -> bool:
        return bool(self.roles.intersection(roles))


def normalize_acl(value: dict[str, Any] | None, *, owner: str, space_id: str) -> dict[str, Any]:
    value = dict(value or {})
    owners = sorted({str(item) for item in value.get("owners", [])} | {owner})
    readers = sorted({str(item) for item in value.get("readers", [])})
    groups = sorted({str(item) for item in value.get("groups", [])})
    spaces = sorted({str(item) for item in value.get("spaces", [])} | {space_id})
    classification = str(value.get("classification") or "internal")
    if classification not in {"public", "internal", "confidential", "restricted"}:
        raise MemoryError(f"Unsupported classification: {classification}")
    audience = str(
        value.get("audience")
        or ("explicit" if readers or groups or classification in {"confidential", "restricted"} else "space")
    )
    if audience not in {"explicit", "space", "organization"}:
        raise MemoryError(f"Unsupported ACL audience: {audience}")
    return {
        "owners": owners,
        "readers": readers,
        "groups": groups,
        "spaces": spaces,
        "classification": classification,
        "audience": audience,
    }


def shared_vault_slug(space_id: str) -> str:
    return "team-" + slugify(space_id)


def ensure_team_vault(root: Path, space_id: str) -> str:
    vault_slug = shared_vault_slug(space_id)
    try:
        vault_path, vault = load_vault(root, vault_slug)
        team = dict(vault.get("team") or {})
        if not team.get("managed") or team.get("space_id") != space_id:
            raise MemoryError(f"Vault {vault_slug} already exists and is not the Team space {space_id}.")
        if team.get("read_only"):
            raise MemoryError(f"Team space {space_id} is detached and read-only.")
        return vault_slug
    except MemoryError as exc:
        if "Unknown vault" not in str(exc):
            raise
    create_vault(
        root,
        {
            "slug": vault_slug,
            "title": f"Shared — {space_id}",
            "purpose": f"Authorized Team projection for {space_id}",
            "audience": ["team"],
            "confidentiality": "restricted",
        },
    )
    vault_path, vault = load_vault(root, vault_slug)
    vault["team"] = {"managed": True, "space_id": space_id, "read_only": False}
    write_data(vault_path / "vault.yaml", vault)
    return vault_slug


def can_read(principal: Principal, *, scope: str, space_id: str, acl: dict[str, Any]) -> bool:
    if principal.has_any_role(Role.ADMIN):
        return True
    if principal.id in acl.get("owners", []) or principal.id in acl.get("readers", []):
        return True
    if principal.groups.intersection(str(item) for item in acl.get("groups", [])):
        return True
    audience = str(acl.get("audience") or "explicit")
    if audience == "organization" and scope == "organization":
        return principal.has_any_role(Role.READER, Role.CONTRIBUTOR, Role.CURATOR, Role.ADMIN)
    return audience == "space" and space_id in principal.spaces and space_id in acl.get("spaces", [])


def team_session_path(root: Path) -> Path:
    return root_runtime_dir(root) / "team" / "session.json"


MAX_OFFLINE_LEASE_SECONDS = 31 * 24 * 60 * 60


def _parse_session_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        return None
    return timestamp.astimezone(timezone.utc)


def cached_team_principal(root: Path) -> Principal | None:
    """Load a still-valid Team entitlement lease, failing closed.

    Shared projections are deliberately unavailable after this lease expires.
    That limits a removed member's offline access window; the next successful
    Team sync atomically refreshes the snapshot from the server.
    """

    session_path = team_session_path(root)
    if not session_path.is_file():
        return None
    try:
        session = json.loads(session_path.read_text(encoding="utf-8"))
        if not isinstance(session, dict):
            return None
        checked_at = _parse_session_timestamp(session.get("checkedAt"))
        expires_at = _parse_session_timestamp(session.get("offlineLeaseExpiresAt"))
        now = datetime.now(timezone.utc)
        if (
            checked_at is None
            or expires_at is None
            or checked_at > now + timedelta(minutes=5)
            or expires_at <= now
            or expires_at <= checked_at
            or expires_at - checked_at > timedelta(seconds=MAX_OFFLINE_LEASE_SECONDS)
        ):
            return None
        valid_roles = {role.value for role in Role}
        principal_id = session["principalId"]
        roles = session.get("roles", [])
        spaces = session.get("spaces", [])
        groups = session.get("groups", [])
        kind = session.get("kind") or "user"
        if (
            not isinstance(principal_id, str)
            or not principal_id
            or not isinstance(roles, list)
            or not all(isinstance(item, str) for item in roles)
            or not isinstance(spaces, list)
            or not all(isinstance(item, str) and item for item in spaces)
            or not isinstance(groups, list)
            or not all(isinstance(item, str) and item for item in groups)
            or not isinstance(kind, str)
            or not kind
        ):
            return None
        return Principal(
            principal_id,
            frozenset(Role(item) for item in roles if item in valid_roles),
            frozenset(spaces),
            frozenset(groups),
            kind,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def local_event_visible(root: Path, event: MemoryEvent) -> bool:
    if event.scope == "private":
        return True
    principal = cached_team_principal(root)
    return bool(
        principal
        and can_read(principal, scope=event.scope, space_id=event.space_id, acl=event.acl)
    )


def local_evidence_visible(engine: MemoryEngine, reference: str) -> bool:
    return any(
        local_event_visible(engine.root, event)
        for event in engine.events.events_referencing_evidence(reference)
    )


def can_contribute(principal: Principal, space_id: str) -> bool:
    return principal.has_any_role(Role.ADMIN, Role.CURATOR, Role.CONTRIBUTOR, Role.SERVICE) and (
        space_id in principal.spaces or principal.has_any_role(Role.ADMIN)
    )


def derive_acl(
    evidence_acls: list[dict[str, Any]],
    *,
    owner: str,
    destination_space: str,
) -> dict[str, Any]:
    """Return the most restrictive intersection of evidence ACLs.

    Empty evidence ACLs cannot be used to manufacture broader access.
    """

    if not evidence_acls:
        return normalize_acl({}, owner=owner, space_id=destination_space)
    readers = set(str(item) for item in evidence_acls[0].get("readers", []))
    groups = set(str(item) for item in evidence_acls[0].get("groups", []))
    spaces = set(str(item) for item in evidence_acls[0].get("spaces", []))
    levels = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}
    classification = "public"
    audience = "space"
    for acl in evidence_acls:
        readers.intersection_update(str(item) for item in acl.get("readers", []))
        groups.intersection_update(str(item) for item in acl.get("groups", []))
        spaces.intersection_update(str(item) for item in acl.get("spaces", []))
        candidate = str(acl.get("classification") or "internal")
        if levels.get(candidate, 3) > levels[classification]:
            classification = candidate
        if str(acl.get("audience") or "explicit") != "space":
            audience = "explicit"
    if destination_space not in spaces:
        raise MemoryError("Derived knowledge cannot be published to a space absent from every evidence ACL.")
    return normalize_acl(
        {
            "readers": sorted(readers),
            "groups": sorted(groups),
            "spaces": [destination_space],
            "classification": classification,
            "audience": audience,
        },
        owner=owner,
        space_id=destination_space,
    )


@dataclass(frozen=True)
class RiskDecision:
    review_required: bool
    reasons: tuple[str, ...]


class ReviewPolicy:
    def __init__(self, low_confidence_threshold: float = 0.75):
        self.low_confidence_threshold = low_confidence_threshold

    def evaluate(
        self,
        *,
        destination_scope: str,
        confidence: float | None,
        classification: str,
        contradiction: bool,
        acl_widening: bool,
        operation: str,
        stale_base: bool,
    ) -> RiskDecision:
        reasons: list[str] = []
        if destination_scope == "organization":
            reasons.append("organization-promotion")
        if confidence is not None and confidence < self.low_confidence_threshold:
            reasons.append("low-confidence")
        if classification in {"confidential", "restricted"}:
            reasons.append("sensitive")
        if contradiction:
            reasons.append("contradiction")
        if acl_widening:
            reasons.append("acl-widening")
        if operation in {"retract", "purge"}:
            reasons.append(operation)
        if stale_base:
            reasons.append("stale-base")
        return RiskDecision(bool(reasons), tuple(reasons))


class ProposalService:
    def __init__(self, engine: MemoryEngine, policy: ReviewPolicy | None = None):
        self.engine = engine
        self.policy = policy or ReviewPolicy()

    def propose(
        self,
        *,
        principal: Principal,
        space_id: str,
        scope: str,
        assertion: dict[str, Any],
        evidence_refs: list[str],
        acl: dict[str, Any],
        expected_stream_version: int | None = None,
    ) -> dict[str, Any]:
        if scope == "private":
            raise MemoryError("Team proposals must target a shared scope.")
        if not can_contribute(principal, space_id):
            raise MemoryError("The principal cannot contribute to this space.")
        assertion_id = str(assertion.get("assertionId") or uuid7())
        stream_id = f"assertion:{space_id}:{assertion_id}"
        current = self.engine.events.stream_version(stream_id)
        stale = expected_stream_version is not None and expected_stream_version != current
        decision = self.policy.evaluate(
            destination_scope=scope,
            confidence=assertion.get("confidence"),
            classification=str(acl.get("classification") or "internal"),
            contradiction=bool(assertion.get("contradiction", False)),
            acl_widening=bool(assertion.get("aclWidening", False)),
            operation=str(assertion.get("operation") or "upsert"),
            stale_base=stale,
        )
        event = MemoryEvent(
            event_type="assertion.proposed",
            stream_id=stream_id,
            idempotency_key=str(assertion.get("idempotencyKey") or f"proposal:{assertion_id}:{uuid7()}"),
            actor=EventActor(type="user", id=principal.id),
            plugin=PluginRef(id="team-contribution", version="1.0.0"),
            scope=scope,  # type: ignore[arg-type]
            space_id=space_id,
            evidence_refs=evidence_refs,
            acl=normalize_acl(acl, owner=principal.id, space_id=space_id),
            payload={
                **assertion,
                "vault": shared_vault_slug(space_id),
                "assertionId": assertion_id,
                "status": "proposed",
                "reviewRequired": decision.review_required,
                "reviewReasons": list(decision.reasons),
                "expectedStreamVersion": expected_stream_version,
            },
        )
        # A stale edit is preserved as a proposal instead of being merged into the
        # assertion stream whose base has changed.
        append_stream = stream_id if not stale else f"conflict:{stream_id}:{event.event_id}"
        event.stream_id = append_stream
        persisted, _ = self.engine.append(event, expected_stream_version=0 if stale else current)
        return persisted.to_dict()

    def review(
        self,
        *,
        principal: Principal,
        proposal_event_id: str,
        decision: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        if not principal.has_any_role(Role.ADMIN, Role.CURATOR):
            raise MemoryError("Only a curator or admin can review a proposal.")
        proposal = self.engine.events.get(proposal_event_id)
        if proposal is None or proposal.event_type != "assertion.proposed":
            raise MemoryError(f"Unknown proposal: {proposal_event_id}")
        if proposal.scope != "private":
            raise MemoryError("Shared proposals must be reviewed by the Team server.")
        if decision not in {"accept", "reject", "dispute", "retract"}:
            raise MemoryError(f"Unsupported review decision: {decision}")
        latest = self.engine.events.latest_stream_event(proposal.stream_id)
        review_key = f"review:{proposal.event_id}:{decision}"
        if latest is not None and latest.idempotency_key == review_key:
            return latest.to_dict()
        transitions = {
            "assertion.proposed": {"accept", "reject", "dispute"},
            "assertion.disputed": {"accept", "retract"},
            "assertion.accepted": {"retract"},
        }
        current_state = latest.event_type if latest else "missing"
        if decision not in transitions.get(current_state, set()):
            raise MemoryError(f"Decision {decision} is invalid from state {current_state}.")
        event_type = {
            "accept": "assertion.accepted",
            "reject": "assertion.rejected",
            "dispute": "assertion.disputed",
            "retract": "assertion.retracted",
        }[decision]
        payload = dict(proposal.payload)
        payload.update(
            {
                "status": event_type.rsplit(".", 1)[1],
                "proposalEventId": proposal.event_id,
                "reviewedBy": principal.id,
                "reviewedAt": utc_now(),
                "reviewReason": reason,
            }
        )
        if event_type == "assertion.accepted" and not proposal.evidence_refs:
            procedural_human = payload.get("kind") == "procedural" and proposal.actor.type == "user"
            if not procedural_human:
                raise MemoryError("Accepted assertions require evidence.")
        current = self.engine.events.stream_version(proposal.stream_id)
        reviewed = MemoryEvent(
            event_type=event_type,
            stream_id=proposal.stream_id,
            idempotency_key=review_key,
            actor=EventActor(type="user", id=principal.id),
            plugin=PluginRef(id="team-review", version="1.0.0"),
            scope=proposal.scope,
            space_id=proposal.space_id,
            evidence_refs=proposal.evidence_refs,
            acl=proposal.acl,
            payload=payload,
            causation_id=proposal.event_id,
        )
        persisted, _ = self.engine.append(reviewed, expected_stream_version=current)
        return persisted.to_dict()


class TeamClient:
    def __init__(
        self,
        engine: MemoryEngine,
        server_url: str,
        token_provider: Callable[[], str],
        *,
        timeout_seconds: int = 30,
    ) -> None:
        self.engine = engine
        parsed = urllib.parse.urlparse(server_url)
        if not parsed.hostname:
            raise MemoryError("Team server URL must be an absolute HTTP(S) URL.")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise MemoryError("Team server URL cannot contain credentials, a query, or a fragment.")
        local_http = parsed.scheme == "http" and parsed.hostname in {
            "localhost", "127.0.0.1", "::1"
        }
        if parsed.scheme != "https" and not local_http:
            raise MemoryError("Team server URL must use HTTPS outside local development.")
        if parsed.path not in {"", "/"}:
            raise MemoryError("Team server URL must not include an API path.")
        self.server_url = server_url.rstrip("/")
        self.token_provider = token_provider
        self.timeout_seconds = timeout_seconds

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str = "application/json",
    ) -> tuple[int, bytes, dict[str, str]]:
        request = urllib.request.Request(
            self.server_url + path,
            data=body,
            method=method,
            headers={
                "Authorization": "Bearer " + self.token_provider(),
                "Content-Type": content_type,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return response.status, response.read(), dict(response.headers.items())
        except urllib.error.HTTPError as exc:
            if method == "HEAD" and exc.code == 404:
                return 404, b"", dict(exc.headers.items())
            detail = exc.read().decode("utf-8", errors="replace")
            raise MemoryError(f"Team server returned HTTP {exc.code}: {detail}") from exc
        except OSError as exc:
            raise MemoryError(f"Team server is unavailable: {exc}") from exc

    def sync(self, *, pull_cursor: int | None = None, limit: int = 100) -> dict[str, Any]:
        _, session_body, _ = self._request("GET", "/v1/session")
        session = json.loads(session_body)
        session["serverUrl"] = self.server_url
        session_path = team_session_path(self.engine.root)
        session_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".team-session.", suffix=".tmp", dir=session_path.parent
        )
        temporary_session = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(session, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_session, session_path)
            if os.name != "nt":
                directory = os.open(session_path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            temporary_session.unlink(missing_ok=True)
        checkpoint_id = "team-client:" + self.server_url
        if pull_cursor is None:
            saved_cursor = self.engine.events.connector_checkpoint(checkpoint_id, "events")
            pull_cursor = int(saved_cursor.get("position", 0)) if isinstance(saved_cursor, dict) else 0
        pushed = rejected = push_conflicts = 0
        for event in self.engine.events.pending_outbox(limit):
            if event.scope == "private":
                self.engine.events.mark_outbox(event.event_id, "rejected", error="private events never leave the device")
                rejected += 1
                continue
            for reference in event.evidence_refs:
                digest = reference.split(":", 1)[1]
                status, _, _ = self._request("HEAD", f"/v1/blobs/{digest}")
                if status == 404:
                    content = self.engine.evidence.path(reference).read_bytes()
                    self._request("PUT", f"/v1/blobs/{digest}", body=content, content_type="application/octet-stream")
            payload = json.dumps({"events": [event.to_dict()]}, ensure_ascii=False).encode("utf-8")
            try:
                _, response, _ = self._request("POST", "/v1/events:append", body=payload)
                result = json.loads(response)
                receipt = result["events"][0]
                if receipt.get("conflictProposalId"):
                    self.engine.events.mark_outbox(
                        event.event_id,
                        "rejected",
                        error=f"conflict proposal {receipt['conflictProposalId']}",
                        remote_position=int(receipt["position"]),
                    )
                    push_conflicts += 1
                    continue
                self.engine.events.mark_outbox(
                    event.event_id,
                    "accepted",
                    remote_position=int(receipt["position"]),
                )
                pushed += 1
            except MemoryError as exc:
                self.engine.events.mark_outbox(event.event_id, "retry", error=str(exc))
                break
        query = urllib.parse.urlencode({"cursor": pull_cursor, "limit": limit})
        _, response, _ = self._request("GET", f"/v1/events?{query}")
        remote = json.loads(response)
        pulled = duplicates = conflicts = 0
        for raw in remote.get("events", []):
            event = MemoryEvent.from_dict(raw)
            existing = self.engine.events.get_by_idempotency_key(event.idempotency_key)
            if existing:
                if idempotency_fingerprint(existing) != idempotency_fingerprint(event):
                    raise MemoryError(f"Remote idempotency collision: {event.idempotency_key}")
                duplicates += 1
                continue
            current = self.engine.events.stream_version(event.stream_id)
            if event.stream_version != current + 1:
                conflicts += 1
                continue
            for reference in event.evidence_refs:
                if self.engine.evidence.has(reference):
                    continue
                digest = reference.split(":", 1)[1]
                _, content, headers = self._request("GET", f"/v1/blobs/{digest}")
                media_type = headers.get("Content-Type", headers.get("content-type", "application/octet-stream"))
                stored = self.engine.evidence.put_bytes(content, media_type=media_type.split(";", 1)[0])
                if stored.reference != reference:
                    raise MemoryError(f"Team server returned invalid evidence for {reference}.")
            if event.event_type in {
                "source.captured", "source.revised", "source.audio.captured", "source.published",
                "transcription.created", "assertion.accepted",
            } and event.payload.get("vault"):
                vault_slug = str(event.payload["vault"])
                expected_vault = shared_vault_slug(event.space_id)
                if vault_slug != expected_vault:
                    raise MemoryError(
                        f"Remote shared event targets {vault_slug}; expected isolated vault {expected_vault}."
                    )
                ensure_team_vault(self.engine.root, event.space_id)
            if event.event_type == "projection.edit.accepted":
                normalized_path = str(event.payload.get("path") or "").replace("\\", "/").lstrip("./")
                expected_prefix = shared_vault_slug(event.space_id) + "/"
                if not normalized_path.startswith(expected_prefix) or ".." in normalized_path.split("/"):
                    raise MemoryError(
                        f"Remote projection edit escapes isolated Team vault {expected_prefix}."
                    )
                ensure_team_vault(self.engine.root, event.space_id)
            self.engine.append(event, expected_stream_version=current, enqueue=False)
            pulled += 1
        returned_cursor = int(remote.get("cursor", pull_cursor))
        if conflicts == 0:
            self.engine.events.set_connector_checkpoint(
                checkpoint_id,
                "events",
                {"position": returned_cursor},
                self.engine.events.latest_position(),
            )
        heartbeat_reported = False
        heartbeat_error: str | None = None
        try:
            # This is deliberately a one-way, per-identity device fingerprint:
            # operational telemetry identifies neither a filesystem path nor a
            # reusable machine identifier, and two accounts on one device do
            # not overwrite one another's status row.
            fingerprint_input = f"{self.engine.root.resolve()}\x00{session['principalId']}"
            client_id = hashlib.sha256(fingerprint_input.encode("utf-8")).hexdigest()
            outbox_counts = self.engine.events.outbox_status_counts()
            self._request(
                "POST",
                "/v1/replication/status",
                body=json.dumps(
                    {
                        "clientId": client_id,
                        "pullCursor": returned_cursor,
                        "outboxPending": outbox_counts.get("pending", 0) + outbox_counts.get("retry", 0),
                    },
                    separators=(",", ":"),
                ).encode("utf-8"),
            )
            heartbeat_reported = True
        except MemoryError as exc:
            # The data sync result remains truthful: an observability failure is
            # surfaced separately and never fabricated as a healthy heartbeat.
            heartbeat_error = str(exc)
        return {
            "ok": conflicts == 0 and push_conflicts == 0,
            "pushed": pushed,
            "rejected": rejected,
            "pulled": pulled,
            "duplicates": duplicates,
            "conflicts": conflicts,
            "pushConflicts": push_conflicts,
            "cursor": returned_cursor,
            "heartbeatReported": heartbeat_reported,
            "heartbeatError": heartbeat_error,
        }


def detach_team(root: Path) -> dict[str, Any]:
    changed: list[str] = []
    for entry in load_registry(root).get("vaults", []):
        vault_path, vault = load_vault(root, str(entry["slug"]))
        team = dict(vault.get("team") or {})
        if not team.get("managed"):
            continue
        team["read_only"] = True
        team["detached_at"] = utc_now()
        vault["team"] = team
        write_data(vault_path / "vault.yaml", vault)
        changed.append(str(entry["slug"]))
    return {"ok": True, "sharedVaultsReadOnly": changed, "privateMemoryChanged": False}
