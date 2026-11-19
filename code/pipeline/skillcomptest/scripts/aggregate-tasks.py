#!/usr/bin/env python3
"""Aggregate per-task ExecAgent results into platform-level outputs."""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.lib.io_utils import load_json
from scripts.lib.constants import CANONICAL_STATUSES, compute_verdict


def collect_task_results(exec_root, task_ids):
    """Collect per-task metrics from task directories."""
    tasks = []
    pass_count = 0
    fail_count = 0
    blocked_count = 0
    unsupported_count = 0
    unknown_count = 0
    observed_task_metrics_count = 0
    missing_task_metrics = []
    failure_categories = set()
    total_duration = 0.0

    for tid in task_ids:
        task_dir = exec_root / "tasks" / tid
        metrics = load_json(task_dir / "task_metrics.json")
        bash_timing = load_json(task_dir / "bash_timing.json")

        status = "unknown"
        failure_category = ""
        summary = ""
        duration_s = bash_timing.get("duration_s", 0) if bash_timing else 0

        if metrics:
            observed_task_metrics_count += 1
            status = metrics.get("status", "unknown")
            failure_category = metrics.get("failure_category", "")
            summary = metrics.get("summary", "")
            if status not in CANONICAL_STATUSES:
                status = "unknown"
                failure_category = failure_category or "unknown"
                summary = summary or "task_metrics.json used an unrecognized status"
        else:
            status = "unknown"
            failure_category = "unknown"
            summary = "task_metrics.json missing; runner did not produce a task result"
            missing_task_metrics.append(tid)

        if status == "pass":
            pass_count += 1
        elif status == "fail":
            fail_count += 1
        elif status == "blocked":
            blocked_count += 1
        elif status == "unsupported":
            unsupported_count += 1
        else:
            unknown_count += 1

        if failure_category and status != "pass":
            failure_categories.add(failure_category)

        total_duration += duration_s

        tasks.append({
            "task_id": tid,
            "status": status,
            "failure_category": failure_category,
            "duration_s": duration_s,
            "summary": summary[:200] if summary else "",
        })

    return (
        tasks,
        pass_count,
        fail_count,
        blocked_count,
        unsupported_count,
        unknown_count,
        observed_task_metrics_count,
        missing_task_metrics,
        failure_categories,
        total_duration,
    )


def compute_env_summary(exec_root, task_ids):
    """Extract environment evidence from the first task that has it."""
    env_summary = {
        "os": "",
        "shell": "",
        "available_runtimes": [],
        "available_browsers": [],
        "network": True,
        "disk_writable": True,
        "critical_blockers": [],
    }
    for tid in task_ids:
        metrics = load_json(exec_root / "tasks" / tid / "task_metrics.json")
        if metrics and metrics.get("environment_evidence"):
            ev = metrics["environment_evidence"]
            if ev.get("available_capabilities"):
                env_summary["available_runtimes"] = [
                    c for c in ev.get("available_capabilities", [])
                    if any(kw in c.lower() for kw in ("runtime", "python", "node", "npm", "pip"))
                ]
            if ev.get("missing_capabilities"):
                env_summary["critical_blockers"] = list(ev.get("missing_capabilities", []))
            break
    return env_summary


