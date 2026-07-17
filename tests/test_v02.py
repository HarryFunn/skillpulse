"""v0.2 tests: SkillRun semantics, replay gate, and JSON reporting."""

from __future__ import annotations

import json
import random
import sqlite3

import pytest

from skillguard import (
    HealthChecker,
    JsonReporter,
    LifecycleManager,
    ReplayConfig,
    SkillRun,
    SkillState,
    SkillStore,
    ToolCall,
)
from skillguard.cli import main
from skillguard.lifecycle import ProbationConfig


@pytest.fixture
def store(tmp_path):
    instance = SkillStore(tmp_path / "v02.db")
    instance.add_skill("s1", "Skill One", content="old")
    LifecycleManager(instance).activate_initial("s1")
    yield instance
    instance.close()


def _skill_runs(store, successes: int, failures: int, version: int = 1,
                prefix: str = "baseline"):
    index = 0
    for success in ([True] * successes + [False] * failures):
        store.record_skill_run(SkillRun(
            run_id=f"{prefix}-{index}", skill_id="s1", version=version,
            success=success, ts=1000 + index,
            input_data={"case": index}, error="boom" if not success else "",
            model="model-a", task_tag="task-a",
        ))
        index += 1


def _pass_replay(manager, candidate, fix_failures=True, regress=False):
    return manager.replay(
        "s1", candidate.version,
        lambda _content, run: (True if not run.success and fix_failures
                               else (False if run.success and regress else run.success)),
    )


def test_skill_run_and_tool_call_are_distinct_and_idempotent(store):
    run = SkillRun("run-1", "s1", 1, True)
    call = ToolCall("call-1", "browser", True)
    assert store.record_tool_call(call) is True
    assert store.record_tool_call(call) is False
    assert store.record_skill_run(run) is True
    assert store.record_skill_run(run) is False
    assert store.link_tool_calls("run-1", ["call-1"]) == 1
    assert store.link_tool_calls("run-1", ["call-1"]) == 0
    assert len(store.get_skill_runs("s1", 1)) == 1
    assert len(store.get_tool_calls(run_id="run-1")) == 1


def test_health_uses_skill_runs_not_tool_calls(store):
    _skill_runs(store, successes=30, failures=20)
    # Tool calls can all succeed while the final Skill outcome fails.
    for index in range(50):
        store.record_tool_call(ToolCall(f"call-{index}", "browser", True, ts=index))
    report = HealthChecker(store).check("s1", 1)
    assert report.n_total == 50
    assert report.recent_rate == 0.0
    assert report.degraded


def test_replay_passes_then_admits_candidate_to_probation(store):
    _skill_runs(store, successes=8, failures=4)
    manager = LifecycleManager(store, replay_config=ReplayConfig(
        min_cases=5, min_failed_cases=2, min_fix_rate=0.75,
        max_regression_rate=0.1))
    candidate = manager.repair("s1", lambda old, reasons: "fixed")
    report = _pass_replay(manager, candidate)
    assert report.passed
    assert report.fix_rate == 1.0 and report.regression_rate == 0.0
    assert store.get_version("s1", candidate.version).state == SkillState.PROBATION
    assert store.get_replay_report("s1", candidate.version).passed


def test_replay_failure_keeps_candidate_out_of_probation(store):
    _skill_runs(store, successes=8, failures=4)
    manager = LifecycleManager(store, replay_config=ReplayConfig(
        min_cases=5, min_failed_cases=2, min_fix_rate=0.75,
        max_regression_rate=0.1))
    candidate = manager.repair("s1", lambda old, reasons: "bad fix")
    report = _pass_replay(manager, candidate, fix_failures=False, regress=True)
    assert not report.passed
    assert report.fix_rate == 0.0 and report.regression_rate == 1.0
    assert store.get_version("s1", candidate.version).state == SkillState.CANDIDATE
    assert {manager.route("s1").version for _ in range(20)} == {1}


def test_probation_uses_skill_run_outcomes_after_replay(store):
    _skill_runs(store, successes=8, failures=4)
    manager = LifecycleManager(
        store,
        replay_config=ReplayConfig(min_cases=5, min_failed_cases=1),
        probation_config=ProbationConfig(min_trials=5, promote_threshold=0.8,
                                         traffic_share=0.5),
    )
    candidate = manager.repair("s1", lambda old, reasons: "fixed")
    assert _pass_replay(manager, candidate).passed
    rng = random.Random(3)
    assert candidate.version in {manager.route("s1", rng).version for _ in range(50)}
    for index in range(5):
        store.record_skill_run(SkillRun(
            run_id=f"canary-{index}", skill_id="s1", version=candidate.version,
            success=True, ts=2000 + index))
    assert manager.evaluate_probation("s1") == "promoted"
    assert store.get_skill("s1").active_version == candidate.version


def test_json_report_contains_runs_replay_and_tool_call_summary(store):
    _skill_runs(store, successes=8, failures=4)
    store.record_tool_call(ToolCall("call-1", "browser", True))
    manager = LifecycleManager(store, replay_config=ReplayConfig(
        min_cases=5, min_failed_cases=1))
    candidate = manager.repair("s1", lambda old, reasons: "fixed")
    _pass_replay(manager, candidate)
    report = JsonReporter(store).library()
    assert report["schema_version"] == "1.0"
    assert report["summary"]["skill_count"] == 1
    assert report["summary"]["tool_call_count"] == 1
    versions = report["skills"][0]["versions"]
    assert versions[0]["skill_run_count"] == 12
    assert versions[1]["replay"]["passed"] is True


def test_cli_report_is_valid_json(store, capsys):
    _skill_runs(store, successes=3, failures=2)
    main(["--db", store.db_path, "report"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["skill_count"] == 1


def test_doctor_json_replaces_infinity_with_null(store, capsys):
    main(["--db", store.db_path, "doctor", "--format", "json"])
    raw = capsys.readouterr().out
    assert "Infinity" not in raw
    payload = json.loads(raw)
    assert payload["reports"][0]["staleness_days"] is None


def test_existing_v01_database_migrates_without_rebuild(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE executions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            skill_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            success INTEGER NOT NULL,
            ts REAL NOT NULL,
            latency_ms REAL,
            error TEXT NOT NULL DEFAULT '',
            task_tag TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT ''
        );
        INSERT INTO executions (skill_id, version, success, ts)
        VALUES ('legacy', 1, 1, 1000);
    """)
    conn.close()
    migrated = SkillStore(path)
    columns = {row["name"] for row in
               migrated._conn.execute("PRAGMA table_info(executions)").fetchall()}
    assert {"execution_id", "source"}.issubset(columns)
    assert migrated.get_executions("legacy", 1)[0].success is True
    # New v0.2 tables are available without recreating the database.
    assert migrated.get_tool_calls() == []
    migrated.close()
