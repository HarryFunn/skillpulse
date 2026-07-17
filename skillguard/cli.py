"""Command-line interface for SkillGuard.

Commands:
    add       register a new skill (first version auto-activated)
    record    record one execution outcome for a skill version
    status    show the whole library's health at a glance
    doctor    run degradation detection, flag degraded skills
    attribute root-cause a degradation and recommend an action
    repair    create a repaired version (stub repair or from a file) -> probation
    evaluate  decide promote/reject for a probation version
    promote   manually promote a version to active
    rollback  manually reject a version
    history   show the version tree and audit events of one skill
    ingest    import real agent session logs (Claude Code / Codex) as records
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

from .attribution import Attributor
from .health import HealthChecker
from .ingest import SessionIngestor
from .lifecycle import LifecycleManager
from .models import ExecutionRecord
from .store import SkillStore


def _fmt_rate(rate: float | None) -> str:
    return f"{rate:.0%}" if rate is not None else "-"


def cmd_add(args: argparse.Namespace) -> None:
    store = SkillStore(args.db)
    content = Path(args.content_file).read_text() if args.content_file else ""
    store.add_skill(args.skill_id, args.name or args.skill_id,
                    args.description, content)
    LifecycleManager(store).activate_initial(args.skill_id)
    print(f"added skill '{args.skill_id}' (v1 active)")


def cmd_record(args: argparse.Namespace) -> None:
    store = SkillStore(args.db)
    skill = store.get_skill(args.skill_id)
    if skill is None:
        sys.exit(f"unknown skill: {args.skill_id}")
    version = args.version if args.version is not None else skill.active_version
    if version is None:
        sys.exit(f"skill {args.skill_id} has no active version; pass --version")
    store.record_execution(ExecutionRecord(
        skill_id=args.skill_id, version=version, success=args.success,
        latency_ms=args.latency_ms, error=args.error or "", task_tag=args.tag or "",
        model=args.model or "",
    ))
    print(f"recorded {'success' if args.success else 'FAILURE'} "
          f"for {args.skill_id}@{version}")


def cmd_status(args: argparse.Namespace) -> None:
    store = SkillStore(args.db)
    checker = HealthChecker(store)
    skills = store.list_skills()
    if not skills:
        print("library is empty")
        return
    header = f"{'skill':<20} {'ver':>3} {'state':<10} {'runs':>5} {'recent':>7} {'ewma':>6} {'stale(d)':>9} status"
    print(header)
    print("-" * len(header))
    for skill in skills:
        if skill.active_version is None:
            print(f"{skill.skill_id:<20} {'-':>3} {'no active':<10}")
            continue
        v = store.get_version(skill.skill_id, skill.active_version)
        r = checker.check(skill.skill_id, skill.active_version)
        stale = "-" if math.isinf(r.staleness_days) else f"{r.staleness_days:.1f}"
        print(f"{skill.skill_id:<20} {r.version:>3} {v.state.value:<10} "
              f"{r.n_total:>5} {_fmt_rate(r.recent_rate):>7} "
              f"{_fmt_rate(r.ewma_rate):>6} {stale:>9} {r.status}")


def cmd_doctor(args: argparse.Namespace) -> None:
    store = SkillStore(args.db)
    manager = LifecycleManager(store)
    reports = manager.checker.check_all_active()
    flagged = manager.scan()
    for r in reports:
        marker = "!!" if r.degraded else "ok"
        print(f"[{marker}] {r.skill_id}@{r.version}: {'; '.join(r.reasons)}")
    if flagged:
        print(f"\nflagged {len(flagged)} skill(s) as DEGRADED: {', '.join(flagged)}")
        print("next: `skillguard attribute <skill_id>` to find the root cause and recommended action")
    else:
        print("\nno new degradations flagged")


def cmd_attribute(args: argparse.Namespace) -> None:
    store = SkillStore(args.db)
    skill = store.get_skill(args.skill_id)
    if skill is None:
        sys.exit(f"unknown skill: {args.skill_id}")
    version = args.version if args.version is not None else skill.active_version
    if version is None:
        sys.exit(f"skill {args.skill_id} has no active version; pass --version")
    report = Attributor(store).attribute(args.skill_id, version)
    print(f"{args.skill_id}@{version}")
    print(f"  root cause : {report.cause.value}  (confidence {report.confidence:.0%})")
    print(f"  action     : {report.recommended_action}")
    print("  scores     : " + ", ".join(f"{k}={v:.2f}" for k, v in report.scores.items()))
    print("  evidence   :")
    for e in report.evidence:
        print(f"    - {e}")



def cmd_repair(args: argparse.Namespace) -> None:
    store = SkillStore(args.db)
    manager = LifecycleManager(store)
    if args.content_file:
        new_content = Path(args.content_file).read_text()
        repair_fn = lambda old, reasons: new_content
        note = f"manual repair from {args.content_file}"
    else:
        # placeholder repair: annotate the content; real deployments plug in an LLM here
        repair_fn = lambda old, reasons: old + f"\n# repair needed, reasons: {reasons}"
        note = "stub repair (no content file given)"
    candidate = manager.repair(args.skill_id, repair_fn, note=note)
    print(f"created {candidate.key} in PROBATION "
          f"(gets {manager.probation.traffic_share:.0%} of traffic)")
    print(f"promote bar: >={manager.probation.promote_threshold:.0%} success "
          f"over >={manager.probation.min_trials} trials, then `skillguard evaluate {args.skill_id}`")


def cmd_evaluate(args: argparse.Namespace) -> None:
    store = SkillStore(args.db)
    manager = LifecycleManager(store)
    outcome = manager.evaluate_probation(args.skill_id)
    print(f"probation decision for {args.skill_id}: {outcome}")


def cmd_promote(args: argparse.Namespace) -> None:
    store = SkillStore(args.db)
    LifecycleManager(store).promote(args.skill_id, args.version)
    print(f"promoted {args.skill_id}@{args.version} to ACTIVE")


def cmd_rollback(args: argparse.Namespace) -> None:
    store = SkillStore(args.db)
    LifecycleManager(store).rollback(args.skill_id, args.version)
    print(f"rejected {args.skill_id}@{args.version}")


def cmd_ingest(args: argparse.Namespace) -> None:
    store = SkillStore(args.db)
    ingestor = SessionIngestor(store, auto_register=not args.no_register)
    path = Path(args.path)
    if path.is_dir():
        n = ingestor.ingest_dir(path, args.format, pattern=args.pattern)
    else:
        n = ingestor.ingest_file(path, args.format)
    print(f"ingested {n} execution record(s) from {args.format} session(s)")
    print("next: `skillguard status` / `skillguard doctor`")


def cmd_history(args: argparse.Namespace) -> None:
    store = SkillStore(args.db)
    skill = store.get_skill(args.skill_id)
    if skill is None:
        sys.exit(f"unknown skill: {args.skill_id}")
    print(f"skill: {skill.skill_id} ({skill.name})  active=v{skill.active_version}")
    print("\nversions:")
    for v in store.list_versions(args.skill_id):
        parent = f" <- v{v.parent_version}" if v.parent_version else ""
        note = f"  ({v.repair_note})" if v.repair_note else ""
        print(f"  v{v.version}{parent} [{v.state.value}]{note}")
    print("\nevents:")
    for e in store.get_events(args.skill_id):
        print(f"  {e['kind']}: {e['payload']}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="skillguard",
                                description="Skill library version management and degradation detection")
    p.add_argument("--db", default="skillguard.db", help="path to the sqlite database")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("add", help="register a new skill")
    sp.add_argument("skill_id")
    sp.add_argument("--name")
    sp.add_argument("--description", default="")
    sp.add_argument("--content-file", help="file with the skill body (code/prompt)")
    sp.set_defaults(func=cmd_add)

    sp = sub.add_parser("record", help="record an execution outcome")
    sp.add_argument("skill_id")
    outcome = sp.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--ok", dest="success", action="store_true")
    outcome.add_argument("--fail", dest="success", action="store_false")
    sp.add_argument("--version", type=int, help="defaults to the active version")
    sp.add_argument("--latency-ms", type=float)
    sp.add_argument("--error", help="error message on failure")
    sp.add_argument("--tag", help="task-type tag")
    sp.add_argument("--model", help="model that ran the skill (enables attribution)")
    sp.set_defaults(func=cmd_record)

    sp = sub.add_parser("status", help="library health overview")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("doctor", help="run degradation detection and flag skills")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("attribute", help="root-cause a degradation and recommend an action")
    sp.add_argument("skill_id")
    sp.add_argument("--version", type=int, help="defaults to the active version")
    sp.set_defaults(func=cmd_attribute)

    sp = sub.add_parser("repair", help="create a repaired version, start probation")
    sp.add_argument("skill_id")
    sp.add_argument("--content-file", help="file with the repaired skill body")
    sp.set_defaults(func=cmd_repair)

    sp = sub.add_parser("evaluate", help="promote/reject a probation version")
    sp.add_argument("skill_id")
    sp.set_defaults(func=cmd_evaluate)

    sp = sub.add_parser("promote", help="manually promote a version")
    sp.add_argument("skill_id")
    sp.add_argument("version", type=int)
    sp.set_defaults(func=cmd_promote)

    sp = sub.add_parser("rollback", help="manually reject a version")
    sp.add_argument("skill_id")
    sp.add_argument("version", type=int)
    sp.set_defaults(func=cmd_rollback)

    sp = sub.add_parser("history", help="version tree and audit events for a skill")
    sp.add_argument("skill_id")
    sp.set_defaults(func=cmd_history)

    sp = sub.add_parser("ingest", help="import real agent session logs as execution records")
    sp.add_argument("path", help="a transcript file or a directory of transcripts")
    sp.add_argument("--format", required=True, choices=["claude", "codex"],
                    help="session log format")
    sp.add_argument("--pattern", default="*.jsonl",
                    help="glob for files when path is a directory (default *.jsonl)")
    sp.add_argument("--no-register", action="store_true",
                    help="skip tools that aren't already registered skills")
    sp.set_defaults(func=cmd_ingest)

    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
