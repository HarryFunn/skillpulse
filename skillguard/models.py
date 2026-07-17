"""Data models for skills, versions, executions, and health reports."""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field


class SkillState(str, enum.Enum):
    """Lifecycle state of a skill version.

    State machine:
        candidate -> probation -> active -> (degraded) -> retired
                          \\-> rejected
    """

    CANDIDATE = "candidate"    # newly created/repaired, not yet trialed
    PROBATION = "probation"    # in trial: routed a small share of traffic
    ACTIVE = "active"          # current serving version
    DEGRADED = "degraded"      # flagged by degradation detection
    RETIRED = "retired"        # replaced or pruned
    REJECTED = "rejected"      # failed probation


@dataclass
class SkillVersion:
    """One immutable version of a skill's implementation."""

    skill_id: str
    version: int
    content: str                       # code / prompt / SKILL.md body
    state: SkillState = SkillState.CANDIDATE
    created_at: float = field(default_factory=time.time)
    parent_version: int | None = None  # version this was repaired from
    repair_note: str = ""              # why this version exists

    @property
    def key(self) -> str:
        return f"{self.skill_id}@{self.version}"


@dataclass
class Skill:
    """A named skill with a pointer to its active version."""

    skill_id: str
    name: str
    description: str = ""
    active_version: int | None = None
    created_at: float = field(default_factory=time.time)


@dataclass
class ExecutionRecord:
    """Outcome of one invocation of a specific skill version."""

    skill_id: str
    version: int
    success: bool
    ts: float = field(default_factory=time.time)
    latency_ms: float | None = None
    error: str = ""                    # error class/message on failure
    task_tag: str = ""                 # optional task-type label
    model: str = ""                    # model that ran the skill (for attribution)


@dataclass
class HealthReport:
    """Health diagnosis for one skill version."""

    skill_id: str
    version: int
    n_total: int
    n_recent: int
    baseline_rate: float | None        # long-run success rate (None if too few runs)
    recent_rate: float | None          # recent-window success rate
    ewma_rate: float | None            # exponentially weighted success rate
    z_score: float | None              # two-proportion z (recent vs baseline)
    staleness_days: float              # days since last execution
    degraded: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.degraded:
            return "DEGRADED"
        if self.n_total == 0:
            return "UNKNOWN"
        return "HEALTHY"
