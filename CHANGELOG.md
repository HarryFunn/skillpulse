# Changelog

## 0.3.0

- Add a provider-neutral observability integration layer with explicit
  `SourceRun` to `SkillRun` mapping.
- Add Langfuse v4 ingestion through Observations API v2 and Scores API v3.
- Add Phoenix ingestion through project root spans plus trace/root-span annotations.
- Add `skillpulse sync langfuse` and `skillpulse sync phoenix` commands with
  environment-based authentication and evaluation-driven success rules.
- Make remote synchronization idempotent and resumable with SQLite checkpoints,
  bounded polling windows, cursor safety checks, and a delayed-trace overlap.
- Preserve provider metadata and evaluation evidence on imported `SkillRun`
  records, including additive migrations for existing databases.
- Add isolated adapter, mapping, checkpoint, retry, and real-HTTP CLI tests.
- Validate both adapters against local Langfuse 3.222.0 and Phoenix 19.2.0
  services with opt-in trace-to-SQLite integration tests and idempotent re-syncs.
- Use Langfuse's current Observations v2 contract (without deprecated
  `parseIoAsJson`) and include the required `traceId` when querying v3 scores
  for root observations.
- Document reproducible Docker Desktop deployments while keeping provider
  source, credentials, databases, and container data outside version control.

## 0.2.0

- Rename the project from SkillGuard to SkillPulse to avoid conflicts with an
  existing Agent Skills permission framework and unrelated commercial products.
- Clarify that candidate content is authored externally by a human, LLM, or
  deterministic rule; the CLI now requires `repair --content-file` and no
  longer creates placeholder repair content.
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
