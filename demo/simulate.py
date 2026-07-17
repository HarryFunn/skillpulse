"""End-to-end demo: a skill that silently breaks when its environment changes.

Story:
    A "web-scrape-title" skill works fine for a while. Then the target site
    changes its HTML, and the skill starts failing every call. SkillGuard:
      1. detects the degradation from the execution stream (not from any
         version number or git diff),
      2. flags the active version DEGRADED,
      3. accepts a repaired version into a canary probation trial,
      4. promotes the repaired version once it proves itself, retiring the old.

Run:  python -m demo.simulate   (from the repo root)
"""

from __future__ import annotations

import random
import tempfile
import time
from pathlib import Path

from skillguard import Attributor, ExecutionRecord, LifecycleManager, SkillStore
from skillguard.lifecycle import ProbationConfig

SKILL = "web-scrape-title"


def line(title: str) -> None:
    print(f"\n{'=' * 4} {title} {'=' * 4}")


def run() -> None:
    rng = random.Random(7)
    db = Path(tempfile.mkdtemp()) / "demo.db"
    store = SkillStore(db)
    mgr = LifecycleManager(
        store,
        probation_config=ProbationConfig(traffic_share=0.3, min_trials=15,
                                         promote_threshold=0.8),
    )

    line("1. register skill and warm it up (healthy)")
    store.add_skill(SKILL, "Scrape page <title>", content="css_selector = 'head > title'")
    mgr.activate_initial(SKILL)
    # start ~130 minutes ago so the final execution lands near "now"
    ts = time.time() - 130 * 60
    for _ in range(40):                       # baseline: reliably succeeds
        store.record_execution(ExecutionRecord(SKILL, 1, True, ts=ts,
                                               task_tag="scrape", model="gpt-x"))
        ts += 60
    _print_status(mgr)

    line("2. the target site changes its HTML -> skill silently breaks")
    for _ in range(25):                       # environment drift: now failing
        store.record_execution(ExecutionRecord(
            SKILL, 1, False, ts=ts, error="SelectorNotFound: head > title",
            task_tag="scrape", model="gpt-x"))
        ts += 60
    _print_status(mgr)

    line("3. doctor scan detects degradation")
    report = mgr.checker.check(SKILL, 1)
    print("diagnosis:", "; ".join(report.reasons))
    flagged = mgr.scan()
    print("flagged as DEGRADED:", flagged)

    line("4. attribute the root cause -> pick the right action")
    attr = Attributor(store).attribute(SKILL, 1)
    print(f"root cause: {attr.cause.value} (confidence {attr.confidence:.0%})")
    print(f"action    : {attr.recommended_action}")
    for e in attr.evidence:
        print(f"  - {e}")

    line("5. repair -> new version enters canary probation")
    def repair_fn(old: str, reasons: list[str]) -> str:
        # a real system would call an LLM here; we simulate the fix
        return old.replace("head > title", "meta[property='og:title']")
    candidate = mgr.repair(SKILL, repair_fn, note="auto-repair after selector break")
    print(f"created {candidate.key} (PROBATION); content -> {candidate.content!r}")

    line("6. serve traffic; repaired version works, old one still broken")
    for _ in range(60):
        served = mgr.route(SKILL, rng)
        # v1 still broken, the repaired v2 succeeds
        ok = served.version == candidate.version
        err = "" if ok else "SelectorNotFound: head > title"
        store.record_execution(ExecutionRecord(SKILL, served.version, ok, ts=ts,
                                               error=err, task_tag="scrape", model="gpt-x"))
        ts += 60
    decision = mgr.evaluate_probation(SKILL)
    print("probation decision:", decision)
    _print_status(mgr)

    line("7. audit trail")
    for e in store.get_events(SKILL):
        print(f"  {e['kind']}: {e['payload']}")

    store.close()


def _print_status(mgr: LifecycleManager) -> None:
    for r in mgr.checker.check_all_active():
        rate = f"{r.recent_rate:.0%}" if r.recent_rate is not None else "-"
        print(f"  {r.skill_id}@{r.version}: status={r.status} recent={rate} "
              f"runs={r.n_total}")


if __name__ == "__main__":
    run()
