"""Seed one evaluated Skill trace into a real Langfuse instance."""

from __future__ import annotations

import argparse
import base64
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _request(base_url: str, auth: str, method: str, path: str,
             payload: dict[str, Any] | None = None,
             params: dict[str, Any] | None = None,
             expected: tuple[int, ...] = (200,)) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}{path}"
    if params:
        url = f"{url}?{urlencode(params)}"
    request = Request(
        url,
        data=(json.dumps(payload).encode("utf-8")
              if payload is not None else None),
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urlopen(request, timeout=30) as response:
            status = response.status
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Langfuse returned HTTP {exc.code} for {path}: {body}") from exc
    if status not in expected:
        raise RuntimeError(f"Langfuse returned unexpected HTTP {status} for {path}: {body}")
    return json.loads(body) if body else {}


def seed(base_url: str, public_key: str, secret_key: str,
         skill_id: str, version: int, score: float) -> dict:
    timestamp = _now()
    now_ns = time.time_ns()
    trace_id = uuid.uuid4().hex
    observation_id = uuid.uuid4().hex[:16]
    metadata = {
        "skillpulse.skill_id": skill_id,
        "skillpulse.version": version,
        "skillpulse.task_tag": "real-provider-e2e",
    }
    auth = base64.b64encode(
        f"{public_key}:{secret_key}".encode("utf-8")).decode("ascii")
    attributes = [
        {"key": "langfuse.trace.name",
         "value": {"stringValue": "skillpulse-real-e2e"}},
        {"key": "session.id",
         "value": {"stringValue": "skillpulse-real-session"}},
        {"key": "langfuse.trace.input",
         "value": {"stringValue": json.dumps({
             "question": "Does the real Langfuse adapter work?",
         })}},
        {"key": "langfuse.trace.output",
         "value": {"stringValue": json.dumps({
             "answer": "This trace passed through Langfuse.",
         })}},
        {"key": "langfuse.trace.metadata",
         "value": {"stringValue": json.dumps(metadata)}},
        {"key": "langfuse.observation.type",
         "value": {"stringValue": "span"}},
        {"key": "langfuse.observation.input",
         "value": {"stringValue": json.dumps({
             "question": "Does the real Langfuse adapter work?",
         })}},
        {"key": "langfuse.observation.output",
         "value": {"stringValue": json.dumps({
             "answer": "This trace passed through Langfuse.",
         })}},
        {"key": "langfuse.observation.metadata",
         "value": {"stringValue": json.dumps(metadata)}},
        {"key": "langfuse.observation.model.name",
         "value": {"stringValue": "skillpulse-real-model"}},
    ]
    trace_ingestion = _request(
        base_url,
        auth,
        "POST",
        "/api/public/otel/v1/traces",
        {
            "resourceSpans": [{
                "resource": {"attributes": []},
                "scopeSpans": [{
                    "scope": {"name": "skillpulse-real-test", "version": "1"},
                    "spans": [{
                        # Langfuse's JSON OTLP endpoint accepts SDK-style hex IDs.
                        "traceId": trace_id,
                        "spanId": observation_id,
                        "name": "skillpulse-real-root",
                        "kind": 1,
                        "startTimeUnixNano": str(now_ns),
                        "endTimeUnixNano": str(now_ns + 10_000_000),
                        "attributes": attributes,
                        "status": {"code": 1},
                    }],
                }],
            }],
        },
    )
    rejected = ((trace_ingestion.get("partialSuccess") or {})
                .get("rejectedSpans", 0))
    if rejected:
        raise RuntimeError(f"Langfuse rejected OTLP spans: {trace_ingestion}")

    deadline = time.monotonic() + 30
    while True:
        visible = _request(
            base_url,
            auth,
            "GET",
            "/api/public/v2/observations",
            params={"traceId": trace_id, "limit": 1},
        )
        if visible.get("data"):
            break
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Langfuse did not make trace {trace_id} visible within 30 seconds")
        time.sleep(0.5)

    score_ingestion = _request(
        base_url,
        auth,
        "POST",
        "/api/public/ingestion",
        {
            "batch": [{
                "id": str(uuid.uuid4()),
                "type": "score-create",
                "timestamp": timestamp,
                "body": {
                    "id": str(uuid.uuid4()),
                    "traceId": trace_id,
                    "name": "correctness",
                    "value": score,
                    "comment": "Real local Langfuse integration test",
                },
            }],
        },
        expected=(207,),
    )
    errors = score_ingestion.get("errors", [])
    if errors:
        raise RuntimeError(f"Langfuse rejected ingestion events: {errors}")
    return {
        "trace_id": trace_id,
        "observation_id": observation_id,
        "score": score,
        "trace_ingestion": trace_ingestion,
        "score_ingestion": score_ingestion,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:3000")
    parser.add_argument("--public-key", required=True)
    parser.add_argument("--secret-key", required=True)
    parser.add_argument("--skill-id", default="support")
    parser.add_argument("--version", type=int, default=1)
    parser.add_argument("--score", type=float, default=0.93)
    args = parser.parse_args()
    print(json.dumps(seed(
        args.base_url, args.public_key, args.secret_key,
        args.skill_id, args.version, args.score,
    ), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
