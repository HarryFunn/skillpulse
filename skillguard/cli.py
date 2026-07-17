"""SkillGuard command-line interface."""

from __future__ import annotations

import argparse
import json
import math
import sys
import uuid
from dataclasses import asdict
from pathlib import Path

from .attribution import Attributor
from .health import HealthChecker
from .ingest import SessionIngestor
from .lifecycle import LifecycleManager
from .models import ExecutionRecord, SkillRun
from .reporting import JsonReporter
from .store import SkillStore


def _fmt_rate(rate: float | None) -> str:
    return f"{rate:.0%}" if rate is not None else "-"


def _json(data: dict | list) -> None:
    print(json.dumps(_json_safe(data), indent=2, sort_keys=True, allow_nan=False))


def _json_safe(value):
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _version(store: SkillStore, skill_id: str, explicit: int | None) -> int:
    skill = store.get_skill(skill_id)
    if skill is None:
        sys.exit(f"unknown skill: {skill_id}")
    version = explicit if explicit is not None else skill.active_version
    if version is None:
        sys.exit(f"skill {skill_id} has no active version; pass --version")
    return version


def cmd_add(args: argparse.Namespace) -> None:
    store = SkillStore(args.db)
    content = Path(args.content_file).read_text() if args.content_file else ""
    store.add_skill(args.skill_id, args.name or args.skill_id,
                    args.description, content)
    LifecycleManager(store).activate_initial(args.skill_id)
    print(f"added skill '{args.skill_id}' (v1 active)")


def cmd_record(args: argparse.Namespace) -> None:
    store = SkillStore(args.db)
    version = _version(store, args.skill_id, args.version)
    added = store.record_execution(ExecutionRecord(
        skill_id=args.skill_id, version=version, success=args.success,
        latency_ms=args.latency_ms, error=args.error or "", task_tag=args.tag or "",
        model=args.model or "", execution_id=args.execution_id or "",
        source=args.source,
    ))
    print(f"{'recorded' if added else 'duplicate'} "
          f"{'success' if args.success else 'FAILURE'} for {args.skill_id}@{version}")


def cmd_run_record(args: argparse.Namespace) -> None:
    store = SkillStore(args.db)
    version = _version(store, args.skill_id, args.version)
    run_id = args.run_id or f"manual:{uuid.uuid4().hex}"
    added = store.record_skill_run(SkillRun(
        run_id=run_id, skill_id=args.skill_id, version=version,
        success=args.success, error=args.error or "", task_tag=args.tag or "",
        model=args.model or "", source=args.source,
        session_id=args.session_id or "",
        input_data=_load_json(args.input_json),
        output_data=_load_json(args.output_json),
    ))
    linked = store.link_tool_calls(run_id, args.tool_call_id or []) if added else 0
    payload = {"run_id": run_id, "skill_id": args.skill_id,
               "version": version, "added": added,
               "linked_tool_calls": linked}
    if args.format == "json":
        _json(payload)
    else:
        print(f"{'recorded' if added else 'duplicate'} SkillRun {run_id} "
              f"for {args.skill_id}@{version}")


def _load_json(path: str | None) -> dict:
    if not path:
        return {}
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object in {path}")
    return value


def cmd_status(args: argparse.Namespace) -> None:
    store = SkillStore(args.db)
    if args.format == "json":
        _json(JsonReporter(store).library())
        return
    checker = HealthChecker(store)
    skills = store.list_skills()
    if not skills:
        print("library is empty")
        return
    header = (f"{'skill':<20} {'ver':>3} {'state':<10} {'runs':>5} "
              f"{'recent':>7} {'ewma':>6} {'stale(d)':>9} status")
    print(header)
    print("-" * len(header))
    for skill in skills:
        if skill.active_version is None:
            print(f"{skill.skill_id:<20} {'-':>3} {'no active':<10}")
            continue
        version = store.get_version(skill.skill_id, skill.active_version)
        report = checker.check(skill.skill_id, skill.active_version)
        stale = "-" if math.isinf(report.staleness_days) else f"{report.staleness_days:.1f}"
        print(f"{skill.skill_id:<20} {report.version:>3} {version.state.value:<10} "
              f"{report.n_total:>5} {_fmt_rate(report.recent_rate):>7} "
              f"{_fmt_rate(report.ewma_rate):>6} {stale:>9} {report.status}")


