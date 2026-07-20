"""Langfuse v4 adapter using Observations v2 and Scores v3."""

from __future__ import annotations

import base64
import json
import os
import time
from datetime import datetime, timezone
from typing import Any

from .base import IntegrationError
from .http import JsonHttpClient
from .models import IngestBatch, SourceEvaluation, SourceRun


class LangfuseSource:
    name = "langfuse"

    def __init__(self, public_key: str | None = None,
                 secret_key: str | None = None,
                 base_url: str | None = None,
                 page_size: int = 100,
                 client: JsonHttpClient | None = None) -> None:
        self.base_url = (base_url or os.getenv("LANGFUSE_BASE_URL")
                         or "https://cloud.langfuse.com").rstrip("/")
        self.page_size = max(1, min(page_size, 100))
        self.stream_id = f"langfuse:{self.base_url}"
        if client is None:
            public_key = public_key or os.getenv("LANGFUSE_PUBLIC_KEY")
            secret_key = secret_key or os.getenv("LANGFUSE_SECRET_KEY")
            if not public_key or not secret_key:
                raise IntegrationError(
                    "Langfuse requires LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY")
            token = base64.b64encode(
                f"{public_key}:{secret_key}".encode("utf-8")).decode("ascii")
            client = JsonHttpClient(
                self.base_url, headers={"Authorization": f"Basic {token}"})
        self.client = client
        self._window_end: float | None = None

    def fetch_runs(self, since: float | None = None,
                   cursor: str | None = None) -> IngestBatch:
        if cursor is None or self._window_end is None:
            self._window_end = time.time()
        params: dict[str, Any] = {
            "fields": "core,basic,io,metadata,model,trace_context",
            "fromStartTime": _iso(since or 0.0),
            "toStartTime": _iso(self._window_end),
            "limit": self.page_size,
        }
        if cursor:
            params["cursor"] = cursor
        payload = self.client.get("/api/public/v2/observations", params)
        rows = payload.get("data", [])
        if not isinstance(rows, list):
            raise IntegrationError("Langfuse observations response has invalid data")

        roots = [row for row in rows if isinstance(row, dict)
                 and row.get("parentObservationId") in (None, "")]
        trace_ids = [str(row["traceId"]) for row in roots if row.get("traceId")]
        observation_to_trace = {
            str(row["id"]): str(row["traceId"])
            for row in roots if row.get("id") and row.get("traceId")
        }
        evaluations = self._fetch_scores(trace_ids, observation_to_trace)
        runs = [self._source_run(row, evaluations.get(str(row.get("traceId")), {}))
                for row in roots if row.get("traceId")]
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        next_cursor = meta.get("cursor") or None
        return IngestBatch(runs=runs, next_cursor=str(next_cursor) if next_cursor else None,
                           watermark=self._window_end, scanned=len(rows))

    def _fetch_scores(
        self,
        trace_ids: list[str],
        observation_to_trace: dict[str, str],
    ) -> dict[str, dict[str, SourceEvaluation]]:
        if not trace_ids and not observation_to_trace:
            return {}
        grouped: dict[str, dict[str, SourceEvaluation]] = {}
        selected_at: dict[tuple[str, str], float] = {}
        queries = (
            {"traceId": list(dict.fromkeys(trace_ids))},
            {
                "traceId": list(dict.fromkeys(observation_to_trace.values())),
                "observationId": list(observation_to_trace),
            },
        )
        for filters in queries:
            if not all(filters.values()):
                continue
            cursor: str | None = None
            seen: set[str] = set()
            while True:
                params: dict[str, Any] = {
                    "fields": "subject,details",
                    "limit": 100,
                }
                params.update({
                    name: ",".join(identifiers)
                    for name, identifiers in filters.items()
                })
                if cursor:
                    params["cursor"] = cursor
                payload = self.client.get("/api/public/v3/scores", params)
                rows = payload.get("data", [])
                if not isinstance(rows, list):
                    raise IntegrationError("Langfuse scores response has invalid data")
                for row in rows:
                    if not isinstance(row, dict) or not row.get("name"):
                        continue
                    trace_id = _score_trace_id(row)
                    if not trace_id:
                        trace_id = observation_to_trace.get(_score_observation_id(row), "")
                    if not trace_id or trace_id not in trace_ids:
                        continue
                    name = str(row["name"])
                    key = (trace_id, name)
                    updated_at = _timestamp(row.get("updatedAt") or row.get("timestamp")
                                            or row.get("createdAt"))
                    if key in selected_at and updated_at <= selected_at[key]:
                        continue
                    selected_at[key] = updated_at
                    grouped.setdefault(trace_id, {})[name] = SourceEvaluation(
                        name=name, value=row.get("value"),
                        comment=str(row.get("comment") or ""),
                        source=str(row.get("source") or ""),
                        metadata=(row.get("metadata")
                                  if isinstance(row.get("metadata"), dict) else {}),
                    )
                meta = (payload.get("meta")
                        if isinstance(payload.get("meta"), dict) else {})
                next_cursor = meta.get("cursor") or None
                if not next_cursor:
                    break
                next_cursor = str(next_cursor)
                if next_cursor in seen:
                    raise IntegrationError("Langfuse scores returned a repeated cursor")
                seen.add(next_cursor)
                cursor = next_cursor
        return grouped

    def _source_run(self, row: dict[str, Any],
                    evaluations: dict[str, SourceEvaluation]) -> SourceRun:
        trace_id = str(row["traceId"])
        project_id = str(row.get("projectId") or "default")
        level = str(row.get("level") or "DEFAULT").upper()
        metadata = dict(row.get("metadata") or {}) if isinstance(
            row.get("metadata"), dict) else {}
        metadata.update({
            "langfuse.trace_id": trace_id,
            "langfuse.observation_id": str(row.get("id") or ""),
            "langfuse.trace_name": str(row.get("traceName") or ""),
            "langfuse.release": str(row.get("release") or ""),
            "langfuse.observation_version": str(row.get("version") or ""),
        })
        error = str(row.get("statusMessage") or "") if level == "ERROR" else ""
        return SourceRun(
            source_id=f"langfuse:{project_id}:{trace_id}",
            source=self.name,
            ts=_timestamp(row.get("startTime")),
            success_hint=level != "ERROR",
            name=str(row.get("traceName") or row.get("name") or ""),
            input_data=_object(row.get("input")),
            output_data=_object(row.get("output")),
            error=error,
            model=str(row.get("providedModelName") or ""),
            session_id=str(row.get("sessionId") or ""),
            metadata=metadata,
            evaluations=evaluations,
        )


def _score_trace_id(row: dict[str, Any]) -> str:
    direct = row.get("traceId") or row.get("trace_id")
    if direct:
        return str(direct)
    subject = row.get("subject")
    if not isinstance(subject, dict) or str(subject.get("kind", "")).lower() != "trace":
        return ""
    return str(subject.get("id") or subject.get("traceId")
               or subject.get("trace_id") or "")


def _score_observation_id(row: dict[str, Any]) -> str:
    direct = row.get("observationId") or row.get("observation_id")
    if direct:
        return str(direct)
    subject = row.get("subject")
    if (not isinstance(subject, dict)
            or str(subject.get("kind", "")).lower() != "observation"):
        return ""
    return str(subject.get("id") or subject.get("observationId")
               or subject.get("observation_id") or "")


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"value": value}
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    return {} if value is None else {"value": value}


def _timestamp(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    raw = str(value or "")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _iso(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")
