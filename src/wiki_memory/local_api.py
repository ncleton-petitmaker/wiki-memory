from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any

from .capture import capture_item
from .config import MemoryError, root_runtime_dir
from .engine import MemoryEngine
from .events import MemoryEvent
from .operations import propose_assertion, review_local_proposal
from .object_store import stream_and_close
from .search import query_memory
from .team import local_event_visible, local_evidence_visible


def local_api_token(root: Path) -> str:
    identity = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:24]
    try:
        import keyring

        token = keyring.get_password("wiki-memory/local-api", identity)
        if not token:
            token = secrets.token_urlsafe(48)
            keyring.set_password("wiki-memory/local-api", identity, token)
        if token:
            return token
    except Exception:
        # Headless Linux and minimal containers commonly have no keyring backend.
        # The fallback remains outside the memory root and is permission-restricted.
        pass
    path = root_runtime_dir(root) / "secrets" / "local-api-token"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        descriptor, temporary_name = tempfile.mkstemp(prefix="local-api-token-", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(secrets.token_urlsafe(48))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
            if os.name != "nt":
                directory = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
    if os.name != "nt":
        os.chmod(path, 0o600)
    return path.read_text(encoding="utf-8").strip()


def create_local_app(root: Path, token: str | None = None):
    try:
        from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
        from fastapi.responses import JSONResponse, StreamingResponse
    except ImportError as exc:
        raise MemoryError("The local HTTP API requires the 'server' optional dependencies.") from exc
    globals().update(
        {
            "Request": Request,
            "Response": Response,
            "JSONResponse": JSONResponse,
            "StreamingResponse": StreamingResponse,
        }
    )
    root = root.expanduser().resolve()
    engine = MemoryEngine(root)
    expected_token = token or local_api_token(root)
    app = FastAPI(title="Wiki Memory Local API", version="1.0.0")

    def authorize(authorization: str | None = Header(default=None)) -> None:
        if not authorization or not secrets.compare_digest(authorization, "Bearer " + expected_token):
            raise HTTPException(status_code=401, detail="Invalid local bearer token")

    async def json_body(request: Request) -> dict[str, Any]:
        maximum = int(os.environ.get("WIKI_MEMORY_MAX_JSON_BYTES", str(16 * 1024 * 1024)))
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > maximum:
                    raise HTTPException(status_code=413, detail="JSON request exceeds configured size limit")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid Content-Length header") from exc
        raw = await request.body()
        if len(raw) > maximum:
            raise HTTPException(status_code=413, detail="JSON request exceeds configured size limit")
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON request") from exc
        if not isinstance(value, dict):
            raise HTTPException(status_code=422, detail="JSON request must be an object")
        return value

    @app.exception_handler(MemoryError)
    async def memory_error_handler(_: Request, exc: MemoryError):
        return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})

    @app.get("/v1/health")
    def health(_: None = Depends(authorize)) -> dict[str, Any]:
        verified = engine.verify()
        return {"ok": verified["ok"], "mode": "solo", "events": verified["ledger"]["events"]}

    @app.post("/v1/captures")
    async def captures(request: Request, _: None = Depends(authorize)) -> dict[str, Any]:
        value = await json_body(request)
        if value.get("scope", "private") != "private":
            raise HTTPException(status_code=422, detail="Capture is private; use the previewed publication workflow to share")
        return capture_item(
            root,
            value["vault"],
            source_type=str(value.get("sourceType") or "document"),
            source_url=value.get("url"),
            source_file=Path(value["file"]) if value.get("file") else None,
            text=value.get("text"),
            title=value.get("title"),
            connector=str(value.get("connector") or "manual"),
            scope="private",
            space_id="local-owner",
            actor_id=str(value.get("actorId") or "local-owner"),
        )

    @app.post("/v1/events:append")
    async def append_events(request: Request, _: None = Depends(authorize)) -> dict[str, Any]:
        value = await json_body(request)
        events = []
        for raw in value.get("events", []):
            # Reject Team-only decisions before deserializing their full
            # payload. This keeps the trust boundary explicit even if an
            # attacker submits an otherwise malformed event.
            if not isinstance(raw, dict):
                raise HTTPException(status_code=422, detail="Each event must be an object")
            raw_scope = str(raw.get("scope", "private"))
            raw_type = str(raw.get("eventType", ""))
            if raw_scope != "private":
                privileged = {
                    "assertion.accepted",
                    "assertion.rejected",
                    "assertion.disputed",
                    "assertion.retracted",
                    "projection.edit.accepted",
                    "projection.edit.rejected",
                    "source.publication.disputed",
                    "source.publication.rejected",
                }
                if raw_type in privileged:
                    raise HTTPException(
                        status_code=409,
                        detail="Shared decisions can only be appended by the Team server",
                    )
                if raw_scope == "organization" and raw_type not in {
                    "assertion.proposed",
                    "source.publication.proposed",
                }:
                    raise HTTPException(
                        status_code=409,
                        detail="Organization knowledge must pass Team curator review",
                    )
            event = MemoryEvent.from_dict(raw)
            expected = raw.get("expectedStreamVersion")
            if expected is None:
                expected = event.stream_version - 1 if event.stream_version > 0 else 0
            persisted, created = engine.append(event, expected_stream_version=int(expected))
            events.append({"event": persisted.to_dict(), "created": created})
        return {"events": events}

    @app.get("/v1/events")
    def events(cursor: int = 0, limit: int = 100, _: None = Depends(authorize)) -> dict[str, Any]:
        values = list(engine.events.iter_events(cursor, limit=min(max(limit, 1), 1000)))
        next_cursor = max((int(event.position or cursor) for event in values), default=cursor)
        visible = [event for event in values if local_event_visible(root, event)]
        return {"events": [event.to_dict() for event in visible], "cursor": next_cursor}

    @app.head("/v1/blobs/{digest}")
    def head_blob(digest: str, _: None = Depends(authorize)) -> Response:
        reference = f"sha256:{digest.lower()}"
        visible = engine.evidence.has(reference) and local_evidence_visible(engine, reference)
        return Response(status_code=200 if visible else 404)

    @app.put("/v1/blobs/{digest}")
    async def put_blob(digest: str, request: Request, _: None = Depends(authorize)) -> dict[str, Any]:
        file_descriptor, temporary_name = tempfile.mkstemp(prefix="wiki-memory-local-api-")
        hasher = hashlib.sha256()
        maximum = int(os.environ.get("WIKI_MEMORY_MAX_BLOB_BYTES", str(2 * 1024 * 1024 * 1024)))
        received = 0
        try:
            with os.fdopen(file_descriptor, "wb") as handle:
                async for chunk in request.stream():
                    received += len(chunk)
                    if received > maximum:
                        raise HTTPException(status_code=413, detail="Blob exceeds configured size limit")
                    hasher.update(chunk)
                    handle.write(chunk)
            if hasher.hexdigest() != digest.lower():
                raise HTTPException(status_code=422, detail="Blob checksum mismatch")
            metadata = engine.evidence.put_file(Path(temporary_name), media_type=request.headers.get("content-type"))
        finally:
            Path(temporary_name).unlink(missing_ok=True)
        return {"reference": metadata.reference}

    @app.get("/v1/blobs/{digest}")
    def get_blob(digest: str, _: None = Depends(authorize)):
        reference = f"sha256:{digest.lower()}"
        if not engine.evidence.has(reference) or not local_evidence_visible(engine, reference):
            raise HTTPException(status_code=404, detail="Blob not found")
        if not engine.evidence.verify(reference):
            raise HTTPException(status_code=500, detail="Evidence integrity verification failed")
        return StreamingResponse(
            stream_and_close(engine.evidence.open(reference)),
            media_type=engine.evidence.metadata(reference).media_type,
        )

    @app.post("/v1/search")
    async def search(request: Request, _: None = Depends(authorize)) -> dict[str, Any]:
        value = await json_body(request)
        return query_memory(root, str(value["query"]), int(value.get("limit", 10)))

    @app.post("/v1/proposals")
    async def proposals(request: Request, _: None = Depends(authorize)) -> dict[str, Any]:
        value = await json_body(request)
        assertion = dict(value["assertion"])
        if value.get("vault"):
            assertion["vault"] = str(value["vault"])
        return propose_assertion(
            engine,
            actor_id=str(value.get("actorId") or "local-owner"),
            scope=str(value.get("scope") or "private"),
            space_id=str(value.get("spaceId") or "local-owner"),
            assertion=assertion,
            evidence_refs=[str(item) for item in value.get("evidenceRefs", [])],
        )

    @app.post("/v1/proposals/{proposal_id}/review")
    async def review(proposal_id: str, request: Request, _: None = Depends(authorize)) -> dict[str, Any]:
        value = await json_body(request)
        return review_local_proposal(
            engine,
            actor_id=str(value.get("actorId") or "local-owner"),
            proposal_event_id=proposal_id,
            decision=str(value["decision"]),
            reason=value.get("reason"),
        )

    return app
