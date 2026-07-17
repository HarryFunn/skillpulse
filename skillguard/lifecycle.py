"""Lifecycle manager: repair sub-flow, probation trials, promote/rollback.

The core idea: when a skill degrades, do NOT replan the whole task and do NOT
silently overwrite the skill. Instead:

    1. `flag`    - degradation detection marks the active version DEGRADED
    2. `repair`  - a new CANDIDATE version is created (by an LLM, a human,
                   or any repair callback), linked to its parent
    3. `trial`   - the candidate enters PROBATION; the router sends it a
                   small share of traffic while the old version keeps serving
    4. `promote` - if probation stats clear the bar, candidate becomes ACTIVE
                   and the old version is RETIRED
    5. `rollback`- if probation fails, candidate is REJECTED and the previous
                   healthy version stays/returns ACTIVE
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Callable

from .health import HealthChecker, HealthConfig
from .models import SkillState, SkillVersion
from .store import SkillStore

# A repair function takes (old content, degradation reasons) and returns new content.
RepairFn = Callable[[str, list[str]], str]


@dataclass
class ProbationConfig:
    traffic_share: float = 0.2       # share of calls routed to the probation version
    min_trials: int = 10             # trials needed before a promote/reject decision
    promote_threshold: float = 0.8   # probation success rate required to promote


class LifecycleManager:
    def __init__(self, store: SkillStore,
                 health_config: HealthConfig | None = None,
                 probation_config: ProbationConfig | None = None) -> None:
        self.store = store
        self.checker = HealthChecker(store, health_config)
        self.probation = probation_config or ProbationConfig()

    # -- routing -------------------------------------------------------------

    def route(self, skill_id: str, rng: random.Random | None = None) -> SkillVersion:
        """Pick which version should serve the next call.

        If a probation version exists, it gets `traffic_share` of calls;
        otherwise the active version serves everything.
        """
        rng = rng or random.Random()
        skill = self.store.get_skill(skill_id)
        if skill is None:
            raise KeyError(f"unknown skill: {skill_id}")

        probation_version = self._find_version_in_state(skill_id, SkillState.PROBATION)
        active = (self.store.get_version(skill_id, skill.active_version)
                  if skill.active_version is not None else None)

        if probation_version is not None and (
            active is None or rng.random() < self.probation.traffic_share
        ):
            return probation_version
        if active is None:
            raise RuntimeError(f"skill {skill_id} has no active version")
        return active

    # -- degradation flagging --------------------------------------------------

    def scan(self) -> list[str]:
        """Run degradation detection on all active versions; flag degraded ones.

        Returns the ids of skills that were newly flagged.
        """
        flagged = []
        for report in self.checker.check_all_active():
            if not report.degraded:
                continue
            version = self.store.get_version(report.skill_id, report.version)
            if version is not None and version.state == SkillState.ACTIVE:
                self.store.set_version_state(report.skill_id, report.version,
                                             SkillState.DEGRADED)
                self.store.log_event(report.skill_id, "degradation_flagged",
                                     {"version": report.version,
                                      "reasons": report.reasons})
                flagged.append(report.skill_id)
        return flagged

    # -- repair sub-flow ---------------------------------------------------------

    def repair(self, skill_id: str, repair_fn: RepairFn,
               note: str = "auto-repair") -> SkillVersion:
        """Create a repaired CANDIDATE version from the degraded/active version
        and immediately move it into PROBATION.
        """
        skill = self.store.get_skill(skill_id)
        if skill is None:
            raise KeyError(f"unknown skill: {skill_id}")
        if skill.active_version is None:
            raise RuntimeError(f"skill {skill_id} has no version to repair")

        current = self.store.get_version(skill_id, skill.active_version)
        assert current is not None
        report = self.checker.check(skill_id, current.version)

        new_content = repair_fn(current.content, report.reasons)
        candidate = self.store.add_version(
            skill_id, new_content,
            parent_version=current.version,
            repair_note=f"{note}; triggered by: {'; '.join(report.reasons)}",
        )
        self.store.set_version_state(skill_id, candidate.version, SkillState.PROBATION)
        candidate.state = SkillState.PROBATION
        return candidate

    # -- probation decisions ---------------------------------------------------

    def evaluate_probation(self, skill_id: str) -> str:
        """Decide the fate of a probation version.

        Returns one of: "pending" (not enough trials), "promoted", "rejected".
        """
        probation_version = self._find_version_in_state(skill_id, SkillState.PROBATION)
        if probation_version is None:
            return "pending"

        runs = self.store.get_executions(skill_id, probation_version.version)
        if len(runs) < self.probation.min_trials:
            return "pending"

        success_rate = sum(r.success for r in runs) / len(runs)
        if success_rate >= self.probation.promote_threshold:
            self.promote(skill_id, probation_version.version)
            return "promoted"
        self.rollback(skill_id, probation_version.version)
        return "rejected"

    def promote(self, skill_id: str, version: int) -> None:
        """Make `version` the active one; retire the previous active version."""
        skill = self.store.get_skill(skill_id)
        if skill is None:
            raise KeyError(f"unknown skill: {skill_id}")
        if skill.active_version is not None and skill.active_version != version:
            self.store.set_version_state(skill_id, skill.active_version,
                                         SkillState.RETIRED)
        self.store.set_version_state(skill_id, version, SkillState.ACTIVE)
        self.store.set_active_version(skill_id, version)
        self.store.log_event(skill_id, "promoted", {"version": version})

    def rollback(self, skill_id: str, version: int) -> None:
        """Reject a probation/candidate version; the active version keeps serving.

        If the active version was flagged DEGRADED, it stays flagged — a further
        repair attempt or manual intervention is expected.
        """
        self.store.set_version_state(skill_id, version, SkillState.REJECTED)
        self.store.log_event(skill_id, "rolled_back", {"version": version})

    def activate_initial(self, skill_id: str) -> None:
        """Promote a brand-new skill's first version straight to ACTIVE.

        Skips probation: there is no incumbent to compare against.
        """
        versions = self.store.list_versions(skill_id)
        if not versions:
            raise RuntimeError(f"skill {skill_id} has no versions")
        self.promote(skill_id, versions[0].version)

    # -- helpers ----------------------------------------------------------------

    def _find_version_in_state(self, skill_id: str,
                               state: SkillState) -> SkillVersion | None:
        for v in reversed(self.store.list_versions(skill_id)):
            if v.state == state:
                return v
        return None
