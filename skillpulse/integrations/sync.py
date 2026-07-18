"""Cursor-driven, idempotent synchronization into SkillStore."""

from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..store import SkillStore
from .base import IntegrationError, RunSource
from .mapping import MappingError, RunMapper


@dataclass
class SyncResult:
    provider: str
    since: float
    pages: int = 0
    scanned: int = 0
    fetched: int = 0
    added: int = 0
    duplicates: int = 0
    skipped: int = 0
    checkpoint: float | None = None
    skip_reasons: Counter[str] = field(default_factory=Counter)

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "since": _iso(self.since),
            "pages": self.pages,
            "scanned": self.scanned,
            "fetched": self.fetched,
            "added": self.added,
            "duplicates": self.duplicates,
            "skipped": self.skipped,
            "checkpoint": _iso(self.checkpoint) if self.checkpoint is not None else None,
            "skip_reasons": dict(sorted(self.skip_reasons.items())),
        }


class RunSynchronizer:
    """Import complete provider polling windows and checkpoint only on success."""

    def __init__(self, store: SkillStore, mapper: RunMapper,
                 overlap_seconds: float = 600.0,
                 default_lookback_seconds: float = 86400.0,
                 max_pages: int = 1000) -> None:
        self.store = store
        self.mapper = mapper
        self.overlap_seconds = max(0.0, overlap_seconds)
        self.default_lookback_seconds = max(0.0, default_lookback_seconds)
        self.max_pages = max_pages

    def sync(self, source: RunSource, since: float | None = None,
             use_checkpoint: bool = True) -> SyncResult:
        checkpoint_key = f"{source.stream_id}|{self.mapper.config.checkpoint_key}"
        saved = (self.store.get_integration_checkpoint(checkpoint_key)
                 if use_checkpoint and since is None else None)
        if since is not None:
            effective_since = since
        elif saved is not None:
            effective_since = max(0.0, saved - self.overlap_seconds)
        else:
            effective_since = max(0.0, time.time() - self.default_lookback_seconds)

        result = SyncResult(provider=source.name, since=effective_since)
        cursor: str | None = None
        seen_cursors: set[str] = set()
        completed_watermark: float | None = None

        while True:
            if result.pages >= self.max_pages:
                raise IntegrationError(
                    f"{source.name} exceeded the {self.max_pages}-page safety limit")
            batch = source.fetch_runs(since=effective_since, cursor=cursor)
            result.pages += 1
            result.scanned += batch.scanned
            result.fetched += len(batch.runs)
            if batch.watermark is not None:
                completed_watermark = batch.watermark

            for source_run in batch.runs:
                try:
                    run = self.mapper.map(source_run, self.store)
                except MappingError as exc:
                    result.skipped += 1
                    result.skip_reasons[str(exc)] += 1
                    continue
                if self.store.record_skill_run(run):
                    result.added += 1
                else:
                    result.duplicates += 1

            next_cursor = batch.next_cursor
            if next_cursor is None:
                break
            if next_cursor == cursor or next_cursor in seen_cursors:
                raise IntegrationError(
                    f"{source.name} returned a repeated pagination cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        if use_checkpoint and completed_watermark is not None:
            self.store.set_integration_checkpoint(checkpoint_key, completed_watermark)
            result.checkpoint = completed_watermark
        return result


_DURATION = re.compile(r"^(?P<amount>\d+(?:\.\d+)?)(?P<unit>[smhdw])$")
_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_since(value: str, now: float | None = None) -> float:
    """Parse an epoch, ISO-8601 timestamp, or lookback such as ``24h``."""
    raw = value.strip()
    match = _DURATION.fullmatch(raw.lower())
    if match:
        current = time.time() if now is None else now
        return max(0.0, current - float(match.group("amount")) * _SECONDS[match.group("unit")])
    try:
        return float(raw)
    except ValueError:
        pass
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(
            f"invalid --since value {value!r}; use ISO-8601, epoch seconds, or 24h/7d") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _iso(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")
