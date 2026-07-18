"""Provider-neutral records used by observability integrations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SourceEvaluation:
    """One score or annotation attached to a provider trace."""

    name: str
    value: bool | int | float | str | None
    comment: str = ""
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "comment": self.comment,
            "source": self.source,
            "metadata": self.metadata,
        }


@dataclass
class SourceRun:
    """A complete trace normalized without assuming a Skill mapping."""

    source_id: str
    source: str
    ts: float
    success_hint: bool | None
    name: str = ""
    input_data: dict[str, Any] = field(default_factory=dict)
    output_data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    model: str = ""
    session_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    evaluations: dict[str, SourceEvaluation] = field(default_factory=dict)


@dataclass
class IngestBatch:
    """One cursor page returned by a RunSource."""

    runs: list[SourceRun]
    next_cursor: str | None = None
    watermark: float | None = None
    scanned: int = 0
