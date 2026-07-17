"""Machine-readable JSON reports for SkillPulse."""

from __future__ import annotations

import json
import math
from dataclasses import asdict

from .attribution import Attributor
from .health import HealthChecker
from .store import SkillStore


class JsonReporter:
    def __init__(self, store: SkillStore) -> None:
        self.store = store
        self.checker = HealthChecker(store)
        self.attributor = Attributor(store)

    def library(self) -> dict:
        skills = []
        for skill in self.store.list_skills():
            item = asdict(skill)
            item["versions"] = []
            for version in self.store.list_versions(skill.skill_id):
                version_data = asdict(version)
                version_data["state"] = version.state.value
                health = self.checker.check(skill.skill_id, version.version)
                health_data = asdict(health)
                if math.isinf(health_data["staleness_days"]):
                    health_data["staleness_days"] = None
                if health_data["z_score"] is not None and math.isinf(health_data["z_score"]):
                    health_data["z_score"] = None
                version_data["health"] = health_data
                replay = self.store.get_replay_report(skill.skill_id, version.version)
                version_data["replay"] = asdict(replay) if replay else None
                version_data["skill_run_count"] = len(
                    self.store.get_skill_runs(skill.skill_id, version.version))
                item["versions"].append(version_data)
            if skill.active_version is not None:
                attr = self.attributor.attribute(skill.skill_id, skill.active_version)
                item["active_attribution"] = {
                    "cause": attr.cause.value,
                    "confidence": attr.confidence,
                    "recommended_action": attr.recommended_action,
                    "scores": attr.scores,
                    "evidence": attr.evidence,
                }
            else:
                item["active_attribution"] = None
            skills.append(item)
        return {
            "schema_version": "1.0",
            "summary": {
                "skill_count": len(skills),
                "tool_call_count": len(self.store.get_tool_calls()),
                "degraded_count": sum(
                    1 for s in skills for v in s["versions"]
                    if v["state"] == "degraded"),
            },
            "skills": skills,
        }

    def dumps(self, indent: int = 2) -> str:
        return json.dumps(self.library(), indent=indent, sort_keys=True)
