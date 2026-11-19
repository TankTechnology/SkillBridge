#!/usr/bin/env python3
"""Export a lightweight durable summary for an existing batch run.

This script reads existing DATA_STORE artifacts only. It does not execute skills
or copy raw agent logs into git-tracked documentation.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"ERROR: required artifact not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"ERROR: invalid JSON in {path}: {exc}") from None
    if not isinstance(data, dict):
        raise SystemExit(f"ERROR: expected JSON object in {path}")
    return data


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def relative_to_data_store(path: Path, data_store: Path) -> str:
    try:
        return path.resolve().relative_to(data_store.resolve()).as_posix()
    except ValueError:
        return path.name


def read_queue_skill_ids(queue_path: Path) -> list[str]:
    if not queue_path.is_file():
        return []
    skill_ids: list[str] = []
    for raw in queue_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|")
        if len(parts) >= 2:
            skill_ids.append(f"{parts[0]}/{parts[1]}")
        else:
            skill_ids.append(line)
    return skill_ids


def batch_list_text(list_path: Path, queue_path: Path) -> str:
    if list_path.is_file():
        text = list_path.read_text(encoding="utf-8")
        return text if text.endswith("\n") else text + "\n"
    skill_ids = read_queue_skill_ids(queue_path)
    header = "# Generated from batch queue because no curated SKILL_LIST artifact was found.\n"
    return header + "".join(f"{skill_id}\n" for skill_id in skill_ids)


def compact_results(results: list[dict]) -> list[dict]:
    compact: list[dict] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source", ""))
        skill = str(item.get("skill", ""))
        skill_id = f"{source}/{skill}" if source and skill else skill or source
        compact.append(
            {
                "skill_id": skill_id,
                "source": source,
                "skill": skill,
                "status": item.get("status", ""),
                "tasks_generated": item.get("tasks_generated", 0),
                "tasks_pass": item.get("tasks_pass", 0),
                "tasks_fail": item.get("tasks_fail", 0),
                "tasks_blocked": item.get("tasks_blocked", 0),
                "tasks_unsupported": item.get("tasks_unsupported", 0),
                "tasks_unknown": item.get("tasks_unknown", 0),
                "success_rate": item.get("success_rate", 0),
                "portability_verdict": item.get("portability_verdict", ""),
                "exec_duration_s": item.get("exec_duration_s", 0),
                "runner_cost_usd": item.get("runner_cost_usd"),
                "runner_input_tokens": item.get("runner_input_tokens"),
                "runner_output_tokens": item.get("runner_output_tokens"),
            }
        )
    return compact


def build_summary(run_id: str, data_store: Path, report: dict, plan: dict, artifacts: dict[str, str]) -> dict:
    aggregate = report.get("aggregate", {})
    if not isinstance(aggregate, dict):
        aggregate = {}
    profile = plan.get("profile", {})
    if not isinstance(profile, dict):
        profile = {}

    return {
        "schema_version": "linux_run_summary_v1",
        "run_id": run_id,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "platform": report.get("platform", profile.get("platform", "")),
        "profile": profile,
        "queue_policy": plan.get("queue_policy", ""),
        "sample_refresh_policy": plan.get("sample_refresh_policy", ""),
        "allow_rerun_complete": plan.get("allow_rerun_complete", False),
        "source": {
            "data_store_name": data_store.name,
        },
        "artifacts": artifacts,
        "start_time": report.get("start_time", ""),
        "end_time": report.get("end_time", ""),
        "duration_s": report.get("duration_s", 0),
        "total_skills": report.get("total_skills", 0),
        "skills_with_samples": report.get("skills_with_samples", 0),
        "skills_executed": report.get("skills_executed", 0),
        "sample_rate": report.get("sample_rate", ""),
        "exec_rate": report.get("exec_rate", ""),
        "aggregate": aggregate,
        "queued_skill_ids": plan.get("queued", []),
        "results": compact_results(report.get("results", [])),
    }


def fmt_cost(value) -> str:
    if isinstance(value, (int, float)):
        return f"${value:.4f}"
    return ""


def markdown_table_value(value) -> str:
    return str(value if value is not None else "").replace("|", "\\|")


def build_markdown(summary: dict) -> str:
    aggregate = summary["aggregate"]
    lines = [
        f"# {summary['run_id']}",
        "",
        f"- Platform: {summary.get('platform', '')}",
        f"- Profile: {summary.get('profile', {}).get('task_design_model', '')}",
        f"- Skills: {summary.get('skills_executed', 0)}/{summary.get('total_skills', 0)} executed",
        (
            f"- Tasks: {aggregate.get('total_tasks', 0)} total | "
            f"pass={aggregate.get('pass', 0)} fail={aggregate.get('fail', 0)} "
            f"blocked={aggregate.get('blocked', 0)} unsupported={aggregate.get('unsupported', 0)} "
            f"unknown={aggregate.get('unknown', 0)}"
        ),
        f"- Compatibility rate: {aggregate.get('compatibility_rate', '')}",
        f"- Runner cost: {fmt_cost(aggregate.get('runner_total_cost_usd'))}",
        f"- Report artifact: {summary.get('artifacts', {}).get('report', '')}",
        "",
        "## Skill Results",
        "",
        "| Skill | Status | Pass | Fail | Blocked | Unsupported | Unknown | Verdict | Cost |",
        "|---|---:|---:|---:|---:|---:|---:|---|---:|",
    ]
    for item in summary["results"]:
        lines.append(
            "| "
            + " | ".join(
                [
                    markdown_table_value(item["skill_id"]),
                    markdown_table_value(item["status"]),
                    markdown_table_value(item["tasks_pass"]),
                    markdown_table_value(item["tasks_fail"]),
                    markdown_table_value(item["tasks_blocked"]),
                    markdown_table_value(item["tasks_unsupported"]),
                    markdown_table_value(item["tasks_unknown"]),
                    markdown_table_value(item["portability_verdict"]),
                    markdown_table_value(fmt_cost(item["runner_cost_usd"])),
                ]
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def index_entry(summary: dict) -> dict:
    aggregate = summary["aggregate"]
    return {
        "run_id": summary["run_id"],
        "platform": summary.get("platform", ""),
        "total_skills": summary.get("total_skills", 0),
        "skills_executed": summary.get("skills_executed", 0),
        "total_tasks": aggregate.get("total_tasks", 0),
        "pass": aggregate.get("pass", 0),
        "fail": aggregate.get("fail", 0),
        "blocked": aggregate.get("blocked", 0),
        "unsupported": aggregate.get("unsupported", 0),
        "unknown": aggregate.get("unknown", 0),
        "runner_total_cost_usd": aggregate.get("runner_total_cost_usd"),
        "summary": f"runs/{summary['run_id']}.json",
        "report": summary["artifacts"]["report"],
    }


def update_index(index_path: Path, entry: dict) -> None:
    entries: list[dict] = []
    if index_path.is_file():
        for raw in index_path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            try:
                existing = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(existing, dict) and existing.get("run_id") != entry["run_id"]:
                entries.append(existing)
    entries.append(entry)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in entries),
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True, help="Batch ID to export")
    parser.add_argument(
        "--data-store",
        default=os.environ.get("DATA_STORE", str(REPO_ROOT / ".cache" / "skillcomptest")),
        help="SkillCompTest DATA_STORE root",
    )
    parser.add_argument(
        "--docs-root",
        default=str(REPO_ROOT / "docs" / "linux-testing"),
        help="Destination root for durable Linux testing records",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_id = args.run_id
    data_store = Path(args.data_store)
    docs_root = Path(args.docs_root)
    queue_dir = data_store / "queues"

    queue_path = queue_dir / f"{run_id}.queue"
    list_path = queue_dir / f"{run_id}.list"
    plan_path = queue_dir / f"{run_id}.plan.json"
    report_path = queue_dir / f"{run_id}.report.json"

    report = load_json(report_path)
    plan = load_json(plan_path) if plan_path.is_file() else {}
    artifacts = {
        "report": relative_to_data_store(report_path, data_store),
        "queue": relative_to_data_store(queue_path, data_store),
        "plan": relative_to_data_store(plan_path, data_store),
    }
    if list_path.is_file():
        artifacts["source_skill_list"] = relative_to_data_store(list_path, data_store)

    summary = build_summary(run_id, data_store, report, plan, artifacts)

    batch_path = docs_root / "batches" / f"{run_id}.txt"
    summary_path = docs_root / "runs" / f"{run_id}.json"
    markdown_path = docs_root / "runs" / f"{run_id}.md"
    index_path = docs_root / "index.jsonl"

    batch_path.parent.mkdir(parents=True, exist_ok=True)
    batch_path.write_text(batch_list_text(list_path, queue_path), encoding="utf-8")
    write_json(summary_path, summary)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(build_markdown(summary), encoding="utf-8")
    update_index(index_path, index_entry(summary))

    print(f"Exported batch list: {batch_path}")
    print(f"Exported run summary: {summary_path}")
    print(f"Exported run notes: {markdown_path}")
    print(f"Updated run index: {index_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
