#!/usr/bin/env python3
"""Build a clean SkillFab repair workflow dataset from reviewed OS labels.

This script is read-only with respect to skill packages and SkillFab worktrees.
It does not execute skill code, generated tasks, package managers, setup
scripts, browsers, or GUI commands.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import subprocess
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = REPO_ROOT.parent
DEFAULT_TEST_REPO = WORKSPACE_ROOT / "skillcomptest"
DEFAULT_INVENTORY = (
    DEFAULT_TEST_REPO
    / "docs/cross-os-testing/analysis/current/three-os-clean-os-issue-inventory-20260626-v2.jsonl"
)
DEFAULT_SKILLFAB_WORK = WORKSPACE_ROOT / ".skillfab-work"
DEFAULT_OUT_DIR = REPO_ROOT / "docs/repair-workflow"
DATE_STAMP = "20260626"

PACKAGE_CATEGORY = "package_entrypoint_contract"
SMALL_REPAIR_FIXABILITIES = {
    "install_contract_fix",
    "metadata_or_target_os_contract_fix",
    "package_entrypoint_fix",
    "runtime_contract_fix",
    "script_portability_fix",
    "small_script_or_path_fix",
    "small_skill_guide_fix",
    "small_skill_script_fix",
}
ADAPTER_REPAIR_FIXABILITIES = {
    "platform_adapter_needed",
    "session_or_permission_adapter_fix",
}
BOUNDARY_FIXABILITIES = {
    "not_functionally_fixable_for_macos",
    "platform_adapter_or_not_portable_by_design",
    "requires_environment_provisioning",
}

FIELDNAMES = [
    "skill_id",
    "target_os",
    "slug_family",
    "paper_category",
    "fixability_class",
    "repair_scope",
    "repair_priority",
    "dynamic_outcome",
    "success_rate",
    "short_issue",
    "repair_implication",
    "local_skillfab_artifact_count",
    "local_skillfab_artifacts",
    "source_file",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--skillfab-work", type=Path, default=DEFAULT_SKILLFAB_WORK)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument(
        "--batch-list",
        type=Path,
        help="Optional newline-delimited skill_id, source/slug, or slug list for a new repair batch.",
    )
    parser.add_argument(
        "--exclude-list",
        type=Path,
        help="Optional newline-delimited skill_id, source/slug, or slug list to remove from the active queue.",
    )
    args = parser.parse_args()

    issue_rows = read_jsonl(args.inventory)
    source_issue_label_count = len(issue_rows)
    batch_filter = read_batch_filter(args.batch_list) if args.batch_list else None
    if batch_filter:
        issue_rows = [row for row in issue_rows if row_matches_batch_filter(row, batch_filter)]
    exclusion_filter = read_batch_filter(args.exclude_list) if args.exclude_list else None
    rows_before_exclusion = len(issue_rows)
    if exclusion_filter:
        issue_rows = [row for row in issue_rows if not row_matches_batch_filter(row, exclusion_filter)]
    excluded_issue_labels = rows_before_exclusion - len(issue_rows)
    artifacts = scan_skillfab_work(args.skillfab_work)
    artifacts_by_slug = index_artifacts_by_slug(artifacts)
    repair_rows = [repair_row(row, artifacts_by_slug) for row in issue_rows]
    repair_rows = sorted(repair_rows, key=repair_sort_key)

    candidates = [
        row
        for row in repair_rows
        if row["repair_scope"] in {"candidate_small_repair", "candidate_adapter_repair"}
    ]
    local_audit = sorted(artifacts, key=lambda row: row["artifact_path"])
    summary = build_summary(
        repair_rows,
        candidates,
        local_audit,
        args.inventory,
        args.batch_list,
        batch_filter,
        args.exclude_list,
        exclusion_filter,
        excluded_issue_labels,
        source_issue_label_count,
    )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(args.out_dir / f"repair-candidates-{DATE_STAMP}.jsonl", candidates)
    write_csv(args.out_dir / f"repair-candidates-{DATE_STAMP}.csv", candidates)
    write_jsonl(args.out_dir / f"local-skillfab-work-audit-{DATE_STAMP}.jsonl", local_audit)
    write_json(args.out_dir / f"repair-data-summary-{DATE_STAMP}.json", summary)
    (args.out_dir / f"repair-data-summary-{DATE_STAMP}.md").write_text(render_summary_md(summary), encoding="utf-8")
    (args.out_dir / "README.md").write_text(render_readme(summary), encoding="utf-8")

    print(json.dumps({"repair_candidates": len(candidates), "local_artifacts": len(local_audit)}, sort_keys=True))
    return 0


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def read_batch_filter(path: Path) -> set[str]:
    values = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.split("#", 1)[0].strip()
        if value:
            values.add(value)
    return values


def row_matches_batch_filter(row: dict[str, Any], batch_filter: set[str]) -> bool:
    skill_id = str(row["skill_id"])
    slug = skill_slug(skill_id)
    candidates = {
        skill_id,
        f"{source_of(skill_id)}/{slug}",
        slug,
    }
    return bool(candidates & batch_filter)


def repair_row(row: dict[str, Any], artifacts_by_slug: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    skill_id = str(row["skill_id"])
    slug = skill_slug(skill_id)
    local_artifacts = artifacts_by_slug.get(slug, [])
    safe_artifacts = [safe_artifact_ref(artifact) for artifact in local_artifacts]
    scope = repair_scope(row)
    return {
        "schema_version": "skill_repair_candidate_v1",
        "skill_id": skill_id,
        "target_os": row.get("target_os", ""),
        "slug_family": f"{source_of(skill_id)}/{slug}",
        "paper_category": row.get("paper_category", ""),
        "fixability_class": row.get("fixability_class", ""),
        "repair_scope": scope,
        "repair_priority": repair_priority(scope, row.get("fixability_class", ""), safe_artifacts),
        "dynamic_outcome": row.get("dynamic_outcome", ""),
        "success_rate": row.get("success_rate", ""),
        "short_issue": row.get("short_issue", ""),
        "repair_implication": row.get("repair_implication", ""),
        "local_skillfab_artifact_count": len(safe_artifacts),
        "local_skillfab_artifacts": safe_artifacts,
        "source_file": row.get("source_file", ""),
    }


def repair_scope(row: dict[str, Any]) -> str:
    target_os = str(row.get("target_os", ""))
    category = str(row.get("paper_category", ""))
    fixability = str(row.get("fixability_class", ""))
    if target_os == "macos" and category == PACKAGE_CATEGORY:
        return "excluded_download_artifact"
    if fixability in SMALL_REPAIR_FIXABILITIES:
        return "candidate_small_repair"
    if fixability in ADAPTER_REPAIR_FIXABILITIES:
        return "candidate_adapter_repair"
    if fixability in BOUNDARY_FIXABILITIES:
        return "boundary_or_not_recommended"
    return "needs_manual_triage"


def repair_priority(scope: str, fixability: str, artifacts: list[dict[str, Any]]) -> str:
    if scope == "excluded_download_artifact":
        return "P_exclude_data_artifact"
    if artifacts:
        return "P0_existing_skillfab_work"
    if scope == "candidate_small_repair":
        if fixability in {"small_skill_script_fix", "small_script_or_path_fix", "script_portability_fix"}:
            return "P1_script_or_path_fix"
        if fixability in {"small_skill_guide_fix", "install_contract_fix", "metadata_or_target_os_contract_fix"}:
            return "P2_contract_or_guide_fix"
        return "P2_small_package_or_runtime_fix"
    if scope == "candidate_adapter_repair":
        return "P3_adapter_research"
    if scope == "boundary_or_not_recommended":
        return "P4_boundary_or_environment"
    return "P5_manual_triage"


def scan_skillfab_work(root: Path) -> list[dict[str, Any]]:
    if not root.is_dir():
        return []
    candidates: list[Path] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if child.name == "cc-migrations":
            candidates.extend(sorted(path for path in child.iterdir() if path.is_dir()))
        else:
            candidates.append(child)

    rows = []
    for path in candidates:
        if not looks_like_skillfab_work(path):
            continue
        rows.append(scan_artifact(path, root))
    return rows


def looks_like_skillfab_work(path: Path) -> bool:
    return any(
        (path / rel).exists()
        for rel in (
            "SKILL.md",
            "repo/SKILL.md",
            ".skillfab-info.json",
            ".skillfab-verify.json",
            "submission.json",
        )
    )


def scan_artifact(path: Path, root: Path) -> dict[str, Any]:
    submission = safe_json(path / "submission.json")
    info = safe_json(path / ".skillfab-info.json")
    verify = safe_json(path / ".skillfab-verify.json")
    skill_md_path = "SKILL.md" if (path / "SKILL.md").is_file() else "repo/SKILL.md" if (path / "repo/SKILL.md").is_file() else ""
    package_guess = package_guess_from_artifact(path.name)
    status = git_status_short(path)
    return {
        "schema_version": "local_skillfab_work_audit_v1",
        "artifact_path": rel(path),
        "artifact_name": path.name,
        "slug_guess": strip_version_suffix(package_guess),
        "package_guess": package_guess,
        "has_skill_md": bool(skill_md_path),
        "skill_md_path": skill_md_path,
        "has_git": (path / ".git").exists(),
        "git_dirty": bool(status),
        "git_status_short": status,
        "issue_id": first_nonempty(info.get("issue_id"), submission.get("issue_id")),
        "repo_id": first_nonempty(info.get("repo_id"), submission.get("repo_id")),
        "submission_id": first_nonempty(info.get("submission_id"), submission.get("submission_id")),
        "submission_status": str(submission.get("status") or ""),
        "created_at": str(submission.get("created_at") or ""),
        "verify_status": str(verify.get("status") or ""),
        "verify_commit_sha": str(verify.get("commit_sha") or ""),
    }


def safe_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}


def git_status_short(path: Path) -> str:
    if not (path / ".git").exists():
        return ""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "status", "--short"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


def index_artifacts_by_slug(artifacts: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for artifact in artifacts:
        slug = str(artifact.get("slug_guess") or "")
        if slug:
            result[slug].append(artifact)
    return result


def safe_artifact_ref(artifact: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifact_path": artifact["artifact_path"],
        "artifact_name": artifact["artifact_name"],
        "issue_id": artifact.get("issue_id", ""),
        "submission_id": artifact.get("submission_id", ""),
        "verify_status": artifact.get("verify_status", ""),
        "git_dirty": artifact.get("git_dirty", False),
        "has_skill_md": artifact.get("has_skill_md", False),
    }


def build_summary(
    rows: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    inventory_path: Path,
    batch_list_path: Path | None,
    batch_filter: set[str] | None,
    exclude_list_path: Path | None,
    exclusion_filter: set[str] | None,
    excluded_issue_labels: int,
    source_issue_label_count: int,
) -> dict[str, Any]:
    candidate_by_os = Counter(row["target_os"] for row in candidates)
    candidate_by_scope = Counter(row["repair_scope"] for row in rows)
    candidate_by_priority = Counter(row["repair_priority"] for row in candidates)
    candidate_by_fixability = Counter(row["fixability_class"] for row in candidates)
    candidate_by_category = {
        target_os: dict(Counter(row["paper_category"] for row in candidates if row["target_os"] == target_os))
        for target_os in sorted({row["target_os"] for row in candidates})
    }
    matched = [row for row in candidates if row["local_skillfab_artifact_count"]]
    top_queue = [
        {
            "skill_id": row["skill_id"],
            "target_os": row["target_os"],
            "repair_priority": row["repair_priority"],
            "paper_category": row["paper_category"],
            "fixability_class": row["fixability_class"],
            "short_issue": row["short_issue"],
        }
        for row in candidates
        if row["repair_priority"] != "P0_existing_skillfab_work"
    ][:30]
    return {
        "schema_version": "skill_repair_dataset_summary_v1",
        "generated_at": DATE_STAMP,
        "unit": "reviewed skill package x target OS issue label",
        "source_inventory": rel(inventory_path),
        "batch_filter_file": rel(batch_list_path) if batch_list_path else "",
        "batch_filter_requested_entries": len(batch_filter or []),
        "batch_filter_matched_issue_labels": len(rows) if batch_filter else 0,
        "exclude_filter_file": rel(exclude_list_path) if exclude_list_path else "",
        "exclude_filter_requested_entries": len(exclusion_filter or []),
        "exclude_filter_removed_issue_labels": excluded_issue_labels,
        "source_inventory_issue_labels": source_issue_label_count,
        "active_issue_labels": len(rows),
        "scope_definition": {
            "candidate_small_repair": "Issue appears repairable by changing skill scripts, guides, metadata, install contract, package entrypoint, or runtime contract.",
            "candidate_adapter_repair": "Issue likely needs a target-OS adapter and should be treated as a larger repair/research case.",
            "excluded_download_artifact": "macOS package/entrypoint rows attributed to incorrect original download; keep as audit, exclude from repair claims.",
            "boundary_or_not_recommended": "Platform-intrinsic or environment/permission case; usually not a SkillFab migration target.",
        },
        "all_issue_labels": len(rows),
        "repair_candidate_labels": len(candidates),
        "repair_candidate_labels_by_os": dict(candidate_by_os),
        "all_labels_by_repair_scope": dict(candidate_by_scope),
        "repair_candidate_labels_by_priority": dict(candidate_by_priority),
        "repair_candidate_labels_by_fixability": dict(candidate_by_fixability),
        "repair_candidate_labels_by_category_and_os": candidate_by_category,
        "local_skillfab_artifacts": len(artifacts),
        "repair_candidates_with_local_artifact": len(matched),
        "top_next_queue": top_queue,
        "safe_wording": [
            "Repair candidates are derived from reviewed root-cause labels, not raw dynamic failures.",
            "macOS package/entrypoint rows caused by incorrect downloads are excluded from SkillFab repair claims.",
            "A repair candidate is not a completed repair; completion requires a SkillFab issue/submission/review record and reused-task re-evaluation where possible.",
        ],
        "forbidden_wording": [
            "Do not treat every confirmed OS issue as repairable.",
            "Do not count local worktree presence as review acceptance or post-repair success.",
            "Do not include push_url or scoped authentication tokens from SkillFab submission files in committed datasets.",
            "Do not include clone or push URLs from SkillFab submission files in committed datasets.",
            "Do not mix archived local repair worktrees into a new batch unless the batch explicitly reuses them.",
            "Do not count legacy-repair excluded skills as active repair candidates for the new batch.",
        ],
    }


def render_summary_md(summary: dict[str, Any]) -> str:
    lines = [
        "# SkillFab Repair Dataset Summary",
        "",
        f"Generated: {DATE_STAMP}",
        "",
        f"Unit: `{summary['unit']}`",
        f"Source inventory issue labels: {summary['source_inventory_issue_labels']}",
        f"Active reviewed issue labels: {summary['active_issue_labels']}",
        f"Repair candidate labels: {summary['repair_candidate_labels']}",
        f"Local SkillFab artifacts audited: {summary['local_skillfab_artifacts']}",
        f"Repair candidates with local artifacts: {summary['repair_candidates_with_local_artifact']}",
        f"Legacy excluded issue labels: {summary['exclude_filter_removed_issue_labels']}",
        "",
        "## Candidate Labels By OS",
        "",
        "| OS | Candidate labels |",
        "|---|---:|",
    ]
    for target_os, count in sorted(summary["repair_candidate_labels_by_os"].items()):
        lines.append(f"| {target_os} | {count} |")
    lines.extend(["", "## Repair Scopes", ""])
    for scope, count in sorted(summary["all_labels_by_repair_scope"].items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"- `{scope}`: {count}")
    lines.extend(["", "## Repair Priorities", ""])
    for priority, count in sorted(summary["repair_candidate_labels_by_priority"].items()):
        lines.append(f"- `{priority}`: {count}")
    lines.extend(["", "## Top Next Queue", ""])
    lines.extend(
        f"- `{row['skill_id']}` on `{row['target_os']}`: `{row['repair_priority']}` / `{row['fixability_class']}`"
        for row in summary["top_next_queue"][:15]
    )
    lines.extend(["", "## Safe Wording", ""])
    lines.extend(f"- {item}" for item in summary["safe_wording"])
    lines.extend(["", "## Forbidden Wording", ""])
    lines.extend(f"- {item}" for item in summary["forbidden_wording"])
    return "\n".join(lines) + "\n"


def render_readme(summary: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# SkillFab Repair Workflow Data",
            "",
            "This is the source-of-truth workspace for SkillFab-based repair orchestration in",
            "the Compa-Skill study. `skillcomptest` owns compatibility testing and dynamic",
            "benchmark outputs; this directory owns repair candidate manifests, SkillFab",
            "interaction records, and RQ2/RQ3 repair workflow evidence.",
            "",
            "This directory separates repair evidence from detection evidence. RQ1 uses",
            "the sibling `skillcomptest/docs/cross-os-testing/analysis/current/` directory",
            "for compatibility detection; RQ2 should use this directory for SkillFab repair",
            "candidates, local work audits, and eventual before/after repair outcomes.",
            "",
            "## Current Files",
            "",
            f"- `repair-candidates-{DATE_STAMP}.jsonl`: reviewed labels that are plausible SkillFab repair targets.",
            f"- `repair-candidates-{DATE_STAMP}.csv`: tabular copy of the candidate queue.",
            f"- `local-skillfab-work-audit-{DATE_STAMP}.jsonl`: safe metadata from local `.skillfab-work` directories.",
            f"- `repair-data-summary-{DATE_STAMP}.json`: machine-readable count summary.",
            f"- `repair-data-summary-{DATE_STAMP}.md`: human-readable count summary.",
            f"- `legacy-repair-exclusions-{DATE_STAMP}.txt`: previous repair attempts excluded from the active queue.",
            "",
            "## Method Boundary",
            "",
            "- The dataset is built from reviewed root-cause labels, not raw dynamic failures.",
            "- It excludes macOS package/entrypoint labels caused by incorrect original downloads.",
            "- It does not prove repair success. A successful repair row still needs a SkillFab",
            "  issue/submission/review record and reused-task before/after evaluation when possible.",
            "- Local `.skillfab-work` directories are audited only as file metadata; token-bearing",
            "  fields such as `push_url`, scoped tokens, and remote URLs are never copied into",
            "  committed outputs.",
            "- Use `--batch-list` to pin a new repair batch. The batch list may contain full",
            "  `Source/package-version` ids, normalized `Source/slug` families, or plain slugs.",
            "- Use `--exclude-list` to keep previous repair attempts out of the active queue.",
            "",
            "## PCC-Inspired Workflow Mapping",
            "",
            "The clean repair workflow follows the earlier PCC/SkillBridge pattern but keeps",
            "roles and data products explicit:",
            "",
            "1. Analysis Agent localizes a reviewed OS defect and writes a scoped SkillFab issue.",
            "2. Dev Agent patches only the isolated skill package/repository.",
            "3. Review Agent performs static package review and records accept/needs-work findings.",
            "4. Evaluation reuses pre-repair tasks where safe to produce before/after outcomes.",
            "",
            "## Current Summary",
            "",
            f"- Source inventory issue labels: {summary['source_inventory_issue_labels']}",
            f"- Active reviewed issue labels: {summary['active_issue_labels']}",
            f"- Repair candidate labels: {summary['repair_candidate_labels']}",
            f"- Local SkillFab artifacts audited: {summary['local_skillfab_artifacts']}",
            f"- Candidates with local artifact evidence: {summary['repair_candidates_with_local_artifact']}",
            f"- Legacy excluded issue labels: {summary['exclude_filter_removed_issue_labels']}",
            "",
        ]
    )


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            csv_row = {field: row.get(field, "") for field in FIELDNAMES}
            csv_row["local_skillfab_artifacts"] = json.dumps(
                csv_row["local_skillfab_artifacts"], ensure_ascii=False, sort_keys=True
            )
            writer.writerow(csv_row)


def repair_sort_key(row: dict[str, Any]) -> tuple[int, str, str]:
    priority_order = {
        "P0_existing_skillfab_work": 0,
        "P1_script_or_path_fix": 1,
        "P2_contract_or_guide_fix": 2,
        "P2_small_package_or_runtime_fix": 3,
        "P3_adapter_research": 4,
        "P4_boundary_or_environment": 5,
        "P5_manual_triage": 6,
        "P_exclude_data_artifact": 7,
    }
    return (priority_order.get(row["repair_priority"], 99), row["target_os"], row["skill_id"])


def package_guess_from_artifact(name: str) -> str:
    value = name
    value = re.sub(r"-issue(?:-\d+)?$", "", value)
    value = re.sub(r"-linux$", "", value)
    value = re.sub(r"-round\d+$", "", value)
    value = re.sub(r"-version-metadata$", "", value)
    return value


def skill_slug(skill_id: str) -> str:
    return strip_version_suffix(package_of(skill_id))


def strip_version_suffix(name: str) -> str:
    return re.sub(r"[-_]?v?\d+(?:[._-]\d+)*(?:[._-]?\w+)?$", "", name).rstrip("-_") or name


def source_of(skill_id: str) -> str:
    return skill_id.split("/", 1)[0] if "/" in skill_id else "unknown"


def package_of(skill_id: str) -> str:
    return skill_id.split("/", 1)[1] if "/" in skill_id else skill_id


def first_nonempty(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return ""


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
