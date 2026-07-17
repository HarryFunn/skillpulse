"""Offline replay gate for repaired Skill versions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Union

from .models import ReplayCase, ReplayReport, SkillRun
from .store import SkillStore

# Evaluate candidate content against one historical run. Implementations may
# call a sandbox, an agent runtime, or a deterministic test harness.
ReplayFn = Callable[[str, SkillRun], Union[bool, tuple[bool, str]]]


@dataclass
class ReplayConfig:
    min_cases: int = 5
    min_failed_cases: int = 1
    min_fix_rate: float = 0.6
    max_regression_rate: float = 0.1


class ReplayGate:
    def __init__(self, store: SkillStore,
                 config: ReplayConfig | None = None) -> None:
        self.store = store
        self.config = config or ReplayConfig()

    def evaluate(self, skill_id: str, candidate_version: int,
                 replay_fn: ReplayFn) -> ReplayReport:
        candidate = self.store.get_version(skill_id, candidate_version)
        if candidate is None:
            raise KeyError(f"unknown version: {skill_id}@{candidate_version}")
        if candidate.parent_version is None:
            raise ValueError("offline replay requires a repaired version with a parent")

        historical = self.store.get_skill_runs(skill_id, candidate.parent_version)
        cases: list[ReplayCase] = []
        for run in historical:
            outcome = replay_fn(candidate.content, run)
            if isinstance(outcome, tuple):
                success, error = bool(outcome[0]), str(outcome[1])
            else:
                success, error = bool(outcome), ""
            cases.append(ReplayCase(run_id=run.run_id,
                                    baseline_success=run.success,
                                    candidate_success=success,
                                    error=error))
        report = self._build_report(skill_id, candidate_version,
                                    candidate.parent_version, cases)
        self.store.save_replay_report(report)
        self.store.log_event(skill_id, "replay_completed", {
            "candidate_version": candidate_version,
            "passed": report.passed,
            "fix_rate": report.fix_rate,
            "regression_rate": report.regression_rate,
            "total_cases": report.total_cases,
            "reasons": report.reasons,
        })
        return report

    def evaluate_results(self, skill_id: str, candidate_version: int,
                         results: dict[str, bool]) -> ReplayReport:
        """Evaluate externally-produced results keyed by historical run_id."""
        return self.evaluate(
            skill_id, candidate_version,
            lambda _content, run: results.get(run.run_id, False),
        )

    def _build_report(self, skill_id: str, candidate_version: int,
                      parent_version: int,
                      cases: list[ReplayCase]) -> ReplayReport:
        cfg = self.config
        failed = [c for c in cases if not c.baseline_success]
        successful = [c for c in cases if c.baseline_success]
        fixed = sum(c.candidate_success for c in failed)
        regressions = sum(not c.candidate_success for c in successful)
        fix_rate = fixed / len(failed) if failed else 0.0
        regression_rate = regressions / len(successful) if successful else 0.0

        reasons: list[str] = []
        if len(cases) < cfg.min_cases:
            reasons.append(f"only {len(cases)} cases (< {cfg.min_cases})")
        if len(failed) < cfg.min_failed_cases:
            reasons.append(
                f"only {len(failed)} historical failures (< {cfg.min_failed_cases})")
        if fix_rate < cfg.min_fix_rate:
            reasons.append(
                f"fix rate {fix_rate:.0%} below {cfg.min_fix_rate:.0%}")
        if regression_rate > cfg.max_regression_rate:
            reasons.append(
                f"regression rate {regression_rate:.0%} above "
                f"{cfg.max_regression_rate:.0%}")
        passed = not reasons
        if passed:
            reasons.append("offline replay gate passed")

        return ReplayReport(
            skill_id=skill_id, candidate_version=candidate_version,
            parent_version=parent_version, total_cases=len(cases),
            failed_cases=len(failed), successful_cases=len(successful),
            fixed_failures=fixed, regressions=regressions,
            fix_rate=fix_rate, regression_rate=regression_rate,
            passed=passed, reasons=reasons,
        )
