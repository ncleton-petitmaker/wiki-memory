from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
import tempfile
import urllib.parse
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from .config import MemoryError, load_vault
from .contracts import KnowledgeExtractor, Transcript, TranscriptSegment, TranscriptionProvider
from .engine import MemoryEngine
from .events import EventActor, MemoryEvent, PluginRef, canonical_json


SUPPORTED_AUDIO_SUFFIXES = {".mp3", ".m4a", ".wav"}


class FFmpegMediaDecoder:
    def __init__(self, ffmpeg: str | None = None, ffprobe: str | None = None):
        self.ffmpeg = ffmpeg or shutil.which("ffmpeg")
        self.ffprobe = ffprobe or shutil.which("ffprobe")

    def check(self) -> None:
        if not self.ffmpeg or not self.ffprobe:
            raise MemoryError("Audio ingestion requires ffmpeg and ffprobe.")

    def duration(self, path: Path) -> float:
        self.check()
        completed = subprocess.run(
            [
                str(self.ffprobe),
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if completed.returncode != 0:
            raise MemoryError(f"ffprobe failed: {completed.stderr.strip()}")
        try:
            return float(completed.stdout.strip())
        except ValueError as exc:
            raise MemoryError("ffprobe did not return a valid duration.") from exc

    def split(self, path: Path, maximum_seconds: int, directory: Path) -> list[tuple[Path, float]]:
        duration = self.duration(path)
        if duration <= maximum_seconds:
            return [(path, 0.0)]
        self.check()
        chunks: list[tuple[Path, float]] = []
        start = 0.0
        index = 0
        while start < duration:
            length = min(float(maximum_seconds), duration - start)
            target = directory / f"chunk-{index:04d}.flac"
            completed = subprocess.run(
                [
                    str(self.ffmpeg),
                    "-v",
                    "error",
                    "-ss",
                    f"{start:.3f}",
                    "-t",
                    f"{length:.3f}",
                    "-i",
                    str(path),
                    "-vn",
                    "-c:a",
                    "flac",
                    str(target),
                ],
                capture_output=True,
                text=True,
                timeout=max(300, int(length)),
                check=False,
            )
            if completed.returncode != 0:
                raise MemoryError(f"ffmpeg split failed: {completed.stderr.strip()}")
            chunks.append((target, start))
            start += length
            index += 1
        return chunks


class MistralTranscriber(TranscriptionProvider):
    id = "transcriber.mistral"

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "voxtral-mini-latest",
        base_url: str | None = None,
    ) -> None:
        if not api_key:
            raise MemoryError("MISTRAL_API_KEY is required.")
        if base_url:
            parsed = urllib.parse.urlparse(base_url)
            if not parsed.hostname:
                raise MemoryError("Mistral base URL must be an absolute HTTP(S) URL.")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise MemoryError("Mistral base URL cannot contain credentials, a query, or a fragment.")
            local_http = parsed.scheme == "http" and parsed.hostname in {
                "localhost", "127.0.0.1", "::1"
            }
            if parsed.scheme != "https" and not local_http:
                raise MemoryError("Mistral base URL must use HTTPS outside local development.")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url

    def capabilities(self) -> dict[str, bool | int]:
        return {"diarization": True, "wordTimestamps": True, "segmentTimestamps": True, "maxSeconds": 10800}

    async def transcribe(
        self,
        audio_path: str,
        *,
        language: str | None,
        diarize: bool,
        timestamp_granularity: Literal["segment", "word"],
        context_bias: list[str],
    ) -> Transcript:
        return await asyncio.to_thread(
            self._transcribe_sync,
            Path(audio_path),
            language,
            diarize,
            timestamp_granularity,
            context_bias,
        )

    def _transcribe_sync(
        self,
        path: Path,
        language: str | None,
        diarize: bool,
        timestamp_granularity: str,
        context_bias: list[str],
    ) -> Transcript:
        try:
            try:
                from mistralai import Mistral
            except ImportError:
                from mistralai.client import Mistral
        except ImportError as exc:
            raise MemoryError("Mistral transcription requires the 'audio-mistral' optional dependency.") from exc
        kwargs: dict[str, Any] = {"api_key": self.api_key}
        if self.base_url:
            kwargs["server_url"] = self.base_url
        client = Mistral(**kwargs)
        with path.open("rb") as handle:
            response = client.audio.transcriptions.complete(
                model=self.model,
                file={"content": handle, "file_name": path.name},
                language=language,
                diarize=diarize,
                timestamp_granularities=[timestamp_granularity],
                context_bias=context_bias[:100],
            )
        raw = response.model_dump() if hasattr(response, "model_dump") else dict(response)
        segments = tuple(
            TranscriptSegment(
                start_seconds=float(segment.get("start", segment.get("start_time", 0))),
                end_seconds=float(segment.get("end", segment.get("end_time", 0))),
                text=str(segment.get("text") or ""),
                speaker=segment.get("speaker") or segment.get("speaker_id"),
                words=tuple(segment.get("words") or ()),
            )
            for segment in raw.get("segments", [])
        )
        return Transcript(
            text=str(raw.get("text") or ""),
            language=raw.get("language") or language,
            provider=self.id,
            model=str(raw.get("model") or self.model),
            segments=segments,
            settings={
                "requestedModel": self.model,
                "language": language,
                "diarize": diarize,
                "timestampGranularity": timestamp_granularity,
                "contextBias": context_bias[:100],
            },
            diarized=bool(diarize and any(segment.speaker for segment in segments)),
        )


class WhisperCppTranscriber(TranscriptionProvider):
    id = "transcriber.whisper-cpp"

    def __init__(self, model_path: Path, binary: str | None = None):
        self.model_path = model_path.expanduser().resolve()
        self.binary = binary or shutil.which("whisper-cli") or shutil.which("main")

    def capabilities(self) -> dict[str, bool | int]:
        return {"diarization": False, "wordTimestamps": False, "segmentTimestamps": True, "maxSeconds": 86400}

    async def transcribe(
        self,
        audio_path: str,
        *,
        language: str | None,
        diarize: bool,
        timestamp_granularity: Literal["segment", "word"],
        context_bias: list[str],
    ) -> Transcript:
        if diarize:
            raise MemoryError("The local whisper.cpp provider does not support diarization.")
        if not self.binary or not self.model_path.is_file():
            raise MemoryError("whisper.cpp binary or model is missing.")
        return await asyncio.to_thread(self._transcribe_sync, Path(audio_path), language)

    def _transcribe_sync(self, path: Path, language: str | None) -> Transcript:
        with tempfile.TemporaryDirectory(prefix="wiki-memory-whisper-") as temporary_directory:
            output = Path(temporary_directory) / "transcript"
            command = [
                str(self.binary),
                "-m",
                str(self.model_path),
                "-f",
                str(path),
                "-oj",
                "-of",
                str(output),
            ]
            if language:
                command.extend(["-l", language])
            completed = subprocess.run(command, capture_output=True, text=True, timeout=86400, check=False)
            if completed.returncode != 0:
                raise MemoryError(f"whisper.cpp failed: {completed.stderr.strip() or completed.stdout.strip()}")
            raw = json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))
        segments: list[TranscriptSegment] = []
        for item in raw.get("transcription", raw.get("segments", [])):
            offsets = item.get("offsets", {})
            start = offsets.get("from", item.get("start", 0))
            end = offsets.get("to", item.get("end", 0))
            if isinstance(start, int) and start > 1000:
                start = start / 1000
            if isinstance(end, int) and end > 1000:
                end = end / 1000
            segments.append(TranscriptSegment(float(start), float(end), str(item.get("text") or "").strip()))
        text = " ".join(segment.text for segment in segments).strip() or str(raw.get("text") or "")
        model_hash = hashlib.sha256()
        with self.model_path.open("rb") as model_handle:
            while block := model_handle.read(1024 * 1024):
                model_hash.update(block)
        return Transcript(
            text=text,
            language=raw.get("result", {}).get("language") or language,
            provider=self.id,
            model=self.model_path.name,
            segments=tuple(segments),
            settings={"modelPathHash": model_hash.hexdigest(), "language": language},
            diarized=False,
        )


