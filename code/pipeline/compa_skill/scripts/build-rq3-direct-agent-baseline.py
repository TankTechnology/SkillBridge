#!/usr/bin/env python3
"""Build the RQ3 direct single-agent repair baseline handoff.

The script is data-only. It aligns a direct-agent repair baseline with the
same 175 target skill-OS cases used by RQ2/RQ3, but it does not run agents,
skills, installers, Docker, browsers, or package managers.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = REPO_ROOT.parent
DEFAULT_RQ3_RELEASE = REPO_ROOT / "data/releases/skillbridge-rq3-round-replay-20260630"
DEFAULT_ISSUE_MANIFEST = REPO_ROOT / "docs/repair-workflow/skillfab-issue-manifest-20260627.jsonl"
DEFAULT_SOURCE_ROOT = WORKSPACE_ROOT / "skillcomptest/.cache/skillcomptest/top5000-sources-v0.7.0"
DEFAULT_OUT_DIR = REPO_ROOT / "data/releases/skillbridge-rq3-direct-agent-baseline-20260701"
TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])sk-[A-Za-z0-9_-]{20,}")

JOB_FIELDS = [
    "schema_version",
    "baseline_job_id",
    "issue_key",
    "skill_id",
    "source",
    "slug",
    "target_os",
    "affected_os",
    "target_cell_count",
    "fixed_task_count",
    "source_package_status",
    "source_package_path",
    "issue_summary",
    "unified_fix_plan",
    "baseline_status",
]

CELL_FIELDS = [
    "schema_version",
    "baseline_job_id",
    "target_os",
    "issue_key",
    "skill_id",
    "source",
    "slug",
    "before_total",
    "before_pass",
    "after_total_skillfab",
    "after_pass_skillfab",
    "fixed_task_count",
    "task_ids",
]

EVAL_FIELDS = [
    "schema_version",
    "baseline_job_id",
    "target_os",
    "issue_key",
    "skill_id",
    "package_ref",
    "task_count",
    "task_ids",
    "expected_result_path",
    "status",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rq3-release", type=Path, default=DEFAULT_RQ3_RELEASE)
    parser.add_argument("--issue-manifest", type=Path, default=DEFAULT_ISSUE_MANIFEST)
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--force", action="store_true", help="replace generated files in out-dir")
    args = parser.parse_args()

    out_dir = args.out_dir
    data_dir = out_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    cells = read_csv(args.rq3_release / "data/rq3_target_cells.csv")
    tasks = read_csv(args.rq3_release / "data/rq3_task_manifest.csv")
    issue_manifest = {row["issue_key"]: row for row in read_jsonl(args.issue_manifest)}

    jobs = build_jobs(cells, tasks, issue_manifest, args.source_root)
    baseline_cells = build_baseline_cells(cells, tasks)
    eval_plan = build_eval_plan(baseline_cells)
    summary = build_summary(jobs, baseline_cells, tasks, args)
    support = build_support_matrix(summary, args)

    write_jsonl(data_dir / "direct_agent_repair_jobs.jsonl", jobs)
    write_csv(data_dir / "direct_agent_repair_jobs.csv", jobs, JOB_FIELDS)
    write_jsonl(data_dir / "direct_agent_target_cells.jsonl", baseline_cells)
    write_csv(data_dir / "direct_agent_target_cells.csv", baseline_cells, CELL_FIELDS)
    write_jsonl(data_dir / "direct_agent_task_manifest.jsonl", normalize_tasks(tasks))
    write_csv(data_dir / "direct_agent_task_manifest.csv", normalize_tasks(tasks), list(normalize_tasks(tasks)[0]))
    write_jsonl(data_dir / "direct_agent_eval_plan.jsonl", eval_plan)
    write_csv(data_dir / "direct_agent_eval_plan.csv", eval_plan, EVAL_FIELDS)
    write_json(out_dir / "summary.json", summary)
    write_json(out_dir / "direct_agent_support_matrix.json", support)
    write_json(out_dir / "release.json", build_release_json(summary, args))
    (out_dir / "DIRECT_AGENT_PROMPT_TEMPLATE.md").write_text(prompt_template(), encoding="utf-8")
    (out_dir / "FIELD_DICTIONARY.md").write_text(field_dictionary(), encoding="utf-8")
    (out_dir / "README.md").write_text(render_readme(summary), encoding="utf-8")

    print(json.dumps(summary["headline_counts"], ensure_ascii=False, sort_keys=True))
    return 0


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sanitize_for_output(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(sanitize_for_output(row), ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            clean = sanitize_for_output(row)
            writer.writerow({field: csv_value(clean.get(field)) for field in fields})


def csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def sanitize_for_output(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: sanitize_for_output(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_for_output(item) for item in value]
    if isinstance(value, str):
        return TOKEN_RE.sub("[REDACTED_SK_TOKEN]", value)
    return value


def parse_json_list(value: str | list[str] | None) -> list[str]:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        loaded = json.loads(value)
        if isinstance(loaded, list):
            return [str(item) for item in loaded]
    except json.JSONDecodeError:
        pass
    return [item.strip() for item in str(value).split(",") if item.strip()]


def split_skill_id(skill_id: str) -> tuple[str, str]:
    if "/" not in skill_id:
        return "", skill_id
    source, slug = skill_id.split("/", 1)
    return source, slug


def source_status(source_root: Path, skill_id: str) -> tuple[str, str]:
    package = source_root / skill_id
    if not package.exists():
        return "missing_source_package", rel(package)
    if not (package / "SKILL.md").exists():
        return "missing_skill_md", rel(package)
    return "ready", rel(package)


def rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        try:
            return path.resolve().relative_to(WORKSPACE_ROOT).as_posix()
        except ValueError:
            return path.as_posix()


def tasks_by_cell(tasks: list[dict[str, str]]) -> dict[tuple[str, str], list[str]]:
    out: dict[tuple[str, str], list[str]] = defaultdict(list)
    for row in tasks:
        out[(row["target_os"], row["issue_key"])].append(row["task_id"])
    return {key: sorted(values) for key, values in out.items()}


def normalize_tasks(tasks: list[dict[str, str]]) -> list[dict[str, Any]]:
    return [
        {
            "schema_version": "rq3_direct_agent_target_task_v1",
            "target_os": row["target_os"],
            "issue_key": row["issue_key"],
            "skill_id": row["skill_id"],
            "task_id": row["task_id"],
            "task_universe": row["task_universe"],
            "task_origin_contract": row["task_origin_contract"],
            "task_manifest_source": row["task_manifest_source"],
            "task_id_status": row["task_id_status"],
        }
        for row in tasks
    ]


def build_jobs(
    cells: list[dict[str, str]],
    tasks: list[dict[str, str]],
    issue_manifest: dict[str, dict[str, Any]],
    source_root: Path,
) -> list[dict[str, Any]]:
    by_issue: dict[str, list[dict[str, str]]] = defaultdict(list)
    for cell in cells:
        by_issue[cell["issue_key"]].append(cell)
    task_map = tasks_by_cell(tasks)
    rows: list[dict[str, Any]] = []
    for issue_key in sorted(by_issue):
        issue_cells = sorted(by_issue[issue_key], key=lambda row: row["target_os"])
        first = issue_cells[0]
        manifest = issue_manifest.get(issue_key, {})
        source, slug = split_skill_id(first["skill_id"])
        status, source_path = source_status(source_root, first["skill_id"])
        target_os = [cell["target_os"] for cell in issue_cells]
        fixed_task_count_by_os = {
            cell["target_os"]: len(task_map.get((cell["target_os"], issue_key), []))
            for cell in issue_cells
        }
        before_by_os = {
            cell["target_os"]: {
                "pass": int(cell["before_pass"]),
                "total": int(cell["before_total"]),
                "fail": int(cell["before_fail"]),
                "blocked": int(cell["before_blocked"]),
                "unsupported": int(cell["before_unsupported"]),
            }
            for cell in issue_cells
        }
        skillfab_after_by_os = {
            cell["target_os"]: {
                "pass": int(cell["after_pass"]),
                "total": int(cell["after_total"]),
                "fail": int(cell["after_fail"]),
                "blocked": int(cell["after_blocked"]),
                "unsupported": int(cell["after_unsupported"]),
            }
            for cell in issue_cells
        }
        rows.append(
            {
                "schema_version": "rq3_direct_agent_repair_job_v1",
                "baseline_job_id": f"DA-{issue_key}",
                "issue_key": issue_key,
                "skill_id": first["skill_id"],
                "source": source or first.get("source", ""),
                "slug": slug or first.get("slug", ""),
                "target_os": target_os,
                "affected_os": parse_json_list(first.get("affected_os")) or manifest.get("affected_os", []),
                "target_cell_count": len(issue_cells),
                "fixed_task_count": sum(fixed_task_count_by_os.values()),
                "fixed_task_count_by_os": fixed_task_count_by_os,
                "source_package_status": status,
                "source_package_path": source_path,
                "issue_summary": manifest.get("issue_summary", ""),
                "unified_fix_plan": manifest.get("unified_fix_plan", ""),
                "pre_repair_baseline": manifest.get("pre_repair_baseline") or {"by_os": before_by_os},
                "rq2_before_by_os": before_by_os,
                "skillfab_final_after_by_os": skillfab_after_by_os,
                "baseline_status": "not_started",
                "baseline_output_package_ref": "",
                "baseline_result_ref": "",
            }
        )
    return rows


def build_baseline_cells(cells: list[dict[str, str]], tasks: list[dict[str, str]]) -> list[dict[str, Any]]:
    task_map = tasks_by_cell(tasks)
    out: list[dict[str, Any]] = []
    for cell in sorted(cells, key=lambda row: (row["target_os"], row["issue_key"])):
        task_ids = task_map.get((cell["target_os"], cell["issue_key"]), [])
        out.append(
            {
                "schema_version": "rq3_direct_agent_target_cell_v1",
                "baseline_job_id": f"DA-{cell['issue_key']}",
                "target_os": cell["target_os"],
                "issue_key": cell["issue_key"],
                "skill_id": cell["skill_id"],
                "source": cell["source"],
                "slug": cell["slug"],
                "before_total": int(cell["before_total"]),
                "before_pass": int(cell["before_pass"]),
                "after_total_skillfab": int(cell["after_total"]),
                "after_pass_skillfab": int(cell["after_pass"]),
                "fixed_task_count": len(task_ids),
                "task_ids": task_ids,
                "current_rq2_source": cell["current_rq2_source"],
            }
        )
    return out


def build_eval_plan(cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "schema_version": "rq3_direct_agent_eval_plan_v1",
            "baseline_job_id": cell["baseline_job_id"],
            "target_os": cell["target_os"],
            "issue_key": cell["issue_key"],
            "skill_id": cell["skill_id"],
            "package_ref": "pending:direct_agent_output_package",
            "task_count": cell["fixed_task_count"],
            "task_ids": cell["task_ids"],
            "expected_result_path": f"results/{cell['target_os']}/{cell['issue_key']}.json",
            "status": "pending_direct_agent_repair",
        }
        for cell in cells
    ]


def build_summary(
    jobs: list[dict[str, Any]],
    cells: list[dict[str, Any]],
    tasks: list[dict[str, str]],
    args: argparse.Namespace,
) -> dict[str, Any]:
    cells_by_os = Counter(cell["target_os"] for cell in cells)
    tasks_by_os = Counter(task["target_os"] for task in tasks)
    jobs_by_target_os_count = Counter(len(job["target_os"]) for job in jobs)
    source_status_counts = Counter(job["source_package_status"] for job in jobs)
    return {
        "schema_version": "rq3_direct_agent_baseline_summary_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "headline_counts": {
            "repair_jobs": len(jobs),
            "target_skill_os_cases": len(cells),
            "fixed_replay_tasks": len(tasks),
            "target_cases_by_os": dict(sorted(cells_by_os.items())),
            "fixed_tasks_by_os": dict(sorted(tasks_by_os.items())),
        },
        "unit_contract": {
            "repair_job_unit": "one direct single-agent repair attempt per issue_key / skill package",
            "evaluation_unit": "one RQ2-aligned <target_os, issue_key, skill_id> target skill-OS case",
            "task_unit": "one reused benchmark replay task; tasks must not be regenerated",
        },
        "baseline_contract": {
            "variant": "direct_single_agent_no_skillfab",
            "agent_roles": ["one developer agent"],
            "excluded_components": ["SkillFab issue workflow", "SkillFab submission workflow", "ReviewAgent feedback loop"],
            "allowed_validation_during_repair": ["static package inspection", "frontmatter checks", "syntax checks", "git diff --check"],
            "forbidden_during_repair": ["running skill behavior", "running generated benchmark tasks", "package manager installs", "Docker", "browser or GUI automation", "network-heavy workflows"],
        },
        "source_files": {
            "rq3_target_cells": rel(args.rq3_release / "data/rq3_target_cells.csv"),
            "rq3_task_manifest": rel(args.rq3_release / "data/rq3_task_manifest.csv"),
            "issue_manifest": rel(args.issue_manifest),
            "source_root": rel(args.source_root),
        },
        "jobs_by_target_os_count": dict(sorted(jobs_by_target_os_count.items())),
        "source_package_status_counts": dict(sorted(source_status_counts.items())),
    }


def build_support_matrix(summary: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    return {
        "schema_version": "rq3_direct_agent_baseline_support_matrix_v1",
        "claims": {
            "same_denominator_as_rq2": {
                "status": "support",
                "evidence": "direct_agent_target_cells has 175 rows with the same OS split as rq3_target_cells",
                "counts": summary["headline_counts"],
            },
            "direct_agent_baseline_is_component_ablation": {
                "status": "support",
                "evidence": "repair jobs exclude SkillFab issue/submission state and ReviewAgent feedback",
                "baseline_contract": summary["baseline_contract"],
            },
            "repair_execution_not_yet_run": {
                "status": "caution",
                "evidence": "baseline_status is initialized to not_started; dynamic replay requires a later execution result release",
            },
        },
        "forbidden_overclaims": [
            "Do not compare direct-agent effectiveness until direct repaired packages have been replayed on the fixed tasks.",
            "Do not count repair jobs as target skill-OS cases; jobs are package-level, cases are OS-level.",
            "Do not claim no-regression certification from static repair output alone.",
        ],
        "source_release": rel(args.rq3_release),
    }


def build_release_json(summary: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    return {
        "schema_version": "compa_skill_release_inventory_v1",
        "tag": args.out_dir.name,
        "name": "SkillBridge RQ3 Direct Single-Agent Baseline Manifest",
        "is_prerelease": True,
        "purpose": "Direct single-agent no-SkillFab repair baseline manifest for RQ3 ablation",
        "denominator": summary["headline_counts"],
        "entrypoints": {
            "handoff_doc": "README.md",
            "repair_jobs": "data/direct_agent_repair_jobs.jsonl",
            "target_cells": "data/direct_agent_target_cells.jsonl",
            "task_manifest": "data/direct_agent_task_manifest.jsonl",
            "eval_plan": "data/direct_agent_eval_plan.jsonl",
            "prompt_template": "DIRECT_AGENT_PROMPT_TEMPLATE.md",
        },
    }


def prompt_template() -> str:
    return """# Direct Single-Agent OS Repair Prompt Template

You are the only repair agent for one Compa-Skill OS-compatibility baseline job.
Do not use SkillFab, do not create issues or submissions, and do not request a
review. Work only on the local package copy provided by the runner.

Inputs supplied by the runner:

- `job.json`: issue key, skill id, target OSes, fixed task counts, OS evidence,
  issue summary, and unified fix plan.
- `package/`: mutable copy of the original skill package.

Hard boundaries:

- Preserve the original skill identity and user-facing semantics.
- Make the smallest contract-level OS compatibility repair.
- Do not run the skill, generated benchmark tasks, installers, Docker, browsers,
  GUI automation, or network-heavy workflows.
- Static validation is allowed: inspect files, check SKILL.md frontmatter,
  `bash -n`, `python3 -m py_compile`, `node --check`, and `git diff --check`.
- Do not hard-code benchmark answers, delete core functionality, or claim OS
  support that the package does not implement.

Final response must be one JSON object with:

```json
{
  "issue_key": "<issue key>",
  "skill_id": "<skill id>",
  "agent_blocked": false,
  "blocked_reason": "",
  "changed_files": [],
  "static_validation": {
    "git_diff_check": "pass|fail|not_run",
    "syntax_checks": "pass|fail|not_run",
    "package_validation": "pass|fail|not_run"
  },
  "summary": "<brief repair summary>"
}
```
"""


def field_dictionary() -> str:
    return """# RQ3 Direct Single-Agent Baseline Field Dictionary

## `data/direct_agent_repair_jobs.*`

- `baseline_job_id`: stable direct-agent repair job id. One job repairs one
  skill package / issue key.
- `target_os`: OSes whose RQ2 target cells are covered by this repair job.
- `fixed_task_count`: number of reused replay tasks across those target OSes.
- `baseline_status`: lifecycle status for this baseline; initialized to
  `not_started`.

## `data/direct_agent_target_cells.*`

- One row per RQ2-aligned target skill-OS case.
- `before_*`: original package replay result.
- `after_*_skillfab`: final SkillFab repaired-package replay result, included
  only as the RQ2 comparison reference.
- `task_ids`: fixed replay task ids to reuse after direct-agent repair.

## `data/direct_agent_eval_plan.*`

- One row per target skill-OS case to replay after the direct agent produces a
  repaired package.
- `package_ref` is pending until the direct-agent runner writes a package.
"""


def render_readme(summary: dict[str, Any]) -> str:
    counts = summary["headline_counts"]
    return f"""# SkillBridge RQ3 Direct Single-Agent Baseline

This directory defines the no-SkillFab, single-agent repair baseline for RQ3.
It is aligned to the same RQ2/RQ3 denominator:

| Unit | Count |
|---|---:|
| Direct repair jobs | {counts['repair_jobs']} |
| Target skill-OS cases | {counts['target_skill_os_cases']} |
| Fixed replay tasks | {counts['fixed_replay_tasks']} |

OS split for target cases:

| OS | Cases | Tasks |
|---|---:|---:|
| Linux | {counts['target_cases_by_os'].get('linux', 0)} | {counts['fixed_tasks_by_os'].get('linux', 0)} |
| macOS | {counts['target_cases_by_os'].get('macos', 0)} | {counts['fixed_tasks_by_os'].get('macos', 0)} |
| Windows | {counts['target_cases_by_os'].get('windows', 0)} | {counts['fixed_tasks_by_os'].get('windows', 0)} |

## Baseline Definition

The direct baseline gives one developer agent the original package and the same
OS failure evidence, but removes SkillFab issue/submission structure and removes
ReviewAgent feedback. The repair unit is a skill package; the evaluation unit is
the RQ2 target skill-OS case.

## Entry Points

- `data/direct_agent_repair_jobs.jsonl`: package-level repair jobs.
- `data/direct_agent_target_cells.jsonl`: 175 target skill-OS cases.
- `data/direct_agent_task_manifest.jsonl`: fixed replay tasks; do not
  regenerate tasks.
- `data/direct_agent_eval_plan.jsonl`: post-repair replay plan.
- `DIRECT_AGENT_PROMPT_TEMPLATE.md`: prompt contract used by the runner.

## Safety

This manifest builder did not run any skill code. During repair, the direct
agent may perform static checks only. Dynamic execution is a separate reused-task
evaluation stage after repaired packages are produced.
"""


if __name__ == "__main__":
    raise SystemExit(main())
