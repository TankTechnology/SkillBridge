#!/usr/bin/env python3
"""Validate pipeline output integrity.

Checks:
  1. platform_summary.json counts match per-task metrics
  2. No corrupted/missing task_metrics.json
  3. Pass+Fail+Blocked+Unsupported == total tasks
  4. timing.json data is consistent with per-task bash_timing.json

Usage:
  python3 validate-outputs.py <exec_root> [--strict]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.lib.io_utils import load_json
from scripts.lib.constants import compute_verdict


def validate_skill(exec_root, strict=False):
    root = Path(exec_root)
    errors = []
    warnings = []

    summary = load_json(root / "platform_summary.json")
    if summary is None:
        errors.append("platform_summary.json: missing or unparseable")
        return errors, warnings

    tasks_dir = root / "tasks"
    if tasks_dir.is_dir():
        task_ids = sorted(d.name for d in tasks_dir.iterdir() if d.is_dir() and d.name.startswith("T_"))
    else:
        task_ids = []

    if not task_ids:
        warnings.append("No task directories found")
        return errors, warnings

    actual_pass = actual_fail = actual_blocked = actual_unsupported = 0
    missing_metrics = []
    corrupted_metrics = []

    for tid in task_ids:
        tm = load_json(tasks_dir / tid / "task_metrics.json")
        if tm is None:
            corrupted_metrics.append(tid)
            continue
        if not isinstance(tm, dict):
            missing_metrics.append(tid)
            continue
        status = tm.get("status", "unknown")
        if status == "pass":
            actual_pass += 1
        elif status == "fail":
            actual_fail += 1
        elif status == "blocked":
            actual_blocked += 1
        elif status == "unsupported":
            actual_unsupported += 1
        else:
            warnings.append(f"{tid}: unknown status '{status}'")

    if missing_metrics:
        errors.append(f"Missing task_metrics.json: {missing_metrics}")
    if corrupted_metrics:
        errors.append(f"Corrupted task_metrics.json: {corrupted_metrics}")

    if actual_pass != summary.get("pass_count", 0):
        errors.append(f"pass_count mismatch: summary={summary.get('pass_count')} actual={actual_pass}")
    if actual_fail != summary.get("fail_count", 0):
        errors.append(f"fail_count mismatch: summary={summary.get('fail_count')} actual={actual_fail}")
    if actual_blocked != summary.get("blocked_count", 0):
        errors.append(f"blocked_count mismatch: summary={summary.get('blocked_count')} actual={actual_blocked}")
    if actual_unsupported != summary.get("unsupported_count", 0):
        errors.append(f"unsupported_count mismatch: summary={summary.get('unsupported_count')} actual={actual_unsupported}")

    total = actual_pass + actual_fail + actual_blocked + actual_unsupported
    if total != len(task_ids) and not missing_metrics:
        errors.append(f"Task count mismatch: {total} classified vs {len(task_ids)} task dirs")

    timing = load_json(root / "timing.json")
    if timing is not None:
        timing_total = 0
        for tid in task_ids:
            bt = load_json(tasks_dir / tid / "bash_timing.json")
            if bt is not None:
                timing_total += bt.get("duration_s", 0)
        if abs(timing_total - timing.get("total_duration_s", 0)) > 1:
            warnings.append(f"timing.json total {timing.get('total_duration_s')}s != sum of bash_timing {timing_total}s")

    verdict = summary.get("portability_verdict", "")
    expected_verdict = compute_verdict(actual_pass, actual_fail, actual_blocked, actual_unsupported, len(task_ids))
    if verdict != expected_verdict:
        pr = actual_pass / len(task_ids) if len(task_ids) > 0 else 0
        warnings.append(f"Verdict '{verdict}' inconsistent with {pr:.0%} pass rate (expected '{expected_verdict}')")

    return errors, warnings


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 validate-outputs.py <exec_root> [--strict]", file=sys.stderr)
        sys.exit(1)

    exec_root = sys.argv[1]
    strict = "--strict" in sys.argv
    errors, warnings = validate_skill(exec_root, strict=strict)

    skill_name = Path(exec_root).name
    print(f"  {skill_name}: ", end="")

    if errors:
        print(f"FAIL ({len(errors)} errors, {len(warnings)} warnings)")
        for e in errors:
            print(f"    ERROR: {e}")
        for w in warnings:
            print(f"    WARN:  {w}")
        sys.exit(1)
    elif warnings and strict:
        print(f"WARN ({len(warnings)} warnings)")
        for w in warnings:
            print(f"    WARN:  {w}")
        sys.exit(1)
    else:
        status = "OK"
        if warnings:
            status += f" ({len(warnings)} warnings)"
        print(status)
        for w in warnings:
            print(f"    WARN:  {w}")


if __name__ == "__main__":
    errors, _ = main()
    sys.exit(1 if errors else 0)
