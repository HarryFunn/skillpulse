"""Public protocol and errors for observability run sources."""

from __future__ import annotations

from typing import Protocol

from .models import IngestBatch


class IntegrationError(RuntimeError):
    """A provider request or synchronization contract failed."""


class RunSource(Protocol):
    """A paginated source of provider-neutral trace outcomes."""

    name: str
    stream_id: str

    def fetch_runs(
        self,
        since: float | None = None,
        cursor: str | None = None,
    ) -> IngestBatch:
        ...
