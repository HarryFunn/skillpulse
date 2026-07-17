"""SQLite-backed store for skills, versions, and execution records."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .models import ExecutionRecord, Skill, SkillState, SkillVersion

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
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_id   TEXT NOT NULL,
    version    INTEGER NOT NULL,
    success    INTEGER NOT NULL,
    ts         REAL NOT NULL,
    latency_ms REAL,
    error      TEXT NOT NULL DEFAULT '',
    task_tag   TEXT NOT NULL DEFAULT '',
    model      TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_exec_skill ON executions (skill_id, version, ts);

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
        """Add columns introduced after the initial schema, if missing."""
        cols = {r["name"] for r in
                self._conn.execute("PRAGMA table_info(executions)").fetchall()}
        if "model" not in cols:
            self._conn.execute(
                "ALTER TABLE executions ADD COLUMN model TEXT NOT NULL DEFAULT ''")

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

    def record_execution(self, rec: ExecutionRecord) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO executions (skill_id, version, success, ts, latency_ms, error, task_tag, model) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (rec.skill_id, rec.version, int(rec.success), rec.ts,
                 rec.latency_ms, rec.error, rec.task_tag, rec.model),
            )

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
                                task_tag=r["task_tag"], model=r["model"])
                for r in rows]

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
