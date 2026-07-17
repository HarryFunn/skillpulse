<p align="center">
  <img src="images/skillguard-banner.png" alt="SkillGuard" width="760" />
</p>

<h3 align="center">Runtime health monitoring and safe lifecycle management for Agent Skills</h3>

<p align="center">
  Detect degradation from real executions, attribute the root cause, and validate repairs before promotion.
</p>

<p align="center">
  <a href="https://github.com/HarryFunn/skillguard/actions/workflows/ci.yml"><img src="https://github.com/HarryFunn/skillguard/actions/workflows/ci.yml/badge.svg" alt="CI" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-059669.svg" alt="MIT License" /></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-3776AB.svg" alt="Python 3.10+" />
  <img src="https://img.shields.io/badge/runtime_dependencies-0-2F3337.svg" alt="Zero runtime dependencies" />
</p>

<p align="center">
  <strong>English</strong> | <a href="README_ZH.md">简体中文</a>
</p>

---

SkillGuard evaluates each Agent Skill version from its real execution history. It detects statistically significant degradation, distinguishes environment drift from model changes, task drift, and intrinsic defects, then manages a gated **detect → attribute → repair → canary → promote/rollback** workflow.

Unlike tools that rely only on upstream versions, Git commits, or file hashes, SkillGuard measures whether a Skill still works in practice.

## Why it's different

- **Degradation from outcomes, not metadata.** A two-proportion z-test compares
  a skill's recent success rate against its own long-run baseline, catching
  silent breakage that version-diff tools miss.
- **Root-cause attribution.** Detecting *that* a skill broke isn't enough to
  know *what to do*. SkillGuard attributes each degradation to one of four
  causes — environment drift, model change, task drift, or an intrinsic skill
  defect — and maps each to a different recommended action.
- **Repair is gated, not blind.** A repaired version enters a canary probation
  trial (a configurable share of traffic) and is only promoted after it clears a
  success bar over a minimum number of trials — otherwise it's rolled back and
  the incumbent keeps serving.
- **Full audit trail.** Every state change is logged, so you can explain why any
  version was flagged, repaired, promoted, or retired.
- **Zero dependencies.** Pure standard library + SQLite. Works as a library or a
  CLI.

## Install

```bash
pip install -e .          # from the repo root
```

## Quick start (CLI)

```bash
# register a skill (first version is auto-activated)
skillguard add scraper --name "Scrape page title" --content-file skill.txt

# stream in execution outcomes as your agent runs the skill
skillguard record scraper --ok
skillguard record scraper --fail --error "SelectorNotFound"

# see the whole library's health at a glance
skillguard status

# run degradation detection; degraded skills are flagged
skillguard doctor

# root-cause the degradation and get a recommended action
skillguard attribute scraper

# create a repaired version -> starts a canary probation trial
skillguard repair scraper --content-file fixed_skill.txt

# after the canary has served enough traffic, decide its fate
skillguard evaluate scraper      # -> promoted | rejected | pending

# inspect the version tree and audit events
skillguard history scraper
```

## Quick start (library)

```python
from skillguard import SkillStore, LifecycleManager, ExecutionRecord

store = SkillStore("skills.db")
mgr = LifecycleManager(store)

store.add_skill("scraper", "Scrape page title", content="selector = 'head > title'")
mgr.activate_initial("scraper")

# record outcomes as the agent runs
store.record_execution(ExecutionRecord("scraper", 1, success=True))

# periodically scan for degradation
flagged = mgr.scan()

# repair a degraded skill (plug your own LLM / human fix into repair_fn)
if flagged:
    mgr.repair("scraper", repair_fn=lambda old, reasons: fix(old, reasons))

# route calls: the canary gets a slice of traffic, the incumbent the rest
version = mgr.route("scraper")

# decide promote/rollback once the canary has enough trials
mgr.evaluate_probation("scraper")
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
records (`skillguard record ... --model <m> --tag <t>`). Thresholds live in
`AttributionConfig`.

## Lifecycle state machine

```
candidate ──activate/promote──► active ──degradation detected──► degraded
    ▲                             │                                  │
    │                             │ repair()                         │ repair()
    └─────────── probation ◄──────┴──────────────────────────────────┘
                    │
        promote (passes bar) ──► active   (old version ──► retired)
        rollback (fails bar) ──► rejected (incumbent keeps serving)
```

## Try the demo

```bash
python -m demo.simulate
```

Simulates a scraper skill that breaks when the target site changes its HTML:
SkillGuard detects the drop, flags it, accepts a repaired version into a canary
trial, and promotes it once proven — printing the full audit trail.

## Ingest your real usage

Instead of synthetic data, point SkillGuard at your agent's local session logs.
It pairs each tool/skill call with its outcome and records it, auto-registering
skills it hasn't seen.

```bash
# Claude Code transcripts
skillguard ingest ~/.claude/projects --format claude

# Codex rollouts
skillguard ingest ~/.codex/sessions --format codex

# then analyze your own history
skillguard status
skillguard doctor
skillguard attribute <skill_id>
```

Outcome heuristics: for Claude Code a `tool_result` with `is_error: true` is a
failure; for Codex a non-zero `exit_code` or an `error` field in the tool output
is a failure. `model` and `task_tag` (from the session's cwd) are captured to
power attribution. A call with no matching result is skipped as incomplete.

## Run the tests

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT
