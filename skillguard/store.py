"""SQLite-backed store for skills, versions, and execution records."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .models import (
    ExecutionRecord,
    ReplayReport,
    Skill,
    SkillRun,
    SkillState,
    SkillVersion,
    ToolCall,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS skills (
    skill_id       TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    description    TEXT NOT NULL DEFAULT '',
    active_version INTEGER,
    created_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS versions (
    skill_id       TEXT NOT NULL,
    version        INTEGER NOT NULL,
    content        TEXT NOT NULL,
    state          TEXT NOT NULL,
    created_at     REAL NOT NULL,
    parent_version INTEGER,
    repair_note    TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (skill_id, version),
    FOREIGN KEY (skill_id) REFERENCES skills(skill_id)
);

CREATE TABLE IF NOT EXISTS executions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_id     TEXT NOT NULL,
    version      INTEGER NOT NULL,
    success      INTEGER NOT NULL,
    ts           REAL NOT NULL,
    latency_ms   REAL,
    error        TEXT NOT NULL DEFAULT '',
    task_tag     TEXT NOT NULL DEFAULT '',
    model        TEXT NOT NULL DEFAULT '',
    execution_id TEXT NOT NULL DEFAULT '',
    source       TEXT NOT NULL DEFAULT 'manual'
);

CREATE INDEX IF NOT EXISTS idx_exec_skill ON executions (skill_id, version, ts);

CREATE TABLE IF NOT EXISTS skill_runs (
    run_id       TEXT PRIMARY KEY,
    skill_id     TEXT NOT NULL,
    version      INTEGER NOT NULL,
    success      INTEGER NOT NULL,
    ts           REAL NOT NULL,
    input_data   TEXT NOT NULL DEFAULT '{}',
    output_data  TEXT NOT NULL DEFAULT '{}',
    error        TEXT NOT NULL DEFAULT '',
    task_tag     TEXT NOT NULL DEFAULT '',
    model        TEXT NOT NULL DEFAULT '',
    source       TEXT NOT NULL DEFAULT 'manual',
    session_id   TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (skill_id, version) REFERENCES versions(skill_id, version)
);

CREATE INDEX IF NOT EXISTS idx_skill_runs_skill ON skill_runs (skill_id, version, ts);

CREATE TABLE IF NOT EXISTS tool_calls (
    call_id      TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    success      INTEGER NOT NULL,
    ts           REAL NOT NULL,
    session_id   TEXT NOT NULL DEFAULT '',
    run_id       TEXT,
    model        TEXT NOT NULL DEFAULT '',
    error        TEXT NOT NULL DEFAULT '',
    task_tag     TEXT NOT NULL DEFAULT '',
    source       TEXT NOT NULL DEFAULT 'manual',
    source_path  TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (run_id) REFERENCES skill_runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_run ON tool_calls (run_id, ts);
CREATE INDEX IF NOT EXISTS idx_tool_calls_name ON tool_calls (name, ts);

CREATE TABLE IF NOT EXISTS replay_reports (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_id           TEXT NOT NULL,
    candidate_version  INTEGER NOT NULL,
    parent_version     INTEGER NOT NULL,
    total_cases        INTEGER NOT NULL,
    failed_cases       INTEGER NOT NULL,
    successful_cases   INTEGER NOT NULL,
    fixed_failures     INTEGER NOT NULL,
    regressions        INTEGER NOT NULL,
    fix_rate           REAL NOT NULL,
    regression_rate    REAL NOT NULL,
    passed             INTEGER NOT NULL,
    reasons            TEXT NOT NULL DEFAULT '[]',
    created_at         REAL NOT NULL,
    UNIQUE(skill_id, candidate_version)
);

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    skill_id TEXT NOT NULL,
    kind     TEXT NOT NULL,
    payload  TEXT NOT NULL DEFAULT '{}'
);
"""


