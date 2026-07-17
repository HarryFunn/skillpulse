"""Health scoring and degradation detection for skill versions.

Degradation is flagged by combining three signals:

1. Recent-vs-baseline drop: a one-sided two-proportion z-test comparing the
   recent window's success rate against the long-run baseline. This catches
   "the environment changed and the skill silently broke".
2. EWMA success rate falling below an absolute floor. This catches skills
   that were never good or decayed gradually.
3. Staleness: no executions for a long time. Stale skills are not failed,
   but they are unverified and should be surfaced for review.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from .models import ExecutionRecord, HealthReport, SkillRun
from .store import SkillStore


@dataclass
class HealthConfig:
    recent_window: int = 20          # last N executions form the "recent" sample
    min_total_runs: int = 10         # below this, not enough data to judge
    min_recent_runs: int = 5         # below this, skip the z-test
    z_threshold: float = 1.645       # one-sided 95% confidence
    ewma_alpha: float = 0.2          # weight of the newest observation
    ewma_floor: float = 0.5          # EWMA below this => degraded
    stale_after_days: float = 30.0   # no runs for this long => stale warning
    stale_is_degraded: bool = False  # staleness alone flags DEGRADED if True


class HealthChecker:
    def __init__(self, store: SkillStore, config: HealthConfig | None = None) -> None:
        self.store = store
        self.config = config or HealthConfig()

    def check(self, skill_id: str, version: int, now: float | None = None) -> HealthReport:
        now = now or time.time()
        cfg = self.config
        runs = self.store.get_skill_runs(skill_id, version)
        if not runs:
            # Backwards compatibility for v0.1/manual instrumentation.
            runs = self.store.get_executions(skill_id, version)

        n_total = len(runs)
        if n_total == 0:
            return HealthReport(
                skill_id=skill_id, version=version, n_total=0, n_recent=0,
                baseline_rate=None, recent_rate=None, ewma_rate=None,
                z_score=None, staleness_days=math.inf, degraded=False,
                reasons=["no execution data"],
            )

        recent = runs[-cfg.recent_window:]
        baseline = runs[:-cfg.recent_window] if n_total > cfg.recent_window else []

        recent_rate = _success_rate(recent)
        baseline_rate = _success_rate(baseline) if baseline else _success_rate(runs)
        ewma_rate = _ewma(runs, cfg.ewma_alpha)
        staleness_days = (now - runs[-1].ts) / 86400.0

        reasons: list[str] = []
        degraded = False

        # Signal 1: statistically significant drop, recent vs baseline.
        z: float | None = None
        if baseline and len(recent) >= cfg.min_recent_runs and n_total >= cfg.min_total_runs:
            z = _two_proportion_z(
                successes_a=sum(r.success for r in baseline), n_a=len(baseline),
                successes_b=sum(r.success for r in recent), n_b=len(recent),
            )
            # positive z means baseline > recent, i.e. performance dropped
            if z is not None and z > cfg.z_threshold:
                degraded = True
                reasons.append(
                    f"success rate dropped: baseline {baseline_rate:.0%} -> "
                    f"recent {recent_rate:.0%} (z={z:.2f} > {cfg.z_threshold})"
                )

        # Signal 2: EWMA below absolute floor.
        if n_total >= cfg.min_total_runs and ewma_rate < cfg.ewma_floor:
            degraded = True
            reasons.append(f"EWMA success rate {ewma_rate:.0%} below floor {cfg.ewma_floor:.0%}")

        # Signal 3: staleness.
        if staleness_days > cfg.stale_after_days:
            reasons.append(f"stale: no executions for {staleness_days:.0f} days")
            if cfg.stale_is_degraded:
                degraded = True

        if not reasons:
            reasons.append("healthy")

        return HealthReport(
            skill_id=skill_id, version=version, n_total=n_total, n_recent=len(recent),
            baseline_rate=baseline_rate, recent_rate=recent_rate, ewma_rate=ewma_rate,
            z_score=z, staleness_days=staleness_days, degraded=degraded, reasons=reasons,
        )

    def check_all_active(self, now: float | None = None) -> list[HealthReport]:
        """Diagnose the active version of every skill in the library."""
        reports = []
        for skill in self.store.list_skills():
            if skill.active_version is None:
                continue
            reports.append(self.check(skill.skill_id, skill.active_version, now=now))
        return reports


def _success_rate(runs: list[ExecutionRecord] | list[SkillRun]) -> float:
    return sum(r.success for r in runs) / len(runs)


def _ewma(runs: list[ExecutionRecord] | list[SkillRun], alpha: float) -> float:
    value = 1.0 if runs[0].success else 0.0
    for r in runs[1:]:
        value = alpha * (1.0 if r.success else 0.0) + (1 - alpha) * value
    return value


def _two_proportion_z(successes_a: int, n_a: int,
                      successes_b: int, n_b: int) -> float | None:
    """z-score for H0: p_a == p_b. Positive when sample A outperforms sample B."""
    if n_a == 0 or n_b == 0:
        return None
    p_a = successes_a / n_a
    p_b = successes_b / n_b
    p_pool = (successes_a + successes_b) / (n_a + n_b)
    denom = math.sqrt(p_pool * (1 - p_pool) * (1 / n_a + 1 / n_b))
    if denom == 0:
        return 0.0
    return (p_a - p_b) / denom
