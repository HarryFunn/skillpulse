"""End-to-end v0.2 demo: detect, attribute, replay, canary, promote."""

from __future__ import annotations

import random
import tempfile
import time
from pathlib import Path

from skillpulse import (
    Attributor,
    LifecycleManager,
    ReplayConfig,
    SkillRun,
    SkillStore,
)
from skillpulse.lifecycle import ProbationConfig

SKILL = "web-scrape-title"


def line(title: str) -> None:
    print(f"\n{'=' * 4} {title} {'=' * 4}")


def run() -> None:
    rng = random.Random(7)
    store = SkillStore(Path(tempfile.mkdtemp()) / "demo.db")
    manager = LifecycleManager(
        store,
        replay_config=ReplayConfig(min_cases=10, min_failed_cases=5,
                                   min_fix_rate=0.8, max_regression_rate=0.1),
        probation_config=ProbationConfig(traffic_share=0.3, min_trials=15,
                                         promote_threshold=0.8),
    )

    line("1. register skill and collect final SkillRun outcomes")
    store.add_skill(SKILL, "Scrape page <title>",
                    content="css_selector = 'head > title'")
    manager.activate_initial(SKILL)
    ts = time.time() - 130 * 60
    for index in range(40):
        _record_run(store, f"healthy-{index}", 1, True, ts)
        ts += 60
    _print_status(manager)

    line("2. environment changes; complete Skill executions start failing")
    for index in range(25):
        _record_run(store, f"failed-{index}", 1, False, ts,
                    error="SelectorNotFound: head > title")
        ts += 60
    _print_status(manager)

    line("3. detect and attribute degradation")
    print("flagged:", manager.scan())
    attribution = Attributor(store).attribute(SKILL, 1)
    print(f"root cause: {attribution.cause.value} (score {attribution.confidence:.2f})")
    print("action    :", attribution.recommended_action)

    line("4. external rule authors a candidate; SkillPulse stores it")
    external_rule = lambda old, _reasons: old.replace(
        "head > title", "meta[property='og:title']")
    candidate = manager.repair(
        SKILL,
        external_rule,
        note="candidate authored by an external selector rule",
    )
    print(f"created {candidate.key} [{candidate.state.value}]")
    print("routed before replay:", manager.route(SKILL, rng).key)

    line("5. replay historical success and failure cases offline")
    replay = manager.replay(
        SKILL, candidate.version,
        # The candidate preserves historical successes and fixes failures.
        lambda _content, _historical_run: True,
    )
    print(f"passed={replay.passed} fix_rate={replay.fix_rate:.0%} "
          f"regression_rate={replay.regression_rate:.0%}")
    print("candidate state:", store.get_version(SKILL, candidate.version).state.value)

    line("6. canary serves live SkillRuns, then promotes")
    candidate_runs = 0
    while candidate_runs < 15:
        served = manager.route(SKILL, rng)
        ok = served.version == candidate.version
        _record_run(store, f"live-{ts}", served.version, ok, ts,
                    error="SelectorNotFound" if not ok else "")
        if served.version == candidate.version:
            candidate_runs += 1
        ts += 60
    print("probation decision:", manager.evaluate_probation(SKILL))
    _print_status(manager)

    line("7. audit trail")
    for event in store.get_events(SKILL):
        print(f"  {event['kind']}: {event['payload']}")
    store.close()


def _record_run(store: SkillStore, run_id: str, version: int, success: bool,
                ts: float, error: str = "") -> None:
    store.record_skill_run(SkillRun(
        run_id=run_id, skill_id=SKILL, version=version, success=success, ts=ts,
        error=error, task_tag="scrape", model="gpt-x",
        input_data={"url": "https://example.test"},
    ))


def _print_status(manager: LifecycleManager) -> None:
    for report in manager.checker.check_all_active():
        rate = f"{report.recent_rate:.0%}" if report.recent_rate is not None else "-"
        print(f"  {report.skill_id}@{report.version}: status={report.status} "
              f"recent={rate} runs={report.n_total}")


if __name__ == "__main__":
    run()
