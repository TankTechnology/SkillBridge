#!/usr/bin/env python3
"""Build the current SkillFab OS-compatibility repair target list.

This script is data-only. It reads the repair manifest, local source package
inventory, and SkillFab SQLite state. It never executes skill code, setup
scripts, tests, package managers, browsers, or GUI commands.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = REPO_ROOT.parent
DEFAULT_MANIFEST = REPO_ROOT / "docs/repair-workflow/skillfab-issue-manifest-20260627.jsonl"
DEFAULT_SKILLFAB_DB = WORKSPACE_ROOT / "SkillFab/data/skillfab.db"
DEFAULT_SOURCE_ROOT = WORKSPACE_ROOT / "skillcomptest/.cache/skillcomptest/top5000-sources-v0.7.0"
DEFAULT_OUT_PREFIX = REPO_ROOT / "docs/repair-workflow/skillfab-repair-targets-20260628"
TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])sk-[A-Za-z0-9_-]{20,}")


CSV_FIELDS = [
    "issue_key",
    "skill_id",
    "source",
    "slug",
    "affected_os",
    "repair_os",
    "target_status",
    "next_action",
    "skillfab_issue_id",
    "skillfab_issue_status",
    "skillfab_submission_id",
    "submission_status",
    "latest_review_result",
    "latest_review_at",
    "latest_commit_sha",
    "duplicate_skillfab_issue_ids",
    "selected_duplicate_policy",
    "source_package_status",
    "source_package_path",
    "issue_title",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--skillfab-db", type=Path, default=DEFAULT_SKILLFAB_DB)
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--out-prefix", type=Path, default=DEFAULT_OUT_PREFIX)
    args = parser.parse_args()

    manifest_rows = [row for row in read_jsonl(args.manifest) if row.get("open_skillfab_issue") is True]
    db_by_key = load_skillfab_state(args.skillfab_db)
    targets = [build_target(row, db_by_key.get(row["issue_key"]), args.source_root) for row in manifest_rows]
    summary = build_summary(targets, args)

    write_jsonl(args.out_prefix.with_suffix(".jsonl"), targets)
    write_csv(args.out_prefix.with_suffix(".csv"), targets)
    write_json(args.out_prefix.with_name(args.out_prefix.name + "-summary.json"), summary)
    args.out_prefix.with_suffix(".md").write_text(render_markdown(summary, targets), encoding="utf-8")
    print(json.dumps({"targets": len(targets), "status_counts": summary["target_status_counts"]}, ensure_ascii=False, sort_keys=True))
    return 0


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(sanitize_for_output(row), ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(sanitize_for_output(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            clean_row = sanitize_for_output(row)
            writer.writerow({field: csv_value(clean_row.get(field)) for field in CSV_FIELDS})


def sanitize_for_output(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: sanitize_for_output(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_for_output(item) for item in value]
    if isinstance(value, str):
        return TOKEN_RE.sub("[REDACTED_SK_TOKEN]", value)
    return value


def csv_value(value: Any) -> str:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if value is None:
        return ""
    return str(value)


def load_skillfab_state(db_path: Path) -> dict[str, dict[str, Any]]:
    if not db_path.exists():
        return {}
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    issues_by_key: dict[str, list[dict[str, Any]]] = {}
    for issue in conn.execute(
        "select id, title, status, created_at, updated_at from issues where title like '[AUTO-REPAIR]%'"
    ):
        issue_key = extract_issue_key(issue["title"])
        if not issue_key:
            continue
        submissions = []
        for submission in conn.execute(
            "select id, status, commit_sha, created_at, updated_at from submissions where issue_id=? order by id",
            (issue["id"],),
        ):
            latest_review = conn.execute(
                """
                select id, review_result, commit_sha, created_at, comment
                from review_comments
                where submission_id=?
                order by id desc
                limit 1
                """,
                (submission["id"],),
            ).fetchone()
            submissions.append(
                {
                    "submission_id": submission["id"],
                    "submission_status": submission["status"],
                    "commit_sha": submission["commit_sha"] or "",
                    "created_at": submission["created_at"],
                    "updated_at": submission["updated_at"],
                    "latest_review_id": latest_review["id"] if latest_review else None,
                    "latest_review_result": latest_review["review_result"] if latest_review else "",
                    "latest_review_commit_sha": latest_review["commit_sha"] if latest_review else "",
                    "latest_review_at": latest_review["created_at"] if latest_review else "",
                    "latest_review_excerpt": (latest_review["comment"] or "")[:300] if latest_review else "",
                }
            )
        issues_by_key.setdefault(issue_key, []).append(
            {
            "skillfab_issue_id": issue["id"],
            "skillfab_issue_status": issue["status"],
            "skillfab_issue_title": issue["title"],
            "skillfab_issue_created_at": issue["created_at"],
            "skillfab_issue_updated_at": issue["updated_at"],
            "submissions": submissions,
            }
        )
    return {issue_key: select_current_issue_state(states) for issue_key, states in issues_by_key.items()}


def select_current_issue_state(states: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick the authoritative state when reruns created duplicate AUTO-REPAIR issues."""
    if not states:
        raise ValueError("states must not be empty")
    selected = dict(sorted(states, key=issue_state_rank)[-1])
    duplicate_issue_ids = sorted(int(state["skillfab_issue_id"]) for state in states)
    if len(states) > 1:
        selected["duplicate_skillfab_issue_ids"] = duplicate_issue_ids
        selected["selected_duplicate_policy"] = "prefer_most_complete_then_newest"
    else:
        selected["duplicate_skillfab_issue_ids"] = []
        selected["selected_duplicate_policy"] = ""
    return selected


