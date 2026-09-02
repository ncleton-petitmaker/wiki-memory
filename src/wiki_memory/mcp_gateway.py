from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, TextIO

from .capture import capture_item
from .config import MemoryError
from .engine import MemoryEngine
from .operations import (
    propose_assertion,
    publication_preview,
    publish_private_event,
    review_local_proposal,
)
from .search import query_memory
from .team import local_evidence_visible


MCP_PROTOCOL_VERSION = "2025-06-18"


def tool_definitions(include_review: bool = True) -> list[dict[str, Any]]:
    tools = [
        {
            "name": "memory_capture",
            "description": "Preserve text, a URL, or a local file as evidence in Wiki Memory. Defaults to private.",
            "inputSchema": {
                "type": "object",
                "required": ["vault"],
                "properties": {
                    "vault": {"type": "string"},
                    "text": {"type": "string"},
                    "url": {"type": "string"},
                    "file": {"type": "string"},
                    "title": {"type": "string"},
                },
                "oneOf": [{"required": ["text"]}, {"required": ["url"]}, {"required": ["file"]}],
                "additionalProperties": False,
            },
        },
        {
            "name": "memory_search",
            "description": "Search authorized memory and return source-linked results.",
            "inputSchema": {
                "type": "object",
                "required": ["query"],
                "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 10}},
                "additionalProperties": False,
            },
        },
        {
            "name": "memory_get_evidence",
            "description": "Read metadata and, for small textual evidence, its exact content.",
            "inputSchema": {
                "type": "object",
                "required": ["reference"],
                "properties": {"reference": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}},
                "additionalProperties": False,
            },
        },
        {
            "name": "memory_propose_change",
            "description": "Create a sourced knowledge proposal without silently publishing it.",
            "inputSchema": {
                "type": "object",
                "required": ["vault", "assertion", "evidenceRefs"],
                "properties": {
                    "vault": {"type": "string", "minLength": 1},
                    "assertion": {"type": "object"},
                    "evidenceRefs": {"type": "array", "items": {"type": "string"}},
                    "scope": {"enum": ["private", "team", "organization"], "default": "private"},
                    "spaceId": {"type": "string", "default": "local-owner"},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "memory_publish",
            "description": "Preview, then explicitly publish a private event to a shared space.",
            "inputSchema": {
                "type": "object",
                "required": ["eventId", "scope", "spaceId"],
                "properties": {
                    "eventId": {"type": "string"},
                    "scope": {"enum": ["team", "organization"]},
                    "spaceId": {"type": "string"},
                    "previewHash": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
    ]
    if include_review:
        tools.append(
            {
                "name": "memory_review",
                "description": "Accept, reject, dispute, or retract a proposal. Curator capability is required.",
                "inputSchema": {
                    "type": "object",
                    "required": ["proposalEventId", "decision"],
                    "properties": {
                        "proposalEventId": {"type": "string"},
                        "decision": {"enum": ["accept", "reject", "dispute", "retract"]},
                        "reason": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            }
        )
    return tools


class MCPGateway:
    def __init__(self, root: Path, *, actor_id: str = "local-owner", include_review: bool = True):
        self.root = root.resolve()
        self.engine = MemoryEngine(self.root)
        self.actor_id = actor_id
        self.include_review = include_review

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "memory_capture":
            source_file = Path(arguments["file"]) if arguments.get("file") else None
            return capture_item(
                self.root,
                arguments["vault"],
                source_type="document",
                source_url=arguments.get("url"),
                source_file=source_file,
                text=arguments.get("text"),
                title=arguments.get("title"),
                connector="manual",
                scope="private",
                space_id="local-owner",
                actor_id=self.actor_id,
            )
        if name == "memory_search":
            return query_memory(self.root, str(arguments["query"]), int(arguments.get("limit", 10)))
        if name == "memory_get_evidence":
            reference = str(arguments["reference"])
            if not local_evidence_visible(self.engine, reference):
                raise MemoryError("Evidence is missing or outside the current local/Team authorization.")
            metadata = self.engine.evidence.metadata(reference)
            result = {"metadata": metadata.__dict__}
            if metadata.size <= 1024 * 1024 and metadata.media_type.startswith(("text/", "application/json")):
                result["content"] = self.engine.evidence.path(reference).read_text(encoding="utf-8", errors="replace")
            return result
        if name == "memory_propose_change":
            assertion = dict(arguments["assertion"])
            assertion["vault"] = str(arguments["vault"])
            return propose_assertion(
                self.engine,
                actor_id=self.actor_id,
                scope=arguments.get("scope", "private"),
                space_id=arguments.get("spaceId", "local-owner"),
                assertion=assertion,
                evidence_refs=[str(item) for item in arguments["evidenceRefs"]],
            )
        if name == "memory_publish":
            if not arguments.get("previewHash"):
                return publication_preview(
                    self.engine,
                    arguments["eventId"],
                    destination_scope=arguments["scope"],
                    destination_space=arguments["spaceId"],
                    principal_id=self.actor_id,
                )
            return publish_private_event(
                self.engine,
                arguments["eventId"],
                principal_id=self.actor_id,
                destination_scope=arguments["scope"],
                destination_space=arguments["spaceId"],
                preview_hash=arguments["previewHash"],
            )
        if name == "memory_review" and self.include_review:
            return review_local_proposal(
                self.engine,
                actor_id=self.actor_id,
                proposal_event_id=arguments["proposalEventId"],
                decision=arguments["decision"],
                reason=arguments.get("reason"),
            )
        raise MemoryError(f"Unknown or unauthorized MCP tool: {name}")

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        request_id = request.get("id")
        method = request.get("method")
        if method == "notifications/initialized":
            return None
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "wiki-memory", "version": "1.0.0"},
                }
            elif method == "tools/list":
                result = {"tools": tool_definitions(self.include_review)}
            elif method == "tools/call":
                params = request.get("params") or {}
                value = self.call_tool(str(params.get("name")), dict(params.get("arguments") or {}))
                result = {
                    "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, indent=2)}],
                    "isError": False,
                }
            elif method == "ping":
                result = {}
            else:
                raise MemoryError(f"Unsupported MCP method: {method}")
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32000, "message": str(exc)},
            }

    def serve(self, input_stream: TextIO = sys.stdin, output_stream: TextIO = sys.stdout) -> None:
        for line in input_stream:
            if not line.strip():
                continue
            try:
                request = json.loads(line)
                response = self.handle(request)
            except Exception as exc:
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}
            if response is not None:
                output_stream.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
                output_stream.flush()


def serve_mcp(root: Path, *, actor_id: str = "local-owner", include_review: bool = True) -> None:
    MCPGateway(root, actor_id=actor_id, include_review=include_review).serve()