def cmd_doctor(args: argparse.Namespace) -> None:
    store = SkillStore(args.db)
    manager = LifecycleManager(store)
    reports = manager.checker.check_all_active()
    flagged = manager.scan()
    if args.format == "json":
        _json({"flagged": flagged, "reports": [asdict(r) for r in reports]})
        return
    for report in reports:
        marker = "!!" if report.degraded else "ok"
        print(f"[{marker}] {report.skill_id}@{report.version}: "
              f"{'; '.join(report.reasons)}")
    if flagged:
        print(f"\nflagged {len(flagged)} skill(s) as DEGRADED: {', '.join(flagged)}")
        print("next: `skillguard attribute <skill_id>`")
    else:
        print("\nno new degradations flagged")


def cmd_attribute(args: argparse.Namespace) -> None:
    store = SkillStore(args.db)
    version = _version(store, args.skill_id, args.version)
    report = Attributor(store).attribute(args.skill_id, version)
    payload = {"skill_id": report.skill_id, "version": report.version,
               "cause": report.cause.value, "confidence": report.confidence,
               "recommended_action": report.recommended_action,
               "scores": report.scores, "evidence": report.evidence}
    if args.format == "json":
        _json(payload)
        return
    print(f"{args.skill_id}@{version}")
    print(f"  root cause : {report.cause.value} (score {report.confidence:.2f})")
    print(f"  action     : {report.recommended_action}")
    print("  scores     : " + ", ".join(f"{k}={v:.2f}" for k, v in report.scores.items()))
    print("  evidence   :")
    for evidence in report.evidence:
        print(f"    - {evidence}")


def cmd_repair(args: argparse.Namespace) -> None:
    store = SkillStore(args.db)
    manager = LifecycleManager(store)
    if args.content_file:
        new_content = Path(args.content_file).read_text()
        repair_fn = lambda _old, _reasons: new_content
        note = f"manual repair from {args.content_file}"
    else:
        repair_fn = lambda old, reasons: old + f"\n# repair needed: {reasons}"
        note = "stub repair"
    candidate = manager.repair(args.skill_id, repair_fn, note=note)
    print(f"created {candidate.key} in CANDIDATE state")
    print(f"next: `skillguard replay {args.skill_id} {candidate.version} "
          "--results replay-results.json` (probation requires a passing replay)")


def cmd_replay(args: argparse.Namespace) -> None:
    store = SkillStore(args.db)
    raw = json.loads(Path(args.results).read_text())
    if not isinstance(raw, dict):
        sys.exit("replay results must be a JSON object: {run_id: bool}")
    if any(not isinstance(value, bool) for value in raw.values()):
        sys.exit("replay result values must be JSON booleans, not strings/numbers")
    results = {str(key): value for key, value in raw.items()}
    manager = LifecycleManager(store)
    report = manager.replay(
        args.skill_id, args.version,
        lambda _content, run: results.get(run.run_id, False),
    )
    payload = asdict(report)
    if args.format == "json":
        _json(payload)
        return
    decision = "PASSED -> PROBATION" if report.passed else "FAILED -> CANDIDATE"
    print(f"offline replay {decision}")
    print(f"  cases={report.total_cases} fix_rate={report.fix_rate:.0%} "
          f"regression_rate={report.regression_rate:.0%}")
    for reason in report.reasons:
        print(f"  - {reason}")


def cmd_evaluate(args: argparse.Namespace) -> None:
    outcome = LifecycleManager(SkillStore(args.db)).evaluate_probation(args.skill_id)
    print(f"probation decision for {args.skill_id}: {outcome}")


def cmd_promote(args: argparse.Namespace) -> None:
    LifecycleManager(SkillStore(args.db)).promote(args.skill_id, args.version)
    print(f"promoted {args.skill_id}@{args.version} to ACTIVE")


def cmd_rollback(args: argparse.Namespace) -> None:
    LifecycleManager(SkillStore(args.db)).rollback(args.skill_id, args.version)
    print(f"rejected {args.skill_id}@{args.version}")


def cmd_ingest(args: argparse.Namespace) -> None:
    store = SkillStore(args.db)
    ingestor = SessionIngestor(store)
    path = Path(args.path)
    result = (ingestor.ingest_dir(path, args.format, pattern=args.pattern)
              if path.is_dir() else ingestor.ingest_file(path, args.format))
    if args.format_output == "json":
        _json(result.to_dict())
    else:
        print(f"files={result.files} added={result.added} "
              f"duplicates={result.duplicates} skipped={result.skipped}")
        print("imported records are ToolCalls; create SkillRuns with `run-record`")


