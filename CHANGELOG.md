# Changelog

## 0.2.0

- Make Claude Code and Codex session ingestion idempotent with stable ToolCall IDs.
- Report added, duplicate, skipped, and processed-file counts during ingestion.
- Separate ToolCall observations from final SkillRun outcomes.
- Base health checks, attribution, and probation decisions on SkillRun outcomes.
- Add offline historical replay with fix-rate and regression-rate thresholds.
- Require replay approval before a repaired candidate can enter probation.
- Add JSON output for status, doctor, attribution, replay, ingestion, and full reports.
- Add backwards-compatible SQLite migrations for existing v0.1 databases.

## 0.1.0

- Initial statistical degradation detection, attribution, lifecycle management,
  Claude Code/Codex ingestion, CLI, and SQLite persistence.
