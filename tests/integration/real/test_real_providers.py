"""Opt-in end-to-end tests against real local Langfuse and Phoenix services."""

from __future__ import annotations

import os
import time
from collections.abc import Callable

import pytest

from skillpulse.integrations import (
    LangfuseSource,
    MappingConfig,
    PhoenixSource,
    RunMapper,
    RunSynchronizer,
)
from skillpulse.lifecycle import LifecycleManager
from skillpulse.models import SkillRun
from skillpulse.store import SkillStore

from .seed_langfuse import seed as seed_langfuse
from .seed_phoenix import seed as seed_phoenix


pytestmark = pytest.mark.skipif(
    os.getenv("SKILLPULSE_REAL_INTEGRATION") != "1",
    reason="set SKILLPULSE_REAL_INTEGRATION=1 to test deployed providers",
)


def _store(path) -> SkillStore:
    store = SkillStore(path)
    store.add_skill("support", "Support", content="real-provider-test-v1")
    LifecycleManager(store).activate_initial("support")
    return store


def _sync_until(
    store: SkillStore,
    source,
    expected: Callable[[SkillRun], bool],
    since: float,
) -> tuple[SkillRun, RunSynchronizer]:
    synchronizer = RunSynchronizer(
        store,
        RunMapper(MappingConfig(
            success_score="correctness",
            success_threshold=0.8,
        )),
    )
    deadline = time.monotonic() + 60
    while True:
        synchronizer.sync(source, since=since, use_checkpoint=False)
        matching = [run for run in store.get_skill_runs("support", 1)
                    if expected(run)]
        if matching:
            return matching[0], synchronizer
        if time.monotonic() >= deadline:
            raise AssertionError("seeded provider trace was not importable within 60 seconds")
        time.sleep(0.5)


def _assert_normalized_run(run: SkillRun, provider: str, score: float) -> None:
    assert run.source == provider
    assert run.skill_id == "support" and run.version == 1
    assert run.success is True
    assert run.task_tag == "real-provider-e2e"
    assert run.input_data["question"].startswith("Does the real")
    assert run.output_data["answer"].endswith(f"through {provider.title()}.")
    assert run.evaluations["correctness"]["value"] == pytest.approx(score)
    assert "Real local" in run.evaluations["correctness"]["comment"]


def test_langfuse_real_trace_to_sqlite_is_idempotent(tmp_path):
    public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
    secret_key = os.getenv("LANGFUSE_SECRET_KEY")
    if not public_key or not secret_key:
        pytest.skip("set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY")
    base_url = os.getenv("LANGFUSE_BASE_URL", "http://127.0.0.1:3000")
    since = time.time() - 10
    seeded = seed_langfuse(
        base_url, public_key, secret_key, "support", 1, 0.93,
    )
    store = _store(tmp_path / "langfuse-real.db")
    try:
        source = LangfuseSource(
            public_key=public_key,
            secret_key=secret_key,
            base_url=base_url,
        )
        run, synchronizer = _sync_until(
            store,
            source,
            lambda candidate: candidate.run_id.endswith(seeded["trace_id"]),
            since,
        )
        _assert_normalized_run(run, "langfuse", 0.93)
        again = synchronizer.sync(source, since=since, use_checkpoint=False)
        assert again.added == 0 and again.duplicates >= 1
    finally:
        store.close()


def test_phoenix_real_trace_to_sqlite_is_idempotent(tmp_path):
    base_url = os.getenv("PHOENIX_BASE_URL", "http://127.0.0.1:6006")
    since = time.time() - 10
    seeded = seed_phoenix(base_url, "", "support", 1, 0.94)
    store = _store(tmp_path / "phoenix-real.db")
    try:
        source = PhoenixSource(project=seeded["project"], base_url=base_url)
        run, synchronizer = _sync_until(
            store,
            source,
            lambda candidate: candidate.run_id.endswith(seeded["trace_id"]),
            since,
        )
        _assert_normalized_run(run, "phoenix", 0.94)
        again = synchronizer.sync(source, since=since, use_checkpoint=False)
        assert again.added == 0 and again.duplicates == 1
    finally:
        store.close()
