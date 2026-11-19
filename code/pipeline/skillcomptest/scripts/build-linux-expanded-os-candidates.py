#!/usr/bin/env python3
"""Build a broader Linux OS-compatibility review queue.

This is a candidate generator, not a ground-truth labeler. It intentionally
keeps candidates separated by evidence layer so dynamic failures, latent
guide/package risks, and static-only risks are not mixed as confirmed issues.
"""

from __future__ import annotations

import csv
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Iterable


REPO_ROOT = Path(__file__).resolve().parent.parent
ANALYSIS_DIR = REPO_ROOT / "docs" / "cross-os-testing" / "analysis"
ARCHIVE_DIR = ANALYSIS_DIR / "archive" / "20260625-legacy-exploratory"
LINUX_DIR = REPO_ROOT / "docs" / "linux-testing" / "datasets"

EXPLICIT_LABELS = ARCHIVE_DIR / "three-os-explicit-os-issue-labels-20260625.jsonl"
STATIC_PREDICTIONS = LINUX_DIR / "static-skill-predictions.jsonl"

OUT_CSV = ARCHIVE_DIR / "linux-expanded-os-review-candidates-20260625.csv"
OUT_JSON = ARCHIVE_DIR / "linux-expanded-os-review-candidates-20260625.json"
OUT_MD = ARCHIVE_DIR / "linux-expanded-os-review-candidates-20260625.md"

STATIC_OS_LABELS = {
    "guide_only_os_contract",
    "platform_intrinsic",
    "skill_contract_mismatch",
    "skill_os_fixable",
    "skill_package_or_entrypoint",
}

REVIEWED_OR_DECIDED_STATUSES = {
    "confirmed_os_issue",
    "reviewed_excluded_non_os",
    "reviewed_no_observed_os_issue",
    "reviewed_non_os_issue",
    "subagent_reviewed_non_os_issue",
}


def main() -> None:
    labels = [row for row in read_jsonl(EXPLICIT_LABELS) if row.get("target_os") == "linux"]
    labels_by_skill = {row["skill_id"]: row for row in labels}
    static_rows = read_jsonl(STATIC_PREDICTIONS)

    candidates = []
    for static in static_rows:
        if not is_static_os_positive(static):
            continue
        skill_id = static["skill_id"]
        dynamic = labels_by_skill.get(skill_id)

        if dynamic and dynamic["label_status"] in REVIEWED_OR_DECIDED_STATUSES:
            continue

        if dynamic and dynamic["label_status"] == "subagent_reviewed_uncertain_os_issue":
            layer = "subagent_uncertain"
        elif dynamic and dynamic["label_status"] == "unreviewed_dynamic_nonpass":
            layer = "dynamic_nonpass_static_positive"
        elif dynamic and dynamic["label_status"] == "no_observed_dynamic_issue_unreviewed":
            layer = "dynamic_pass_static_positive"
        elif dynamic:
            layer = "other_dynamic_static_positive"
        else:
            layer = "static_only_not_dynamic_seen"

        candidates.append(build_candidate(layer, static, dynamic))

    for dynamic in labels:
        if dynamic["label_status"] != "unreviewed_dynamic_nonpass":
            continue
        if dynamic["skill_id"] in {row["skill_id"] for row in candidates}:
            continue
        if dynamic.get("dynamic_outcome") == "all_unsupported":
            candidates.append(build_dynamic_only_candidate(dynamic))

    candidates.sort(key=candidate_sort_key)
    summary = build_summary(candidates, labels, static_rows)

    write_csv(OUT_CSV, candidates)
    write_json(OUT_JSON, summary)
    write_text(OUT_MD, render_markdown(summary, candidates))

    for path in (OUT_CSV, OUT_JSON, OUT_MD):
        print(f"wrote {path}")


def is_static_os_positive(row: dict[str, Any]) -> bool:
    return bool(row.get("predicted_os_related")) or row.get("predicted_label") in STATIC_OS_LABELS


def build_candidate(layer: str, static: dict[str, Any], dynamic: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "skill_id": static["skill_id"],
        "candidate_layer": layer,
        "priority": priority_for(layer, static, dynamic),
        "static_label": static.get("predicted_label", ""),
        "static_score": static.get("score", 0),
        "static_fixability": static.get("predicted_fixability", ""),
        "dynamic_label_status": dynamic.get("label_status", "") if dynamic else "not_seen",
        "dynamic_outcome": dynamic.get("dynamic_outcome", "") if dynamic else "not_seen",
        "success_rate": dynamic.get("success_rate", "") if dynamic else "",
        "task_total": dynamic.get("task_total", "") if dynamic else "",
        "evidence_excerpt": summarize_static_evidence(static.get("evidence", [])),
        "recommended_review_focus": review_focus(layer),
    }


def build_dynamic_only_candidate(dynamic: dict[str, Any]) -> dict[str, Any]:
    return {
        "skill_id": dynamic["skill_id"],
        "candidate_layer": "dynamic_all_unsupported_without_static_signal",
        "priority": "medium",
        "static_label": "",
        "static_score": 0,
        "static_fixability": "",
        "dynamic_label_status": dynamic.get("label_status", ""),
        "dynamic_outcome": dynamic.get("dynamic_outcome", ""),
        "success_rate": dynamic.get("success_rate", ""),
        "task_total": dynamic.get("task_total", ""),
        "evidence_excerpt": "Dynamic execution marked every task unsupported; static analyzer had no current OS-positive signal.",
        "recommended_review_focus": "Check whether unsupported means platform-intrinsic, missing Linux adapter, or task/environment limitation.",
    }


def priority_for(layer: str, static: dict[str, Any], dynamic: dict[str, Any] | None) -> str:
    score = int(static.get("score") or 0)
    if layer == "subagent_uncertain":
        return "high"
    if layer == "dynamic_nonpass_static_positive" and score >= 65:
        return "high"
    if dynamic and dynamic.get("dynamic_outcome") == "all_unsupported":
        return "high"
    if layer == "dynamic_nonpass_static_positive":
        return "medium"
    if layer == "dynamic_pass_static_positive" and score >= 84:
        return "medium"
    if layer == "static_only_not_dynamic_seen" and score >= 84:
        return "medium"
    return "low"


def review_focus(layer: str) -> str:
    if layer == "dynamic_nonpass_static_positive":
        return "Review first: dynamic non-pass plus static OS signal."
    if layer == "dynamic_pass_static_positive":
        return "Latent issue: tasks passed, but guide/package may still encode Linux-incompatible usage."
    if layer == "static_only_not_dynamic_seen":
        return "Static-only: review before spending dynamic execution cost."
    if layer == "subagent_uncertain":
        return "Needs targeted manual or environment-specific review."
    return "Check dynamic unsupported semantics and package contract."


def candidate_sort_key(row: dict[str, Any]) -> tuple[int, int, str]:
    layer_rank = {
        "subagent_uncertain": 0,
        "dynamic_all_unsupported_without_static_signal": 1,
        "dynamic_nonpass_static_positive": 2,
        "dynamic_pass_static_positive": 3,
        "static_only_not_dynamic_seen": 4,
    }.get(row["candidate_layer"], 9)
    priority_rank = {"high": 0, "medium": 1, "low": 2}.get(row["priority"], 9)
    return (layer_rank, priority_rank, f"{1000 - int(row.get('static_score') or 0):04d}:{row['skill_id']}")


def build_summary(
    candidates: list[dict[str, Any]],
    labels: list[dict[str, Any]],
    static_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": "linux_expanded_os_review_candidates_v1",
        "generated_at": now_utc(),
        "unit": "skill_id",
        "source_files": {
            "explicit_labels": rel(EXPLICIT_LABELS),
            "static_predictions": rel(STATIC_PREDICTIONS),
        },
        "current_linux_dynamic_rows": len(labels),
        "current_static_prediction_rows": len(static_rows),
        "candidate_count": len(candidates),
        "candidate_layer_counts": counter_dict(row["candidate_layer"] for row in candidates),
        "priority_counts": counter_dict(row["priority"] for row in candidates),
        "static_label_counts": counter_dict(row["static_label"] for row in candidates),
        "dynamic_outcome_counts": counter_dict(str(row["dynamic_outcome"]) for row in candidates),
        "claim_audit": {
            "status": "caution",
            "safe_wording": "After reviewing the first 44 Linux candidates, there are still 263 broader Linux OS-review candidates: 36 dynamic non-pass with static OS signals, 102 dynamic-pass latent static positives, 121 static-only not-yet-dynamic skills, 1 subagent-uncertain case, and 3 all-unsupported dynamic-only cases.",
            "forbidden_wording": "Linux has only 41 OS compatibility issues.",
        },
    }


def summarize_static_evidence(evidence: list[dict[str, Any]], limit: int = 6) -> str:
    parts = []
    for item in evidence[:limit]:
        parts.append(
            f"{item.get('rule_id', '')}:{item.get('file', '')}:{item.get('line', '')}:"
            f"{str(item.get('matched_text', ''))[:100]}"
        )
    return " | ".join(parts)


def render_markdown(summary: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    lines = [
        "# Linux Expanded OS Review Candidates",
        "",
        "Date: 2026-06-25",
        "",
        "This file is a candidate queue, not ground truth. It exists because the first 44 reviewed candidates were an intentionally narrow queue.",
        "",
        "## Summary",
        "",
        f"- Current Linux dynamic rows: {summary['current_linux_dynamic_rows']}",
        f"- Candidate count: {summary['candidate_count']}",
        f"- By layer: {summary['candidate_layer_counts']}",
        f"- By priority: {summary['priority_counts']}",
        "",
        "## Top Candidates",
        "",
        "| Priority | Layer | Skill | Static label | Score | Dynamic | Evidence |",
        "| --- | --- | --- | --- | ---: | --- | --- |",
    ]
    for row in candidates[:80]:
        lines.append(
            f"| {row['priority']} | {row['candidate_layer']} | `{row['skill_id']}` | "
            f"{row['static_label']} | {row['static_score']} | {row['dynamic_outcome']} | "
            f"{short_cell(row['evidence_excerpt'])} |"
        )
    lines.extend(
        [
            "",
            "## Reporting Rules",
            "",
            "- Do not add these candidates to confirmed OS issues before root-cause review.",
            "- Review `dynamic_nonpass_static_positive` first; it is the highest value next batch.",
            "- Use `dynamic_pass_static_positive` for guide/package latent issue analysis, not runtime failure prevalence.",
            "- Use `static_only_not_dynamic_seen` to decide what to test next without spending dynamic execution cost blindly.",
        ]
    )
    return "\n".join(lines) + "\n"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "priority",
        "candidate_layer",
        "skill_id",
        "static_label",
        "static_score",
        "static_fixability",
        "dynamic_label_status",
        "dynamic_outcome",
        "success_rate",
        "task_total",
        "recommended_review_focus",
        "evidence_excerpt",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def counter_dict(values: Iterable[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def short_cell(text: str, limit: int = 160) -> str:
    return str(text or "").replace("|", "/")[:limit]


def rel(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


if __name__ == "__main__":
    main()