def issue_state_rank(state: dict[str, Any]) -> tuple[int, str, int]:
    latest_submission = latest(state.get("submissions", []))
    submission_status = latest_submission.get("submission_status", "") if latest_submission else ""
    latest_review_result = latest_submission.get("latest_review_result", "") if latest_submission else ""
    issue_status = state.get("skillfab_issue_status", "")
    return (
        lifecycle_rank(issue_status, submission_status, latest_review_result),
        normalized_timestamp(state.get("skillfab_issue_updated_at", "")),
        int(state.get("skillfab_issue_id") or 0),
    )


def lifecycle_rank(issue_status: str, submission_status: str, review_result: str) -> int:
    if issue_status == "completed" or submission_status == "published":
        return 5
    if review_result == "approved" or submission_status == "approved":
        return 4
    if review_result in {"needs_work", "changes_requested"} or submission_status == "needs_work":
        return 3
    if submission_status == "submitted":
        return 2
    if issue_status:
        return 1
    return 0


def normalized_timestamp(value: str) -> str:
    if not value:
        return ""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(value, fmt).isoformat()
        except ValueError:
            continue
    return value


def extract_issue_key(title: str) -> str | None:
    match = re.search(r"\[(SFOS-\d+)\]", title or "")
    return match.group(1) if match else None


def build_target(row: dict[str, Any], db_state: dict[str, Any] | None, source_root: Path) -> dict[str, Any]:
    skill_id = row["skill_id"]
    source, slug = split_skill_id(skill_id)
    source_path = source_root / skill_id
    source_package_status = source_status(source_path)
    latest_submission = latest(db_state.get("submissions", []) if db_state else [])
    latest_review_result = latest_submission.get("latest_review_result", "") if latest_submission else ""
    submission_status = latest_submission.get("submission_status", "") if latest_submission else ""
    issue_status = db_state.get("skillfab_issue_status", "") if db_state else ""
    target_status = classify_target(issue_status, submission_status, latest_review_result, source_package_status)
    return {
        "schema_version": "skillfab_repair_target_v1",
        "issue_key": row["issue_key"],
        "skill_id": skill_id,
        "source": source,
        "slug": slug,
        "affected_os": row.get("affected_os") or [],
        "repair_os": row.get("repair_os") or [],
        "non_repair_os": row.get("non_repair_os") or [],
        "issue_title": row.get("issue_title", ""),
        "issue_summary": row.get("issue_summary", ""),
        "unified_fix_plan": row.get("unified_fix_plan", ""),
        "pre_repair_baseline": row.get("pre_repair_baseline") or {},
        "target_status": target_status,
        "next_action": next_action(target_status),
        "skillfab_issue_id": db_state.get("skillfab_issue_id") if db_state else None,
        "skillfab_issue_status": issue_status,
        "skillfab_submission_id": latest_submission.get("submission_id") if latest_submission else None,
        "submission_status": submission_status,
        "latest_review_result": latest_review_result,
        "latest_review_at": latest_submission.get("latest_review_at", "") if latest_submission else "",
        "latest_review_excerpt": latest_submission.get("latest_review_excerpt", "") if latest_submission else "",
        "latest_commit_sha": latest_submission.get("commit_sha", "") if latest_submission else "",
        "duplicate_skillfab_issue_ids": db_state.get("duplicate_skillfab_issue_ids", []) if db_state else [],
        "selected_duplicate_policy": db_state.get("selected_duplicate_policy", "") if db_state else "",
        "source_package_status": source_package_status,
        "source_package_path": rel(source_path) if source_path.exists() else "",
        "source_package_has_skill_md": (source_path / "SKILL.md").exists(),
        "source_package_file_count": count_files(source_path) if source_path.exists() else 0,
    }


def split_skill_id(skill_id: str) -> tuple[str, str]:
    if "/" not in skill_id:
        return "", skill_id
    return tuple(skill_id.split("/", 1))  # type: ignore[return-value]


