#!/usr/bin/env python3
"""Build the remaining Windows review queue aligned to the three-OS overlap.

The output is a deterministic CSV that can be fed to
`prepare-windows-claude-review-batch.py`. It uses the paper-facing denominator:
normalized `slug_family` values that have dynamic observations on Linux, macOS,
and Windows.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
import json
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DYNAMIC = REPO_ROOT / "docs/cross-os-testing/datasets/unified-dynamic-skill-labels.jsonl"
DEFAULT_QUEUE = REPO_ROOT / "docs/cross-os-testing/analysis/archive/20260625-legacy-exploratory/windows-system-review-queue-20260626.csv"
DEFAULT_OLD_BATCH_DIR = REPO_ROOT / "docs/cross-os-testing/analysis/archive/20260625-legacy-exploratory/windows-review-batches-20260626"
DEFAULT_OUT = REPO_ROOT / "docs/cross-os-testing/analysis/archive/20260625-legacy-exploratory/windows-common-remaining-review-candidates-20260626.csv"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dynamic-jsonl", type=Path, default=DEFAULT_DYNAMIC)
    parser.add_argument("--windows-queue-csv", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--old-batch-dir", type=Path, default=DEFAULT_OLD_BATCH_DIR)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--summary-json", type=Path)
    parser.add_argument("--summary-md", type=Path)
    args = parser.parse_args()

    dynamic_rows = read_jsonl(args.dynamic_jsonl)
    family_by_os = families_by_os(dynamic_rows)
    common_families = set.intersection(family_by_os["linux"], family_by_os["macos"], family_by_os["windows"])
    old_review_ids, old_review_families = load_old_review_scope(args.old_batch_dir)

    queue_rows = read_csv(args.windows_queue_csv)
    candidates = []
    for row in queue_rows:
        if row.get("target_os") != "windows":
            continue
        if row.get("dynamic_outcome") == "all_pass":
            continue
        if row.get("slug_family") not in common_families:
            continue
        if row.get("skill_id") in old_review_ids or row.get("slug_family") in old_review_families:
            continue
        enriched = dict(row)
        sanitize_path_fields(enriched)
        enriched["common_denominator"] = "three_os_slug_family_overlap"
        enriched["candidate_reason"] = "windows_nonpass_not_in_prior_review_scope"
        candidates.append(enriched)

    candidates.sort(key=sort_key)
    write_csv(args.out, candidates)

    summary = build_summary(dynamic_rows, queue_rows, common_families, old_review_ids, old_review_families, candidates)
    summary_json = args.summary_json or args.out.with_suffix(".summary.json")
    summary_md = args.summary_md or args.out.with_suffix(".summary.md")
    write_json(summary_json, summary)
    summary_md.write_text(render_summary_md(summary), encoding="utf-8")
    print(json.dumps({"out": rel(args.out), "rows": len(candidates), "families": len({r["slug_family"] for r in candidates})}, ensure_ascii=False, sort_keys=True))
    return 0


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def families_by_os(rows: list[dict[str, Any]]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        family = str(row.get("slug_family") or row.get("skill_id") or "")
        target_os = str(row.get("target_os") or "")
        if family and target_os:
            result[target_os].add(family)
    return result


def load_old_review_scope(old_batch_dir: Path) -> tuple[set[str], set[str]]:
    skill_ids: set[str] = set()
    families: set[str] = set()
    if not old_batch_dir.is_dir():
        return skill_ids, families
    for path in sorted(old_batch_dir.glob("*.jsonl")):
        for row in read_jsonl(path):
            skill_id = row.get("skill_id")
            family = row.get("slug_family")
            if isinstance(skill_id, str) and skill_id:
                skill_ids.add(skill_id)
            if isinstance(family, str) and family:
                families.add(family)
    return skill_ids, families


def sort_key(row: dict[str, str]) -> tuple[int, int, int, int, str]:
    outcome_priority = {
        "all_unsupported": 0,
        "all_blocked": 1,
        "unresolved": 2,
        "mixed": 3,
        "all_fail": 4,
    }.get(row.get("dynamic_outcome", ""), 9)
    has_source_penalty = 0 if row.get("has_source") == "True" else 1
    has_sample_penalty = 0 if row.get("has_sample") == "True" else 1
    try:
        review_rank = int(row.get("review_rank") or 999999)
    except ValueError:
        review_rank = 999999
    return (has_source_penalty, has_sample_penalty, outcome_priority, review_rank, row.get("skill_id", ""))


def sanitize_path_fields(row: dict[str, str]) -> None:
    for key in ("source_path", "sample_path", "windows_exec_path", "routing_evidence_files"):
        if key in row:
            row[key] = sanitize_repo_path(row[key])


def sanitize_repo_path(value: str) -> str:
    prefix = REPO_ROOT.as_posix().rstrip("/") + "/"
    return str(value or "").replace(prefix, "")


def build_summary(
    dynamic_rows: list[dict[str, Any]],
    queue_rows: list[dict[str, str]],
    common_families: set[str],
    old_review_ids: set[str],
    old_review_families: set[str],
    candidates: list[dict[str, str]],
) -> dict[str, Any]:
    dynamic_family_by_os = families_by_os(dynamic_rows)
    windows_common_rows = [
        row
        for row in queue_rows
        if row.get("target_os") == "windows" and row.get("slug_family") in common_families
    ]
    windows_common_nonpass = [row for row in windows_common_rows if row.get("dynamic_outcome") != "all_pass"]
    old_reviewed_common = [
        row
        for row in windows_common_nonpass
        if row.get("skill_id") in old_review_ids or row.get("slug_family") in old_review_families
    ]
    return {
        "schema_version": "windows_common_remaining_review_candidates_v1",
        "dynamic_family_counts": {os_name: len(families) for os_name, families in sorted(dynamic_family_by_os.items())},
        "three_os_common_family_count": len(common_families),
        "windows_common_rows": len(windows_common_rows),
        "windows_common_family_count": len({row["slug_family"] for row in windows_common_rows}),
        "windows_common_nonpass_rows": len(windows_common_nonpass),
        "windows_common_nonpass_family_count": len({row["slug_family"] for row in windows_common_nonpass}),
        "old_review_scope_rows_in_common_nonpass": len(old_reviewed_common),
        "old_review_scope_families_in_common_nonpass": len({row["slug_family"] for row in old_reviewed_common}),
        "candidate_rows": len(candidates),
        "candidate_family_count": len({row["slug_family"] for row in candidates}),
        "candidate_outcome_counts": dict(Counter(row.get("dynamic_outcome", "") for row in candidates)),
        "candidate_has_source_counts": dict(Counter(row.get("has_source", "") for row in candidates)),
        "candidate_has_sample_counts": dict(Counter(row.get("has_sample", "") for row in candidates)),
        "candidate_review_track_counts": dict(Counter(row.get("review_track", "") for row in candidates)),
    }


def render_summary_md(summary: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Windows Common Remaining Review Candidates",
            "",
            f"- Three-OS common families: {summary['three_os_common_family_count']}",
            f"- Windows common rows: {summary['windows_common_rows']}",
            f"- Windows common non-pass rows: {summary['windows_common_nonpass_rows']}",
            f"- Already in old review scope: {summary['old_review_scope_rows_in_common_nonpass']} rows / {summary['old_review_scope_families_in_common_nonpass']} families",
            f"- Remaining candidates: {summary['candidate_rows']} rows / {summary['candidate_family_count']} families",
            f"- Outcome counts: `{summary['candidate_outcome_counts']}`",
            f"- Has source counts: `{summary['candidate_has_source_counts']}`",
            f"- Has sample counts: `{summary['candidate_has_sample_counts']}`",
            "",
        ]
    )


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def rel(path: Path | str) -> str:
    path = Path(path)
    try:
        return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


if __name__ == "__main__":
    raise SystemExit(main())
