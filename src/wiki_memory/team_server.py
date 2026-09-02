from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from .config import MemoryError, utc_now
from .evidence import parse_reference
from .events import EventActor, MemoryEvent, PluginRef, uuid7
from .object_store import FileObjectStore, ObjectStore, S3ObjectStore, stream_and_close
from .oidc import OIDCConfig, OIDCVerifier
from .team import (
    Principal, ReviewPolicy, Role, can_contribute, can_read, derive_acl, normalize_acl, shared_vault_slug,
)
from .team_repository import PostgresTeamRepository, TeamRepository


# Connector events are the one route by which a client can assert that an
# external program collected organization data. Team therefore accepts only
# the bundled source identities or explicit administrator approvals. A client
# cannot grant itself access simply by putting an arbitrary plugin id in an
# event payload.
OFFICIAL_TEAM_CONNECTOR_PLUGINS = frozenset({"source-postgres", "source-social-browser", "source-audio"})


def approved_team_connector_plugins_from_environment() -> frozenset[str]:
    raw = os.environ.get("WIKI_MEMORY_TEAM_APPROVED_PLUGIN_IDS", "[]").strip() or "[]"
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MemoryError("WIKI_MEMORY_TEAM_APPROVED_PLUGIN_IDS must be a JSON array.") from exc
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise MemoryError("WIKI_MEMORY_TEAM_APPROVED_PLUGIN_IDS must be a JSON array of plugin IDs.")
    return OFFICIAL_TEAM_CONNECTOR_PLUGINS | frozenset(item.strip() for item in value)


def database_dsn_from_environment() -> str:
    dsn = os.environ.get("DATABASE_URL")
    if dsn:
        return dsn
    names = {
        "host": "DATABASE_HOST",
        "port": "DATABASE_PORT",
        "dbname": "DATABASE_NAME",
        "user": "DATABASE_USER",
        "password": "DATABASE_PASSWORD",
    }
    values = {
        parameter: os.environ.get(environment)
        for parameter, environment in names.items()
    }
    values["port"] = values["port"] or "5432"
    missing = [
        names[parameter]
        for parameter in ("host", "dbname", "user", "password")
        if not values[parameter]
    ]
    if missing:
        raise MemoryError(
            "Configure DATABASE_URL or all split database settings; missing: "
            + ", ".join(missing)
        )
    try:
        from psycopg.conninfo import make_conninfo
    except ImportError as exc:
        raise MemoryError("Team server requires the 'server' optional dependencies.") from exc
    return make_conninfo(**values)


def repository_from_environment() -> TeamRepository:
    dsn = database_dsn_from_environment()
    repository = PostgresTeamRepository(dsn)
    repository.initialize()
    return repository


def object_store_from_environment() -> ObjectStore:
    bucket = os.environ.get("S3_BUCKET")
    if bucket:
        return S3ObjectStore(
            bucket=bucket,
            endpoint_url=os.environ.get("S3_ENDPOINT_URL"),
            region=os.environ.get("S3_REGION"),
            prefix=os.environ.get("S3_PREFIX", "blobs/sha256"),
        )
    local = os.environ.get("TEAM_FILE_OBJECT_STORE")
    if local:
        return FileObjectStore(Path(local))
    raise MemoryError("S3_BUCKET is required unless TEAM_FILE_OBJECT_STORE is explicitly configured for development.")


def oidc_from_environment() -> OIDCVerifier | None:
    issuer = os.environ.get("OIDC_ISSUER")
    audience = os.environ.get("OIDC_AUDIENCE")
    if not issuer or not audience:
        return None
    raw_mapping = os.environ.get("OIDC_GROUP_SPACE_MAP", "{}").strip() or "{}"
    try:
        mapping_value = json.loads(raw_mapping)
        if not isinstance(mapping_value, dict):
            raise TypeError("mapping must be an object")
        group_space_map = {
            str(group): tuple(str(space) for space in (spaces if isinstance(spaces, list) else [spaces]))
            for group, spaces in mapping_value.items()
        }
    except (json.JSONDecodeError, TypeError) as exc:
        raise MemoryError(f"OIDC_GROUP_SPACE_MAP must be a JSON object: {exc}") from exc
    return OIDCVerifier(
        OIDCConfig(
            issuer=issuer,
            audience=audience,
            jwks_url=os.environ.get("OIDC_JWKS_URL"),
            groups_claim=os.environ.get("OIDC_GROUPS_CLAIM", "groups"),
            roles_claim=os.environ.get("OIDC_ROLES_CLAIM", "roles"),
            group_space_map=group_space_map,
        )
    )


