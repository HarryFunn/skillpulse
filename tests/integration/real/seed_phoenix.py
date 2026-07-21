"""Seed one evaluated Skill trace into a real Phoenix instance."""

from __future__ import annotations

import argparse
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _request(base_url: str, method: str, path: str,
             payload: dict[str, Any] | None = None,
             params: dict[str, Any] | None = None,
             expected: tuple[int, ...] = (200,)) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}{path}"
    if params:
        url = f"{url}?{urlencode(params)}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urlopen(request, timeout=30) as response:
            status = response.status
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Phoenix returned HTTP {exc.code} for {path}: {body}") from exc
    if status not in expected:
        raise RuntimeError(f"Phoenix returned unexpected HTTP {status} for {path}: {body}")
    return json.loads(body) if body else {}


def seed(base_url: str, project: str, skill_id: str,
         version: int, score: float) -> dict[str, Any]:
    """Create a project, root span, and synchronous trace annotation."""
    project_name = project or f"skillpulse-real-{uuid.uuid4().hex[:10]}"
    _request(
        base_url,
        "POST",
        "/v1/projects",
        {"name": project_name, "description": "SkillPulse real adapter test"},
    )

    trace_id = uuid.uuid4().hex
    span_id = uuid.uuid4().hex[:16]
    started_at = datetime.now(timezone.utc)
    ended_at = started_at + timedelta(milliseconds=10)
    span_result = _request(
        base_url,
        "POST",
        f"/v1/projects/{quote(project_name, safe='')}/spans",
        {
            "data": [{
                "name": "skillpulse-real-root",
                "context": {"trace_id": trace_id, "span_id": span_id},
                "span_kind": "AGENT",
                "parent_id": None,
                "start_time": _iso(started_at),
                "end_time": _iso(ended_at),
                "status_code": "OK",
                "status_message": "",
                "attributes": {
                    "skillpulse.skill_id": skill_id,
                    "skillpulse.version": version,
                    "skillpulse.task_tag": "real-provider-e2e",
                    "input.value": json.dumps({
                        "question": "Does the real Phoenix adapter work?",
                    }),
                    "output.value": json.dumps({
                        "answer": "This trace passed through Phoenix.",
                    }),
                    "llm.model_name": "skillpulse-real-model",
                    "session.id": "skillpulse-real-session",
                },
                "events": [],
            }],
        },
        expected=(202,),
    )
    deadline = time.monotonic() + 30
    while True:
        visible = _request(
            base_url,
            "GET",
            f"/v1/projects/{quote(project_name, safe='')}/spans",
            params={"trace_id": trace_id, "limit": 1},
        )
        if visible.get("data"):
            break
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Phoenix did not make trace {trace_id} visible within 30 seconds")
        time.sleep(0.2)
    annotation_result = _request(
        base_url,
        "POST",
        "/v1/trace_annotations",
        {
            "data": [{
                "name": "correctness",
                "annotator_kind": "CODE",
                "trace_id": trace_id,
                "result": {
                    "label": "correct",
                    "score": score,
                    "explanation": "Real local Phoenix integration test",
                },
                "metadata": {"test": "skillpulse-real-provider"},
                "identifier": f"skillpulse-{uuid.uuid4().hex}",
            }],
        },
        params={"sync": "true"},
    )
    return {
        "project": project_name,
        "trace_id": trace_id,
        "span_id": span_id,
        "score": score,
        "spans": span_result,
        "annotation": annotation_result,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:6006")
    parser.add_argument("--project", default="")
    parser.add_argument("--skill-id", default="support")
    parser.add_argument("--version", type=int, default=1)
    parser.add_argument("--score", type=float, default=0.94)
    args = parser.parse_args()
    result = seed(
        args.base_url, args.project, args.skill_id, args.version, args.score,
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
