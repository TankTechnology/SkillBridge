#!/usr/bin/env python3
"""Evaluate static skill predictions against Linux reviewed labels."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATASET = REPO_ROOT / "docs" / "linux-testing" / "datasets"

OS_POSITIVE_LABELS = {
    "skill_os_fixable",
    "skill_package_or_entrypoint",
    "skill_contract_mismatch",
    "guide_only_os_contract",
    "platform_intrinsic",
}

FIXABLE_LABELS = {
    "skill_os_fixable",
    "skill_package_or_entrypoint",
    "skill_contract_mismatch",
    "guide_only_os_contract",
}

COMPACT_OS_POSITIVE_LABELS = {
    "os_specific_execution_issue",
    "os_install_or_dependency_contract_issue",
    "package_or_entrypoint_issue",
}

EXCLUDED_SCOPES = {
    "excluded_external_credential_or_service",
    "excluded_benchmark_capability_gap",
}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.strip():
            rows.append(json.loads(raw))
    return rows


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def evaluate(labels: list[dict[str, Any]], predictions: list[dict[str, Any]], quality: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    pred_by_skill = {row["skill_id"]: row for row in predictions}
    eval_rows: list[dict[str, Any]] = []
    for label in labels:
        if quality and label.get("label_quality") != quality:
            continue
        pred = pred_by_skill.get(label["skill_id"])
        if pred is None:
            continue
        dynamic_label = label.get("dynamic_label", "")
        predicted_label = pred.get("predicted_label", "")
        row = {
            "skill_id": label["skill_id"],
            "label_quality": label.get("label_quality", ""),
            "dynamic_label": dynamic_label,
            "predicted_label": predicted_label,
            "dynamic_os_positive": dynamic_label in OS_POSITIVE_LABELS,
            "predicted_os_positive": predicted_label in OS_POSITIVE_LABELS,
            "dynamic_fixable_positive": dynamic_label in FIXABLE_LABELS,
            "predicted_fixable_positive": predicted_label in FIXABLE_LABELS,
            "category_match": dynamic_label == predicted_label,
            "static_confidence": pred.get("confidence", ""),
            "static_score": pred.get("score", 0),
            "dynamic_evidence": label.get("evidence_summary", ""),
            "static_rules": [e.get("rule_id") for e in pred.get("evidence", [])[:5]],
        }
        eval_rows.append(row)

    summary = {
        "schema_version": "static_analyzer_eval_v1",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "label_quality_filter": quality or "all",
        "joined_skills": len(eval_rows),
        "os_issue_detection": binary_metrics(eval_rows, "dynamic_os_positive", "predicted_os_positive"),
        "fixable_issue_detection": binary_metrics(eval_rows, "dynamic_fixable_positive", "predicted_fixable_positive"),
        "category_exact_match": exact_match(eval_rows),
        "dynamic_label_counts": counts(row["dynamic_label"] for row in eval_rows),
        "predicted_label_counts": counts(row["predicted_label"] for row in eval_rows),
        "missed_os_positives": [
            compact_row(row)
            for row in eval_rows
            if row["dynamic_os_positive"] and not row["predicted_os_positive"]
        ],
        "false_positive_os_candidates": [
            compact_row(row)
            for row in eval_rows
            if row["predicted_os_positive"] and not row["dynamic_os_positive"]
        ][:50],
    }
    return summary, eval_rows


def evaluate_reviewed(
    labels: list[dict[str, Any]],
    predictions: list[dict[str, Any]],
    quality: str,
    target_os: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    deduped_labels, duplicate_count, conflicts = dedupe_reviewed_labels(labels, quality, target_os)
    pred_by_skill = {row["skill_id"]: row for row in predictions}
    eval_rows: list[dict[str, Any]] = []
    missing_predictions: list[dict[str, Any]] = []

    for label in deduped_labels:
        pred = pred_by_skill.get(label["skill_id"])
        if pred is None:
            missing_predictions.append(label)
            continue
        scope_status = label.get("scope_status", "")
        primary_label = label.get("primary_label", "")
        predicted_label = pred.get("predicted_label", "")
        in_scope = scope_status == "in_scope"
        row = {
            "skill_id": label["skill_id"],
            "target_os": label.get("target_os", ""),
            "label_quality": label.get("label_quality", ""),
            "source_run_ids": label.get("source_run_ids", []),
            "task_results": label.get("task_results", []),
            "reviewed_scope_status": scope_status,
            "reviewed_primary_label": primary_label,
            "predicted_label": predicted_label,
            "in_scope": in_scope,
            "reviewed_os_positive": in_scope and primary_label in COMPACT_OS_POSITIVE_LABELS,
            "predicted_os_positive": predicted_label in OS_POSITIVE_LABELS,
            "reviewed_fixable_positive": in_scope and primary_label in COMPACT_OS_POSITIVE_LABELS,
            "predicted_fixable_positive": predicted_label in FIXABLE_LABELS,
            "reviewed_package_entrypoint_positive": in_scope and primary_label == "package_or_entrypoint_issue",
            "predicted_package_entrypoint_positive": predicted_label == "skill_package_or_entrypoint",
            "reviewed_excluded": scope_status in EXCLUDED_SCOPES,
            "predicted_excluded": predicted_label == "auth_or_service_blocked",
            "static_confidence": pred.get("confidence", ""),
            "static_score": pred.get("score", 0),
            "review_notes": label.get("notes", ""),
            "review_evidence_refs": label.get("evidence_refs", []),
            "static_rules": [e.get("rule_id") for e in pred.get("evidence", [])[:5]],
        }
        eval_rows.append(row)

    in_scope_rows = [row for row in eval_rows if row["in_scope"]]
    summary = {
        "schema_version": "static_analyzer_reviewed_eval_v1",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "label_source": "reviewed",
        "label_quality_filter": quality or "all",
        "target_os_filter": target_os or "all",
        "raw_reviewed_rows": sum(1 for row in labels if _reviewed_label_in_filter(row, quality, target_os)),
        "duplicate_reviewed_rows": duplicate_count,
        "label_conflicts": conflicts,
        "deduped_reviewed_skills": len(deduped_labels),
        "missing_prediction_count": len(missing_predictions),
        "joined_skills": len(eval_rows),
        "reviewed_scope_counts": counts(row["reviewed_scope_status"] for row in eval_rows),
        "reviewed_primary_label_counts_in_scope": counts(row["reviewed_primary_label"] for row in in_scope_rows),
        "predicted_label_counts": counts(row["predicted_label"] for row in eval_rows),
        "source_run_counts": counts(run for row in eval_rows for run in row.get("source_run_ids", [])),
        "os_issue_detection_in_scope": binary_metrics(in_scope_rows, "reviewed_os_positive", "predicted_os_positive"),
        "fixable_issue_detection_in_scope": binary_metrics(in_scope_rows, "reviewed_fixable_positive", "predicted_fixable_positive"),
        "package_entrypoint_detection_in_scope": binary_metrics(
            in_scope_rows,
            "reviewed_package_entrypoint_positive",
            "predicted_package_entrypoint_positive",
        ),
        "excluded_scope_detection": binary_metrics(eval_rows, "reviewed_excluded", "predicted_excluded"),
        "missed_os_positives": [
            compact_reviewed_row(row)
            for row in in_scope_rows
            if row["reviewed_os_positive"] and not row["predicted_os_positive"]
        ],
        "false_positive_os_candidates": [
            compact_reviewed_row(row)
            for row in in_scope_rows
            if row["predicted_os_positive"] and not row["reviewed_os_positive"]
        ][:50],
        "excluded_scope_false_negatives": [
            compact_reviewed_row(row)
            for row in eval_rows
            if row["reviewed_excluded"] and not row["predicted_excluded"]
        ][:50],
        "excluded_scope_false_positives": [
            compact_reviewed_row(row)
            for row in eval_rows
            if row["predicted_excluded"] and not row["reviewed_excluded"]
        ][:50],
    }
    return summary, eval_rows


def dedupe_reviewed_labels(
    labels: list[dict[str, Any]], quality: str, target_os: str
) -> tuple[list[dict[str, Any]], int, list[dict[str, Any]]]:
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    duplicate_count = 0
    conflicts: list[dict[str, Any]] = []
    for label in labels:
        if not _reviewed_label_in_filter(label, quality, target_os):
            continue
        key = (label["skill_id"], label.get("target_os", ""))
        if key not in by_key:
            row = dict(label)
            row["source_run_ids"] = [label.get("source_run_id", "")]
            row["task_results"] = [label.get("task_result", "")]
            by_key[key] = row
            continue

        duplicate_count += 1
        existing = by_key[key]
        if label.get("source_run_id", "") not in existing["source_run_ids"]:
            existing["source_run_ids"].append(label.get("source_run_id", ""))
        if label.get("task_result", "") not in existing["task_results"]:
            existing["task_results"].append(label.get("task_result", ""))
        if (
            existing.get("scope_status") != label.get("scope_status")
            or existing.get("primary_label") != label.get("primary_label")
        ):
            conflicts.append(
                {
                    "skill_id": label["skill_id"],
                    "target_os": label.get("target_os", ""),
                    "kept_scope_status": existing.get("scope_status", ""),
                    "kept_primary_label": existing.get("primary_label", ""),
                    "conflict_scope_status": label.get("scope_status", ""),
                    "conflict_primary_label": label.get("primary_label", ""),
                }
            )
    return list(by_key.values()), duplicate_count, conflicts


def _reviewed_label_in_filter(row: dict[str, Any], quality: str, target_os: str) -> bool:
    if quality and row.get("label_quality") != quality:
        return False
    if target_os and row.get("target_os") != target_os:
        return False
    return True


def binary_metrics(rows: list[dict[str, Any]], truth_key: str, pred_key: str) -> dict[str, Any]:
    tp = sum(1 for row in rows if row[truth_key] and row[pred_key])
    fp = sum(1 for row in rows if not row[truth_key] and row[pred_key])
    fn = sum(1 for row in rows if row[truth_key] and not row[pred_key])
    tn = sum(1 for row in rows if not row[truth_key] and not row[pred_key])
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }


def exact_match(rows: list[dict[str, Any]]) -> dict[str, Any]:
    relevant = [row for row in rows if row["dynamic_os_positive"]]
    match = sum(1 for row in relevant if row["category_match"])
    return {
        "evaluated_os_positive": len(relevant),
        "exact_matches": match,
        "accuracy": round(match / len(relevant), 4) if relevant else 0.0,
    }


def compact_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "skill_id": row["skill_id"],
        "dynamic_label": row["dynamic_label"],
        "predicted_label": row["predicted_label"],
        "static_confidence": row["static_confidence"],
        "static_rules": row["static_rules"],
    }


def compact_reviewed_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "skill_id": row["skill_id"],
        "reviewed_scope_status": row["reviewed_scope_status"],
        "reviewed_primary_label": row["reviewed_primary_label"],
        "predicted_label": row["predicted_label"],
        "static_confidence": row["static_confidence"],
        "static_rules": row["static_rules"],
        "source_run_ids": row.get("source_run_ids", []),
    }


def counts(values) -> dict[str, int]:
    result: dict[str, int] = defaultdict(int)
    for value in values:
        result[str(value)] += 1
    return dict(sorted(result.items()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--label-source", choices=("dynamic", "reviewed"), default="dynamic")
    parser.add_argument("--quality", default=None, help="label quality filter; use empty string for all")
    parser.add_argument("--target-os", default="linux", help="target OS filter for reviewed labels; use empty string for all")
    parser.add_argument("--predictions", type=Path, default=None, help="Static prediction JSONL path.")
    parser.add_argument("--labels", type=Path, default=None, help="Dynamic or reviewed label JSONL path.")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--joined-out", type=Path, default=None)
    args = parser.parse_args(argv)

    predictions = read_jsonl(args.predictions or (args.dataset_dir / "static-skill-predictions.jsonl"))
    if args.label_source == "reviewed":
        quality = "reviewed_seed" if args.quality is None else args.quality
        labels = read_jsonl(args.labels or (args.dataset_dir / "os-compat-reviewed-labels.jsonl"))
        summary, rows = evaluate_reviewed(labels, predictions, quality, args.target_os)
        out = args.out or (args.dataset_dir / "static-reviewed-eval-summary.json")
        joined_out = args.joined_out or (args.dataset_dir / "static-reviewed-joined.jsonl")
    else:
        quality = "silver_reviewed" if args.quality is None else args.quality
        labels = read_jsonl(args.labels or (args.dataset_dir / "dynamic-skill-labels.jsonl"))
        summary, rows = evaluate(labels, predictions, quality)
        out = args.out or (args.dataset_dir / "static-eval-summary.json")
        joined_out = args.joined_out or (args.dataset_dir / "static-dynamic-joined.jsonl")

    write_json(out, summary)
    write_jsonl(joined_out, rows)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
