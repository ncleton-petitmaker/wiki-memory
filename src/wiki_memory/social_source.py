from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, AsyncIterator

from .capture import SOCIAL_CONNECTORS
from .config import MemoryError, utc_now
from .contracts import (
    CheckResult,
    ConnectorCapabilities,
    ConnectorSpec,
    SourceCatalog,
    SourceConnector,
    SourceMessage,
    SourceSelection,
    SourceStream,
)
from .events import canonical_json


class SocialBrowserConnector(SourceConnector):
    """Read a user-approved, normalized browser capture without owning browser credentials.

    The browser extension or controlled browser writes a JSON export selected by the
    user.  This connector deliberately has no account, cookie, or network API: it
    makes the source's collection policy visible and makes an import replayable.
    """

    stream_name = "social.items"

    def __init__(self, input_path: str | Path | None = None):
        self._configured_input_path = str(input_path).strip() if input_path is not None else ""

    def _input_path(self, config: dict[str, Any]) -> Path:
        # SourceConnector.read deliberately receives a selection, not plugin
        # configuration.  Prefer the path injected by the plugin loader; accept
        # the selection value too so one-off CLI/import callers can use the same
        # connector without a plugin manager.
        value = self._configured_input_path or str(config.get("inputPath") or "").strip()
        if not value:
            raise MemoryError("Social browser connector requires an explicit inputPath selected by the user.")
        path = Path(value).expanduser().resolve()
        if not path.is_file():
            raise MemoryError(f"Social browser capture does not exist or is not a file: {path}")
        return path

    @staticmethod
    def _items(path: Path) -> list[dict[str, Any]]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MemoryError(f"Could not read normalized social browser capture: {exc}") from exc
        items = raw.get("items", []) if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            raise MemoryError("Social browser capture must be an array or an object with an items array.")
        normalized: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise MemoryError(f"Social browser item {index} must be an object.")
            connector = str(item.get("connector") or "").lower()
            if connector not in SOCIAL_CONNECTORS:
                raise MemoryError(f"Unsupported social connector at item {index}: {connector or '(missing)'}")
            normalized.append(item)
        return normalized

    async def spec(self) -> ConnectorSpec:
        return ConnectorSpec(
            id="source-social-browser",
            display_name="Social browser export",
            config_schema={
                "type": "object",
                "required": ["inputPath"],
                "properties": {"inputPath": {"type": "string", "minLength": 1}},
                "additionalProperties": False,
            },
            capabilities=ConnectorCapabilities(backfill=True, incremental=False, hard_deletes=False),
        )

    async def check(self, config: dict[str, Any], secret_handles: dict[str, str]) -> CheckResult:
        if secret_handles:
            return CheckResult(False, "Social browser exports do not accept secrets.")
        try:
            items = self._items(self._input_path(config))
        except MemoryError as exc:
            return CheckResult(False, str(exc))
        return CheckResult(True, "Normalized social browser capture is ready.", {"items": len(items)})

    async def discover(self, config: dict[str, Any]) -> SourceCatalog:
        self._items(self._input_path(config))
        return SourceCatalog(
            (
                SourceStream(
                    name=self.stream_name,
                    schema={"type": "object", "additionalProperties": True},
                    primary_key=("source_url",),
                    capabilities=ConnectorCapabilities(backfill=True, incremental=False, hard_deletes=False),
                ),
            )
        )

    async def read(
        self,
        selection: SourceSelection,
        cursor: Any | None,
        signal: Any | None = None,
    ) -> AsyncIterator[SourceMessage]:
        if set(selection.streams) != {self.stream_name}:
            raise MemoryError(f"Social browser connector only exposes the {self.stream_name} stream.")
        items = self._items(self._input_path(selection.streams[self.stream_name]))
        input_hash = hashlib.sha256(canonical_json(items).encode("utf-8")).hexdigest()
        for index, item in enumerate(items):
            if getattr(signal, "is_set", lambda: False)():
                return
            source_id = str(item.get("id") or item.get("source_url") or "").strip()
            if not source_id:
                source_id = hashlib.sha256(canonical_json(item).encode("utf-8")).hexdigest()
            version = str(item.get("updated_at") or item.get("published_at") or "").strip()
            if not version:
                version = hashlib.sha256(canonical_json(item).encode("utf-8")).hexdigest()
            yield SourceMessage(
                type="record",
                stream=self.stream_name,
                emitted_at=utc_now(),
                source_id=source_id,
                source_version=version,
                occurred_at=str(item.get("published_at") or "").strip() or None,
                payload=item,
            )
        # The cursor is the full immutable export digest.  It is persisted only by
        # SourceIngestionRuntime after every preceding source event is durable.
        yield SourceMessage(type="checkpoint", stream=self.stream_name, emitted_at=utc_now(), cursor={"sha256": input_hash})
