<p align="center">
  <img src="images/skillpulse-banner.png" alt="SkillPulse" width="760" />
</p>

<h3 align="center">Runtime health monitoring and safe lifecycle management for Agent Skills</h3>

<p align="center">
  Detect degradation from real executions, attribute the root cause, and validate externally-authored candidates before promotion.
</p>

<p align="center">
  <a href="https://github.com/HarryFunn/skillpulse/actions/workflows/ci.yml"><img src="https://github.com/HarryFunn/skillpulse/actions/workflows/ci.yml/badge.svg" alt="CI" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-059669.svg" alt="MIT License" /></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-3776AB.svg" alt="Python 3.10+" />
  <img src="https://img.shields.io/badge/runtime_dependencies-0-2F3337.svg" alt="Zero runtime dependencies" />
</p>

<p align="center">
  <strong>English</strong> | <a href="README_ZH.md">简体中文</a>
</p>

---

SkillPulse evaluates each Agent Skill version from its real execution history. It detects statistically significant degradation, distinguishes environment drift from model changes, task drift, and intrinsic defects, then manages a gated **detect → attribute → submit candidate → offline replay → canary → promote/rollback** workflow. Candidate content is authored externally by a human, LLM, or deterministic repair rule; SkillPulse validates and manages it rather than generating it.

Unlike tools that rely only on upstream versions, Git commits, or file hashes, SkillPulse measures whether a Skill still works in practice.

## Why it's different

- **Degradation from outcomes, not metadata.** A two-proportion z-test compares
  a skill's recent success rate against its own long-run baseline, catching
  silent breakage that version-diff tools miss.
- **Root-cause attribution.** Detecting *that* a skill broke isn't enough to
  know *what to do*. SkillPulse attributes each degradation to one of four
  causes — environment drift, model change, task drift, or an intrinsic skill
  defect — and maps each to a different recommended action.
- **External candidates are gated.** A human, LLM, or rule authors the candidate;
  SkillPulse first replays history, then admits passing candidates to a canary
  trial. Failed candidates never replace the incumbent.
- **Full audit trail.** Every state change is logged, so you can explain why any
  version was flagged, submitted, promoted, or retired.
- **Zero dependencies.** Pure standard library + SQLite. Works as a library or a
  CLI.

## Install

```bash
pip install -e .          # from the repo root
```

## Quick start (CLI)

```bash
# register a Skill; v1 becomes active
skillpulse add scraper --name "Scrape page title" --content-file skill.txt

# record the final outcome of complete Skill executions
skillpulse run-record scraper --ok --run-id run-001
skillpulse run-record scraper --fail --run-id run-002 \
  --error "SelectorNotFound" --model claude-sonnet --tag web-scraping

skillpulse status
skillpulse doctor
skillpulse attribute scraper

# submit a candidate authored by a human, LLM, or deterministic rule
skillpulse repair scraper --content-file externally-authored-skill.txt

# replay-results.json maps historical run_id -> candidate outcome
skillpulse replay scraper 2 --results replay-results.json

# only a replay-approved candidate enters probation
skillpulse evaluate scraper      # -> promoted | rejected | pending

# export a machine-readable report
skillpulse report --output report.json
```

## Quick start (library)

```python
from skillpulse import LifecycleManager, SkillRun, SkillStore

store = SkillStore("skills.db")
manager = LifecycleManager(store)
store.add_skill("scraper", "Scrape page title", content="selector = 'head > title'")
manager.activate_initial("scraper")

store.record_skill_run(SkillRun(
    run_id="run-001",
    skill_id="scraper",
    version=1,
    success=True,
    input_data={"url": "https://example.test"},
))

if manager.scan():
    # The provider authors the candidate; SkillPulse only manages validation.
    candidate = manager.repair(
        "scraper",
        repair_fn=lambda old, reasons: external_repair_provider(old, reasons),
    )
    replay = manager.replay(
        "scraper", candidate.version,
        replay_fn=lambda candidate_content, historical_run: replay_in_sandbox(
            candidate_content, historical_run.input_data),
    )
    if replay.passed:
        version = manager.route("scraper")  # canary is now eligible
```