def source_status(path: Path) -> str:
    if not path.exists():
        return "missing_source_package"
    if not (path / "SKILL.md").exists():
        return "missing_skill_md"
    return "ready"


def count_files(path: Path) -> int:
    return sum(1 for item in path.rglob("*") if item.is_file())


def latest(submissions: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not submissions:
        return None
    return sorted(submissions, key=lambda item: int(item["submission_id"]))[-1]


def classify_target(issue_status: str, submission_status: str, review_result: str, source_package_status: str) -> str:
    if issue_status == "completed" or submission_status == "published":
        return "completed"
    if review_result == "approved" or submission_status == "approved":
        return "review_approved_open"
    if review_result in {"needs_work", "changes_requested"} or submission_status == "needs_work":
        return "needs_work"
    if submission_status == "submitted":
        return "submitted_pending_review"
    if source_package_status != "ready":
        return f"not_started_{source_package_status}"
    if issue_status:
        return "issue_open_no_submission"
    return "not_started_ready"


def next_action(target_status: str) -> str:
    return {
        "completed": "none",
        "review_approved_open": "close_or_publish_in_skillfab",
        "needs_work": "run_next_repair_round",
        "submitted_pending_review": "wait_or_run_review_agent",
        "issue_open_no_submission": "resume_dev_agent_submission",
        "not_started_ready": "run_first_repair_round",
        "not_started_missing_skill_md": "skip_or_reacquire_source_package",
        "not_started_missing_source_package": "skip_or_reacquire_source_package",
    }.get(target_status, "manual_triage")


def build_summary(targets: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, Any]:
    status_counts = Counter(row["target_status"] for row in targets)
    next_action_counts = Counter(row["next_action"] for row in targets)
    source_counts = Counter(row["source"] for row in targets)
    repair_os_counts = Counter(os_name for row in targets for os_name in row["repair_os"])
    actionable_remaining = [
        row for row in targets
        if row["next_action"] in {"run_first_repair_round", "run_next_repair_round", "resume_dev_agent_submission"}
    ]
    blocked_source = [row for row in targets if row["next_action"] == "skip_or_reacquire_source_package"]
    duplicate_issues = [row for row in targets if row["duplicate_skillfab_issue_ids"]]
    return {
        "schema_version": "skillfab_repair_target_summary_v1",
        "generated_at": "2026-06-28",
        "sample_universe": "manifest rows with open_skillfab_issue=true",
        "unit": "one SkillFab issue per skill package",
        "source_manifest": rel(args.manifest),
        "source_skillfab_db": rel(args.skillfab_db),
        "source_package_root": rel(args.source_root),
        "target_count": len(targets),
        "target_status_counts": dict(sorted(status_counts.items())),
        "next_action_counts": dict(sorted(next_action_counts.items())),
        "source_counts": dict(sorted(source_counts.items())),
        "repair_os_counts": dict(sorted(repair_os_counts.items())),
        "actionable_remaining_count": len(actionable_remaining),
        "blocked_source_count": len(blocked_source),
        "duplicate_skillfab_issue_count": len(duplicate_issues),
        "done_or_approved_count": status_counts["completed"] + status_counts["review_approved_open"],
        "needs_work_issue_keys": [row["issue_key"] for row in targets if row["target_status"] == "needs_work"],
        "review_approved_open_issue_keys": [row["issue_key"] for row in targets if row["target_status"] == "review_approved_open"],
        "blocked_source_issue_keys": [row["issue_key"] for row in blocked_source],
        "duplicate_skillfab_issues": [
            {
                "issue_key": row["issue_key"],
                "skill_id": row["skill_id"],
                "duplicate_skillfab_issue_ids": row["duplicate_skillfab_issue_ids"],
                "selected_skillfab_issue_id": row["skillfab_issue_id"],
                "selected_status": row["target_status"],
            }
            for row in duplicate_issues
        ],
        "recommended_next_ready_issue_keys": [
            row["issue_key"] for row in targets
            if row["target_status"] == "not_started_ready"
        ][:50],
        "caveats": [
            "SkillFab DB is the lifecycle source of truth; tracking JSONL can contain historical failed attempts and reruns.",
            "Duplicate AUTO-REPAIR issues are not deleted; this target list selects the most complete and newest issue state.",
            "post_repair dynamic re-evaluation is intentionally not recorded yet.",
            "review-agent token and cost fields are not reliable for Kimi Code and may remain null.",
        ],
    }


def render_markdown(summary: dict[str, Any], targets: list[dict[str, Any]]) -> str:
    lines = [
        "# SkillFab OS Repair Targets",
        "",
        f"Generated: {summary['generated_at']}",
        "",
        "## Scope",
        "",
        f"- Sample universe: {summary['sample_universe']}",
        f"- Unit: {summary['unit']}",
        f"- Target count: {summary['target_count']}",
        f"- Lifecycle source of truth: `{summary['source_skillfab_db']}`",
        f"- Manifest source: `{summary['source_manifest']}`",
        "",
        "## Current Status",
        "",
        "| status | count | meaning |",
        "|---|---:|---|",
    ]
    meanings = {
        "completed": "SkillFab issue completed or latest submission published.",
        "review_approved_open": "Latest review approved, but SkillFab issue still open.",
        "needs_work": "Latest review requested changes; run another repair round.",
        "submitted_pending_review": "Submission is waiting for review.",
        "issue_open_no_submission": "SkillFab issue exists but no submission is present.",
        "not_started_ready": "Not started in SkillFab and local source package has SKILL.md.",
        "not_started_missing_skill_md": "Local source package exists but SKILL.md is missing.",
        "not_started_missing_source_package": "Local source package is missing.",
    }
    for status, count in summary["target_status_counts"].items():
        lines.append(f"| `{status}` | {count} | {meanings.get(status, '')} |")
    lines.extend([
        "",
        "## Next Actions",
        "",
        "| next_action | count |",
        "|---|---:|",
    ])
    for action, count in summary["next_action_counts"].items():
        lines.append(f"| `{action}` | {count} |")
    lines.extend([
        "",
        "## Needs Work Now",
        "",
        "| issue_key | skill_id | repair_os | submission | latest_review |",
        "|---|---|---|---:|---|",
    ])
    for row in [r for r in targets if r["target_status"] == "needs_work"]:
        lines.append(
            f"| `{row['issue_key']}` | `{row['skill_id']}` | {', '.join(row['repair_os'])} | "
            f"{row.get('skillfab_submission_id') or ''} | `{row.get('latest_review_result') or ''}` |"
        )
    lines.extend([
        "",
        "## Approved But Still Open",
        "",
        "| issue_key | skill_id | repair_os | submission | next_action |",
        "|---|---|---|---:|---|",
    ])
    for row in [r for r in targets if r["target_status"] == "review_approved_open"]:
        lines.append(
            f"| `{row['issue_key']}` | `{row['skill_id']}` | {', '.join(row['repair_os'])} | "
            f"{row.get('skillfab_submission_id') or ''} | `{row['next_action']}` |"
        )
    if summary["duplicate_skillfab_issues"]:
        lines.extend([
            "",
            "## Duplicate SkillFab Issues",
            "",
            "Duplicate AUTO-REPAIR issues are retained in SkillFab history. The target list uses the selected issue below.",
            "",
            "| issue_key | skill_id | issue_ids | selected_issue | selected_status |",
            "|---|---|---|---:|---|",
        ])
        for row in summary["duplicate_skillfab_issues"]:
            issue_ids = ", ".join(str(issue_id) for issue_id in row["duplicate_skillfab_issue_ids"])
            lines.append(
                f"| `{row['issue_key']}` | `{row['skill_id']}` | {issue_ids} | "
                f"{row['selected_skillfab_issue_id']} | `{row['selected_status']}` |"
            )
    lines.extend([
        "",
        "## Actionable Repair Queue",
        "",
        "| issue_key | skill_id | repair_os | target_status | next_action |",
        "|---|---|---|---|---|",
    ])
    actionable_actions = {"run_first_repair_round", "run_next_repair_round", "resume_dev_agent_submission"}
    for row in [r for r in targets if r["next_action"] in actionable_actions]:
        lines.append(
            f"| `{row['issue_key']}` | `{row['skill_id']}` | {', '.join(row['repair_os'])} | "
            f"`{row['target_status']}` | `{row['next_action']}` |"
        )
    lines.extend([
        "",
        "## Missing Source Packages",
        "",
        "| issue_key | skill_id | repair_os | source_status | next_action |",
        "|---|---|---|---|---|",
    ])
    for row in [r for r in targets if r["next_action"] == "skip_or_reacquire_source_package"]:
        lines.append(
            f"| `{row['issue_key']}` | `{row['skill_id']}` | {', '.join(row['repair_os'])} | "
            f"`{row['source_package_status']}` | `{row['next_action']}` |"
        )
    lines.extend([
        "",
        "## Recommended Next Batch",
        "",
        "These are the first ready, not-started issue keys with local `SKILL.md` available.",
        "",
        "```text",
        " ".join(summary["recommended_next_ready_issue_keys"][:30]),
        "```",
        "",
        "## Data Caveats",
        "",
    ])
    for caveat in summary["caveats"]:
        lines.append(f"- {caveat}")
    return "\n".join(lines) + "\n"


def rel(path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(REPO_ROOT))
    except ValueError:
        try:
            return str(resolved.relative_to(WORKSPACE_ROOT))
        except ValueError:
            return str(path)


if __name__ == "__main__":
    raise SystemExit(main())
