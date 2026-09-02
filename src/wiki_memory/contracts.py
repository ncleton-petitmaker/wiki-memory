from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Literal, Protocol


JsonValue = Any
SourceMessageType = Literal["record", "delete", "checkpoint", "schema-change", "warning"]


@dataclass(frozen=True)
class ConnectorCapabilities:
    backfill: bool = True
    incremental: bool = False
    webhooks: bool = False
    subscriptions: bool = False
    hard_deletes: bool = False
    schema_changes: bool = False
    attachments: bool = False


@dataclass(frozen=True)
class ConnectorSpec:
    id: str
    display_name: str
    config_schema: dict[str, Any]
    capabilities: ConnectorCapabilities = field(default_factory=ConnectorCapabilities)
    documentation_url: str | None = None


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceStream:
    name: str
    schema: dict[str, Any]
    primary_key: tuple[str, ...] = ()
    default_cursor: tuple[str, ...] = ()
    capabilities: ConnectorCapabilities = field(default_factory=ConnectorCapabilities)


@dataclass(frozen=True)
class SourceCatalog:
    streams: tuple[SourceStream, ...]


@dataclass(frozen=True)
class SourceSelection:
    streams: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class SourceMessage:
    type: SourceMessageType
    stream: str
    emitted_at: str
    source_id: str | None = None
    source_version: str | None = None
    occurred_at: str | None = None
    payload: dict[str, Any] | None = None
    cursor: JsonValue | None = None
    schema: dict[str, Any] | None = None
    warning: str | None = None
    evidence: tuple[str, ...] = ()


class Disposable(Protocol):
    async def close(self) -> None: ...


class SourceConnector(ABC):
    @abstractmethod
    async def spec(self) -> ConnectorSpec: ...

    @abstractmethod
    async def check(self, config: dict[str, Any], secret_handles: dict[str, str]) -> CheckResult: ...

    @abstractmethod
    async def discover(self, config: dict[str, Any]) -> SourceCatalog: ...

    @abstractmethod
    def read(
        self,
        selection: SourceSelection,
        cursor: JsonValue | None,
        signal: Any | None = None,
    ) -> AsyncIterator[SourceMessage]: ...

    async def subscribe(
        self,
        selection: SourceSelection,
        emit: Callable[[SourceMessage], Awaitable[None]],
    ) -> Disposable:
        raise NotImplementedError("This connector does not support subscriptions.")

    async def fetch(self, reference: str) -> bytes:
        raise NotImplementedError("This connector does not support fetching attachments.")


@dataclass(frozen=True)
class TranscriptSegment:
    start_seconds: float
    end_seconds: float
    text: str
    speaker: str | None = None
    words: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str | None
    provider: str
    model: str
    segments: tuple[TranscriptSegment, ...]
    settings: dict[str, Any]
    diarized: bool


class TranscriptionProvider(ABC):
    id: str

    @abstractmethod
    def capabilities(self) -> dict[str, bool | int]: ...

    @abstractmethod
    async def transcribe(
        self,
        audio_path: str,
        *,
        language: str | None,
        diarize: bool,
        timestamp_granularity: Literal["segment", "word"],
        context_bias: list[str],
    ) -> Transcript: ...


@dataclass(frozen=True)
class ExtractedKnowledge:
    kind: Literal["decision", "task", "fact"]
    title: str
    body: str
    confidence: float | None = None
    occurred_at: str | None = None
    segment_indexes: tuple[int, ...] = ()


class KnowledgeExtractor(ABC):
    id: str
    version: str

    @abstractmethod
    async def extract(self, transcript: Transcript) -> tuple[ExtractedKnowledge, ...]: ...


class SearchProvider(Protocol):
    def search(self, query: str, limit: int, *, actor_id: str, spaces: set[str]) -> list[dict[str, Any]]: ...
