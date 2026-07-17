"""Tests for session-log ingestion (Claude Code / Codex transcripts)."""

from __future__ import annotations

import json

import pytest

from skillguard import SessionIngestor, SkillStore
from skillguard.ingest import parse_claude_session, parse_codex_session


@pytest.fixture
def store(tmp_path):
    s = SkillStore(tmp_path / "ingest.db")
    yield s
    s.close()


def _write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")


# -- Claude Code format ------------------------------------------------------

def _claude_use(tid, name, ts, model="claude-x"):
    return {"type": "assistant", "timestamp": ts, "cwd": "/home/u/proj",
            "message": {"role": "assistant", "model": model,
                        "content": [{"type": "tool_use", "id": tid, "name": name}]}}


def _claude_result(tid, is_error):
    return {"type": "user",
            "message": {"role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": tid,
                                     "is_error": is_error,
                                     "content": "boom" if is_error else "ok"}]}}


def test_parse_claude_pairs_use_and_result(tmp_path):
    f = tmp_path / "s.jsonl"
    _write_jsonl(f, [
        _claude_use("t1", "scrape", "2026-07-01T10:00:00Z"),
        _claude_result("t1", is_error=False),
        _claude_use("t2", "scrape", "2026-07-01T10:01:00Z"),
        _claude_result("t2", is_error=True),
    ])
    invs = parse_claude_session(f)
    assert len(invs) == 2
    assert invs[0].name == "scrape" and invs[0].success is True
    assert invs[1].success is False and invs[1].error == "boom"
    assert invs[0].model == "claude-x"
    assert invs[0].task_tag == "proj"           # basename of cwd
    assert invs[0].ts > 0


def test_claude_unmatched_use_is_skipped(tmp_path):
    f = tmp_path / "s.jsonl"
    _write_jsonl(f, [_claude_use("t1", "scrape", "2026-07-01T10:00:00Z")])
    assert parse_claude_session(f) == []


def test_ingest_auto_registers_and_records(store, tmp_path):
    f = tmp_path / "s.jsonl"
    _write_jsonl(f, [
        _claude_use("t1", "scrape", "2026-07-01T10:00:00Z"),
        _claude_result("t1", is_error=False),
        _claude_use("t2", "scrape", "2026-07-01T10:01:00Z"),
        _claude_result("t2", is_error=True),
    ])
    n = SessionIngestor(store).ingest_file(f, fmt="claude")
    assert n == 2
    skill = store.get_skill("scrape")
    assert skill is not None and skill.active_version == 1
    runs = store.get_executions("scrape", 1)
    assert [r.success for r in runs] == [True, False]


def test_ingest_no_register_skips_unknown(store, tmp_path):
    f = tmp_path / "s.jsonl"
    _write_jsonl(f, [
        _claude_use("t1", "scrape", "2026-07-01T10:00:00Z"),
        _claude_result("t1", is_error=False),
    ])
    n = SessionIngestor(store, auto_register=False).ingest_file(f, fmt="claude")
    assert n == 0
    assert store.get_skill("scrape") is None


# -- Codex format ------------------------------------------------------------

def test_parse_codex_pairs_call_and_output(tmp_path):
    f = tmp_path / "rollout.jsonl"
    _write_jsonl(f, [
        {"type": "session_meta", "payload": {"model": "codex-1", "cwd": "/home/u/api"}},
        {"type": "function_call", "timestamp": "2026-07-01T10:00:00Z",
         "payload": {"call_id": "c1", "name": "run_tests"}},
        {"type": "function_call_output",
         "payload": {"call_id": "c1", "output": {"exit_code": 0}}},
        {"type": "function_call", "timestamp": "2026-07-01T10:05:00Z",
         "payload": {"call_id": "c2", "name": "run_tests"}},
        {"type": "function_call_output",
         "payload": {"call_id": "c2", "output": {"exit_code": 1, "error": "failed"}}},
    ])
    invs = parse_codex_session(f)
    assert len(invs) == 2
    assert invs[0].name == "run_tests" and invs[0].success is True
    assert invs[1].success is False
    assert invs[0].model == "codex-1"
    assert invs[0].task_tag == "api"


def test_ingest_dir_walks_files(store, tmp_path):
    d = tmp_path / "sessions"
    d.mkdir()
    for i in range(2):
        _write_jsonl(d / f"s{i}.jsonl", [
            _claude_use(f"t{i}", "scrape", "2026-07-01T10:00:00Z"),
            _claude_result(f"t{i}", is_error=False),
        ])
    n = SessionIngestor(store).ingest_dir(d, fmt="claude")
    assert n == 2
    assert len(store.get_executions("scrape", 1)) == 2


def test_ingest_rejects_unknown_format(store, tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        SessionIngestor(store).ingest_file(f, fmt="bogus")