def merge_transcripts(parts: list[tuple[Transcript, float]]) -> Transcript:
    if not parts:
        raise MemoryError("Cannot merge an empty transcription.")
    segments: list[TranscriptSegment] = []
    for transcript, offset in parts:
        segments.extend(
            TranscriptSegment(
                start_seconds=segment.start_seconds + offset,
                end_seconds=segment.end_seconds + offset,
                text=segment.text,
                speaker=segment.speaker,
                words=segment.words,
            )
            for segment in transcript.segments
        )
    first = parts[0][0]
    return Transcript(
        text="\n".join(transcript.text.strip() for transcript, _ in parts if transcript.text.strip()),
        language=first.language,
        provider=first.provider,
        model=first.model,
        segments=tuple(segments),
        settings={**first.settings, "chunks": len(parts)},
        diarized=all(transcript.diarized for transcript, _ in parts),
    )


class AudioIngestor:
    def __init__(
        self,
        engine: MemoryEngine,
        transcriber: TranscriptionProvider,
        decoder: FFmpegMediaDecoder | None = None,
        extractor: KnowledgeExtractor | None = None,
    ) -> None:
        self.engine = engine
        self.transcriber = transcriber
        self.decoder = decoder or FFmpegMediaDecoder()
        self.extractor = extractor

    async def ingest(
        self,
        path: Path,
        *,
        vault: str,
        title: str | None = None,
        language: str | None = None,
        diarize: bool = True,
        timestamp_granularity: Literal["segment", "word"] = "segment",
        context_bias: list[str] | None = None,
        actor_id: str = "local-owner",
    ) -> dict[str, Any]:
        path = path.expanduser().resolve()
        if path.suffix.lower() not in SUPPORTED_AUDIO_SUFFIXES or not path.is_file():
            raise MemoryError("Audio source must be an existing MP3, M4A, or WAV file.")
        _, vault_config = load_vault(self.engine.root, vault)
        if (vault_config.get("team") or {}).get("managed"):
            raise MemoryError(
                "Audio ingestion is private-first; ingest into a private vault, then use the explicit publication workflow."
            )
        evidence = self.engine.evidence.put_file(path)
        source_id = evidence.sha256[:16]
        stream_id = f"source:{vault}:{source_id}"
        current = self.engine.events.stream_version(stream_id)
        heading = title or path.stem
        captured = MemoryEvent(
            event_type="source.audio.captured",
            stream_id=stream_id,
            idempotency_key=f"audio:{evidence.sha256}",
            actor=EventActor(type="user", id=actor_id),
            plugin=PluginRef(id="source-audio", version="1.0.0"),
            evidence_refs=[evidence.reference],
            payload={
                "sourceId": source_id,
                "vault": vault,
                "partition": "audio",
                "title": heading,
                "body": f"Audio preserved as `{evidence.reference}`. Transcription pending.",
                "metadata": {
                    "source_type": "audio",
                    "connector": "audio",
                    "content_hash": evidence.sha256,
                    "original_name": path.name,
                    "epistemic_status": "unverified",
                },
            },
        )
        captured_event, _ = self.engine.append(captured, expected_stream_version=current)
        try:
            capabilities = self.transcriber.capabilities()
            if diarize and not capabilities.get("diarization", False):
                raise MemoryError(f"Provider {self.transcriber.id} does not support diarization; disable it explicitly.")
            maximum_seconds = int(capabilities.get("maxSeconds", 10800))
            with tempfile.TemporaryDirectory(prefix="wiki-memory-audio-") as temporary_directory:
                chunks = self.decoder.split(path, maximum_seconds, Path(temporary_directory))
                parts: list[tuple[Transcript, float]] = []
                for chunk, offset in chunks:
                    transcript = await self.transcriber.transcribe(
                        str(chunk),
                        language=language,
                        diarize=diarize,
                        timestamp_granularity=timestamp_granularity,
                        context_bias=context_bias or [],
                    )
                    parts.append((transcript, offset))
                transcript = merge_transcripts(parts)
        except Exception as exc:
            error_detail = str(exc)
            provider_secret = getattr(self.transcriber, "api_key", None)
            if isinstance(provider_secret, str) and provider_secret:
                error_detail = error_detail.replace(provider_secret, "[REDACTED]")
            current = self.engine.events.stream_version(stream_id)
            failed = MemoryEvent(
                event_type="transcription.failed",
                stream_id=stream_id,
                idempotency_key=f"transcription-failed:{captured_event.event_id}:{self.transcriber.id}:{current + 1}",
                actor=EventActor(type="system", id="source-audio"),
                plugin=PluginRef(id="source-audio", version="1.0.0"),
                evidence_refs=[evidence.reference],
                payload={
                    "sourceId": source_id,
                    "provider": self.transcriber.id,
                    "error": error_detail[:2000],
                },
                causation_id=captured_event.event_id,
            )
            self.engine.append(failed, expected_stream_version=current)
            raise
        transcript_payload = {
            "provider": transcript.provider,
            "model": transcript.model,
            "language": transcript.language,
            "diarized": transcript.diarized,
            "settings": transcript.settings,
            "text": transcript.text,
            "segments": [asdict(segment) for segment in transcript.segments],
        }
        transcript_evidence = self.engine.evidence.put_bytes(
            canonical_json(transcript_payload).encode("utf-8"),
            media_type="application/json",
            original_name=f"{source_id}-transcript.json",
        )
        fingerprint = hashlib.sha256(canonical_json(transcript_payload).encode("utf-8")).hexdigest()
        current = self.engine.events.stream_version(stream_id)
        event = MemoryEvent(
            event_type="transcription.created",
            stream_id=stream_id,
            idempotency_key=f"transcription:{evidence.sha256}:{fingerprint}",
            actor=EventActor(type="system", id="source-audio"),
            plugin=PluginRef(id="source-audio", version="1.0.0"),
            evidence_refs=[evidence.reference, transcript_evidence.reference],
            payload={
                "sourceId": source_id,
                "vault": vault,
                "partition": "audio",
                "title": heading,
                "body": transcript.text,
                "transcription": transcript_payload,
                "metadata": {
                    "source_type": "audio_transcript",
                    "connector": "audio",
                    "content_hash": evidence.sha256,
                    "epistemic_status": "unverified",
                },
            },
            causation_id=captured_event.event_id,
        )
        persisted, created = self.engine.append(event, expected_stream_version=current)
        proposals: list[str] = []
        if self.extractor is not None:
            extracted = await self.extractor.extract(transcript)
            for index, item in enumerate(extracted):
                assertion_id = hashlib.sha256(
                    f"{persisted.event_id}:{self.extractor.id}:{self.extractor.version}:{index}".encode("utf-8")
                ).hexdigest()[:24]
                proposal = MemoryEvent(
                    event_type="assertion.proposed",
                    stream_id=f"assertion:local-owner:{assertion_id}",
                    idempotency_key=f"audio-extraction:{assertion_id}:{self.extractor.version}",
                    actor=EventActor(type="system", id=self.extractor.id),
                    plugin=PluginRef(id=self.extractor.id, version=self.extractor.version),
                    evidence_refs=[transcript_evidence.reference, evidence.reference],
                    payload={
                        "assertionId": assertion_id,
                        "vault": vault,
                        "kind": item.kind,
                        "title": item.title,
                        "body": item.body,
                        "confidence": item.confidence,
                        "occurredAt": item.occurred_at,
                        "segmentIndexes": list(item.segment_indexes),
                        "status": "proposed",
                        "inference": True,
                        "sourceEventId": persisted.event_id,
                    },
                    causation_id=persisted.event_id,
                )
                proposed, _ = self.engine.append(proposal, expected_stream_version=0)
                proposals.append(proposed.event_id)
        return {
            "status": "transcribed" if created else "duplicate",
            "sourceId": source_id,
            "eventId": persisted.event_id,
            "evidence": evidence.reference,
            "provider": transcript.provider,
            "model": transcript.model,
            "segments": len(transcript.segments),
            "transcriptEvidence": transcript_evidence.reference,
            "proposals": proposals,
        }