## How degradation is detected

A skill version is flagged **DEGRADED** when any of these fire:

1. **Recent-vs-baseline drop** — a one-sided two-proportion z-test on the recent
   window's success rate vs the long-run baseline exceeds `z_threshold`
   (default 1.645 ≈ 95% confidence). Catches sudden environment breakage.
2. **EWMA below floor** — the exponentially weighted success rate falls under
   `ewma_floor`. Catches gradual decay.
3. **Staleness** — no executions for `stale_after_days`. Reported as a warning;
   flags DEGRADED only if `stale_is_degraded` is set.

All thresholds live in `HealthConfig`; probation behavior in `ProbationConfig`.

## Root-cause attribution

Once a skill is flagged, `Attributor` classifies *why* it degraded from
interpretable signals in the execution stream (change-point sharpness, dominant
error signature, model shift, task out-of-distribution) and recommends an
action:

- **Environment drift** — sudden break, one dominant error, same model & tasks
  → **repair** the skill to the new environment.
- **Model change** — failures concentrated on a model unseen while healthy
  → **re-verify**; the fix is likely prompt/model adaptation, not skill logic.
- **Task drift** — failures on task types never seen when healthy
  → **narrow scope**; the skill is being used out-of-distribution, not broken.
- **Skill defect** — flaky throughout with no external explanation
  → **rewrite** rather than patch.

Attribution needs the optional `model` and `task_tag` fields on execution
records (`skillpulse record ... --model <m> --tag <t>`). Thresholds live in
`AttributionConfig`.

## Who authors a candidate?

SkillPulse does not generate Skill content. The `repair_fn` extension point may
call a human workflow, an LLM, or deterministic repair rules. The CLI requires
an explicit `--content-file` and never creates a placeholder candidate.
SkillPulse takes responsibility after submission: persistence, offline replay,
live canary evaluation, promotion, and rejection.

## Two-level candidate gate

```
active ──degradation──► degraded ──external submission──► candidate
                                                │
                                   offline replay gate
                                   ├── fail ──► candidate
                                   └── pass ──► probation
                                                    │
                                      live canary evaluation
                                      ├── pass ──► active (old -> retired)
                                      └── fail ──► rejected
```

Offline replay measures both `fix_rate` on historical failures and
`regression_rate` on historical successes. A candidate must satisfy both
thresholds before it can receive live canary traffic.

## Try the demo

```bash
python -m demo.simulate
```

Simulates a scraper skill that breaks when the target site changes its HTML:
SkillPulse detects and attributes the drop, accepts an externally-authored
candidate, gates it through offline replay and live canary evaluation, and
promotes it once proven — printing the full audit trail.

## ToolCall vs SkillRun

SkillPulse deliberately separates two levels of evidence:

- `ToolCall`: one tool invocation inside an agent session.
- `SkillRun`: the final outcome of a complete Skill execution, which may contain
  multiple tool calls.

Claude Code and Codex transcripts expose tool calls, so `ingest` stores
`ToolCall` records only. It does not equate a successful tool call with a
successful Skill and does not auto-register tool names as Skills.

```bash
skillpulse ingest ~/.claude/projects --format claude
skillpulse ingest ~/.codex/sessions --format codex
```

Import is idempotent. Stable IDs derived from the transcript path and original
call ID prevent repeated imports from duplicating data. The CLI reports
`added`, `duplicates`, `skipped`, and `files` counts. Calls with no matching
result are skipped as incomplete.

Record the final Skill outcome separately with `run-record` or the Python
`record_skill_run()` API. Imported calls can be attached with repeatable
`--tool-call-id <stable-id>` arguments. Health detection, attribution, replay,
and probation then use SkillRun outcomes rather than raw tool-call success.

## Run the tests

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT
