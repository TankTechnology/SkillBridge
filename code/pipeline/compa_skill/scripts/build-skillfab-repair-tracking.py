#!/usr/bin/env python3
"""Build the SkillFab repair tracking manifest.

The manifest is one row per skill package / SkillFab issue. It joins the
repair-planning list with pre-repair dynamic benchmark summaries so later RQ2
and RQ3 analyses can track repair rounds, review-agent usage, time, tokens, and
cost without mutating the original benchmark data.

This script is data-only. It does not execute skill code, setup scripts,
generated tasks, package managers, browsers, or GUI commands.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter
import json
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = REPO_ROOT.parent
DEFAULT_TEST_REPO = WORKSPACE_ROOT / "skillcomptest"
DATE_STAMP = "20260627"
DEFAULT_ISSUES = REPO_ROOT / "docs/repair-workflow/os-issue-skills-simple-20260627.csv"
DEFAULT_DYNAMIC = DEFAULT_TEST_REPO / "docs/cross-os-testing/datasets/unified-dynamic-skill-labels.jsonl"
DEFAULT_OUT_PREFIX = REPO_ROOT / "docs/repair-workflow/skillfab-issue-manifest-20260627"

OS_ORDER = {"linux": 0, "macos": 1, "windows": 2}
TOKEN_FIELDS = ("runner_input_tokens", "runner_output_tokens")
COUNT_FIELDS = ("pass", "fail", "blocked", "unsupported", "unknown", "pending", "total")

CSV_FIELDS = [
    "issue_key",
    "skill_id",
    "open_skillfab_issue",
    "affected_os",
    "repair_os",
    "non_repair_os",
    "issue_title",
    "unified_fix_plan",
    "baseline_outcome_summary",
    "baseline_total_tasks",
    "baseline_pass",
    "baseline_fail",
    "baseline_blocked",
    "baseline_unsupported",
    "baseline_unknown",
    "baseline_runner_cost_usd",
    "baseline_runner_input_tokens",
    "baseline_runner_output_tokens",
    "baseline_models",
    "baseline_missing_os",
    "tracking_status",
    "skillfab_issue_id",
    "skillfab_submission_id",
    "latest_round_index",
    "latest_review_agent_used",
    "latest_review_verdict",
    "latest_repair_result",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issues-csv", type=Path, default=DEFAULT_ISSUES)
    parser.add_argument("--dynamic-jsonl", type=Path, default=DEFAULT_DYNAMIC)
    parser.add_argument("--out-prefix", type=Path, default=DEFAULT_OUT_PREFIX)
    args = parser.parse_args()

    issue_rows = read_csv(args.issues_csv)
    dynamic_by_key = index_dynamic(read_jsonl(args.dynamic_jsonl))
    manifest = build_manifest(issue_rows, dynamic_by_key)
    summary = build_summary(manifest, args.issues_csv, args.dynamic_jsonl)

    write_jsonl(args.out_prefix.with_suffix(".jsonl"), manifest)
    write_csv(args.out_prefix.with_suffix(".csv"), manifest)
    write_json(args.out_prefix.with_name(args.out_prefix.name + "-summary.json"), summary)
    args.out_prefix.with_name(args.out_prefix.name + "-summary.md").write_text(
        render_summary_md(summary), encoding="utf-8"
    )

    print(json.dumps({"manifest_rows": len(manifest), "open_issues": summary["open_skillfab_issue_count"]}, sort_keys=True))
    return 0


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def index_dynamic(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    result = {}
    for row in rows:
        target_os = str(row.get("target_os") or "")
        skill_id = str(row.get("skill_id") or "")
        if target_os and skill_id:
            result[(target_os, skill_id)] = row
    return result


def build_manifest(
    issue_rows: list[dict[str, str]],
    dynamic_by_key: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    manifest = []
    for index, row in enumerate(issue_rows, start=1):
        affected_os = split_os(row["affected_os"])
        repair_os = split_os(row.get("repair_os", ""))
        non_repair_os = split_os(row.get("non_repair_os", ""))
        baseline = build_pre_repair_baseline(row["skill_id"], affected_os, dynamic_by_key)
        issue_key = f"SFOS-{index:04d}"
        manifest.append(
            {
                "schema_version": "skillfab_repair_issue_manifest_v1",
                "issue_key": issue_key,
                "skill_id": row["skill_id"],
                "open_skillfab_issue": row["open_skillfab_issue"] == "yes",
                "affected_os": affected_os,
                "repair_os": repair_os,
                "non_repair_os": non_repair_os,
                "os_issue_label_count": int(row.get("os_issue_label_count") or 0),
                "repair_label_count": int(row.get("repair_label_count") or 0),
                "non_repair_label_count": int(row.get("non_repair_label_count") or 0),
                "issue_title": row.get("issue_title", ""),
                "issue_summary": row.get("issue_summary", ""),
                "unified_fix_plan": row.get("unified_fix_plan", ""),
                "source_files": split_semicolon(row.get("source_files", "")),
                "pre_repair_baseline": baseline,
                "skillfab_tracking": {
                    "tracking_status": "not_started",
                    "skillfab_issue_id": "",
                    "skillfab_submission_id": "",
                    "skillfab_repository": "",
                    "worktree_path": "",
                    "current_round_index": 0,
                    "latest_review_agent_used": None,
                    "latest_review_verdict": "",
                    "latest_repair_result": "",
                },
                "rq3_measurement": {
                    "record_rounds_in": "docs/repair-workflow/skillfab-tracking/rounds.jsonl",
                    "record_events_in": "docs/repair-workflow/skillfab-tracking/events.jsonl",
                    "compare_round_1_without_review_to_later_reviewed_rounds": True,
                    "fields_to_record": [
                        "wall_time_seconds",
                        "input_tokens",
                        "output_tokens",
                        "cache_creation_input_tokens",
                        "cache_read_input_tokens",
                        "total_cost_usd",
                        "review_agent_used",
                        "review_verdict",
                        "round_result",
                    ],
                },
            }
        )
    return manifest


def build_pre_repair_baseline(
    skill_id: str,
    affected_os: list[str],
    dynamic_by_key: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    by_os = {}
    missing = []
    totals = {field: 0 for field in COUNT_FIELDS}
    cost = 0.0
    cost_seen = False
    token_totals = {field: 0 for field in TOKEN_FIELDS}
    token_seen = {field: False for field in TOKEN_FIELDS}
    models: set[str] = set()
    outcomes = {}
    raw_paths: list[str] = []
    source_artifacts: list[str] = []

    for target_os in affected_os:
        dynamic = dynamic_by_key.get((target_os, skill_id))
        if not dynamic:
            missing.append(target_os)
            by_os[target_os] = {"baseline_status": "missing_dynamic_record"}
            continue
        task_counts = {field: int(dynamic.get("task_counts", {}).get(field) or 0) for field in COUNT_FIELDS}
        for field, value in task_counts.items():
            totals[field] += value
        runner_cost = number_or_none(dynamic.get("runner_cost_usd"))
        if runner_cost is not None:
            cost += float(runner_cost)
            cost_seen = True
        for field in TOKEN_FIELDS:
            value = int_or_none(dynamic.get(field))
            if value is not None:
                token_totals[field] += value
                token_seen[field] = True
        for model in dynamic.get("runner_models") or []:
            if model:
                models.add(str(model))
        for path in dynamic.get("raw_paths") or []:
            if path:
                raw_paths.append(str(path))
        if dynamic.get("source_artifact"):
            source_artifacts.append(str(dynamic["source_artifact"]))
        outcome = str(dynamic.get("dynamic_outcome") or "")
        outcomes[target_os] = outcome
        by_os[target_os] = {
            "baseline_status": "joined",
            "dynamic_outcome": outcome,
            "success_rate": dynamic.get("success_rate"),
            "dominant_failure_category": dynamic.get("dominant_failure_category", ""),
            "exec_status": dynamic.get("exec_status", ""),
            "sample_status": dynamic.get("sample_status", ""),
            "task_counts": task_counts,
            "runner_cost_usd": runner_cost,
            "runner_input_tokens": int_or_none(dynamic.get("runner_input_tokens")),
            "runner_output_tokens": int_or_none(dynamic.get("runner_output_tokens")),
            "runner_models": sorted(str(model) for model in dynamic.get("runner_models") or []),
            "raw_paths": [str(path) for path in dynamic.get("raw_paths") or []],
            "source_artifact": dynamic.get("source_artifact", ""),
        }

    return {
        "by_os": by_os,
        "missing_dynamic_os": missing,
        "outcome_summary": "; ".join(f"{target_os}:{outcome}" for target_os, outcome in sorted(outcomes.items(), key=lambda item: os_key(item[0]))),
        "task_totals": totals,
        "runner_cost_usd": round(cost, 8) if cost_seen else None,
        "runner_input_tokens": token_totals["runner_input_tokens"] if token_seen["runner_input_tokens"] else None,
        "runner_output_tokens": token_totals["runner_output_tokens"] if token_seen["runner_output_tokens"] else None,
        "runner_models": sorted(models),
        "raw_paths": sorted(set(raw_paths)),
        "source_artifacts": sorted(set(source_artifacts)),
    }


def build_summary(manifest: list[dict[str, Any]], issues_csv: Path, dynamic_jsonl: Path) -> dict[str, Any]:
    open_rows = [row for row in manifest if row["open_skillfab_issue"]]
    hold_rows = [row for row in manifest if not row["open_skillfab_issue"]]
    affected_os_distribution = Counter(len(row["affected_os"]) for row in manifest)
    open_affected_distribution = Counter(len(row["affected_os"]) for row in open_rows)
    open_repair_distribution = Counter(len(row["repair_os"]) for row in open_rows)
    missing_dynamic = [
        {"issue_key": row["issue_key"], "skill_id": row["skill_id"], "missing_dynamic_os": row["pre_repair_baseline"]["missing_dynamic_os"]}
        for row in manifest
        if row["pre_repair_baseline"]["missing_dynamic_os"]
    ]
    return {
        "schema_version": "skillfab_repair_issue_manifest_summary_v1",
        "generated_at": DATE_STAMP,
        "unit": "one SkillFab issue per skill package",
        "source_issue_list": rel(issues_csv),
        "source_dynamic_labels": rel(dynamic_jsonl),
        "manifest_rows": len(manifest),
        "open_skillfab_issue_count": len(open_rows),
        "hold_only_skill_count": len(hold_rows),
        "affected_os_distribution": {str(key): value for key, value in sorted(affected_os_distribution.items())},
        "open_issue_affected_os_distribution": {str(key): value for key, value in sorted(open_affected_distribution.items())},
        "open_issue_repair_os_distribution": {str(key): value for key, value in sorted(open_repair_distribution.items())},
        "multi_os_open_issue_count": sum(1 for row in open_rows if len(row["affected_os"]) > 1),
        "multi_repair_os_issue_count": sum(1 for row in open_rows if len(row["repair_os"]) > 1),
        "baseline_missing_dynamic_record_count": len(missing_dynamic),
        "baseline_missing_dynamic_records": missing_dynamic,
        "tracking_files": {
            "events_jsonl": "docs/repair-workflow/skillfab-tracking/events.jsonl",
            "rounds_jsonl": "docs/repair-workflow/skillfab-tracking/rounds.jsonl",
            "published_artifacts_jsonl": "docs/repair-workflow/skillfab-tracking/published-artifacts.jsonl",
        },
        "rq3_notes": [
            "Use one manifest row as one repair unit. Do not split one skill into multiple SkillFab issues solely because multiple OS labels are affected.",
            "Round 1 may represent the development agent before review-agent feedback; later rounds can record review_agent_used=true.",
            "Record wall time, token usage, cost, review verdict, and before/after benchmark outcomes per round.",
            "Do not rerun task generation for repaired skills unless existing tasks are invalid.",
        ],
    }


def render_summary_md(summary: dict[str, Any]) -> str:
    lines = [
        "# SkillFab Repair Issue Manifest Summary (2026-06-27)",
        "",
        f"Unit: `{summary['unit']}`",
        f"Source issue list: `{summary['source_issue_list']}`",
        f"Source dynamic labels: `{summary['source_dynamic_labels']}`",
        "",
        "## Counts",
        "",
        "| Item | Count |",
        "|---|---:|",
        f"| Manifest rows | {summary['manifest_rows']} |",
        f"| Open SkillFab issues | {summary['open_skillfab_issue_count']} |",
        f"| Hold-only skills | {summary['hold_only_skill_count']} |",
        f"| Multi-OS open issues | {summary['multi_os_open_issue_count']} |",
        f"| Multi-repair-OS issues | {summary['multi_repair_os_issue_count']} |",
        f"| Missing dynamic baseline records | {summary['baseline_missing_dynamic_record_count']} |",
        "",
        "## Tracking Files",
        "",
    ]
    for label, path in summary["tracking_files"].items():
        lines.append(f"- `{label}`: `{path}`")
    lines.extend(["", "## RQ3 Notes", ""])
    lines.extend(f"- {item}" for item in summary["rq3_notes"])
    if summary["baseline_missing_dynamic_records"]:
        lines.extend(["", "## Missing Dynamic Baselines", "", "| Issue | Skill | Missing OS |", "|---|---|---|"])
        for row in summary["baseline_missing_dynamic_records"]:
            lines.append(f"| `{row['issue_key']}` | `{row['skill_id']}` | {', '.join(row['missing_dynamic_os'])} |")
    return "\n".join(lines) + "\n"


def split_os(value: str) -> list[str]:
    return sorted([item.strip() for item in str(value or "").split(",") if item.strip()], key=os_key)


def split_semicolon(value: str) -> list[str]:
    return sorted([item.strip() for item in str(value or "").split(";") if item.strip()])


def os_key(value: str) -> tuple[int, str]:
    return (OS_ORDER.get(value, 99), value)


def number_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def csv_row(row: dict[str, Any]) -> dict[str, Any]:
    baseline = row["pre_repair_baseline"]
    totals = baseline["task_totals"]
    tracking = row["skillfab_tracking"]
    return {
        "issue_key": row["issue_key"],
        "skill_id": row["skill_id"],
        "open_skillfab_issue": "yes" if row["open_skillfab_issue"] else "no",
        "affected_os": ", ".join(row["affected_os"]),
        "repair_os": ", ".join(row["repair_os"]),
        "non_repair_os": ", ".join(row["non_repair_os"]),
        "issue_title": row["issue_title"],
        "unified_fix_plan": row["unified_fix_plan"],
        "baseline_outcome_summary": baseline["outcome_summary"],
        "baseline_total_tasks": totals["total"],
        "baseline_pass": totals["pass"],
        "baseline_fail": totals["fail"],
        "baseline_blocked": totals["blocked"],
        "baseline_unsupported": totals["unsupported"],
        "baseline_unknown": totals["unknown"],
        "baseline_runner_cost_usd": "" if baseline["runner_cost_usd"] is None else baseline["runner_cost_usd"],
        "baseline_runner_input_tokens": "" if baseline["runner_input_tokens"] is None else baseline["runner_input_tokens"],
        "baseline_runner_output_tokens": "" if baseline["runner_output_tokens"] is None else baseline["runner_output_tokens"],
        "baseline_models": "; ".join(baseline["runner_models"]),
        "baseline_missing_os": ", ".join(baseline["missing_dynamic_os"]),
        "tracking_status": tracking["tracking_status"],
        "skillfab_issue_id": tracking["skillfab_issue_id"],
        "skillfab_submission_id": tracking["skillfab_submission_id"],
        "latest_round_index": tracking["current_round_index"],
        "latest_review_agent_used": "" if tracking["latest_review_agent_used"] is None else tracking["latest_review_agent_used"],
        "latest_review_verdict": tracking["latest_review_verdict"],
        "latest_repair_result": tracking["latest_repair_result"],
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(csv_row(row))


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def rel(path: Path | str) -> str:
    path = Path(path)
    try:
        return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        try:
            return path.resolve().relative_to(WORKSPACE_ROOT.resolve()).as_posix()
        except ValueError:
            return path.as_posix()


if __name__ == "__main__":
    raise SystemExit(main())
