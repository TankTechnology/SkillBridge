#!/usr/bin/env python3
"""Show lightweight status and profiling for one SkillCompTest batch run."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def load_events(event_log: Path, run_id: str) -> list[dict[str, Any]]:
    if not event_log.is_file():
        return []
    events: list[dict[str, Any]] = []
    for raw in event_log.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("batch_id") == run_id:
            events.append(event)
    return events


def fmt_duration(seconds: Any) -> str:
    if not isinstance(seconds, (int, float)):
        return "n/a"
    seconds = int(seconds)
    minutes, sec = divmod(seconds, 60)
    hours, minute = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minute:02d}m{sec:02d}s"
    if minutes:
        return f"{minutes}m{sec:02d}s"
    return f"{sec}s"


def fmt_cost(value: Any) -> str:
    return f"${value:.4f}" if isinstance(value, (int, float)) else "n/a"


def summarize_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(str(event.get("event", "")) for event in events)
    durations = {}
    for name in ("phase1_end", "phase2_end", "pipeline_end"):
        for event in reversed(events):
            if event.get("event") == name and isinstance(event.get("duration_s"), (int, float)):
                durations[name] = event["duration_s"]
                break
    pipeline_complete = any(event.get("event") == "pipeline_end" for event in events)
    active_tasks = counts["task_start"] - counts["task_done"] - counts["task_fail"] - counts["task_timeout"]
    active_skills = counts["stage2_start"] - counts["stage2_done"] - counts["stage2_fail"] - counts["stage2_skip"]
    if pipeline_complete:
        active_tasks = 0
        active_skills = 0
    return {
        "counts": counts,
        "durations": durations,
        "active_tasks": max(active_tasks, 0),
        "active_skills": max(active_skills, 0),
    }


def pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_tail(path: Path, lines: int) -> list[str]:
    if lines <= 0 or not path.is_file():
        return []
    text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return text[-lines:]


def queued_count(plan: dict[str, Any]) -> int:
    queued = plan.get("queued")
    return len(queued) if isinstance(queued, list) else 0


def print_report_summary(report: dict[str, Any]) -> None:
    aggregate = report.get("aggregate", {})
    if not isinstance(aggregate, dict):
        aggregate = {}
    print(
        "Skills: "
        f"{report.get('skills_executed', 0)}/{report.get('total_skills', 0)} executed | "
        f"samples={report.get('skills_with_samples', 0)}"
    )
    print(
        "Tasks: "
        f"{aggregate.get('total_tasks', 0)} total | "
        f"pass={aggregate.get('pass', 0)} fail={aggregate.get('fail', 0)} "
        f"blocked={aggregate.get('blocked', 0)} unsupported={aggregate.get('unsupported', 0)} "
        f"unknown={aggregate.get('unknown', 0)}"
    )
    print(
        "Cost/time: "
        f"{fmt_cost(aggregate.get('runner_total_cost_usd'))} | "
        f"wall={fmt_duration(report.get('duration_s'))} | "
        f"avg_skill={fmt_duration(aggregate.get('avg_skill_time_s'))}"
    )


def print_events_summary(events: list[dict[str, Any]]) -> None:
    summary = summarize_events(events)
    counts: Counter[str] = summary["counts"]
    durations = summary["durations"]
    print(
        "Events: "
        f"S1 done/fail/skip={counts['stage1_done']}/{counts['stage1_fail']}/{counts['stage1_skip']} | "
        f"S2 done/fail/skip={counts['stage2_done']}/{counts['stage2_fail']}/{counts['stage2_skip']} | "
        f"tasks done/fail/timeout/skip="
        f"{counts['task_done']}/{counts['task_fail']}/{counts['task_timeout']}/{counts['task_skip']}"
    )
    print(
        "Phase durations: "
        f"S1={fmt_duration(durations.get('phase1_end'))} | "
        f"S2={fmt_duration(durations.get('phase2_end'))} | "
        f"pipeline={fmt_duration(durations.get('pipeline_end'))} | "
        f"active_skills={summary['active_skills']} active_tasks={summary['active_tasks']}"
    )


def print_recent_events(events: list[dict[str, Any]], count: int) -> None:
    if count <= 0 or not events:
        return
    print("Recent events:")
    for event in events[-count:]:
        pieces = [str(event.get("iso", "")), str(event.get("event", ""))]
        for key in ("skill", "task_id", "duration_s", "exit_code"):
            if key in event:
                value = event[key]
                if key == "duration_s":
                    value = fmt_duration(value)
                pieces.append(f"{key}={value}")
        print("  " + " ".join(piece for piece in pieces if piece))


def print_log_tail(log_path: Path, tail: int) -> None:
    lines = read_tail(log_path, tail)
    if not lines:
        return
    print(f"Log tail: {log_path}")
    for line in lines:
        print(f"  {line}")


def infer_status(report: dict[str, Any], events: list[dict[str, Any]], watch: dict[str, Any], plan: dict[str, Any]) -> str:
    if report:
        return "complete"
    if watch.get("finished_at"):
        return "failed_or_incomplete" if watch.get("docker_returncode") else "finished_no_report"
    if pid_alive(watch.get("pid")):
        return "running"
    if any(event.get("event") == "pipeline_start" for event in events):
        if any(event.get("event") == "pipeline_end" for event in events):
            return "finished_no_report"
        return "interrupted_or_running"
    if plan:
        return "planned"
    return "not_found"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--data-store",
        default=os.environ.get("DATA_STORE", str(REPO_ROOT / ".cache" / "skillcomptest")),
    )
    parser.add_argument("--tail", type=int, default=20, help="batch launcher log lines to print")
    parser.add_argument("--recent-events", type=int, default=8)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_id = args.run_id
    data_store = Path(args.data_store)
    queue_dir = data_store / "queues"
    watcher_dir = data_store / "watchers"

    plan_path = queue_dir / f"{run_id}.plan.json"
    report_path = queue_dir / f"{run_id}.report.json"
    watch_path = watcher_dir / f"{run_id}.watch.json"
    log_path = watcher_dir / f"{run_id}.log"

    plan = load_json(plan_path)
    report = load_json(report_path)
    watch = load_json(watch_path)
    events = load_events(data_store / "pipeline_events.jsonl", run_id)

    print(f"Run: {run_id}")
    print(f"Status: {infer_status(report, events, watch, plan)}")
    if plan:
        profile = plan.get("profile", {})
        if not isinstance(profile, dict):
            profile = {}
        print(
            "Plan: "
            f"queued={queued_count(plan)} | "
            f"model={profile.get('claude_model', '')} | "
            f"profile={profile.get('task_design_model', '')} | "
            f"policy={plan.get('queue_policy', '')}"
        )
    if watch:
        started_at = watch.get("started_at", "")
        finished_at = watch.get("finished_at", "")
        pid = watch.get("pid")
        print(
            "Watcher: "
            f"pid={pid} alive={pid_alive(pid)} | "
            f"started={started_at} | finished={finished_at or 'n/a'} | "
            f"log={log_path}"
        )
    if report:
        print_report_summary(report)
    if events:
        print_events_summary(events)
        print_recent_events(events, args.recent_events)
    elif not report:
        print("Events: none yet")
    print_log_tail(log_path, args.tail)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
