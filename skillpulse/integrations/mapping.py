"""Explicitly map provider traces into SkillPulse SkillRuns."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..models import SkillRun
from ..store import SkillStore
from .models import SourceEvaluation, SourceRun


class MappingError(ValueError):
    """A source trace cannot be mapped without guessing."""


@dataclass(frozen=True)
class MappingConfig:
    """Fallback identity and evaluation semantics for a provider stream.

    Without ``skill_id`` and ``version`` fallbacks, root trace metadata must use
    ``skillpulse.skill_id`` and optionally ``skillpulse.version``. A missing
    version resolves to the registered Skill's active version.
    """

    skill_id: str | None = None
    version: int | None = None
    success_score: str | None = None
    success_threshold: float = 0.5
    success_labels: tuple[str, ...] = (
        "true", "pass", "passed", "success", "succeeded", "correct", "ok",
    )
    failure_labels: tuple[str, ...] = (
        "false", "fail", "failed", "failure", "incorrect", "error",
    )

    @property
    def checkpoint_key(self) -> str:
        return ":".join((
            self.skill_id or "metadata",
            str(self.version) if self.version is not None else "active",
            self.success_score or "provider-status",
            str(self.success_threshold),
        ))


class RunMapper:
    def __init__(self, config: MappingConfig | None = None) -> None:
        self.config = config or MappingConfig()

    def map(self, source_run: SourceRun, store: SkillStore) -> SkillRun:
        skill_id = self.config.skill_id or _first(source_run.metadata, (
            "skillpulse.skill_id",
            "skillpulse.skill.id",
            "metadata.skillpulse.skill_id",
            "metadata.skillpulse.skill.id",
        ))
        if not skill_id:
            raise MappingError("missing skill identity (set --skill-id or skillpulse.skill_id)")
        skill_id = str(skill_id)
        skill = store.get_skill(skill_id)
        if skill is None:
            raise MappingError(f"unknown skill: {skill_id}")

        raw_version = self.config.version
        if raw_version is None:
            raw_version = _first(source_run.metadata, (
                "skillpulse.version",
                "skillpulse.skill_version",
                "skillpulse.skill.version",
                "metadata.skillpulse.version",
                "metadata.skillpulse.skill_version",
            ))
        if raw_version is None:
            raw_version = skill.active_version
        try:
            version = int(raw_version) if raw_version is not None else None
        except (TypeError, ValueError) as exc:
            raise MappingError(f"invalid version for {skill_id}: {raw_version!r}") from exc
        if version is None:
            raise MappingError(f"skill {skill_id} has no active version")
        if store.get_version(skill_id, version) is None:
            raise MappingError(f"unknown version: {skill_id}@{version}")

        success, decision = self._success(source_run)
        task_tag = _first(source_run.metadata, (
            "skillpulse.task_tag", "skillpulse.task.tag",
            "metadata.skillpulse.task_tag", "task_tag",
        )) or source_run.name
        error = source_run.error
        if not success and not error:
            error = decision

        metadata = dict(source_run.metadata)
        metadata.setdefault("skillpulse.source_run_name", source_run.name)
        metadata["skillpulse.outcome_basis"] = decision
        evaluations = {
            name: evaluation.to_dict()
            for name, evaluation in source_run.evaluations.items()
        }
        return SkillRun(
            run_id=source_run.source_id,
            skill_id=skill_id,
            version=version,
            success=success,
            ts=source_run.ts,
            input_data=source_run.input_data,
            output_data=source_run.output_data,
            error=error,
            task_tag=str(task_tag),
            model=source_run.model,
            source=source_run.source,
            session_id=source_run.session_id,
            metadata=metadata,
            evaluations=evaluations,
        )

    def _success(self, run: SourceRun) -> tuple[bool, str]:
        score_name = self.config.success_score
        if score_name:
            evaluation = run.evaluations.get(score_name)
            if evaluation is None:
                raise MappingError(f"missing required evaluation: {score_name}")
            success = self._evaluation_success(evaluation)
            return success, f"evaluation {score_name}={evaluation.value!r}"

        explicit = _first(run.metadata, (
            "skillpulse.success", "skillpulse.outcome.success",
            "metadata.skillpulse.success",
        ))
        if explicit is not None:
            success = _as_bool(explicit, self.config)
            return success, f"metadata skillpulse.success={explicit!r}"
        if run.success_hint is None:
            raise MappingError(
                "missing outcome (set --success-score or skillpulse.success)")
        return run.success_hint, "provider root status"

    def _evaluation_success(self, evaluation: SourceEvaluation) -> bool:
        value = evaluation.value
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return float(value) >= self.config.success_threshold
        return _as_bool(value, self.config)


def _first(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = _lookup(data, key)
        if value is not None and value != "":
            return value
    return None


def _lookup(data: dict[str, Any], path: str) -> Any:
    if path in data:
        return data[path]
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _as_bool(value: Any, config: MappingConfig) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in config.success_labels:
        return True
    if normalized in config.failure_labels:
        return False
    raise MappingError(f"cannot interpret outcome value: {value!r}")
