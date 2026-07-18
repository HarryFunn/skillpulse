"""Arize Phoenix adapter using root spans and trace annotations."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from .base import IntegrationError
from .http import JsonHttpClient
from .models import IngestBatch, SourceEvaluation, SourceRun


class PhoenixSource:
    name = "phoenix"

    def __init__(self, project: str | None = None,
                 base_url: str | None = None,
                 api_key: str | None = None,
                 page_size: int = 100,
                 client: JsonHttpClient | None = None) -> None:
        self.project = project or os.getenv("PHOENIX_PROJECT") or ""
        if not self.project:
            raise IntegrationError("Phoenix requires --project or PHOENIX_PROJECT")
        self.base_url = (base_url or os.getenv("PHOENIX_BASE_URL")
                         or os.getenv("PHOENIX_HOST")
                         or "http://localhost:6006").rstrip("/")
        self.page_size = max(1, min(page_size, 1000))
        self.stream_id = f"phoenix:{self.base_url}:{self.project}"
        if client is None:
            api_key = api_key or os.getenv("PHOENIX_API_KEY")
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            client = JsonHttpClient(self.base_url, headers=headers)
        self.client = client
        self._window_end: float | None = None

    @property
    def _project_path(self) -> str:
        return quote(self.project, safe="")

    def fetch_runs(self, since: float | None = None,
                   cursor: str | None = None) -> IngestBatch:
        if cursor is None or self._window_end is None:
            self._window_end = time.time()
        params: dict[str, Any] = {
            "parent_id": "null",
            "start_time": _iso(since or 0.0),
            "end_time": _iso(self._window_end),
            "limit": self.page_size,
        }
        if cursor:
            params["cursor"] = cursor
        payload = self.client.get(
            f"/v1/projects/{self._project_path}/spans", params)
        rows = payload.get("data", [])
        if not isinstance(rows, list):
            raise IntegrationError("Phoenix spans response has invalid data")
        roots = [row for row in rows if isinstance(row, dict)]
        trace_ids = [_trace_id(row) for row in roots]
        trace_ids = [trace_id for trace_id in trace_ids if trace_id]
        span_to_trace = {
            _span_id(row): _trace_id(row)
            for row in roots if _span_id(row) and _trace_id(row)
        }
        evaluations = self._fetch_annotations(trace_ids, span_to_trace)
        runs = [self._source_run(row, evaluations.get(_trace_id(row), {}))
                for row in roots if _trace_id(row)]
        next_cursor = payload.get("next_cursor") or None
        return IngestBatch(runs=runs, next_cursor=str(next_cursor) if next_cursor else None,
                           watermark=self._window_end, scanned=len(rows))

    def _fetch_annotations(
        self, trace_ids: list[str], span_to_trace: dict[str, str],
    ) -> dict[str, dict[str, SourceEvaluation]]:
        if not trace_ids and not span_to_trace:
            return {}
        grouped: dict[str, dict[str, SourceEvaluation]] = {}
        selected_at: dict[tuple[str, str], float] = {}
        queries = (
            ("trace_annotations", "trace_ids", list(dict.fromkeys(trace_ids)), None),
            ("span_annotations", "span_ids", list(span_to_trace), span_to_trace),
        )
        for resource, parameter, identifiers, id_to_trace in queries:
            if not identifiers:
                continue
            cursor: str | None = None
            seen: set[str] = set()
            while True:
                params: list[tuple[str, Any]] = [
                    *((parameter, identifier) for identifier in identifiers),
                    ("limit", 10000),
                ]
                if cursor:
                    params.append(("cursor", cursor))
                payload = self.client.get(
                    f"/v1/projects/{self._project_path}/{resource}", params)
                rows = payload.get("data", [])
                if not isinstance(rows, list):
                    raise IntegrationError("Phoenix annotations response has invalid data")
                for row in rows:
                    if not isinstance(row, dict) or not row.get("name"):
                        continue
                    if id_to_trace is None:
                        trace_id = str(row.get("trace_id") or "")
                    else:
                        trace_id = id_to_trace.get(str(row.get("span_id") or ""), "")
                    if trace_id not in trace_ids:
                        continue
                    name = str(row["name"])
                    key = (trace_id, name)
                    updated_at = _timestamp(
                        row.get("updated_at") or row.get("created_at"))
                    if key in selected_at and updated_at <= selected_at[key]:
                        continue
                    selected_at[key] = updated_at
                    result = (row.get("result")
                              if isinstance(row.get("result"), dict) else {})
                    value = result.get("score")
                    if value is None:
                        value = result.get("label")
                    grouped.setdefault(trace_id, {})[name] = SourceEvaluation(
                        name=name, value=value,
                        comment=str(result.get("explanation") or ""),
                        source=str(row.get("annotator_kind")
                                   or row.get("source") or ""),
                        metadata=(row.get("metadata")
                                  if isinstance(row.get("metadata"), dict) else {}),
                    )
                next_cursor = payload.get("next_cursor") or None
                if not next_cursor:
                    break
                next_cursor = str(next_cursor)
                if next_cursor in seen:
                    raise IntegrationError("Phoenix annotations returned a repeated cursor")
                seen.add(next_cursor)
                cursor = next_cursor
        return grouped

    def _source_run(self, row: dict[str, Any],
                    evaluations: dict[str, SourceEvaluation]) -> SourceRun:
        trace_id = _trace_id(row)
        attributes = dict(row.get("attributes") or {}) if isinstance(
            row.get("attributes"), dict) else {}
        status = str(row.get("status_code") or "UNSET").upper()
        error = str(row.get("status_message") or "") if status == "ERROR" else ""
        if status == "ERROR" and not error:
            error = _event_error(row.get("events"))
        metadata = dict(attributes)
        metadata.update({
            "phoenix.trace_id": trace_id,
            "phoenix.span_id": str((row.get("context") or {}).get("span_id") or ""),
            "phoenix.span_kind": str(row.get("span_kind") or ""),
        })
        return SourceRun(
            source_id=f"phoenix:{self.project}:{trace_id}",
            source=self.name,
            ts=_timestamp(row.get("start_time")),
            success_hint=status != "ERROR",
            name=str(row.get("name") or ""),
            input_data=_object(attributes.get("input.value")),
            output_data=_object(attributes.get("output.value")),
            error=error,
            model=str(attributes.get("llm.model_name")
                      or attributes.get("gen_ai.request.model") or ""),
            session_id=str(attributes.get("session.id") or ""),
            metadata=metadata,
            evaluations=evaluations,
        )


def _trace_id(row: dict[str, Any]) -> str:
    context = row.get("context")
    if isinstance(context, dict) and context.get("trace_id"):
        return str(context["trace_id"])
    return str(row.get("trace_id") or "")


def _span_id(row: dict[str, Any]) -> str:
    context = row.get("context")
    if isinstance(context, dict) and context.get("span_id"):
        return str(context["span_id"])
    return str(row.get("span_id") or "")


def _event_error(events: Any) -> str:
    if not isinstance(events, list):
        return ""
    for event in events:
        if not isinstance(event, dict):
            continue
        attributes = event.get("attributes")
        if isinstance(attributes, dict):
            message = attributes.get("exception.message")
            if message:
                return str(message)
    return ""


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
