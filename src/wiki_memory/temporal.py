from __future__ import annotations

import calendar
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import load_registry, load_vault, safe_child, utc_now


TEMPORAL_FIELDS = {
    "valid_from",
    "valid_until",
    "recorded_at",
    "invalidated_at",
    "supersedes",
    "superseded_by",
}
DATE_FIELDS = {"valid_from", "valid_until", "recorded_at", "invalidated_at"}
SYSTEM_DATE_FIELDS = {"recorded_at", "invalidated_at"}
SOURCE_DATE_FIELDS = ("published_at", "meeting_at", "sent_at", "source_date", "date")
WIKILINK = re.compile(r"^\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]$")
WIKILINKS = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")


@dataclass(frozen=True)
class TemporalNote:
    path: Path
    relative_path: str
    vault_path: Path
    vault_slug: str
    kind: str
    metadata: dict[str, Any]
    body: str


def parse_temporal_date(value: Any) -> datetime | None:
    """Parse an ISO date or timestamp into an aware UTC datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    elif isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return None
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.combine(date.fromisoformat(candidate), time.min)
            except ValueError as exc:
                raise ValueError(f"not an ISO 8601 date or timestamp: {value}") from exc
    else:
        raise ValueError(f"not an ISO 8601 date or timestamp: {value}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_temporal_date(value: Any, *, timestamp: bool = False) -> str | None:
    parsed = parse_temporal_date(value)
    if parsed is None:
        return None
    if not timestamp and isinstance(value, (date, str)) and not isinstance(value, datetime):
        raw = value.isoformat() if isinstance(value, date) else value.strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            return raw
    return parsed.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def source_valid_from(metadata: dict[str, Any]) -> str | None:
    """Return an explicit source date, never an ingestion or filesystem date."""
    for field in SOURCE_DATE_FIELDS:
        value = metadata.get(field)
        if value not in (None, ""):
            try:
                return normalize_temporal_date(value)
            except ValueError:
                return None
    return None


def temporal_defaults_from_source(
    metadata: dict[str, Any], *, recorded_at: str | None = None
) -> dict[str, Any]:
    valid_from = source_valid_from(metadata)
    return {
        "valid_from": valid_from,
        "valid_until": None,
        "recorded_at": recorded_at or utc_now(),
        "invalidated_at": None,
        "supersedes": None,
        "superseded_by": None,
    }


def temporal_open_questions(metadata: dict[str, Any]) -> list[dict[str, str]]:
    if source_valid_from(metadata) is not None:
        return []
    return [
        {
            "field": "valid_from",
            "question": "When did this fact become true? The source provides no date.",
        }
    ]


def has_temporal_metadata(metadata: dict[str, Any]) -> bool:
    return any(field in metadata for field in TEMPORAL_FIELDS)


def iter_temporal_notes(root: Path) -> Iterable[TemporalNote]:
    # Imported lazily to avoid a capture -> temporal -> capture cycle.
    from .capture import _parse_frontmatter

    root = root.resolve()
    for entry in load_registry(root).get("vaults", []):
        vault_path, vault = load_vault(root, entry["slug"])
        for role, kind in (("wiki", "wiki"), ("outputs", "synthesis")):
            role_root = vault_path / vault["folders"][role]
            if not role_root.is_dir():
                continue
            for path in sorted(role_root.rglob("*.md")):
                metadata, body = _parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
                yield TemporalNote(
                    path=path.resolve(),
                    relative_path=path.relative_to(root).as_posix(),
                    vault_path=vault_path,
                    vault_slug=entry["slug"],
                    kind=kind,
                    metadata=metadata,
                    body=body,
                )


def note_index(notes: Iterable[TemporalNote]) -> dict[str, list[TemporalNote]]:
    index: dict[str, list[TemporalNote]] = {}
    for note in notes:
        index.setdefault(note.path.stem.lower(), []).append(note)
    return index


def wikilink_target(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = WIKILINK.fullmatch(value.strip())
    return match.group(1).strip() if match else None


def relative_wikilink(source: Path, target: Path) -> str:
    relative = os.path.relpath(target.with_suffix(""), start=source.parent).replace(os.sep, "/")
    return f"[[{relative}]]"


def resolve_wikilink(
    root: Path,
    source: Path,
    value: Any,
    *,
    by_stem: dict[str, list[TemporalNote]] | None = None,
) -> Path | None:
    target = wikilink_target(value)
    if target is None:
        return None
    candidate_text = target if target.endswith(".md") else target + ".md"
    candidates = [source.parent / candidate_text, root / candidate_text]
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            resolved.relative_to(root.resolve())
        except (OSError, ValueError):
            continue
        if resolved.is_file():
            return resolved
    if "/" not in target and "\\" not in target and by_stem is not None:
        matches = by_stem.get(Path(target).stem.lower(), [])
        if len(matches) == 1:
            return matches[0].path
    return None


def _interval_contains(
    start: datetime | None,
    end: datetime | None,
    point: datetime,
    *,
    unknown_start_is_open: bool,
) -> bool:
    if start is None and not unknown_start_is_open:
        return False
    return (start is None or start <= point) and (end is None or point < end)


def temporal_decision(
    metadata: dict[str, Any], mode: str, at: datetime
) -> tuple[bool, str | None]:
    """Return whether a note is visible and, when excluded, the reason."""
    try:
        valid_from = parse_temporal_date(metadata.get("valid_from"))
        valid_until = parse_temporal_date(metadata.get("valid_until"))
        recorded_at = parse_temporal_date(metadata.get("recorded_at"))
        invalidated_at = parse_temporal_date(metadata.get("invalidated_at"))
    except ValueError:
        return False, "invalid-temporal-date"

    if mode == "system":
        if recorded_at is None:
            return False, "missing-recorded-at"
        visible = _interval_contains(recorded_at, invalidated_at, at, unknown_start_is_open=False)
        return visible, None if visible else "outside-system-time"
    if mode == "world":
        if valid_from is None:
            return False, "missing-valid-from"
        visible = _interval_contains(valid_from, valid_until, at, unknown_start_is_open=False)
        return visible, None if visible else "outside-world-time"
    if mode != "current":
        raise ValueError(f"unknown temporal query mode: {mode}")

    if metadata.get("superseded_by"):
        return False, "superseded"
    if invalidated_at is not None and invalidated_at <= at:
        return False, "invalidated"
    if valid_until is not None and valid_until <= at:
        return False, "expired-in-world"
    if valid_from is not None and valid_from > at:
        return False, "not-yet-valid"
    return True, "missing-valid-from" if valid_from is None else None


def _load_temporal_note(root: Path, path: str | Path) -> TemporalNote:
    resolved = safe_child(root, path)
    if not resolved.is_file() or resolved.suffix.lower() != ".md":
        raise ValueError(f"fact note does not exist: {path}")
    for note in iter_temporal_notes(root):
        if note.path == resolved:
            return note
    raise ValueError(f"fact note is outside the Wiki and Syntheses folders: {path}")


def supersession_proposal(
    root: Path,
    left_path: str | Path,
    right_path: str | Path,
    *,
    observed_at: str | None = None,
) -> dict[str, Any]:
    """Order a known contradictory pair without changing either note."""
    root = root.resolve()
    left = _load_temporal_note(root, left_path)
    right = _load_temporal_note(root, right_path)
    try:
        left_valid = parse_temporal_date(left.metadata.get("valid_from"))
        right_valid = parse_temporal_date(right.metadata.get("valid_from"))
    except ValueError as exc:
        return {
            "status": "ambiguous",
            "reason": "invalid-valid-from",
            "detail": str(exc),
            "facts": [left.relative_path, right.relative_path],
            "updates": {},
        }
    if left_valid is None or right_valid is None:
        return {
            "status": "ambiguous",
            "reason": "missing-valid-from",
            "facts": [left.relative_path, right.relative_path],
            "updates": {},
        }
    if left_valid == right_valid:
        return {
            "status": "ambiguous",
            "reason": "equal-valid-from",
            "facts": [left.relative_path, right.relative_path],
            "updates": {},
        }

    older, newer = (left, right) if left_valid < right_valid else (right, left)
    newer_value = newer.metadata.get("valid_from")
    learned_at = normalize_temporal_date(observed_at or utc_now(), timestamp=True)
    newer_updates: dict[str, Any] = {"supersedes": relative_wikilink(newer.path, older.path)}
    older_updates: dict[str, Any] = {
        "valid_until": newer_value,
        "invalidated_at": learned_at,
        "superseded_by": relative_wikilink(older.path, newer.path),
    }
    if not older.metadata.get("recorded_at"):
        older_updates["recorded_at"] = learned_at
    if not newer.metadata.get("recorded_at"):
        newer_updates["recorded_at"] = learned_at
    return {
        "status": "ready",
        "reason": "ordered-by-valid-from",
        "current": newer.relative_path,
        "superseded": older.relative_path,
        "updates": {
            older.relative_path: older_updates,
            newer.relative_path: newer_updates,
        },
    }


def validate_temporal_metadata(note: TemporalNote) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for field in DATE_FIELDS:
        if field not in note.metadata or note.metadata.get(field) in (None, ""):
            continue
        try:
            parse_temporal_date(note.metadata[field])
        except ValueError as exc:
            findings.append({"code": "invalid-temporal-date", "path": note.relative_path, "detail": f"{field}: {exc}"})
    for field in SYSTEM_DATE_FIELDS:
        value = note.metadata.get(field)
        if value in (None, ""):
            continue
        timezone_present = (
            isinstance(value, datetime)
            and value.tzinfo is not None
            or isinstance(value, str)
            and re.fullmatch(r"\d{4}-\d{2}-\d{2}T[^\s]+(?:Z|[+-]\d{2}:\d{2})", value.strip()) is not None
        )
        if not timezone_present:
            findings.append(
                {
                    "code": "invalid-system-timestamp",
                    "path": note.relative_path,
                    "detail": f"{field} must be an RFC 3339 timestamp with timezone",
                }
            )
    for field in ("supersedes", "superseded_by"):
        value = note.metadata.get(field)
        if value not in (None, "") and wikilink_target(value) is None:
            findings.append({"code": "invalid-temporal-wikilink", "path": note.relative_path, "detail": field})

    try:
        valid_from = parse_temporal_date(note.metadata.get("valid_from"))
        valid_until = parse_temporal_date(note.metadata.get("valid_until"))
        recorded_at = parse_temporal_date(note.metadata.get("recorded_at"))
        invalidated_at = parse_temporal_date(note.metadata.get("invalidated_at"))
    except ValueError:
        return findings
    if valid_from is not None and valid_until is not None and valid_until < valid_from:
        findings.append({"code": "invalid-world-interval", "path": note.relative_path, "detail": "valid_until precedes valid_from"})
    if recorded_at is not None and invalidated_at is not None and invalidated_at < recorded_at:
        findings.append({"code": "invalid-system-interval", "path": note.relative_path, "detail": "invalidated_at precedes recorded_at"})
    return findings


def supersession_findings(root: Path, notes: list[TemporalNote]) -> list[dict[str, str]]:
    root = root.resolve()
    by_path = {note.path: note for note in notes}
    by_stem = note_index(notes)
    findings: list[dict[str, str]] = []
    outgoing: dict[Path, Path] = {}
    incoming: dict[Path, list[Path]] = {}

    for note in notes:
        for field, reciprocal in (("supersedes", "superseded_by"), ("superseded_by", "supersedes")):
            value = note.metadata.get(field)
            if value in (None, ""):
                continue
            target = resolve_wikilink(root, note.path, value, by_stem=by_stem)
            if target is None or target not in by_path:
                findings.append({"code": "broken-supersession-target", "path": note.relative_path, "detail": f"{field}: {value}"})
                continue
            reciprocal_target = resolve_wikilink(
                root,
                target,
                by_path[target].metadata.get(reciprocal),
                by_stem=by_stem,
            )
            if reciprocal_target != note.path:
                findings.append({"code": "nonreciprocal-supersession", "path": note.relative_path, "detail": f"{field}: {value}"})
            if field == "supersedes":
                outgoing[note.path] = target
                incoming.setdefault(target, []).append(note.path)
                try:
                    newer = parse_temporal_date(note.metadata.get("valid_from"))
                    older = parse_temporal_date(by_path[target].metadata.get("valid_from"))
                    if newer is not None and older is not None and newer <= older:
                        findings.append({"code": "invalid-supersession-order", "path": note.relative_path, "detail": value})
                except ValueError:
                    pass
        if note.metadata.get("superseded_by") and (
            note.metadata.get("valid_until") in (None, "")
            or note.metadata.get("invalidated_at") in (None, "")
        ):
            findings.append(
                {
                    "code": "incomplete-supersession-lifecycle",
                    "path": note.relative_path,
                    "detail": "superseded facts need valid_until and invalidated_at",
                }
            )

    for target, replacements in incoming.items():
        if len(replacements) > 1:
            findings.append({
                "code": "branched-supersession",
                "path": by_path[target].relative_path,
                "detail": ",".join(sorted(by_path[path].relative_path for path in replacements)),
            })

    for start in outgoing:
        seen: set[Path] = set()
        current: Path | None = start
        while current in outgoing:
            if current in seen:
                findings.append({"code": "supersession-cycle", "path": by_path[start].relative_path, "detail": "cycle detected"})
                break
            seen.add(current)
            current = outgoing[current]
    unique: dict[tuple[str, str, str], dict[str, str]] = {}
    for item in findings:
        unique[(item["code"], item["path"], item["detail"])] = item
    return list(unique.values())


def source_links(root: Path, note: TemporalNote) -> list[Path]:
    source_root: Path | None = None
    for entry in load_registry(root).get("vaults", []):
        if entry["slug"] == note.vault_slug:
            vault_path, vault = load_vault(root, entry["slug"])
            source_root = (vault_path / vault["folders"]["sources"] / "items").resolve()
            break
    if source_root is None:
        return []
    values: list[str] = [f"[[{target}]]" for target in WIKILINKS.findall(note.body)]
    for key in ("source", "sources"):
        value = note.metadata.get(key)
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, list):
            values.extend(item for item in value if isinstance(item, str))
    resolved: list[Path] = []
    source_by_stem: dict[str, list[Path]] = {}
    if source_root.is_dir():
        for candidate in source_root.rglob("*.md"):
            source_by_stem.setdefault(candidate.stem.lower(), []).append(candidate.resolve())
    for value in values:
        target = resolve_wikilink(root, note.path, value)
        if target is None:
            raw_target = wikilink_target(value)
            if raw_target and "/" not in raw_target and "\\" not in raw_target:
                candidates = source_by_stem.get(Path(raw_target).stem.lower(), [])
                target = candidates[0] if len(candidates) == 1 else None
        if target is None:
            continue
        try:
            target.relative_to(source_root)
        except ValueError:
            continue
        if target not in resolved:
            resolved.append(target)
    return resolved


def subtract_calendar_months(point: datetime, months: int) -> datetime:
    if months < 0:
        raise ValueError("months must be non-negative")
    absolute = point.year * 12 + (point.month - 1) - months
    year, month_zero = divmod(absolute, 12)
    month = month_zero + 1
    day = min(point.day, calendar.monthrange(year, month)[1])
    return point.replace(year=year, month=month, day=day)