class SkillStore:
    """Persistence layer. All lifecycle/health logic lives elsewhere."""

    def __init__(self, db_path: str | Path = "skillguard.db") -> None:
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Apply additive, backwards-compatible schema migrations."""
        cols = {r["name"] for r in
                self._conn.execute("PRAGMA table_info(executions)").fetchall()}
        additions = {
            "model": "TEXT NOT NULL DEFAULT ''",
            "execution_id": "TEXT NOT NULL DEFAULT ''",
            "source": "TEXT NOT NULL DEFAULT 'manual'",
        }
        for name, definition in additions.items():
            if name not in cols:
                self._conn.execute(
                    f"ALTER TABLE executions ADD COLUMN {name} {definition}")
        # Manual records may have an empty id; imported records must be unique.
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_exec_external_id "
            "ON executions(execution_id) WHERE execution_id <> ''")

    def close(self) -> None:
        self._conn.close()

    # -- skills ------------------------------------------------------------

    def add_skill(self, skill_id: str, name: str, description: str = "",
                  content: str = "") -> Skill:
        """Register a skill; its first version starts as CANDIDATE."""
        now = time.time()
        with self._conn:
            self._conn.execute(
                "INSERT INTO skills (skill_id, name, description, active_version, created_at) "
                "VALUES (?, ?, ?, NULL, ?)",
                (skill_id, name, description, now),
            )
            self._conn.execute(
                "INSERT INTO versions (skill_id, version, content, state, created_at, "
                "parent_version, repair_note) VALUES (?, 1, ?, ?, ?, NULL, 'initial version')",
                (skill_id, content, SkillState.CANDIDATE.value, now),
            )
        self.log_event(skill_id, "skill_added", {"version": 1})
        return Skill(skill_id=skill_id, name=name, description=description,
                     active_version=None, created_at=now)

    def get_skill(self, skill_id: str) -> Skill | None:
        row = self._conn.execute(
            "SELECT * FROM skills WHERE skill_id = ?", (skill_id,)
        ).fetchone()
        if row is None:
            return None
        return Skill(skill_id=row["skill_id"], name=row["name"],
                     description=row["description"],
                     active_version=row["active_version"],
                     created_at=row["created_at"])

    def list_skills(self) -> list[Skill]:
        rows = self._conn.execute("SELECT * FROM skills ORDER BY skill_id").fetchall()
        return [Skill(skill_id=r["skill_id"], name=r["name"], description=r["description"],
                      active_version=r["active_version"], created_at=r["created_at"])
                for r in rows]

    def set_active_version(self, skill_id: str, version: int | None) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE skills SET active_version = ? WHERE skill_id = ?",
                (version, skill_id),
            )

    # -- versions ----------------------------------------------------------

    def add_version(self, skill_id: str, content: str,
                    parent_version: int | None = None,
                    repair_note: str = "") -> SkillVersion:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM versions WHERE skill_id = ?",
            (skill_id,),
        ).fetchone()
        new_version = row["v"] + 1
        now = time.time()
        with self._conn:
            self._conn.execute(
                "INSERT INTO versions (skill_id, version, content, state, created_at, "
                "parent_version, repair_note) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (skill_id, new_version, content, SkillState.CANDIDATE.value, now,
                 parent_version, repair_note),
            )
        self.log_event(skill_id, "version_added",
                       {"version": new_version, "parent": parent_version})
        return SkillVersion(skill_id=skill_id, version=new_version, content=content,
                            state=SkillState.CANDIDATE, created_at=now,
                            parent_version=parent_version, repair_note=repair_note)

    def get_version(self, skill_id: str, version: int) -> SkillVersion | None:
        row = self._conn.execute(
            "SELECT * FROM versions WHERE skill_id = ? AND version = ?",
            (skill_id, version),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_version(row)

    def list_versions(self, skill_id: str) -> list[SkillVersion]:
        rows = self._conn.execute(
            "SELECT * FROM versions WHERE skill_id = ? ORDER BY version", (skill_id,)
        ).fetchall()
        return [self._row_to_version(r) for r in rows]

    def set_version_state(self, skill_id: str, version: int, state: SkillState) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE versions SET state = ? WHERE skill_id = ? AND version = ?",
                (state.value, skill_id, version),
            )
        self.log_event(skill_id, "state_change", {"version": version, "state": state.value})

    @staticmethod
    def _row_to_version(row: sqlite3.Row) -> SkillVersion:
        return SkillVersion(
            skill_id=row["skill_id"], version=row["version"], content=row["content"],
            state=SkillState(row["state"]), created_at=row["created_at"],
            parent_version=row["parent_version"], repair_note=row["repair_note"],
        )

    # -- executions ---------------------------------------------------------

    def record_execution(self, rec: ExecutionRecord) -> bool:
        """Store a legacy execution record; return False when already imported."""
        statement = (
            "INSERT OR IGNORE" if rec.execution_id else "INSERT"
        ) + (
            " INTO executions (skill_id, version, success, ts, latency_ms, error, "
            "task_tag, model, execution_id, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        with self._conn:
            cur = self._conn.execute(
                statement,
                (rec.skill_id, rec.version, int(rec.success), rec.ts,
                 rec.latency_ms, rec.error, rec.task_tag, rec.model,
                 rec.execution_id, rec.source),
            )
        return cur.rowcount == 1

    def get_executions(self, skill_id: str, version: int | None = None,
                       since: float | None = None) -> list[ExecutionRecord]:
        query = "SELECT * FROM executions WHERE skill_id = ?"
        params: list = [skill_id]
        if version is not None:
            query += " AND version = ?"
            params.append(version)
        if since is not None:
            query += " AND ts >= ?"
            params.append(since)
        query += " ORDER BY ts"
        rows = self._conn.execute(query, params).fetchall()
        return [ExecutionRecord(skill_id=r["skill_id"], version=r["version"],
                                success=bool(r["success"]), ts=r["ts"],
                                latency_ms=r["latency_ms"], error=r["error"],
                                task_tag=r["task_tag"], model=r["model"],
                                execution_id=r["execution_id"], source=r["source"])
                for r in rows]

    # -- skill runs ----------------------------------------------------------

    def record_skill_run(self, run: SkillRun) -> bool:
        """Idempotently store a final Skill-level outcome."""
        with self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO skill_runs (run_id, skill_id, version, success, ts, "
                "input_data, output_data, error, task_tag, model, source, session_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run.run_id, run.skill_id, run.version, int(run.success), run.ts,
                 json.dumps(run.input_data), json.dumps(run.output_data), run.error,
                 run.task_tag, run.model, run.source, run.session_id),
            )
        return cur.rowcount == 1

    def get_skill_runs(self, skill_id: str, version: int | None = None,
                       since: float | None = None) -> list[SkillRun]:
        query = "SELECT * FROM skill_runs WHERE skill_id = ?"
        params: list = [skill_id]
        if version is not None:
            query += " AND version = ?"
            params.append(version)
        if since is not None:
            query += " AND ts >= ?"
            params.append(since)
        query += " ORDER BY ts"
        rows = self._conn.execute(query, params).fetchall()
        return [SkillRun(run_id=r["run_id"], skill_id=r["skill_id"],
                         version=r["version"], success=bool(r["success"]), ts=r["ts"],
                         input_data=json.loads(r["input_data"]),
                         output_data=json.loads(r["output_data"]), error=r["error"],
                         task_tag=r["task_tag"], model=r["model"], source=r["source"],
                         session_id=r["session_id"])
                for r in rows]

    # -- tool calls ----------------------------------------------------------

    def record_tool_call(self, call: ToolCall) -> bool:
        """Idempotently store one tool call by its stable call id."""
        with self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO tool_calls (call_id, name, success, ts, session_id, "
                "run_id, model, error, task_tag, source, source_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (call.call_id, call.name, int(call.success), call.ts, call.session_id,
                 call.run_id, call.model, call.error, call.task_tag, call.source,
                 call.source_path),
            )
        return cur.rowcount == 1

    def link_tool_calls(self, run_id: str, call_ids: list[str]) -> int:
        """Attach existing, currently-unassigned ToolCalls to a SkillRun."""
        if not call_ids:
            return 0
        placeholders = ",".join("?" for _ in call_ids)
        with self._conn:
            cur = self._conn.execute(
                f"UPDATE tool_calls SET run_id = ? WHERE call_id IN ({placeholders}) "
                "AND run_id IS NULL",
                [run_id, *call_ids],
            )
        return cur.rowcount

    def get_tool_calls(self, run_id: str | None = None,
                       name: str | None = None) -> list[ToolCall]:
        clauses, params = [], []
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        if name is not None:
            clauses.append("name = ?")
            params.append(name)
        query = "SELECT * FROM tool_calls"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY ts"
        rows = self._conn.execute(query, params).fetchall()
        return [ToolCall(call_id=r["call_id"], name=r["name"],
                         success=bool(r["success"]), ts=r["ts"],
                         session_id=r["session_id"], run_id=r["run_id"],
                         model=r["model"], error=r["error"], task_tag=r["task_tag"],
                         source=r["source"], source_path=r["source_path"])
                for r in rows]

    # -- replay reports ------------------------------------------------------

    def save_replay_report(self, report: ReplayReport) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO replay_reports (skill_id, candidate_version, "
                "parent_version, total_cases, failed_cases, successful_cases, "
                "fixed_failures, regressions, fix_rate, regression_rate, passed, "
                "reasons, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (report.skill_id, report.candidate_version, report.parent_version,
                 report.total_cases, report.failed_cases, report.successful_cases,
                 report.fixed_failures, report.regressions, report.fix_rate,
                 report.regression_rate, int(report.passed), json.dumps(report.reasons),
                 time.time()),
            )

    def get_replay_report(self, skill_id: str,
                          candidate_version: int) -> ReplayReport | None:
        row = self._conn.execute(
            "SELECT * FROM replay_reports WHERE skill_id = ? AND candidate_version = ?",
            (skill_id, candidate_version),
        ).fetchone()
        if row is None:
            return None
        return ReplayReport(
            skill_id=row["skill_id"], candidate_version=row["candidate_version"],
            parent_version=row["parent_version"], total_cases=row["total_cases"],
            failed_cases=row["failed_cases"], successful_cases=row["successful_cases"],
            fixed_failures=row["fixed_failures"], regressions=row["regressions"],
            fix_rate=row["fix_rate"], regression_rate=row["regression_rate"],
            passed=bool(row["passed"]), reasons=json.loads(row["reasons"]),
        )

    # -- events (audit trail) -------------------------------------------------

    def log_event(self, skill_id: str, kind: str, payload: dict | None = None) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO events (ts, skill_id, kind, payload) VALUES (?, ?, ?, ?)",
                (time.time(), skill_id, kind, json.dumps(payload or {})),
            )

    def get_events(self, skill_id: str | None = None) -> list[dict]:
        if skill_id:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE skill_id = ? ORDER BY ts", (skill_id,)
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM events ORDER BY ts").fetchall()
        return [{"ts": r["ts"], "skill_id": r["skill_id"], "kind": r["kind"],
                 "payload": json.loads(r["payload"])} for r in rows]
