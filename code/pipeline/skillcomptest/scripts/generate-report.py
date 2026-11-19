#!/usr/bin/env python3
"""Generate batch-report.json from pipeline outputs.

Usage:
  python3 generate-report.py <task_queue_file> <exec_dir> <samples_dir> \
      <task_design_model> <batch_report_path> <platform> \
      <start_time> <end_time> <duration_s> <task_count>
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.lib.io_utils import load_json
from scripts.lib.runner_metrics import summarize_metric_records


def estimate_tokens(char_count):
    """Rough token estimate from log file character count (~4 chars/token)."""
    return char_count // 4


def load_runner_metric(path):
    data = load_json(str(path))
    return data if isinstance(data, dict) else None


def collect_runner_metrics(sample_out, exec_out):
    records = []
    sample_metric = Path(sample_out) / "runner_metrics.json"
    metric = load_runner_metric(sample_metric)
    if metric is not None:
        records.append(metric)

    tasks_exec_dir = Path(exec_out) / "tasks"
    if tasks_exec_dir.is_dir():
        for metric_path in sorted(tasks_exec_dir.glob("T_*/runner_metrics.json")):
            metric = load_runner_metric(metric_path)
            if metric is not None:
                records.append(metric)
    return records, summarize_metric_records(records)


def main():
    if len(sys.argv) < 10:
        print("Usage: python3 generate-report.py <task_queue> <exec_dir> <samples_dir> "
              "<design_model> <report_path> <platform> <start> <end> <duration_s> <task_count>",
              file=sys.stderr)
        sys.exit(1)

    queue_file = sys.argv[1]
    exec_dir = sys.argv[2]
    samples_dir = sys.argv[3]
    design_model = sys.argv[4]
    report_path = sys.argv[5]
    platform = sys.argv[6]
    start_time = sys.argv[7]
    end_time = sys.argv[8]
    try:
        duration_s = int(sys.argv[9])
    except ValueError:
        print(f"ERROR: duration_s must be an integer, got '{sys.argv[9]}'", file=sys.stderr)
        sys.exit(1)
    task_count = int(sys.argv[10]) if len(sys.argv) > 10 else 5

    results = []
    total_pass = 0
    total_fail = 0
    total_blocked = 0
    total_unsupported = 0
    total_unknown = 0
    total_tasks = 0
    total_timing_s = 0.0
    grand_total_chars = 0
    all_runner_metric_records = []

    for line in open(queue_file):
        try:
            src, name, _ = line.strip().split("|")
        except ValueError:
            print(f"WARNING: skipping malformed queue line: {line.strip()}", file=sys.stderr)
            continue

        exec_out = os.path.join(exec_dir, src, name, design_model, design_model)
        sample_out = os.path.join(samples_dir, src, name, design_model)

        status = "not_run"
        tasks_gen = 0
        tasks_pass = 0
        tasks_fail = 0
        tasks_blocked = 0
        tasks_unsupported = 0
        tasks_unknown = 0
        portability_verdict = ""
        success_rate = 0.0
        stage_duration_s = 0.0
        total_chars = 0

        tasks_dir = os.path.join(sample_out, "tasks")
        if os.path.isdir(tasks_dir):
            tasks_gen = len(os.listdir(tasks_dir))

        summary_file = os.path.join(exec_out, "platform_summary.json")
        summary = load_json(summary_file)
        if summary is not None:
            status = "executed"
            tasks_pass = summary.get("pass_count", 0)
            tasks_fail = summary.get("fail_count", 0)
            tasks_blocked = summary.get("blocked_count", 0)
            tasks_unsupported = summary.get("unsupported_count", 0)
            tasks_unknown = summary.get("unknown_count", 0)
            portability_verdict = summary.get("portability_verdict", "")
            success_rate = summary.get("platform_success_rate", 0.0)
            total_pass += tasks_pass
            total_fail += tasks_fail
            total_blocked += tasks_blocked
            total_unsupported += tasks_unsupported
            total_unknown += tasks_unknown
            total_tasks += tasks_pass + tasks_fail + tasks_blocked + tasks_unsupported + tasks_unknown

        timing_file = os.path.join(exec_out, "timing.json")
        timing = load_json(timing_file)
        if timing is not None:
            stage_duration_s = timing.get("total_duration_s", 0)
            total_timing_s += stage_duration_s

        tasks_exec_dir = os.path.join(exec_out, "tasks")
        if os.path.isdir(tasks_exec_dir):
            for td in os.listdir(tasks_exec_dir):
                log_path = os.path.join(tasks_exec_dir, td, "execution.log")
                if os.path.isfile(log_path):
                    total_chars += os.path.getsize(log_path)
        s1_log = os.path.join(sample_out, "sample_agent.log")
        if os.path.isfile(s1_log):
            total_chars += os.path.getsize(s1_log)
        grand_total_chars += total_chars
        runner_records, runner_summary = collect_runner_metrics(sample_out, exec_out)
        all_runner_metric_records.extend(runner_records)

        results.append({
            "source": src,
            "skill": name,
            "status": status,
            "tasks_generated": tasks_gen,
            "tasks_pass": tasks_pass,
            "tasks_fail": tasks_fail,
            "tasks_blocked": tasks_blocked,
            "tasks_unsupported": tasks_unsupported,
            "tasks_unknown": tasks_unknown,
            "success_rate": success_rate,
            "portability_verdict": portability_verdict,
            "exec_duration_s": stage_duration_s,
            "est_tokens": estimate_tokens(total_chars),
            "log_chars": total_chars,
            "runner_metric_files": runner_summary["metric_file_count"],
            "runner_cost_usd": runner_summary["total_cost_usd"],
            "runner_input_tokens": runner_summary["input_tokens"],
            "runner_output_tokens": runner_summary["output_tokens"],
            "runner_cache_creation_input_tokens": runner_summary["cache_creation_input_tokens"],
            "runner_cache_read_input_tokens": runner_summary["cache_read_input_tokens"],
            "runner_estimated_prompt_tokens": runner_summary["estimated_prompt_tokens"],
            "runner_estimated_log_tokens": runner_summary["estimated_log_tokens"],
        })

    total = len(results)
    executed = sum(1 for r in results if r["status"] == "executed")
    samples_threshold = max(task_count - 2, 1)
    samples = sum(1 for r in results if r["tasks_generated"] >= samples_threshold)
    runner_aggregate = summarize_metric_records(all_runner_metric_records)

    report = {
        "platform": platform,
        "start_time": start_time,
        "end_time": end_time,
        "duration_s": duration_s,
        "total_skills": total,
        "skills_with_samples": samples,
        "skills_executed": executed,
        "sample_rate": f"{round(samples / total * 100, 1)}%" if total > 0 else "N/A",
        "exec_rate": f"{round(executed / total * 100, 1)}%" if total > 0 else "N/A",
        "aggregate": {
            "total_tasks": total_tasks,
            "pass": total_pass,
            "fail": total_fail,
            "blocked": total_blocked,
            "unsupported": total_unsupported,
            "unknown": total_unknown,
            "compatibility_rate": f"{round(total_pass / total_tasks * 100, 1)}%" if total_tasks > 0 else "N/A",
            "total_exec_time_s": round(total_timing_s, 1),
            "avg_skill_time_s": round(total_timing_s / max(executed, 1), 1),
            "est_total_tokens": estimate_tokens(grand_total_chars),
            "total_log_mb": round(grand_total_chars / (1024 * 1024), 2),
            "runner_metric_files": runner_aggregate["metric_file_count"],
            "runner_total_cost_usd": runner_aggregate["total_cost_usd"],
            "runner_input_tokens": runner_aggregate["input_tokens"],
            "runner_output_tokens": runner_aggregate["output_tokens"],
            "runner_cache_creation_input_tokens": runner_aggregate["cache_creation_input_tokens"],
            "runner_cache_read_input_tokens": runner_aggregate["cache_read_input_tokens"],
            "runner_estimated_prompt_tokens": runner_aggregate["estimated_prompt_tokens"],
            "runner_estimated_log_tokens": runner_aggregate["estimated_log_tokens"],
        },
        "results": results,
    }

    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(f"Total skills: {total}")
    print(f"Samples generated: {samples} ({report['sample_rate']})")
    print(f"Executed: {executed} ({report['exec_rate']})")
    if total_tasks > 0:
        print(
            f"Tasks: {total_tasks} total | pass={total_pass} fail={total_fail} "
            f"blocked={total_blocked} unsupported={total_unsupported} unknown={total_unknown}"
        )
        print(f"Compatibility rate: {report['aggregate']['compatibility_rate']}")
    if total_timing_s > 0:
        print(f"Exec time: {total_timing_s:.0f}s ({total_timing_s / 60:.0f}m) | Avg/skill: {total_timing_s / max(executed, 1):.0f}s")
    if grand_total_chars > 0:
        print(f"Est tokens: {estimate_tokens(grand_total_chars):,} (~{grand_total_chars / (1024*1024):.1f}MB logs)")
    if runner_aggregate["metric_file_count"] > 0:
        cost = runner_aggregate["total_cost_usd"]
        cost_text = f"${cost:.4f}" if cost is not None else "N/A"
        print(
            "Runner metrics: "
            f"{runner_aggregate['metric_file_count']} files | "
            f"cost={cost_text} | "
            f"input={runner_aggregate['input_tokens'] or 0:,} "
            f"output={runner_aggregate['output_tokens'] or 0:,}"
        )
    print(f"Wall duration: {duration_s}s ({duration_s // 60}m)")
    print(f"Report: {report_path}")


if __name__ == "__main__":
    main()
