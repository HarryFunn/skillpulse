"""Tests for SkillGuard health detection and lifecycle management."""

from __future__ import annotations

import random

import pytest

from skillguard import (
    ExecutionRecord,
    HealthChecker,
    HealthConfig,
    LifecycleManager,
    SkillState,
    SkillStore,
)
from skillguard.lifecycle import ProbationConfig


@pytest.fixture
def store(tmp_path):
    s = SkillStore(tmp_path / "test.db")
    yield s
    s.close()


def _seed(store: SkillStore, skill_id: str = "s1", content: str = "body") -> None:
    store.add_skill(skill_id, skill_id, content=content)
    LifecycleManager(store).activate_initial(skill_id)


def _record(store, skill_id, version, successes, failures, base_ts=1000.0):
    ts = base_ts
    for _ in range(successes):
        store.record_execution(ExecutionRecord(skill_id, version, True, ts=ts))
        ts += 1
    for _ in range(failures):
        store.record_execution(ExecutionRecord(skill_id, version, False, ts=ts))
        ts += 1


# -- store round-trip -------------------------------------------------------

def test_add_skill_creates_first_version(store):
    store.add_skill("s1", "Skill One", "desc", "content")
    skill = store.get_skill("s1")
    assert skill.name == "Skill One"
    versions = store.list_versions("s1")
    assert len(versions) == 1
    assert versions[0].state == SkillState.CANDIDATE


def test_activate_initial_sets_active(store):
    _seed(store)
    skill = store.get_skill("s1")
    assert skill.active_version == 1
    assert store.get_version("s1", 1).state == SkillState.ACTIVE


# -- health: healthy case ---------------------------------------------------

def test_healthy_skill_not_degraded(store):
    _seed(store)
    _record(store, "s1", 1, successes=40, failures=0)
    report = HealthChecker(store).check("s1", 1)
    assert not report.degraded
    assert report.status == "HEALTHY"
    assert report.recent_rate == 1.0


def test_no_data_is_unknown(store):
    _seed(store)
    report = HealthChecker(store).check("s1", 1)
    assert report.status == "UNKNOWN"
    assert not report.degraded


# -- health: recent-vs-baseline drop ----------------------------------------

def test_recent_drop_flagged_by_ztest(store):
    _seed(store)
    # baseline: 30 successes; recent window (20): all failures -> clear drop
    _record(store, "s1", 1, successes=30, failures=0)
    _record(store, "s1", 1, successes=0, failures=20, base_ts=2000.0)
    report = HealthChecker(store).check("s1", 1)
    assert report.degraded
    assert report.z_score is not None and report.z_score > 1.645
    assert any("dropped" in r for r in report.reasons)


def test_gradual_decay_flagged_by_ewma(store):
    _seed(store)
    cfg = HealthConfig(ewma_floor=0.6)
    # mostly failures recently -> EWMA sinks below floor
    _record(store, "s1", 1, successes=5, failures=0)
    _record(store, "s1", 1, successes=2, failures=18, base_ts=2000.0)
    report = HealthChecker(store, cfg).check("s1", 1)
    assert report.degraded
    assert any("EWMA" in r for r in report.reasons)


def test_staleness_reported(store):
    _seed(store)
    _record(store, "s1", 1, successes=15, failures=0, base_ts=0.0)
    # now is far in the future relative to last execution
    report = HealthChecker(store).check("s1", 1, now=100 * 86400.0)
    assert report.staleness_days > 30
    assert any("stale" in r for r in report.reasons)
    # staleness alone should not flag degraded by default
    assert not report.degraded


# -- lifecycle: scan flags degraded active version --------------------------

def test_scan_flags_degraded(store):
    _seed(store)
    _record(store, "s1", 1, successes=30, failures=0)
    _record(store, "s1", 1, successes=0, failures=20, base_ts=2000.0)
    flagged = LifecycleManager(store).scan()
    assert "s1" in flagged
    assert store.get_version("s1", 1).state == SkillState.DEGRADED


# -- lifecycle: repair -> probation -> promote ------------------------------

def test_repair_creates_probation_candidate(store):
    _seed(store)
    _record(store, "s1", 1, successes=30, failures=0)
    _record(store, "s1", 1, successes=0, failures=20, base_ts=2000.0)
    mgr = LifecycleManager(store)
    mgr.scan()
    candidate = mgr.repair("s1", repair_fn=lambda old, reasons: "fixed body")
    assert candidate.version == 2
    assert candidate.parent_version == 1
    assert candidate.state == SkillState.PROBATION


def test_probation_promotes_on_success(store):
    _seed(store)
    mgr = LifecycleManager(store, probation_config=ProbationConfig(min_trials=10,
                                                                   promote_threshold=0.8))
    candidate = mgr.repair("s1", repair_fn=lambda old, reasons: "fixed")
    _record(store, "s1", candidate.version, successes=10, failures=0, base_ts=3000.0)
    outcome = mgr.evaluate_probation("s1")
    assert outcome == "promoted"
    assert store.get_skill("s1").active_version == candidate.version
    assert store.get_version("s1", 1).state == SkillState.RETIRED
    assert store.get_version("s1", candidate.version).state == SkillState.ACTIVE


def test_probation_rejects_on_failure(store):
    _seed(store)
    mgr = LifecycleManager(store, probation_config=ProbationConfig(min_trials=10,
                                                                   promote_threshold=0.8))
    candidate = mgr.repair("s1", repair_fn=lambda old, reasons: "still broken")
    _record(store, "s1", candidate.version, successes=3, failures=7, base_ts=3000.0)
    outcome = mgr.evaluate_probation("s1")
    assert outcome == "rejected"
    # active version unchanged; candidate rejected
    assert store.get_skill("s1").active_version == 1
    assert store.get_version("s1", candidate.version).state == SkillState.REJECTED


def test_probation_pending_before_min_trials(store):
    _seed(store)
    mgr = LifecycleManager(store, probation_config=ProbationConfig(min_trials=10))
    candidate = mgr.repair("s1", repair_fn=lambda old, reasons: "fixed")
    _record(store, "s1", candidate.version, successes=3, failures=0, base_ts=3000.0)
    assert mgr.evaluate_probation("s1") == "pending"


# -- lifecycle: routing splits traffic --------------------------------------

def test_route_splits_traffic_to_probation(store):
    _seed(store)
    mgr = LifecycleManager(store,
                           probation_config=ProbationConfig(traffic_share=0.3))
    candidate = mgr.repair("s1", repair_fn=lambda old, reasons: "fixed")
    rng = random.Random(42)
    picks = [mgr.route("s1", rng).version for _ in range(1000)]
    probation_share = picks.count(candidate.version) / len(picks)
    # roughly matches configured share (allow generous tolerance)
    assert 0.2 < probation_share < 0.4


def test_route_all_active_without_probation(store):
    _seed(store)
    picks = {LifecycleManager(store).route("s1").version for _ in range(50)}
    assert picks == {1}
