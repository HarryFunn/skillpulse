from __future__ import annotations

from skillpulse.integrations import LangfuseSource, MappingConfig, RunMapper
from skillpulse.lifecycle import LifecycleManager
from skillpulse.store import SkillStore

from .conftest import FakeJsonClient


def test_langfuse_reads_root_observations_and_v3_scores(tmp_path):
    observations = {
        "data": [
            {
                "id": "root-1",
                "traceId": "trace-1",
                "projectId": "project-1",
                "parentObservationId": None,
                "startTime": "2026-07-01T12:00:00Z",
                "name": "support-root",
                "traceName": "support-agent",
                "level": "DEFAULT",
                "statusMessage": "",
                "input": {"question": "hello"},
                "output": {"answer": "hi"},
                "providedModelName": "gpt-test",
                "sessionId": "session-1",
                "metadata": {
                    "skillpulse.skill_id": "support",
                    "skillpulse.version": 1,
                    "skillpulse.task_tag": "chat",
                },
            },
            {
                "id": "child-1",
                "traceId": "trace-1",
                "projectId": "project-1",
                "parentObservationId": "root-1",
                "startTime": "2026-07-01T12:00:01Z",
                "name": "llm-child",
                "level": "ERROR",
            },
        ],
        "meta": {"cursor": None},
    }
    scores = {
        "data": [{
            "id": "score-1",
            "name": "correctness",
            "value": 0.95,
            "source": "EVAL",
            "comment": "grounded",
            "updatedAt": "2026-07-01T12:00:02Z",
            "subject": {"kind": "trace", "id": "trace-1"},
        }],
        "meta": {},
    }
    root_scores = {
        "data": [{
            "id": "score-2",
            "name": "correctness",
            "value": 0.98,
            "source": "EVAL",
            "comment": "newer root evaluation",
            "updatedAt": "2026-07-01T12:00:03Z",
            "subject": {"kind": "observation", "id": "root-1"},
        }],
        "meta": {},
    }
    client = FakeJsonClient({
        "/api/public/v2/observations": [observations],
        "/api/public/v3/scores": [scores, root_scores],
    })
    source = LangfuseSource(base_url="https://langfuse.test", client=client)
    batch = source.fetch_runs(since=1.0)

    assert batch.scanned == 2 and len(batch.runs) == 1
    run = batch.runs[0]
    assert run.source_id == "langfuse:project-1:trace-1"
    assert run.input_data == {"question": "hello"}
    assert run.evaluations["correctness"].value == 0.98
    assert run.evaluations["correctness"].comment == "newer root evaluation"
    assert [path for path, _ in client.calls] == [
        "/api/public/v2/observations",
        "/api/public/v3/scores",
        "/api/public/v3/scores",
    ]
    assert "parseIoAsJson" not in client.calls[0][1]
    assert client.calls[1][1]["traceId"] == "trace-1"
    assert "observationId" not in client.calls[1][1]
    assert client.calls[2][1]["traceId"] == "trace-1"
    assert client.calls[2][1]["observationId"] == "root-1"

    store = SkillStore(tmp_path / "langfuse.db")
    store.add_skill("support", "Support", content="v1")
    LifecycleManager(store).activate_initial("support")
    mapped = RunMapper(MappingConfig(
        success_score="correctness", success_threshold=0.8)).map(run, store)
    assert mapped.success is True and mapped.model == "gpt-test"
    assert mapped.task_tag == "chat"
    store.close()
