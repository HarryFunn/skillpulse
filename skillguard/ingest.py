"""Ingest real agent session logs into SkillGuard execution records.

Coding agents already record every tool/skill invocation and its outcome in
local session transcripts. This adapter turns those transcripts into
`ExecutionRecord`s so you can run degradation detection and attribution against
your *own* usage instead of synthetic data.

Supported inputs:

    Claude Code   ~/.claude/projects/<encoded-cwd>/*.jsonl
                  pairs `tool_use` blocks with their `tool_result` (is_error);
                  model comes from the assistant message, task_tag from cwd.

    Codex         ~/.codex/sessions/**/rollout-*.jsonl
                  pairs `function_call` / `custom_tool_call` with the matching
                  `*_output` entry; failure inferred from the output payload.

Both are line-delimited JSON. Outcome heuristics are intentionally simple and
documented; a tool invocation with no matching result is skipped (incomplete).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .models import ExecutionRecord
from .store import SkillStore


@dataclass
class ToolInvocation:
    """A normalized tool/skill call extracted from a transcript."""

    name: str
    success: bool
    ts: float
    model: str = ""
    error: str = ""
    task_tag: str = ""


def _parse_ts(value) -> float:
    """Parse an ISO-8601 string or epoch number into epoch seconds."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return 0.0


def _iter_json_lines(path: Path) -> Iterable[dict]:
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj


def parse_claude_session(path: Path) -> list[ToolInvocation]:
    """Extract tool invocations from a Claude Code transcript file."""
    pending: dict[str, dict] = {}   # tool_use_id -> {name, ts, model}
    invocations: list[ToolInvocation] = []
    task_tag = ""

    for obj in _iter_json_lines(path):
        cwd = obj.get("cwd")
        if cwd and not task_tag:
            task_tag = Path(str(cwd)).name

        msg = obj.get("message") or {}
        role = obj.get("type") or msg.get("role")
        content = msg.get("content")
        ts = _parse_ts(obj.get("timestamp"))
        model = msg.get("model", "") or ""

        if not isinstance(content, list):
            continue

        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use":
                tid = block.get("id")
                if tid:
                    pending[tid] = {"name": block.get("name", "unknown"),
                                    "ts": ts, "model": model}
            elif btype == "tool_result":
                tid = block.get("tool_use_id")
                origin = pending.pop(tid, None)
                if origin is None:
                    continue
                is_error = bool(block.get("is_error", False))
                invocations.append(ToolInvocation(
                    name=origin["name"], success=not is_error,
                    ts=origin["ts"] or ts, model=origin["model"],
                    error=_result_text(block) if is_error else "",
                    task_tag=task_tag,
                ))
    return invocations


def parse_codex_session(path: Path) -> list[ToolInvocation]:
    """Extract tool invocations from a Codex rollout file."""
    pending: dict[str, dict] = {}   # call_id -> {name, ts}
    invocations: list[ToolInvocation] = []
    model = ""
    task_tag = ""

    for obj in _iter_json_lines(path):
        otype = obj.get("type") or obj.get("record_type")
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else obj
        ts = _parse_ts(obj.get("timestamp") or payload.get("timestamp"))

        if otype in ("session_meta", "turn_context"):
            model = payload.get("model", model) or model
            cwd = payload.get("cwd")
            if cwd and not task_tag:
                task_tag = Path(str(cwd)).name
            continue

        if otype in ("function_call", "custom_tool_call"):
            cid = payload.get("call_id") or payload.get("id")
            name = payload.get("name") or payload.get("tool_name") or "unknown"
            if cid:
                pending[cid] = {"name": name, "ts": ts}
        elif otype in ("function_call_output", "custom_tool_call_output"):
            cid = payload.get("call_id") or payload.get("id")
            origin = pending.pop(cid, None)
            if origin is None:
                continue
            success, error = _codex_outcome(payload)
            invocations.append(ToolInvocation(
                name=origin["name"], success=success, ts=origin["ts"] or ts,
                model=model, error=error, task_tag=task_tag,
            ))
    return invocations


def _result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content[:200]
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                return str(part.get("text", ""))[:200]
    return "error"


def _codex_outcome(payload: dict) -> tuple[bool, str]:
    """Infer (success, error) from a Codex tool output payload."""
    output = payload.get("output")
    if isinstance(output, dict):
        if "success" in output:
            ok = bool(output["success"])
            return ok, "" if ok else str(output.get("error", "error"))[:200]
        if output.get("exit_code") is not None:
            code = output.get("exit_code")
            return code == 0, "" if code == 0 else f"exit_code={code}"
    if isinstance(payload.get("is_error"), bool):
        err = payload["is_error"]
        return (not err), "error" if err else ""
    # default: presence of an explicit error field means failure
    err = payload.get("error")
    return (err in (None, "", False)), ("" if not err else str(err)[:200])


_PARSERS = {"claude": parse_claude_session, "codex": parse_codex_session}


class SessionIngestor:
    def __init__(self, store: SkillStore, auto_register: bool = True) -> None:
        self.store = store
        self.auto_register = auto_register

    def ingest_file(self, path: str | Path, fmt: str) -> int:
        """Ingest one transcript file. Returns the number of records added."""
        if fmt not in _PARSERS:
            raise ValueError(f"unknown format '{fmt}'; choose from {sorted(_PARSERS)}")
        invocations = _PARSERS[fmt](Path(path))
        return self._store_invocations(invocations)

    def ingest_dir(self, directory: str | Path, fmt: str,
                   pattern: str = "*.jsonl") -> int:
        total = 0
        for p in sorted(Path(directory).rglob(pattern)):
            total += self.ingest_file(p, fmt)
        return total

    def _store_invocations(self, invocations: list[ToolInvocation]) -> int:
        n = 0
        for inv in invocations:
            skill = self.store.get_skill(inv.name)
            if skill is None:
                if not self.auto_register:
                    continue
                from .lifecycle import LifecycleManager
                self.store.add_skill(inv.name, inv.name)
                LifecycleManager(self.store).activate_initial(inv.name)
                skill = self.store.get_skill(inv.name)
            version = skill.active_version or 1
            self.store.record_execution(ExecutionRecord(
                skill_id=inv.name, version=version, success=inv.success,
                ts=inv.ts, error=inv.error, task_tag=inv.task_tag, model=inv.model,
            ))
            n += 1
        return n