def write_outputs(exec_root, skill_id, platform, tasks, pass_count, fail_count,
                  blocked_count, unsupported_count, unknown_count,
                  observed_task_metrics_count, missing_task_metrics,
                  failure_categories, total_duration):
    """Write platform_summary.json, timing.json, metrics.json, platform_report.md."""
    total_tasks = len(tasks)
    success_rate = pass_count / total_tasks if total_tasks > 0 else 0.0
    verdict = compute_verdict(pass_count, fail_count, blocked_count, unsupported_count, total_tasks)
    env_summary = compute_env_summary(exec_root, [t["task_id"] for t in tasks])

    # platform_summary.json
    platform_summary = {
        "skill": skill_id,
        "platform": platform,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "blocked_count": blocked_count,
        "unsupported_count": unsupported_count,
        "unknown_count": unknown_count,
        "expected_task_count": total_tasks,
        "observed_task_metrics_count": observed_task_metrics_count,
        "missing_task_metrics_count": len(missing_task_metrics),
        "missing_task_metrics": missing_task_metrics,
        "platform_success_rate": round(success_rate, 3),
        "portability_verdict": verdict,
        "failure_categories_observed": sorted(failure_categories),
        "portability_blocker_types": [],
        "environment_summary": env_summary,
        "generated_by": "aggregate-tasks.py",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(exec_root / "platform_summary.json", "w", encoding="utf-8") as f:
        json.dump(platform_summary, f, indent=2, ensure_ascii=False)
    print(
        "  Wrote platform_summary.json "
        f"({pass_count}p/{fail_count}f/{blocked_count}b/{unsupported_count}u/{unknown_count}unknown)"
    )

    # timing.json
    timing = {
        "skill": skill_id,
        "platform": platform,
        "total_duration_s": total_duration,
        "task_count": total_tasks,
        "tasks": {
            t["task_id"]: {"duration_s": t["duration_s"], "status": t["status"]}
            for t in tasks
        },
    }
    with open(exec_root / "timing.json", "w", encoding="utf-8") as f:
        json.dump(timing, f, indent=2, ensure_ascii=False)
    avg = total_duration / total_tasks if total_tasks > 0 else 0
    print(f"  Wrote timing.json (total={total_duration:.0f}s, avg={avg:.0f}s/task)")

    # metrics.json
    metrics_out = {
        "schema_version": "execagent_metrics_v1",
        "platform": platform,
        "skill": skill_id,
        "total_tasks": total_tasks,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "blocked_count": blocked_count,
        "unsupported_count": unsupported_count,
        "unknown_count": unknown_count,
        "expected_task_count": total_tasks,
        "observed_task_metrics_count": observed_task_metrics_count,
        "missing_task_metrics_count": len(missing_task_metrics),
        "missing_task_metrics": missing_task_metrics,
        "platform_success_rate": round(success_rate, 3),
        "task_durations_seconds": {t["task_id"]: t["duration_s"] for t in tasks},
        "total_execution_seconds": total_duration,
        "failure_categories_observed": sorted(failure_categories),
        "portability_verdict": verdict,
        "generated_by": "aggregate-tasks.py",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(exec_root / "metrics.json", "w", encoding="utf-8") as f:
        json.dump(metrics_out, f, indent=2, ensure_ascii=False)
    print("  Wrote metrics.json")

    # platform_report.md
    _write_report(exec_root, skill_id, platform, tasks, pass_count, fail_count,
                  blocked_count, unsupported_count, unknown_count, success_rate,
                  verdict, failure_categories, total_duration)


def _write_report(exec_root, skill_id, platform, tasks, pass_count, fail_count,
                  blocked_count, unsupported_count, unknown_count, success_rate, verdict,
                  failure_categories, total_duration):
    lines = [
        f"# Platform Report: {skill_id} on {platform}",
        "",
        f"**Generated:** {datetime.now(timezone.utc).isoformat()}",
        f"**Verdict:** {verdict}",
        f"**Success Rate:** {success_rate:.1%} ({pass_count}/{len(tasks)} tasks)",
        "",
        "## Summary",
        "",
        f"| Metric | Count |",
        f"|--------|-------|",
        f"| Pass | {pass_count} |",
        f"| Fail | {fail_count} |",
        f"| Blocked | {blocked_count} |",
        f"| Unsupported | {unsupported_count} |",
        f"| Unknown | {unknown_count} |",
        f"| Total Duration | {total_duration:.0f}s |",
        "",
    ]
    if failure_categories:
        lines.append("## Failure Categories Observed")
        lines.append("")
        for cat in sorted(failure_categories):
            lines.append(f"- {cat}")
        lines.append("")
    lines.append("## Task Results")
    lines.append("")
    lines.append("| Task | Status | Failure Category | Duration | Summary |")
    lines.append("|------|--------|------------------|----------|---------|")
    for t in tasks:
        summary = t["summary"].replace("\n", " ")[:80]
        lines.append(
            f"| {t['task_id']} | {t['status']} | {t['failure_category'] or '-'} "
            f"| {t['duration_s']:.0f}s | {summary} |"
        )
    lines.append("")
    lines.append("## Portability Assessment")
    lines.append("")
    if verdict == "full_support":
        lines.append("This skill is fully portable to this platform with no significant issues.")
    elif verdict == "partial_support":
        lines.append("This skill is partially portable. Some tasks encountered platform-specific issues.")
    elif verdict == "blocked":
        lines.append("This skill is mostly blocked on this platform due to environment limitations.")
    elif verdict == "unsupported":
        lines.append("This skill is fundamentally unsupported on this platform.")
    report_path = exec_root / "platform_report.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("  Wrote platform_report.md")


def main():
    if len(sys.argv) < 5:
        print("Usage: python3 aggregate-tasks.py <exec_out> <sample_out> <platform> <skill_id>", file=sys.stderr)
        sys.exit(1)

    exec_out = Path(sys.argv[1])
    sample_out = sys.argv[2]
    platform = sys.argv[3]
    skill_id = sys.argv[4]

    manifest = load_json(os.path.join(sample_out, "benchmark_manifest.json"))
    task_ids = []
    if manifest and "functional_tasks" in manifest:
        task_ids = [t["id"] for t in manifest["functional_tasks"]]
    else:
        tasks_dir = exec_out / "tasks"
        if tasks_dir.is_dir():
            task_ids = sorted(d.name for d in tasks_dir.iterdir()
                            if d.is_dir() and d.name.startswith("T_"))

    if not task_ids:
        print(f"No tasks found in {exec_out}", file=sys.stderr)
        sys.exit(1)

    (
        tasks,
        pass_count,
        fail_count,
        blocked_count,
        unsupported_count,
        unknown_count,
        observed_task_metrics_count,
        missing_task_metrics,
        failure_categories,
        total_duration,
    ) = collect_task_results(exec_out, task_ids)
    write_outputs(exec_out, skill_id, platform, tasks, pass_count, fail_count,
                  blocked_count, unsupported_count, unknown_count,
                  observed_task_metrics_count, missing_task_metrics,
                  failure_categories, total_duration)


if __name__ == "__main__":
    main()
