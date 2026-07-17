"""Idempotently ingest agent session logs as ToolCall records.

A session transcript exposes tool calls, not final Skill outcomes. Imported
calls therefore go to `tool_calls`; callers create explicit `SkillRun` records
when they know whether the complete Skill execution succeeded.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .models import ToolCall
from .store import SkillStore


@dataclass
class ToolInvocation:
    """Normalized tool call extracted from a transcript."""

    raw_call_id: str
    name: str
    success: bool
    ts: float
    model: str = ""
    error: str = ""
    task_tag: str = ""


@dataclass(eq=False)
class IngestResult:
    """Import statistics for one file or directory."""

    added: int = 0
    duplicates: int = 0
    skipped: int = 0
    files: int = 0

    @property
    def total(self) -> int:
        return self.added + self.duplicates + self.skipped

    def __int__(self) -> int:
        return self.added

    def __eq__(self, other) -> bool:
        # Preserve the v0.1 convenience `ingest_file(...) == 2`.
        if isinstance(other, int):
            return self.added == other
        if isinstance(other, IngestResult):
            return (self.added, self.duplicates, self.skipped, self.files) == (
                other.added, other.duplicates, other.skipped, other.files)
        return NotImplemented

    def merge(self, other: "IngestResult") -> None:
        self.added += other.added
        self.duplicates += other.duplicates
        self.skipped += other.skipped
        self.files += other.files

    def to_dict(self) -> dict:
        return {"added": self.added, "duplicates": self.duplicates,
                "skipped": self.skipped, "files": self.files}


def _parse_ts(value) -> float:
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
    pending: dict[str, dict] = {}
    invocations: list[ToolInvocation] = []
    task_tag = ""

    for obj in _iter_json_lines(path):
        cwd = obj.get("cwd")
        if cwd and not task_tag:
            task_tag = Path(str(cwd)).name
        msg = obj.get("message") or {}
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
                    pending[str(tid)] = {"name": block.get("name", "unknown"),
                                         "ts": ts, "model": model}
            elif btype == "tool_result":
                tid = str(block.get("tool_use_id", ""))
                origin = pending.pop(tid, None)
                if origin is None:
                    continue
                is_error = bool(block.get("is_error", False))
                invocations.append(ToolInvocation(
                    raw_call_id=tid, name=origin["name"], success=not is_error,
                    ts=origin["ts"] or ts, model=origin["model"],
                    error=_result_text(block) if is_error else "", task_tag=task_tag,
                ))
    return invocations


def parse_codex_session(path: Path) -> list[ToolInvocation]:
    pending: dict[str, dict] = {}
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
                pending[str(cid)] = {"name": name, "ts": ts}
        elif otype in ("function_call_output", "custom_tool_call_output"):
            cid = str(payload.get("call_id") or payload.get("id") or "")
            origin = pending.pop(cid, None)
            if origin is None:
                continue
            success, error = _codex_outcome(payload)
            invocations.append(ToolInvocation(
                raw_call_id=cid, name=origin["name"], success=success,
                ts=origin["ts"] or ts, model=model, error=error, task_tag=task_tag,
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
        return not err, "error" if err else ""
    err = payload.get("error")
    return err in (None, "", False), "" if not err else str(err)[:200]


_PARSERS = {"claude": parse_claude_session, "codex": parse_codex_session}


class SessionIngestor:
    def __init__(self, store: SkillStore, auto_register: bool = False) -> None:
        # `auto_register` is retained for API compatibility, but tool names are
        # deliberately not promoted to Skills: a ToolCall is not a SkillRun.
        self.store = store
        self.auto_register = auto_register

    def ingest_file(self, path: str | Path, fmt: str) -> IngestResult:
        if fmt not in _PARSERS:
            raise ValueError(f"unknown format '{fmt}'; choose from {sorted(_PARSERS)}")
        source_path = Path(path).expanduser().resolve()
        invocations = _PARSERS[fmt](source_path)
        session_id = _stable_session_id(fmt, source_path)
        result = IngestResult(files=1)
        for inv in invocations:
            if not inv.raw_call_id:
                result.skipped += 1
                continue
            call = ToolCall(
                call_id=f"{session_id}:{inv.raw_call_id}",
                name=inv.name, success=inv.success, ts=inv.ts,
                session_id=session_id, model=inv.model, error=inv.error,
                task_tag=inv.task_tag, source=fmt, source_path=str(source_path),
            )
            if self.store.record_tool_call(call):
                result.added += 1
            else:
                result.duplicates += 1
        return result

    def ingest_dir(self, directory: str | Path, fmt: str,
                   pattern: str = "*.jsonl") -> IngestResult:
        total = IngestResult()
        for path in sorted(Path(directory).expanduser().rglob(pattern)):
            total.merge(self.ingest_file(path, fmt))
        return total


def _stable_session_id(fmt: str, path: Path) -> str:
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:20]
    return f"{fmt}:{digest}"
