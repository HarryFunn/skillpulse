from __future__ import annotations

from skillpulse.integrations import MappingConfig, PhoenixSource, RunMapper
from skillpulse.lifecycle import LifecycleManager
from skillpulse.store import SkillStore

from .conftest import FakeJsonClient


def test_phoenix_reads_root_spans_and_trace_annotations(tmp_path):
    spans = {
        "data": [{
            "name": "support-agent",
            "context": {"trace_id": "abc123", "span_id": "root123"},
            "span_kind": "AGENT",
            "start_time": "2026-07-02T08:30:00Z",
            "end_time": "2026-07-02T08:30:02Z",
            "status_code": "UNSET",
            "status_message": "",
            "attributes": {
                "skillpulse.skill_id": "support",
                "skillpulse.version": 1,
                "input.value": "{\"question\": \"hello\"}",
                "output.value": "{\"answer\": \"not sure\"}",
                "llm.model_name": "model-test",
                "session.id": "session-2",
            },
            "events": [],
        }],
        "next_cursor": None,
    }
    annotations = {
        "data": [{
            "id": "annotation-1",
            "trace_id": "abc123",
            "name": "correctness",
            "annotator_kind": "LLM",
            "updated_at": "2026-07-02T08:30:02Z",
            "result": {
                "label": "correct",
                "score": 0.9,
                "explanation": "trace-level evaluation",
            },
            "metadata": {"rubric": "v2"},
        }],
        "next_cursor": None,
    }
    span_annotations = {
        "data": [{
            "id": "annotation-2",
            "span_id": "root123",
            "name": "correctness",
            "annotator_kind": "LLM",
            "updated_at": "2026-07-02T08:30:03Z",
            "result": {
                "label": "incorrect",
                "score": 0.1,
                "explanation": "answer is unsupported",
            },
            "metadata": {"rubric": "v2"},
        }],
        "next_cursor": None,
    }
    span_path = "/v1/projects/support-bot/spans"
    annotation_path = "/v1/projects/support-bot/trace_annotations"
    span_annotation_path = "/v1/projects/support-bot/span_annotations"
    client = FakeJsonClient({
        span_path: [spans],
        annotation_path: [annotations],
        span_annotation_path: [span_annotations],
    })
    source = PhoenixSource(project="support-bot", client=client)
    batch = source.fetch_runs(since=1.0)

    assert batch.scanned == 1 and len(batch.runs) == 1
    run = batch.runs[0]
    assert run.source_id == "phoenix:support-bot:abc123"
    assert run.success_hint is True  # evaluation below deliberately overrides UNSET
    assert run.input_data == {"question": "hello"}
    assert run.evaluations["correctness"].comment == "answer is unsupported"
    assert client.calls[0][1]["parent_id"] == "null"
    assert ("trace_ids", "abc123") in client.calls[1][1]
    assert ("span_ids", "root123") in client.calls[2][1]

    store = SkillStore(tmp_path / "phoenix.db")
    store.add_skill("support", "Support", content="v1")
    LifecycleManager(store).activate_initial("support")
    mapped = RunMapper(MappingConfig(
        success_score="correctness", success_threshold=0.8)).map(run, store)
    assert mapped.success is False
    assert mapped.error == "evaluation correctness=0.1"
    assert mapped.evaluations["correctness"]["metadata"] == {"rubric": "v2"}
    store.close()
