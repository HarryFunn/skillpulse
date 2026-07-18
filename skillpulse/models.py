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
    """Legacy direct outcome record for one skill-version invocation.

    New integrations should prefer `SkillRun`, which represents the final
    skill-level outcome and can own multiple `ToolCall` records. This model is
    retained for backwards compatibility and low-level/manual instrumentation.
    """

    skill_id: str
    version: int
    success: bool
    ts: float = field(default_factory=time.time)
    latency_ms: float | None = None
    error: str = ""                    # error class/message on failure
    task_tag: str = ""                 # optional task-type label
    model: str = ""                    # model that ran the skill (for attribution)
    execution_id: str = ""             # stable source id; empty for manual records
    source: str = "manual"


@dataclass
class ToolCall:
    """One tool invocation observed inside an agent session or SkillRun."""

    call_id: str
    name: str
    success: bool
    ts: float = field(default_factory=time.time)
    session_id: str = ""
    run_id: str | None = None
    model: str = ""
    error: str = ""
    task_tag: str = ""
    source: str = "manual"
    source_path: str = ""


@dataclass
class SkillRun:
    """Final outcome of one Skill execution, distinct from its tool calls."""

    run_id: str
    skill_id: str
    version: int
    success: bool
    ts: float = field(default_factory=time.time)
    input_data: dict = field(default_factory=dict)
    output_data: dict = field(default_factory=dict)
    error: str = ""
    task_tag: str = ""
    model: str = ""
    source: str = "manual"
    session_id: str = ""
    metadata: dict = field(default_factory=dict)
    evaluations: dict = field(default_factory=dict)


@dataclass
class ReplayCase:
    """A historical SkillRun evaluated against a repaired candidate."""

    run_id: str
    baseline_success: bool
    candidate_success: bool
    error: str = ""


@dataclass
class ReplayReport:
    """Offline replay gate result for a candidate skill version."""

    skill_id: str
    candidate_version: int
    parent_version: int
    total_cases: int
    failed_cases: int
    successful_cases: int
    fixed_failures: int
    regressions: int
    fix_rate: float
    regression_rate: float
    passed: bool
    reasons: list[str] = field(default_factory=list)


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
