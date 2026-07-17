"""Tests for root-cause attribution."""

from __future__ import annotations

import pytest

from skillpulse import Attributor, Cause, ExecutionRecord, LifecycleManager, SkillStore


@pytest.fixture
def store(tmp_path):
    s = SkillStore(tmp_path / "attr.db")
    yield s
    s.close()


def _seed(store, skill_id="s1"):
    store.add_skill(skill_id, skill_id, content="body")
    LifecycleManager(store).activate_initial(skill_id)


def _rec(store, sid, ok, ts, error="", tag="", model=""):
    store.record_execution(ExecutionRecord(sid, 1, ok, ts=ts, error=error,
                                           task_tag=tag, model=model))


def test_environment_drift_sharp_break_shared_error(store):
    _seed(store)
    ts = 1000.0
    for _ in range(30):                       # healthy baseline, same model/task
        _rec(store, "s1", True, ts, tag="scrape", model="gpt-x"); ts += 1
    for _ in range(20):                       # sudden break, one dominant error
        _rec(store, "s1", False, ts, error="SelectorNotFound: title",
             tag="scrape", model="gpt-x"); ts += 1
    report = Attributor(store).attribute("s1", 1)
    assert report.cause == Cause.ENVIRONMENT_DRIFT
    assert "repair" in report.recommended_action


def test_model_change_attribution(store):
    _seed(store)
    ts = 1000.0
    for _ in range(30):                       # healthy on model A
        _rec(store, "s1", True, ts, tag="scrape", model="model-A"); ts += 1
    for _ in range(20):                       # failures all on a new model B
        _rec(store, "s1", False, ts, error="AssertionError: bad format",
             tag="scrape", model="model-B"); ts += 1
    report = Attributor(store).attribute("s1", 1)
    assert report.cause == Cause.MODEL_CHANGE
    assert "re-verify" in report.recommended_action


def test_task_drift_attribution(store):
    _seed(store)
    ts = 1000.0
    for _ in range(30):                       # healthy on task "scrape"
        _rec(store, "s1", True, ts, tag="scrape", model="model-A"); ts += 1
    for _ in range(20):                       # failures on a never-seen task
        _rec(store, "s1", False, ts, error="ValueError: x",
             tag="pdf-extract", model="model-A"); ts += 1
    report = Attributor(store).attribute("s1", 1)
    assert report.cause == Cause.TASK_DRIFT
    assert "narrow scope" in report.recommended_action


def test_skill_defect_when_flaky_throughout(store):
    _seed(store)
    ts = 1000.0
    # no clean break: intermittent failures with varied errors, same model/task
    errors = ["ErrA: 1", "ErrB: 2", "ErrC: 3", "ErrD: 4"]
    for i in range(60):
        ok = (i % 3 != 0)                     # ~33% failures scattered throughout
        _rec(store, "s1", ok, ts, error="" if ok else errors[i % len(errors)],
             tag="scrape", model="model-A"); ts += 1
    report = Attributor(store).attribute("s1", 1)
    assert report.cause == Cause.SKILL_DEFECT
    assert "rewrite" in report.recommended_action


def test_unknown_when_too_few_failures(store):
    _seed(store)
    ts = 1000.0
    for _ in range(30):
        _rec(store, "s1", True, ts); ts += 1
    for _ in range(2):
        _rec(store, "s1", False, ts, error="X: y"); ts += 1
    report = Attributor(store).attribute("s1", 1)
    assert report.cause == Cause.UNKNOWN
    assert report.confidence == 0.0
