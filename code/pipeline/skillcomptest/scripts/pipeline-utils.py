#!/usr/bin/env python3
"""Small JSON-writing utilities for the pipeline harness.

Usage:
  python3 pipeline-utils.py progress <file> <skills_done> <skills_total> <tasks_done> <tasks_total> <elapsed_s>
  python3 pipeline-utils.py task-ids <manifest_json>
  python3 pipeline-utils.py timing-update <timing_json> <key> <start_epoch> <end_epoch> <duration_s>
  python3 pipeline-utils.py timestamp <file> <epoch> [iso_string]
  python3 pipeline-utils.py bash-timing <file> <task_id> <start> <end> <duration> <exit_code>
  python3 pipeline-utils.py runner-failure-metrics <file> <task_id> <platform> <runner> <skill_id> <failure_category> <timeout_s> <model> <exit_code> <duration_s>
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.lib.io_utils import load_json


def cmd_progress():
    path, skills_done, skills_total, tasks_done, tasks_total, elapsed = sys.argv[2:9]
    p = {
        "updated_at": time.time(),
        "iso": datetime.now(timezone.utc).isoformat(),
        "phase": "running",
        "skills_completed": int(skills_done),
        "skills_total": int(skills_total),
        "tasks_completed": int(tasks_done),
        "tasks_total": int(tasks_total),
        "elapsed_s": int(elapsed),
    }
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(p, f)
    os.rename(tmp, path)


def cmd_task_ids():
    manifest_path = sys.argv[2]
    manifest = load_json(manifest_path)
    if manifest:
        for t in manifest.get("functional_tasks", []):
            print(t["id"])


def cmd_timing_update():
    path, key = sys.argv[2], sys.argv[3]
    start, end, dur = int(sys.argv[4]), int(sys.argv[5]), int(sys.argv[6])
    existing = load_json(path) or {}
    existing[key] = {"start_epoch": start, "end_epoch": end, "duration_s": dur}
    with open(path, "w") as f:
        json.dump(existing, f, indent=2)


def cmd_timestamp():
    path, epoch = sys.argv[2], int(sys.argv[3])
    iso = sys.argv[4] if len(sys.argv) > 4 else datetime.now(timezone.utc).isoformat()
    with open(path, "w") as f:
        json.dump({"epoch": epoch, "iso": iso}, f)


def cmd_bash_timing():
    path, tid, start, end, dur, rc = sys.argv[2:]
    with open(path, "w") as f:
        json.dump({
            "task_id": tid,
            "start_epoch": int(start),
            "end_epoch": int(end),
            "duration_s": int(dur),
            "exit_code": int(rc),
        }, f)


def cmd_runner_failure_metrics():
    (
        path,
        tid,
        platform,
        runner,
        skill_id,
        failure_category,
        timeout_s,
        model,
        exit_code,
        duration_s,
    ) = sys.argv[2:12]
    timeout_s_int = int(timeout_s)
    exit_code_int = int(exit_code)
    duration_s_int = int(duration_s)
    if failure_category == "runner_timeout":
        summary = (
            f"Task {tid} for {skill_id} did not produce task_metrics.json before "
            f"the configured runner timeout ({timeout_s_int}s per attempt, "
            f"exit code {exit_code_int}). This is runner-timeout evidence, not "
            "a skill OS compatibility conclusion."
        )
    else:
        summary = (
            f"Task {tid} for {skill_id} finished runner execution with exit code "
            f"{exit_code_int} but did not produce task_metrics.json. This is "
            "runner-missing-metrics evidence, not a skill OS compatibility conclusion."
        )
    metrics = {
        "task_id": tid,
        "platform": platform,
        "runner": runner,
        "status": "blocked",
        "failure_category": failure_category,
        "summary": summary,
        "environment": {
            "model": model,
            "timeout_s": timeout_s_int,
            "duration_s": duration_s_int,
            "exit_code": exit_code_int,
        },
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _cmd_checkpoint():
    """Validate a checkpoint file. Exits 0 if valid, 1 if not.
    Usage: pipeline-utils.py checkpoint <type> <path> [target_count]
    """
    ctype, path = sys.argv[2], sys.argv[3]
    data = load_json(path)
    if data is None:
        sys.exit(1)

    if ctype == "manifest":
        target = int(sys.argv[4]) if len(sys.argv) > 4 else 1
        tasks = data.get("functional_tasks", data.get("tasks", []))
        if len(tasks) < target:
            sys.exit(1)
    elif ctype == "summary":
        concrete_total = (
            data.get("pass_count", 0) +
            data.get("fail_count", 0) +
            data.get("blocked_count", 0) +
            data.get("unsupported_count", 0)
        )
        if concrete_total <= 0:
            sys.exit(1)
        if data.get("unknown_count", 0) > 0:
            sys.exit(1)
        expected = data.get("expected_task_count")
        observed = data.get("observed_task_metrics_count")
        if expected is not None:
            if concrete_total != expected:
                sys.exit(1)
            if observed is not None and observed != expected:
                sys.exit(1)
    elif ctype == "task-metrics":
        if data.get("status") not in ("pass", "fail", "blocked", "unsupported", "pending"):
            sys.exit(1)


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <command> [args...]", file=sys.stderr)
        sys.exit(1)

    cmds = {
        "progress": cmd_progress,
        "task-ids": cmd_task_ids,
        "timing-update": cmd_timing_update,
        "timestamp": cmd_timestamp,
        "bash-timing": cmd_bash_timing,
        "runner-failure-metrics": cmd_runner_failure_metrics,
    }
    cmd = sys.argv[1]
    if cmd in cmds:
        cmds[cmd]()
    elif cmd == "checkpoint":
        _cmd_checkpoint()
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
