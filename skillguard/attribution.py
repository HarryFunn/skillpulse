"""Root-cause attribution for skill degradation.

Detecting *that* a skill degraded is not enough to decide what to do about it.
The same symptom (success rate fell) has very different fixes depending on the
cause. SkillGuard attributes a degradation to one of four root causes and maps
each to a recommended action:

    ENVIRONMENT_DRIFT  external world changed (API/page/schema)   -> repair skill
    MODEL_CHANGE       a different model started running the skill -> re-verify / adapt prompt
    TASK_DRIFT         skill invoked on out-of-distribution tasks  -> narrow scope, don't repair
    SKILL_DEFECT       intrinsically flaky/wrong regardless        -> rewrite skill

The classifier is deliberately transparent: each cause is scored from a small
set of interpretable signals derived from the execution stream, and the report
carries the evidence behind the decision.
"""

from __future__ import annotations

import enum
from collections import Counter
from dataclasses import dataclass, field

from .models import ExecutionRecord
from .store import SkillStore


class Cause(str, enum.Enum):
    ENVIRONMENT_DRIFT = "environment_drift"
    MODEL_CHANGE = "model_change"
    TASK_DRIFT = "task_drift"
    SKILL_DEFECT = "skill_defect"
    UNKNOWN = "unknown"


# Cause -> recommended lifecycle action.
RECOMMENDED_ACTION: dict[Cause, str] = {
    Cause.ENVIRONMENT_DRIFT: "repair: update the skill to match the changed environment",
    Cause.MODEL_CHANGE: "re-verify: the fix may be prompt/model adaptation, not skill logic",
    Cause.TASK_DRIFT: "narrow scope: skill is used out-of-distribution; tighten its trigger instead of repairing",
    Cause.SKILL_DEFECT: "rewrite: the skill is intrinsically unreliable; regenerate rather than patch",
    Cause.UNKNOWN: "collect more execution data before acting",
}


@dataclass
class AttributionReport:
    skill_id: str
    version: int
    cause: Cause
    confidence: float                      # 0..1
    recommended_action: str
    scores: dict[str, float] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)


@dataclass
class AttributionConfig:
    recent_window: int = 20        # runs forming the "recent" (degraded) sample
    min_failures: int = 5          # need at least this many recent failures to attribute
    changepoint_sharp: float = 0.5 # baseline-minus-recent success gap that counts as "sharp"
    error_dominance: float = 0.6   # top error signature share that counts as "dominant"
    ood_task_share: float = 0.6    # share of failing runs on unseen tasks -> task drift
    model_shift_share: float = 0.6 # share of failing runs on a new model -> model change


class Attributor:
    def __init__(self, store: SkillStore, config: AttributionConfig | None = None) -> None:
        self.store = store
        self.config = config or AttributionConfig()

    def attribute(self, skill_id: str, version: int) -> AttributionReport:
        cfg = self.config
        runs = self.store.get_executions(skill_id, version)
        recent = runs[-cfg.recent_window:]
        baseline = runs[:-cfg.recent_window]
        failing = [r for r in recent if not r.success]

        if len(failing) < cfg.min_failures:
            return AttributionReport(
                skill_id, version, Cause.UNKNOWN, 0.0,
                RECOMMENDED_ACTION[Cause.UNKNOWN],
                evidence=[f"only {len(failing)} recent failures (< {cfg.min_failures})"],
            )

        scores: dict[str, float] = {}
        evidence: list[str] = []

        # -- signal: change-point sharpness (sudden break vs always flaky) ------
        base_rate = _rate(baseline) if baseline else _rate(runs)
        recent_rate = _rate(recent)
        gap = base_rate - recent_rate
        sharp = gap >= cfg.changepoint_sharp
        if baseline:
            evidence.append(f"success {base_rate:.0%} (baseline) -> {recent_rate:.0%} (recent), "
                            f"gap {gap:+.0%}")

        # -- signal: dominant error signature -----------------------------------
        sig_counts = Counter(_error_signature(r.error) for r in failing if r.error)
        top_sig, top_share = "", 0.0
        if sig_counts:
            top_sig, top_n = sig_counts.most_common(1)[0]
            top_share = top_n / len(failing)
            evidence.append(f"dominant error '{top_sig}' covers {top_share:.0%} of failures")

        # -- signal: model shift -------------------------------------------------
        base_models = {r.model for r in baseline if r.success and r.model}
        fail_models = [r.model for r in failing if r.model]
        model_shift = 0.0
        if base_models and fail_models:
            new_model_hits = sum(1 for m in fail_models if m not in base_models)
            model_shift = new_model_hits / len(fail_models)
            if model_shift >= cfg.model_shift_share:
                evidence.append(
                    f"{model_shift:.0%} of failures ran on a model unseen in the healthy baseline "
                    f"({sorted(set(fail_models) - base_models)})")

        # -- signal: task out-of-distribution -----------------------------------
        base_tasks = {r.task_tag for r in baseline if r.success and r.task_tag}
        fail_tasks = [r.task_tag for r in failing if r.task_tag]
        ood_share = 0.0
        if base_tasks and fail_tasks:
            ood_hits = sum(1 for t in fail_tasks if t not in base_tasks)
            ood_share = ood_hits / len(fail_tasks)
            if ood_share >= cfg.ood_task_share:
                evidence.append(
                    f"{ood_share:.0%} of failures are on task types never seen when healthy "
                    f"({sorted(set(fail_tasks) - base_tasks)})")

        # -- combine into cause scores ------------------------------------------
        # Model change and task drift are the most specific; check them first.
        scores[Cause.MODEL_CHANGE.value] = model_shift
        scores[Cause.TASK_DRIFT.value] = ood_share
        # Environment drift: a sharp break with a shared error signature, same
        # model, same task distribution.
        env = 0.0
        if sharp:
            env += 0.5
        env += 0.5 * top_share
        if model_shift >= cfg.model_shift_share or ood_share >= cfg.ood_task_share:
            env *= 0.3   # a more specific cause explains it better
        scores[Cause.ENVIRONMENT_DRIFT.value] = env
        # Skill defect: no clean break (flaky throughout) and no external explanation.
        defect = 0.0
        if not sharp:
            defect += 0.5
        if top_share < cfg.error_dominance:
            defect += 0.3
        defect *= (1 - max(model_shift, ood_share))
        scores[Cause.SKILL_DEFECT.value] = defect

        cause_str = max(scores, key=scores.get)
        cause = Cause(cause_str)
        confidence = _confidence(scores)

        if cause == Cause.ENVIRONMENT_DRIFT and top_sig:
            evidence.append(f"interpreted as environment drift: sudden break around '{top_sig}'")

        return AttributionReport(
            skill_id, version, cause, confidence,
            RECOMMENDED_ACTION[cause], scores=scores, evidence=evidence,
        )


def _rate(runs: list[ExecutionRecord]) -> float:
    return sum(r.success for r in runs) / len(runs) if runs else 1.0


def _error_signature(error: str) -> str:
    """Collapse an error string to a coarse signature (its class / first token)."""
    if not error:
        return ""
    head = error.split(":", 1)[0].strip()
    return head or error.strip().split()[0]


def _confidence(scores: dict[str, float]) -> float:
    """Margin between the top score and the runner-up, clamped to 0..1."""
    ordered = sorted(scores.values(), reverse=True)
    if not ordered or ordered[0] <= 0:
        return 0.0
    top = ordered[0]
    second = ordered[1] if len(ordered) > 1 else 0.0
    return max(0.0, min(1.0, (top - second) + 0.3 * top))
