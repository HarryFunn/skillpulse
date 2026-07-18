from __future__ import annotations

import sqlite3

from skillpulse.integrations import (
    IngestBatch,
    MappingConfig,
    RunMapper,
    RunSynchronizer,
    SourceEvaluation,
    SourceRun,
    parse_since,
)
from skillpulse.lifecycle import LifecycleManager
from skillpulse.store import SkillStore


def _store(tmp_path) -> SkillStore:
    store = SkillStore(tmp_path / "integrations.db")
    store.add_skill("support", "Support answer", content="v1")
    LifecycleManager(store).activate_initial("support")
    return store


def _run(run_id: str, success: bool = True) -> SourceRun:
    return SourceRun(
        source_id=run_id,
        source="test",
        ts=1000.0,
        success_hint=success,
        name="support-agent",
        metadata={"skillpulse.skill_id": "support"},
    )


def test_mapping_uses_namespaced_identity_and_evaluation(tmp_path):
    store = _store(tmp_path)
    source = _run("provider:one")
    source.success_hint = True
    source.evaluations["correctness"] = SourceEvaluation(
        "correctness", 0.2, comment="wrong answer", source="LLM")
    mapped = RunMapper(MappingConfig(
        success_score="correctness", success_threshold=0.8)).map(source, store)
    assert mapped.skill_id == "support" and mapped.version == 1
    assert mapped.success is False
    assert mapped.error == "evaluation correctness=0.2"
    assert mapped.task_tag == "support-agent"
    assert mapped.evaluations["correctness"]["comment"] == "wrong answer"
    store.close()


class _PagedSource:
    name = "paged"
    stream_id = "paged:test"

    def fetch_runs(self, since=None, cursor=None):
        if cursor is None:
            return IngestBatch([_run("paged:one")], "next", 2000.0, scanned=3)
        assert cursor == "next"
        return IngestBatch([_run("paged:two", False)], None, 2000.0, scanned=2)


def test_sync_pages_checkpoints_and_retries_idempotently(tmp_path):
    store = _store(tmp_path)
    mapper = RunMapper()
    synchronizer = RunSynchronizer(store, mapper, overlap_seconds=600)

    first = synchronizer.sync(_PagedSource(), since=900.0)
    assert first.to_dict() == {
        "provider": "paged",
        "since": "1970-01-01T00:15:00Z",
        "pages": 2,
        "scanned": 5,
        "fetched": 2,
        "added": 2,
        "duplicates": 0,
        "skipped": 0,
        "checkpoint": "1970-01-01T00:33:20Z",
        "skip_reasons": {},
    }
    second = synchronizer.sync(_PagedSource())
    assert second.since == 1400.0
    assert second.added == 0 and second.duplicates == 2
    runs = store.get_skill_runs("support", 1)
    assert [run.run_id for run in runs] == ["paged:one", "paged:two"]
    assert runs[1].success is False
    store.close()


def test_sync_skips_unknown_skills_without_auto_registration(tmp_path):
    store = _store(tmp_path)
    bad = _run("paged:unknown")
    bad.metadata["skillpulse.skill_id"] = "not-registered"

    class Source:
        name = "test"
        stream_id = "test:unknown"

        def fetch_runs(self, since=None, cursor=None):
            return IngestBatch([bad], watermark=1234.0, scanned=1)

    result = RunSynchronizer(store, RunMapper()).sync(Source(), since=0)
    assert result.skipped == 1 and result.added == 0
    assert result.skip_reasons == {"unknown skill: not-registered": 1}
    assert store.get_skill("not-registered") is None
    store.close()


def test_parse_since_accepts_duration_iso_and_epoch():
    assert parse_since("24h", now=100000.0) == 13600.0
    assert parse_since("1970-01-01T00:01:40Z") == 100.0
    assert parse_since("42") == 42.0


def test_existing_skill_run_table_migrates_evidence_columns(tmp_path):
    path = tmp_path / "v02-runs.db"
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE skill_runs (
            run_id TEXT PRIMARY KEY,
            skill_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            success INTEGER NOT NULL,
            ts REAL NOT NULL,
            input_data TEXT NOT NULL DEFAULT '{}',
            output_data TEXT NOT NULL DEFAULT '{}',
            error TEXT NOT NULL DEFAULT '',
            task_tag TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'manual',
            session_id TEXT NOT NULL DEFAULT ''
        );
        INSERT INTO skill_runs (run_id, skill_id, version, success, ts)
        VALUES ('old-run', 'old-skill', 1, 1, 10.0);
    """)
    connection.close()

    store = SkillStore(path)
    columns = {row["name"] for row in
               store._conn.execute("PRAGMA table_info(skill_runs)").fetchall()}
    assert {"metadata", "evaluations"}.issubset(columns)
    migrated = store.get_skill_runs("old-skill", 1)[0]
    assert migrated.metadata == {} and migrated.evaluations == {}
    store.close()