def create_app(
    repository: TeamRepository | None = None,
    object_store: ObjectStore | None = None,
    oidc: OIDCVerifier | None = None,
    restore_attestation_token: str | None = None,
):
    try:
        from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
        from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
    except ImportError as exc:
        raise MemoryError("Team server requires the 'server' optional dependencies.") from exc
    # Annotations are postponed in this module. FastAPI resolves them through module
    # globals, while these optional imports intentionally happen inside the factory.
    globals().update(
        {
            "Request": Request,
            "Response": Response,
            "HTMLResponse": HTMLResponse,
            "PlainTextResponse": PlainTextResponse,
            "StreamingResponse": StreamingResponse,
        }
    )

    def configured_limit(name: str, default: int, *, minimum: int, maximum: int) -> int:
        try:
            value = int(os.environ.get(name, str(default)))
        except ValueError as exc:
            raise MemoryError(f"{name} must be an integer between {minimum} and {maximum}.") from exc
        if not minimum <= value <= maximum:
            raise MemoryError(f"{name} must be an integer between {minimum} and {maximum}.")
        return value

    repository = repository or repository_from_environment()
    object_store = object_store or object_store_from_environment()
    approved_connector_plugins = approved_team_connector_plugins_from_environment()
    oidc = oidc if oidc is not None else oidc_from_environment()
    repository.initialize()
    bootstrap_token = os.environ.get("WIKI_MEMORY_BOOTSTRAP_TOKEN")
    restore_attestation_token = (
        restore_attestation_token
        if restore_attestation_token is not None
        else os.environ.get("WIKI_MEMORY_RESTORE_ATTESTATION_TOKEN")
    )
    max_json_bytes = configured_limit(
        "WIKI_MEMORY_MAX_JSON_BYTES", 16 * 1024 * 1024, minimum=1024, maximum=64 * 1024 * 1024
    )
    max_blob_bytes = configured_limit(
        "WIKI_MEMORY_MAX_BLOB_BYTES", 256 * 1024 * 1024, minimum=1024, maximum=1024 * 1024 * 1024
    )
    max_events_per_append = configured_limit("WIKI_MEMORY_MAX_EVENTS_PER_APPEND", 100, minimum=1, maximum=1000)
    app = FastAPI(title="Wiki Memory Team", version="1.0.0")
    metrics_lock = threading.Lock()
    request_metrics: dict[tuple[str, int], list[float]] = {}

    @app.middleware("http")
    async def observe_requests(request: Request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        elapsed = time.perf_counter() - started
        route_object = request.scope.get("route")
        route = str(getattr(route_object, "path", request.url.path))
        with metrics_lock:
            bucket = request_metrics.setdefault((route, response.status_code), [0.0, 0.0])
            bucket[0] += 1
            bucket[1] += elapsed
        return response

    def principal(authorization: str | None = Header(default=None)) -> Principal:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Bearer token required")
        token = authorization.split(" ", 1)[1]
        if bootstrap_token and secrets.compare_digest(token, bootstrap_token):
            return Principal("bootstrap-admin", frozenset({Role.ADMIN}), frozenset(), kind="break-glass")
        if oidc is None:
            raise HTTPException(status_code=503, detail="OIDC is not configured and no valid bootstrap token was supplied")
        try:
            return oidc.verify(token)
        except MemoryError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    def require_curator(user: Principal = Depends(principal)) -> Principal:
        if not user.has_any_role(Role.ADMIN, Role.CURATOR):
            raise HTTPException(status_code=403, detail="Curator role required")
        return user

    def require_admin(user: Principal = Depends(principal)) -> Principal:
        if not user.has_any_role(Role.ADMIN):
            raise HTTPException(status_code=403, detail="Admin role required")
        return user

    async def json_body(request: Request) -> dict[str, Any]:
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > max_json_bytes:
                    raise HTTPException(status_code=413, detail="JSON request exceeds configured size limit")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid Content-Length header") from exc
        raw = await request.body()
        if len(raw) > max_json_bytes:
            raise HTTPException(status_code=413, detail="JSON request exceeds configured size limit")
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON request") from exc
        if not isinstance(value, dict):
            raise HTTPException(status_code=422, detail="JSON request must be an object")
        return value

    def required_string(value: dict[str, Any], field: str) -> str:
        raw = value.get(field)
        if not isinstance(raw, str) or not raw.strip():
            raise HTTPException(status_code=422, detail=f"{field} must be a non-empty string")
        return raw

    def optional_string(value: dict[str, Any], field: str) -> str | None:
        raw = value.get(field)
        if raw is None:
            return None
        if not isinstance(raw, str):
            raise HTTPException(status_code=422, detail=f"{field} must be a string")
        return raw

    def object_value(value: dict[str, Any], field: str, *, default: dict[str, Any] | None = None) -> dict[str, Any]:
        raw = value.get(field, default if default is not None else {})
        if raw is None:
            raw = default if default is not None else {}
        if not isinstance(raw, dict):
            raise HTTPException(status_code=422, detail=f"{field} must be an object")
        return dict(raw)

    def bounded_integer(value: dict[str, Any], field: str, *, default: int, minimum: int, maximum: int) -> int:
        raw = value.get(field, default)
        if isinstance(raw, bool):
            raise HTTPException(status_code=422, detail=f"{field} must be an integer")
        try:
            parsed = int(raw)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=f"{field} must be an integer") from exc
        if not minimum <= parsed <= maximum:
            raise HTTPException(status_code=422, detail=f"{field} must be between {minimum} and {maximum}")
        return parsed

    def verify_object(digest: str) -> bool:
        """Verify evidence without exposing an object-store outage as a 500.

        A false return is a normal missing/corrupt object. A provider failure
        is operationally distinct: clients must retry later, never infer that
        evidence is absent or that their Team contribution was accepted.
        """

        try:
            return object_store.verify(digest)
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Evidence storage is unavailable") from exc

    def put_object(digest: str, source: Path, media_type: str) -> None:
        try:
            object_store.put_file(digest, source, media_type)
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Evidence storage is unavailable") from exc

    def open_object(digest: str):
        try:
            return object_store.open(digest)
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Evidence storage is unavailable") from exc

    def validated_evidence_references(value: Any) -> list[str]:
        """Validate untrusted proposal references before ACL or object-store work.

        Event uploads construct ``MemoryEvent`` first, which already validates
        references. Proposal creation accepts its own compact payload, so it
        needs the same boundary check here. In particular, never split an
        untrusted string to obtain an object key: malformed input must be a
        normal 422 response rather than an internal error.
        """

        if not isinstance(value, list) or not all(isinstance(reference, str) for reference in value):
            raise HTTPException(status_code=422, detail="evidenceRefs must be an array of SHA-256 references")
        if len(set(value)) != len(value):
            raise HTTPException(status_code=422, detail="evidenceRefs cannot contain duplicates")
        try:
            for reference in value:
                parse_reference(reference)
        except MemoryError as exc:
            raise HTTPException(status_code=422, detail="evidenceRefs must contain SHA-256 references") from exc
        return value

    def validate_projection_target(event: MemoryEvent) -> None:
        def validate_relative(field: str) -> None:
            value = str(event.payload.get(field) or "")
            normalized = value.replace("\\", "/")
            if normalized.startswith("/") or ".." in normalized.split("/"):
                raise HTTPException(status_code=422, detail=f"Projection {field} must be a safe relative path")

        if event.event_type == "projection.edit.proposed":
            validate_relative("path")
            normalized = str(event.payload.get("path") or "").replace("\\", "/").lstrip("./")
            expected_prefix = shared_vault_slug(event.space_id) + "/"
            if not normalized.startswith(expected_prefix):
                raise HTTPException(
                    status_code=422,
                    detail=f"Shared projection edits must remain under {expected_prefix}",
                )
            return
        projected_types = {
            "source.captured", "source.revised", "source.deleted", "source.audio.captured",
            "source.published", "source.publication.proposed", "transcription.created", "assertion.proposed",
        }
        if event.event_type not in projected_types:
            return
        vault = str(event.payload.get("vault") or "")
        expected = shared_vault_slug(event.space_id)
        if vault != expected:
            raise HTTPException(status_code=422, detail=f"Shared projections must target isolated vault {expected}")
        for field in ("path", "partition"):
            validate_relative(field)

    def derived_proposal_acl(
        user: Principal,
        evidence_refs: list[str],
        requested_acl: dict[str, Any],
        space_id: str,
    ) -> dict[str, Any]:
        evidence_acls: list[dict[str, Any]] = []
        for reference in evidence_refs:
            digest = reference.split(":", 1)[1]
            candidates = [
                event
                for event in repository.events_referencing_blob(digest)
                if space_id in event.acl.get("spaces", [])
                and can_read(user, scope=event.scope, space_id=event.space_id, acl=event.acl)
            ]
            if not candidates:
                raise HTTPException(
                    status_code=409,
                    detail=f"Evidence {reference} has no authorized event in destination space",
                )
            evidence_acls.extend(event.acl for event in candidates)
        return derive_acl([*evidence_acls, requested_acl], owner=user.id, destination_space=space_id)

    def result_evidence_is_verified(result: dict[str, Any]) -> bool:
        """Keep a derived search result behind its canonical, intact proof.

        A search document is only a rebuildable projection. Checking after the
        SQL ACL predicate preserves the no-leak boundary while preventing an
        old projection from being presented if an object was removed or its
        bytes no longer hash to its evidence reference.
        """

        references = result.get("evidenceRefs")
        if not isinstance(references, list) or not all(isinstance(item, str) for item in references):
            return False
        return all(object_store.verify(parse_reference(reference)) for reference in references)

    def validate_reused_evidence_acl(user: Principal, event: MemoryEvent) -> None:
        """Prevent a known content hash from becoming an ACL bypass.

        Evidence is content-addressed, so two sources with identical bytes have
        the same object key.  A contributor who guesses or observes such a key
        must not attach a restricted object to a broader/cross-space event.
        The first Team reference is unrestricted by this check; every later
        reference must be derived from provenance the contributor can read.
        """

        for reference in event.evidence_refs:
            digest = reference.split(":", 1)[1]
            prior = repository.events_referencing_blob(digest)
            if not prior:
                continue
            readable = [
                source
                for source in prior
                if can_read(user, scope=source.scope, space_id=source.space_id, acl=source.acl)
            ]
            if not readable:
                raise HTTPException(
                    status_code=403,
                    detail="Evidence cannot be reused without authorized provenance",
                )
            try:
                derived = derive_acl(
                    [source.acl for source in readable],
                    owner=event.actor.id,
                    destination_space=event.space_id,
                )
            except MemoryError as exc:
                raise HTTPException(
                    status_code=409,
                    detail="Evidence cannot be republished outside its authorized spaces",
                ) from exc
            if derived != event.acl:
                raise HTTPException(
                    status_code=422,
                    detail="Event ACL is broader than its reused evidence ACL",
                )

    def publication_target(event: MemoryEvent) -> tuple[str, dict[str, Any]]:
        """Validate the deferred target of an organization publication.

        A proposal itself is a Team-space object.  This function parses the
        exact ACL the curator is allowed to promote only after reviewing it.
        """

        target = event.payload.get("publicationTarget")
        if not isinstance(target, dict) or str(target.get("scope") or "") != "organization":
            raise HTTPException(status_code=422, detail="Organization publication proposal requires publicationTarget")
        raw_acl = target.get("acl")
        if not isinstance(raw_acl, dict):
            raise HTTPException(status_code=422, detail="publicationTarget.acl must be an object")
        normalized = normalize_acl(raw_acl, owner=event.actor.id, space_id=event.space_id)
        if normalized != raw_acl or normalized["audience"] != "organization":
            raise HTTPException(
                status_code=422,
                detail="publicationTarget ACL must be normalized organization visibility",
            )
        return "organization", normalized

    @app.exception_handler(MemoryError)
    async def memory_error_handler(_: Request, exc: MemoryError):
        return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})

    @app.get("/v1/health")
    def health():
        checks: dict[str, bool] = {"database": False, "objectStore": False}
        try:
            repository.healthcheck()
            checks["database"] = True
        except Exception:
            # Do not reveal DSNs, credentials, bucket names, or provider errors
            # through an unauthenticated health endpoint.
            pass
        try:
            # A syntactically valid impossible digest exercises the same storage
            # client path as real evidence without needing a sentinel object.
            object_store.has("0" * 64)
            checks["objectStore"] = True
        except Exception:
            pass
        payload = {"ok": all(checks.values()), "service": "wiki-memory-team", "time": utc_now(), "checks": checks}
        return JSONResponse(status_code=200 if payload["ok"] else 503, content=payload)

    @app.get("/v1/session")
    def session(user: Principal = Depends(principal)) -> dict[str, Any]:
        return {
            "principalId": user.id,
            "kind": user.kind,
            "roles": sorted(role.value for role in user.roles),
            "spaces": sorted(user.spaces),
            "groups": sorted(user.groups),
            "checkedAt": utc_now(),
        }

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics(_: Principal = Depends(require_admin)) -> str:
        lines = ["# TYPE wiki_memory_events_total gauge"]
        for name, value in sorted(repository.operational_metrics().items()):
            lines.append(f"{name} {value}")
        with metrics_lock:
            snapshot = dict(request_metrics)
        lines.extend(["# TYPE wiki_memory_http_requests_total counter", "# TYPE wiki_memory_http_request_seconds_total counter"])
        for (route, status), values in sorted(snapshot.items()):
            safe_route = route.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'wiki_memory_http_requests_total{{route="{safe_route}",status="{status}"}} {values[0]}')
            lines.append(f'wiki_memory_http_request_seconds_total{{route="{safe_route}",status="{status}"}} {values[1]}')
        return "\n".join(lines) + "\n"

    @app.get("/console", response_class=HTMLResponse)
    def console() -> HTMLResponse:
        page = (Path(__file__).parent / "team_console" / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(
            page,
            headers={
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
            },
        )

    @app.get("/console/style.css", response_class=PlainTextResponse)
    def console_style() -> PlainTextResponse:
        content = (Path(__file__).parent / "team_console" / "style.css").read_text(encoding="utf-8")
        return PlainTextResponse(content, media_type="text/css", headers={"Cache-Control": "no-store"})

    @app.get("/console/app.js", response_class=PlainTextResponse)
    def console_script() -> PlainTextResponse:
        content = (Path(__file__).parent / "team_console" / "app.js").read_text(encoding="utf-8")
        return PlainTextResponse(
            content,
            media_type="application/javascript",
            headers={"Cache-Control": "no-store"},
        )

    @app.head("/v1/blobs/{digest}")
    def head_blob(digest: str, user: Principal = Depends(principal)) -> Response:
        if not verify_object(digest.lower()):
            return Response(status_code=404)
        references = repository.events_referencing_blob(digest.lower())
        visible = any(can_read(user, scope=event.scope, space_id=event.space_id, acl=event.acl) for event in references)
        return Response(status_code=200 if visible else 404)

    @app.put("/v1/blobs/{digest}")
    async def put_blob(digest: str, request: Request, user: Principal = Depends(principal)) -> dict[str, Any]:
        if not user.has_any_role(Role.ADMIN, Role.CURATOR, Role.CONTRIBUTOR, Role.SERVICE):
            raise HTTPException(status_code=403, detail="Contributor role required for blob upload")
        digest = digest.lower()
        hasher = hashlib.sha256()
        size = 0
        file_descriptor, temporary_name = tempfile.mkstemp(prefix="team-blob-")
        try:
            with os.fdopen(file_descriptor, "wb") as handle:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > max_blob_bytes:
                        raise HTTPException(status_code=413, detail="Blob exceeds configured size limit")
                    hasher.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if hasher.hexdigest() != digest:
                raise HTTPException(status_code=422, detail="Blob checksum mismatch")
            put_object(digest, Path(temporary_name), request.headers.get("content-type", "application/octet-stream"))
        finally:
            Path(temporary_name).unlink(missing_ok=True)
        return {"ok": True, "reference": f"sha256:{digest}"}

    @app.post("/v1/captures")
    async def capture(request: Request, user: Principal = Depends(principal)) -> dict[str, Any]:
        payload = await json_body(request)
        text = required_string(payload, "text")
        space_id = required_string(payload, "spaceId")
        scope = optional_string(payload, "scope") or "team"
        if scope not in {"team", "organization"}:
            raise HTTPException(status_code=422, detail="text, spaceId and a shared scope are required")
        title = optional_string(payload, "title")
        source_type = optional_string(payload, "sourceType")
        source_url = optional_string(payload, "url")
        idempotency_key = optional_string(payload, "idempotencyKey")
        requested_capture_acl = object_value(payload, "acl")
        if len(text.encode("utf-8")) > 10 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Text capture exceeds 10 MiB")
        if not can_contribute(user, space_id):
            raise HTTPException(status_code=403, detail=f"Cannot contribute to {space_id}")
        vault_slug = shared_vault_slug(space_id)
        original = {
            "text": text,
            "title": title,
            "url": source_url,
            "sourceType": source_type or "text",
        }
        content = json.dumps(original, ensure_ascii=False, sort_keys=True).encode("utf-8")
        digest = hashlib.sha256(content).hexdigest()
        file_descriptor, temporary_name = tempfile.mkstemp(prefix="team-capture-")
        try:
            with os.fdopen(file_descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            put_object(digest, Path(temporary_name), "application/json")
        finally:
            Path(temporary_name).unlink(missing_ok=True)
        source_id = digest[:16]
        if scope == "organization" and "audience" not in requested_capture_acl:
            requested_capture_acl["audience"] = "organization"
        target_acl = normalize_acl(requested_capture_acl, owner=user.id, space_id=space_id)
        proposal_acl = normalize_acl({}, owner=user.id, space_id=space_id)
        organization_promotion = scope == "organization"
        event = MemoryEvent(
            event_type="source.publication.proposed" if organization_promotion else "source.captured",
            stream_id=f"source:{space_id}:{source_id}",
            idempotency_key=idempotency_key or f"capture:{space_id}:{digest}",
            actor=EventActor(type="user", id=user.id),
            plugin=PluginRef(id="team-contribution", version="1.0.0"),
            scope="team" if organization_promotion else scope,  # type: ignore[arg-type]
            space_id=space_id,
            evidence_refs=[f"sha256:{digest}"],
            acl=proposal_acl if organization_promotion else target_acl,
            payload={
                "sourceId": source_id,
                "vault": vault_slug,
                "partition": "team",
                "title": title or "Team capture",
                "body": text,
                "metadata": {
                    "source_type": source_type or "text",
                    "source_url": source_url,
                    "connector": "team",
                    "content_hash": digest,
                    "epistemic_status": "unverified",
                },
                "status": "proposed" if organization_promotion else "captured",
                "reviewRequired": organization_promotion,
                "reviewReasons": ["organization-promotion"] if organization_promotion else [],
                **({"publicationTarget": {"scope": "organization", "acl": target_acl}} if organization_promotion else {}),
            },
        )
        validate_reused_evidence_acl(user, event)
        persisted, created = repository.append(event, expected_stream_version=0)
        repository.audit(user.id, "capture.create", persisted.event_id, space_id, {"created": created})
        return {"ok": True, "created": created, "event": persisted.to_dict()}

    @app.get("/v1/blobs/{digest}")
    def get_blob(digest: str, user: Principal = Depends(principal)):
        if not verify_object(digest.lower()):
            raise HTTPException(status_code=404, detail="Blob not found")
        references = repository.events_referencing_blob(digest.lower())
        if not any(can_read(user, scope=event.scope, space_id=event.space_id, acl=event.acl) for event in references):
            raise HTTPException(status_code=403, detail="Blob is outside authorized spaces")
        handle = open_object(digest.lower())
        return StreamingResponse(stream_and_close(handle), media_type="application/octet-stream")

    @app.post("/v1/events:append")
    async def append_events(request: Request, user: Principal = Depends(principal)) -> dict[str, Any]:
        payload = await json_body(request)
        raw_events = payload.get("events") if isinstance(payload, dict) else None
        if not isinstance(raw_events, list) or not raw_events:
            raise HTTPException(status_code=422, detail="events must be a non-empty array")
        if len(raw_events) > max_events_per_append:
            raise HTTPException(
                status_code=413,
                detail=f"Event batch exceeds configured limit of {max_events_per_append}",
            )
        receipts: list[dict[str, Any]] = []
        for raw in raw_events:
            event = MemoryEvent.from_dict(raw)
            if event.scope == "private":
                raise HTTPException(status_code=422, detail="Private events cannot be uploaded")
            if event.scope == "organization" and event.event_type not in {
                "assertion.proposed", "source.publication.proposed"
            }:
                raise HTTPException(status_code=409, detail="Organization publication must pass curator review")
            if event.event_type == "source.publication.proposed":
                if event.scope != "team":
                    raise HTTPException(
                        status_code=409,
                        detail="Organization publication proposals remain Team-scoped until curator acceptance",
                    )
                publication_target(event)
            if event.stream_version < 1:
                raise HTTPException(status_code=422, detail="Uploaded events require their exact positive streamVersion")
            if not can_contribute(user, event.space_id):
                raise HTTPException(status_code=403, detail=f"Cannot contribute to {event.space_id}")
            validate_projection_target(event)
            if event.actor.id != user.id and not user.has_any_role(Role.ADMIN):
                raise HTTPException(status_code=403, detail="Event actor does not match authenticated principal")
            if event.actor.type == "connector":
                if user.kind != "service":
                    repository.audit(
                        user.id,
                        "event.connector_identity_denied",
                        event.event_id,
                        event.space_id,
                        {"plugin": event.plugin.id},
                    )
                    raise HTTPException(status_code=403, detail="Connector events require a service identity")
                if event.plugin.id not in approved_connector_plugins:
                    repository.audit(
                        user.id,
                        "event.plugin_denied",
                        event.event_id,
                        event.space_id,
                        {"plugin": event.plugin.id},
                    )
                    raise HTTPException(status_code=403, detail="Connector plugin is not approved for this Team")
            privileged_types = {
                "assertion.accepted", "assertion.rejected", "assertion.disputed", "assertion.retracted",
                "projection.edit.accepted", "projection.edit.rejected",
            }
            if event.event_type in privileged_types:
                raise HTTPException(status_code=409, detail="Submit review decisions through the proposal review endpoint")
            normalized_acl = normalize_acl(event.acl, owner=event.actor.id, space_id=event.space_id)
            if normalized_acl != event.acl:
                raise HTTPException(status_code=422, detail="Event ACL must be normalized before upload")
            if event.event_type == "assertion.proposed" and event.evidence_refs:
                derived = derived_proposal_acl(user, event.evidence_refs, event.acl, event.space_id)
                if derived != event.acl:
                    raise HTTPException(status_code=422, detail="Proposal ACL is broader than its evidence ACL")
            elif event.evidence_refs:
                validate_reused_evidence_acl(user, event)
            missing = [ref for ref in event.evidence_refs if not verify_object(ref.split(":", 1)[1])]
            if missing:
                raise HTTPException(status_code=409, detail={"missingEvidence": missing})
            try:
                persisted, created = repository.append(event)
            except MemoryError as exc:
                if not str(exc).startswith("Stream version conflict"):
                    raise
                conflict = MemoryEvent(
                    event_type="assertion.proposed",
                    stream_id=f"conflict:{event.stream_id}:{event.event_id}",
                    idempotency_key=f"replication-conflict:{event.event_id}",
                    actor=EventActor(type="system", id="team-replication"),
                    plugin=PluginRef(id="team-replication", version="1.0.0"),
                    scope=event.scope,
                    space_id=event.space_id,
                    evidence_refs=event.evidence_refs,
                    acl=event.acl,
                    payload={
                        "kind": "replication-conflict",
                        "status": "proposed",
                        "reviewRequired": True,
                        "reviewReasons": ["stale-base"],
                        "incomingEvent": event.to_dict(),
                        "conflictReason": str(exc),
                    },
                )
                conflict_event, _ = repository.append(conflict, expected_stream_version=0)
                repository.audit(user.id, "event.conflict", event.event_id, event.space_id, {})
                receipts.append(
                    {
                        "eventId": event.event_id,
                        "position": conflict_event.position,
                        "created": False,
                        "conflictProposalId": conflict_event.event_id,
                    }
                )
                continue
            repository.audit(user.id, "event.append", persisted.event_id, persisted.space_id, {"created": created})
            receipts.append({"eventId": persisted.event_id, "position": persisted.position, "created": created})
        return {"ok": True, "events": receipts}

    @app.get("/v1/events")
    def list_events(
        cursor: int = 0,
        limit: int = 100,
        user: Principal = Depends(principal),
    ) -> dict[str, Any]:
        if limit < 1 or limit > 1000:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 1000")
        candidates = repository.list_events(
            cursor,
            limit,
            set(user.spaces),
            user.has_any_role(Role.READER, Role.CONTRIBUTOR, Role.CURATOR, Role.ADMIN),
            principal_id=user.id,
            groups=set(user.groups),
            all_access=user.has_any_role(Role.ADMIN),
        )
        events = [
            event
            for event in candidates
            if can_read(user, scope=event.scope, space_id=event.space_id, acl=event.acl)
        ]
        # Advance past filtered events as well; otherwise one inaccessible event can
        # trap a client on the same page forever without revealing its contents.
        next_cursor = max((int(event.position or cursor) for event in candidates), default=cursor)
        return {"events": [event.to_dict() for event in events], "cursor": next_cursor}

    @app.post("/v1/search")
    async def search(request: Request, user: Principal = Depends(principal)) -> dict[str, Any]:
        payload = await json_body(request)
        query = required_string(payload, "query")
        limit = bounded_integer(payload, "limit", default=10, minimum=1, maximum=100)
        candidates = repository.search(
            query,
            limit * 5,
            set(user.spaces),
            user.has_any_role(Role.READER, Role.CONTRIBUTOR, Role.CURATOR, Role.ADMIN),
            principal_id=user.id,
            groups=set(user.groups),
            all_access=user.has_any_role(Role.ADMIN),
        )
        results: list[dict[str, Any]] = []
        withheld_unverifiable_evidence = 0
        for result in candidates:
            if not can_read(user, scope=result["scope"], space_id=result["spaceId"], acl=result["acl"]):
                continue
            try:
                evidence_verified = result_evidence_is_verified(result)
            except Exception as exc:
                # A transport/provider failure is operationally distinct from
                # a known-bad blob. Do not turn it into a misleading empty
                # search response or disclose storage-provider details.
                raise HTTPException(status_code=503, detail="Evidence integrity verification is unavailable") from exc
            if not evidence_verified:
                withheld_unverifiable_evidence += 1
                continue
            results.append(result)
            if len(results) >= limit:
                break
        return {"results": results, "withheldUnverifiableEvidence": withheld_unverifiable_evidence}

    @app.get("/v1/proposals")
    def proposals(cursor: int = 0, limit: int = 100, user: Principal = Depends(principal)) -> dict[str, Any]:
        events = repository.list_events(
            cursor,
            limit * 5,
            set(user.spaces),
            user.has_any_role(Role.READER, Role.CONTRIBUTOR, Role.CURATOR, Role.ADMIN),
            principal_id=user.id,
            groups=set(user.groups),
            all_access=user.has_any_role(Role.ADMIN),
        )
        proposed: list[dict[str, Any]] = []
        for event in events:
            if event.event_type not in {
                "assertion.proposed", "projection.edit.proposed", "source.publication.proposed"
            }:
                continue
            if not can_read(user, scope=event.scope, space_id=event.space_id, acl=event.acl):
                continue
            if event.event_type == "source.publication.proposed" and repository.proposal_is_resolved(event.event_id):
                continue
            latest = repository.latest_stream_event(event.stream_id)
            if latest and latest.event_type in {
                "assertion.proposed", "assertion.disputed", "projection.edit.proposed",
                "source.publication.proposed", "source.publication.disputed",
            }:
                proposed.append(event.to_dict())
            if len(proposed) >= limit:
                break
        return {"proposals": proposed}

    @app.get("/v1/audit")
    def audit(cursor: int = 0, limit: int = 100, _: Principal = Depends(require_admin)) -> dict[str, Any]:
        limit = min(max(limit, 1), 1000)
        entries = repository.list_audit(cursor, limit)
        return {"events": entries, "cursor": max((entry["id"] for entry in entries), default=cursor)}

    @app.post("/v1/operations/restore-verifications")
    async def record_restore_verification(
        request: Request,
        user: Principal = Depends(require_admin),
        attestation: str | None = Header(default=None, alias="X-Wiki-Memory-Restore-Attestation"),
    ) -> dict[str, Any]:
        """Persist a successful or failed restore rehearsal without its contents."""

        if not restore_attestation_token:
            raise HTTPException(status_code=503, detail="Restore attestation channel is not configured")
        if not attestation or not secrets.compare_digest(attestation, restore_attestation_token):
            raise HTTPException(status_code=403, detail="Valid restore attestation token required")
        payload = await json_body(request)
        status = str(payload.get("status") or "")
        backup_id = str(payload["backupId"]) if payload.get("backupId") is not None else None
        if backup_id is not None and (not backup_id.strip() or len(backup_id) > 200):
            raise HTTPException(status_code=422, detail="backupId must be a non-empty identifier under 200 characters")

        def non_negative_count(field: str) -> int | None:
            value = payload.get(field)
            if value is None:
                return None
            if isinstance(value, bool):
                raise HTTPException(status_code=422, detail=f"{field} must be a non-negative integer")
            try:
                parsed = int(value)
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=f"{field} must be a non-negative integer") from exc
            if parsed < 0:
                raise HTTPException(status_code=422, detail=f"{field} must be a non-negative integer")
            return parsed

        detail = payload.get("detail") or {}
        if not isinstance(detail, dict) or len(json.dumps(detail, ensure_ascii=False)) > 4096:
            raise HTTPException(status_code=422, detail="detail must be a JSON object under 4 KiB")
        recorded = repository.record_restore_verification(
            user.id,
            status=status,
            backup_id=backup_id,
            event_count=non_negative_count("eventCount"),
            evidence_count=non_negative_count("evidenceCount"),
            detail=detail,
        )
        repository.audit(user.id, "restore.verification", str(recorded["id"]), None, {"status": status})
        return {"ok": True, "verification": recorded}

    @app.post("/v1/operations/rebuild-search")
    def rebuild_search_projection(user: Principal = Depends(require_admin)) -> dict[str, Any]:
        """Recreate the derived Team search projection from the immutable ledger."""

        result = repository.rebuild_search_projection(evidence_verify=object_store.verify)
        repository.audit(user.id, "projection.rebuild", "search", None, result)
        return {"ok": True, "rebuild": result}

    @app.post("/v1/replication/status")
    async def replication_status(request: Request, user: Principal = Depends(principal)) -> dict[str, Any]:
        if not user.has_any_role(Role.ADMIN, Role.CURATOR, Role.CONTRIBUTOR, Role.SERVICE):
            raise HTTPException(status_code=403, detail="Replication status requires a contributing identity")
        payload = await json_body(request)
        client_id = str(payload.get("clientId") or "")
        if not re.fullmatch(r"[a-f0-9]{32,64}", client_id):
            raise HTTPException(status_code=422, detail="clientId must be a privacy-preserving hexadecimal fingerprint")

        def non_negative(field: str) -> int:
            value = payload.get(field)
            if isinstance(value, bool):
                raise HTTPException(status_code=422, detail=f"{field} must be a non-negative integer")
            try:
                parsed = int(value)
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=f"{field} must be a non-negative integer") from exc
            if parsed < 0:
                raise HTTPException(status_code=422, detail=f"{field} must be a non-negative integer")
            return parsed

        try:
            repository.report_replication_client(
                client_id,
                user.id,
                non_negative("pullCursor"),
                non_negative("outboxPending"),
            )
        except MemoryError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/v1/proposals/{proposal_id}/review")
    async def review_proposal(
        proposal_id: str,
        request: Request,
        user: Principal = Depends(require_curator),
    ) -> dict[str, Any]:
        proposal = repository.get_event(proposal_id)
        if proposal is None or proposal.event_type not in {
            "assertion.proposed", "projection.edit.proposed", "source.publication.proposed"
        }:
            raise HTTPException(status_code=404, detail="Proposal not found")
        if not can_read(user, scope=proposal.scope, space_id=proposal.space_id, acl=proposal.acl):
            raise HTTPException(status_code=403, detail="Proposal is outside authorized spaces")
        payload = await json_body(request)
        decision = str(payload.get("decision") or "")
        if proposal.event_type == "projection.edit.proposed":
            prefix = "projection.edit"
        elif proposal.event_type == "source.publication.proposed":
            prefix = "source.publication"
        else:
            prefix = "assertion"
        event_type = (
            {
                "accept": "source.published",
                "reject": "source.publication.rejected",
                "dispute": "source.publication.disputed",
            }.get(decision)
            if prefix == "source.publication"
            else {
                "accept": f"{prefix}.accepted",
                "reject": f"{prefix}.rejected",
                "dispute": f"{prefix}.disputed",
                "retract": f"{prefix}.retracted",
            }.get(decision)
        )
        if not event_type:
            raise HTTPException(status_code=422, detail="decision must be accept, reject, dispute, or retract")
        latest = repository.latest_stream_event(proposal.stream_id)
        review_key = f"review:{proposal.event_id}:{decision}"
        existing_review = repository.get_by_idempotency_key(review_key)
        if existing_review is not None:
            return {"ok": True, "event": existing_review.to_dict(), "created": False}
        transitions = {
            "assertion.proposed": {"accept", "reject", "dispute"},
            "assertion.disputed": {"accept", "retract"},
            "assertion.accepted": {"retract"},
        }
        if prefix == "projection.edit":
            transitions = {"projection.edit.proposed": {"accept", "reject"}}
        elif prefix == "source.publication":
            transitions = {
                "source.publication.proposed": {"accept", "reject", "dispute"},
                "source.publication.disputed": {"accept", "reject"},
            }
        current_state = latest.event_type if latest else "missing"
        if decision not in transitions.get(current_state, set()):
            raise HTTPException(status_code=409, detail=f"Decision {decision} is invalid from state {current_state}")
        if proposal.payload.get("kind") == "replication-conflict" and decision == "accept":
            raise HTTPException(
                status_code=422,
                detail="A replication conflict cannot be accepted as knowledge; submit a resolved proposal or reject it",
            )
        if event_type == "assertion.accepted" and not proposal.evidence_refs:
            procedural_human = proposal.payload.get("kind") == "procedural" and proposal.actor.type == "user"
            if not procedural_human:
                raise HTTPException(status_code=422, detail="Accepted assertions require evidence")
        review_scope = proposal.scope
        review_acl = proposal.acl
        review_stream_id = proposal.stream_id
        expected_review_stream_version = latest.stream_version
        if prefix == "source.publication" and decision == "accept":
            review_scope, review_acl = publication_target(proposal)
            # A member authorized for the organization is intentionally not
            # entitled to the Team-scoped proposal.  Continuing its stream
            # would expose a version-2 public event whose version-1 parent is
            # filtered from that member's replication feed.  The accepted
            # publication is therefore the first event in a new public stream,
            # causally linked to the proposal but independently replayable.
            if review_scope != proposal.scope:
                review_stream_id = f"publication:organization:{proposal.event_id}"
                expected_review_stream_version = 0
        review_event = MemoryEvent(
            event_type=event_type,
            stream_id=review_stream_id,
            idempotency_key=review_key,
            actor=EventActor(type="user", id=user.id),
            plugin=PluginRef(id="team-review", version="1.0.0"),
            scope=review_scope,  # type: ignore[arg-type]
            space_id=proposal.space_id,
            evidence_refs=proposal.evidence_refs,
            acl=review_acl,
            payload={
                **proposal.payload,
                "status": event_type.rsplit(".", 1)[1],
                "proposalEventId": proposal.event_id,
                "reviewedBy": user.id,
                "reviewReason": payload.get("reason"),
            },
            causation_id=proposal.event_id,
        )
        persisted, created = repository.append(
            review_event,
            expected_stream_version=expected_review_stream_version,
        )
        repository.audit(user.id, "proposal.review", proposal.event_id, proposal.space_id, {"decision": decision})
        return {"ok": True, "event": persisted.to_dict(), "created": created}

    @app.post("/v1/proposals")
    async def create_proposal(request: Request, user: Principal = Depends(principal)) -> dict[str, Any]:
        payload = await json_body(request)
        space_id = required_string(payload, "spaceId")
        evidence_refs = validated_evidence_references(payload.get("evidenceRefs", []))
        assertion = object_value(payload, "assertion")
        proposal_scope = optional_string(payload, "scope") or "team"
        if proposal_scope not in {"team", "organization"}:
            raise HTTPException(status_code=422, detail="scope must be team or organization")
        requested_acl_value = object_value(payload, "acl")
        expected_version = payload.get("expectedStreamVersion")
        if expected_version is not None:
            if isinstance(expected_version, bool):
                raise HTTPException(status_code=422, detail="expectedStreamVersion must be a non-negative integer")
            try:
                expected_version = int(expected_version)
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail="expectedStreamVersion must be a non-negative integer") from exc
            if expected_version < 0:
                raise HTTPException(status_code=422, detail="expectedStreamVersion must be a non-negative integer")
        requested_assertion_id = optional_string(payload, "assertionId")
        idempotency_key = optional_string(payload, "idempotencyKey")
        if not can_contribute(user, space_id):
            raise HTTPException(status_code=403, detail="Cannot contribute to this space")
        missing = [ref for ref in evidence_refs if not verify_object(parse_reference(ref))]
        if missing:
            raise HTTPException(status_code=409, detail={"missingEvidence": missing})
        assertion["vault"] = shared_vault_slug(space_id)
        if proposal_scope == "organization" and "audience" not in requested_acl_value:
            requested_acl_value["audience"] = "organization"
        requested_acl = normalize_acl(requested_acl_value, owner=user.id, space_id=space_id)
        acl = (
            derived_proposal_acl(user, evidence_refs, requested_acl, space_id)
            if evidence_refs
            else requested_acl
        )
        latest = repository.latest_stream_event(
            f"assertion:{space_id}:{requested_assertion_id}"
        ) if requested_assertion_id else None
        stale = expected_version is not None and expected_version != int(latest.stream_version if latest else 0)
        policy = ReviewPolicy().evaluate(
            destination_scope=proposal_scope,
            confidence=assertion.get("confidence"),
            classification=str(acl.get("classification") or "internal"),
            contradiction=bool(assertion.get("contradiction", False)),
            acl_widening=bool(assertion.get("aclWidening", False)),
            operation=str(assertion.get("operation") or "upsert"),
            stale_base=stale,
        )
        assertion_id = requested_assertion_id or uuid7()
        stream_id = f"assertion:{space_id}:{assertion_id}"
        if stale:
            stream_id = f"conflict:{stream_id}:{uuid7()}"
        event = MemoryEvent(
            event_type="assertion.proposed",
            stream_id=stream_id,
            idempotency_key=idempotency_key or f"proposal:{uuid7()}",
            actor=EventActor(type="user", id=user.id),
            plugin=PluginRef(id="team-contribution", version="1.0.0"),
            scope=proposal_scope,  # type: ignore[arg-type]
            space_id=space_id,
            evidence_refs=evidence_refs,
            acl=acl,
            payload={
                **assertion,
                "assertionId": assertion_id,
                "status": "proposed",
                "reviewRequired": policy.review_required,
                "reviewReasons": list(policy.reasons),
                "expectedStreamVersion": expected_version,
            },
        )
        persisted, created = repository.append(
            event,
            expected_stream_version=0 if stale else expected_version,
        )
        return {"ok": True, "event": persisted.to_dict(), "created": created}

    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        try:
            from opentelemetry import trace
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            provider = TracerProvider(resource=Resource.create({"service.name": "wiki-memory-team"}))
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
            trace.set_tracer_provider(provider)
            FastAPIInstrumentor.instrument_app(app)
        except ImportError as exc:
            raise MemoryError("OTLP is configured but the observability dependencies are not installed.") from exc
    return app


def app_from_environment():
    return create_app()