def cmd_history(args: argparse.Namespace) -> None:
    store = SkillStore(args.db)
    skill = store.get_skill(args.skill_id)
    if skill is None:
        sys.exit(f"unknown skill: {args.skill_id}")
    print(f"skill: {skill.skill_id} ({skill.name}) active=v{skill.active_version}")
    print("\nversions:")
    for version in store.list_versions(args.skill_id):
        parent = f" <- v{version.parent_version}" if version.parent_version else ""
        note = f" ({version.repair_note})" if version.repair_note else ""
        print(f"  v{version.version}{parent} [{version.state.value}]{note}")
    print("\nevents:")
    for event in store.get_events(args.skill_id):
        print(f"  {event['kind']}: {event['payload']}")


def cmd_report(args: argparse.Namespace) -> None:
    text = JsonReporter(SkillStore(args.db)).dumps()
    if args.output:
        Path(args.output).write_text(text + "\n")
        print(f"wrote JSON report to {args.output}")
    else:
        print(text)


def _out_format(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--format", choices=["text", "json"], default="text")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skillguard",
        description="Runtime health and safe lifecycle management for Agent Skills")
    parser.add_argument("--db", default="skillguard.db")
    sub = parser.add_subparsers(dest="command", required=True)

    command = sub.add_parser("add")
    command.add_argument("skill_id")
    command.add_argument("--name")
    command.add_argument("--description", default="")
    command.add_argument("--content-file")
    command.set_defaults(func=cmd_add)

    for name, handler, help_text in (
        ("record", cmd_record, "record a legacy/manual skill execution"),
        ("run-record", cmd_run_record, "record a final SkillRun outcome"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("skill_id")
        outcome = command.add_mutually_exclusive_group(required=True)
        outcome.add_argument("--ok", dest="success", action="store_true")
        outcome.add_argument("--fail", dest="success", action="store_false")
        command.add_argument("--version", type=int)
        command.add_argument("--error")
        command.add_argument("--tag")
        command.add_argument("--model")
        command.add_argument("--source", default="manual")
        if name == "record":
            command.add_argument("--latency-ms", type=float)
            command.add_argument("--execution-id")
        else:
            command.add_argument("--run-id")
            command.add_argument("--session-id")
            command.add_argument("--input-json")
            command.add_argument("--output-json")
            command.add_argument("--tool-call-id", action="append",
                                 help="attach an imported ToolCall; repeat as needed")
            _out_format(command)
        command.set_defaults(func=handler)

    for name, handler in (("status", cmd_status), ("doctor", cmd_doctor)):
        command = sub.add_parser(name)
        _out_format(command)
        command.set_defaults(func=handler)

    command = sub.add_parser("attribute")
    command.add_argument("skill_id")
    command.add_argument("--version", type=int)
    _out_format(command)
    command.set_defaults(func=cmd_attribute)

    command = sub.add_parser("repair")
    command.add_argument("skill_id")
    command.add_argument("--content-file")
    command.set_defaults(func=cmd_repair)

    command = sub.add_parser("replay", help="offline replay gate before probation")
    command.add_argument("skill_id")
    command.add_argument("version", type=int)
    command.add_argument("--results", required=True,
                         help="JSON object mapping historical run_id to bool")
    _out_format(command)
    command.set_defaults(func=cmd_replay)

    command = sub.add_parser("evaluate")
    command.add_argument("skill_id")
    command.set_defaults(func=cmd_evaluate)

    for name, handler in (("promote", cmd_promote), ("rollback", cmd_rollback)):
        command = sub.add_parser(name)
        command.add_argument("skill_id")
        command.add_argument("version", type=int)
        command.set_defaults(func=handler)

    command = sub.add_parser("history")
    command.add_argument("skill_id")
    command.set_defaults(func=cmd_history)

    command = sub.add_parser("ingest")
    command.add_argument("path")
    command.add_argument("--format", required=True, choices=["claude", "codex"])
    command.add_argument("--pattern", default="*.jsonl")
    command.add_argument("--format-output", choices=["text", "json"], default="text")
    command.set_defaults(func=cmd_ingest)

    command = sub.add_parser("report", help="emit the complete JSON report")
    command.add_argument("--output")
    command.set_defaults(func=cmd_report)

    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
