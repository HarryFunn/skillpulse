"""Tests for idempotent ToolCall ingestion."""

from __future__ import annotations

import json

import pytest

from skillguard import SessionIngestor, SkillStore
from skillguard.ingest import parse_claude_session, parse_codex_session


@pytest.fixture
def store(tmp_path):
    instance = SkillStore(tmp_path / "ingest.db")
    yield instance
    instance.close()


def _write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")


def _claude_use(tid, name, ts, model="claude-x"):
    return {"type": "assistant", "timestamp": ts, "cwd": "/home/u/proj",
            "message": {"role": "assistant", "model": model,
                        "content": [{"type": "tool_use", "id": tid, "name": name}]}}


def _claude_result(tid, is_error):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "is_error": is_error,
         "content": "boom" if is_error else "ok"}]}}


def test_parse_claude_pairs_use_and_result(tmp_path):
    path = tmp_path / "s.jsonl"
    _write_jsonl(path, [
        _claude_use("t1", "scrape", "2026-07-01T10:00:00Z"),
        _claude_result("t1", False),
        _claude_use("t2", "scrape", "2026-07-01T10:01:00Z"),
        _claude_result("t2", True),
    ])
    calls = parse_claude_session(path)
    assert len(calls) == 2
    assert calls[0].raw_call_id == "t1"
    assert calls[0].name == "scrape" and calls[0].success is True
    assert calls[1].success is False and calls[1].error == "boom"
    assert calls[0].model == "claude-x" and calls[0].task_tag == "proj"


def test_claude_unmatched_use_is_skipped(tmp_path):
    path = tmp_path / "s.jsonl"
    _write_jsonl(path, [_claude_use("t1", "scrape", "2026-07-01T10:00:00Z")])
    assert parse_claude_session(path) == []


def test_ingest_stores_tool_calls_not_skills(store, tmp_path):
    path = tmp_path / "s.jsonl"
    _write_jsonl(path, [
        _claude_use("t1", "scrape", "2026-07-01T10:00:00Z"),
        _claude_result("t1", False),
        _claude_use("t2", "scrape", "2026-07-01T10:01:00Z"),
        _claude_result("t2", True),
    ])
    result = SessionIngestor(store).ingest_file(path, "claude")
    assert result.added == 2 and result.duplicates == 0
    assert store.get_skill("scrape") is None
    assert store.get_executions("scrape", 1) == []
    calls = store.get_tool_calls(name="scrape")
    assert [call.success for call in calls] == [True, False]


def test_reimport_is_idempotent(store, tmp_path):
    path = tmp_path / "s.jsonl"
    _write_jsonl(path, [
        _claude_use("t1", "scrape", "2026-07-01T10:00:00Z"),
        _claude_result("t1", False),
    ])
    first = SessionIngestor(store).ingest_file(path, "claude")
    second = SessionIngestor(store).ingest_file(path, "claude")
    assert first.to_dict() == {"added": 1, "duplicates": 0, "skipped": 0, "files": 1}
    assert second.to_dict() == {"added": 0, "duplicates": 1, "skipped": 0, "files": 1}
    assert len(store.get_tool_calls()) == 1


def test_same_raw_call_id_in_different_files_is_distinct(store, tmp_path):
    for name in ("one.jsonl", "two.jsonl"):
        _write_jsonl(tmp_path / name, [
            _claude_use("t1", "scrape", "2026-07-01T10:00:00Z"),
            _claude_result("t1", False),
        ])
    result = SessionIngestor(store).ingest_dir(tmp_path, "claude")
    assert result.added == 2 and result.files == 2
    assert len({call.call_id for call in store.get_tool_calls()}) == 2


def test_parse_codex_pairs_call_and_output(tmp_path):
    path = tmp_path / "rollout.jsonl"
    _write_jsonl(path, [
        {"type": "session_meta", "payload": {"model": "codex-1", "cwd": "/home/u/api"}},
        {"type": "function_call", "timestamp": "2026-07-01T10:00:00Z",
         "payload": {"call_id": "c1", "name": "run_tests"}},
        {"type": "function_call_output",
         "payload": {"call_id": "c1", "output": {"exit_code": 0}}},
        {"type": "function_call", "timestamp": "2026-07-01T10:05:00Z",
         "payload": {"call_id": "c2", "name": "run_tests"}},
        {"type": "function_call_output",
         "payload": {"call_id": "c2", "output": {"exit_code": 1}}},
    ])
    calls = parse_codex_session(path)
    assert len(calls) == 2
    assert calls[0].success is True and calls[1].success is False
    assert calls[0].model == "codex-1" and calls[0].task_tag == "api"


def test_ingest_rejects_unknown_format(store, tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        SessionIngestor(store).ingest_file(path, "bogus")
