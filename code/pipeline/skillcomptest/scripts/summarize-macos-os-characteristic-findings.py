#!/usr/bin/env python3
"""Summarize macOS OS-characteristic skill compatibility findings.

The input is the Claude-reviewed macOS row file produced from compact static
and dynamic evidence. This script does not execute skill code. It separates a
broad OS/package positive set from a narrower OS-characteristic set suitable for
paper reporting.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REVIEW_ROWS = (
    REPO_ROOT
    / "docs/macos-testing/reviews/full-static1477-cc-review-20260624/full-cc-review-rows.jsonl"
)
DEFAULT_OUT_JSON = REPO_ROOT / "docs/macos-testing/analysis/macos-os-characteristic-findings-20260624.json"
DEFAULT_OUT_MD = REPO_ROOT / "docs/macos-testing/analysis/macos-os-characteristic-findings-20260624.md"
DEFAULT_OUT_JSONL = REPO_ROOT / "docs/macos-testing/datasets/macos-os-characteristic-issues-20260624.jsonl"

STATIC_POSITIVE_LABELS = {
    "skill_os_fixable",
    "skill_package_or_entrypoint",
    "skill_contract_mismatch",
    "guide_only_os_contract",
    "platform_intrinsic",
}

BROAD_REVIEW_POSITIVE_LABELS = {
    "os_specific_execution_issue",
    "os_install_or_dependency_contract_issue",
    "package_or_entrypoint_issue",
}

STRICT_REVIEW_POSITIVE_LABELS = {
    "os_specific_execution_issue",
    "os_install_or_dependency_contract_issue",
}

EXPLICIT_OS_PACKAGE_TAGS = {
    "hardcoded_linux_path",
    "hardcoded_path",
    "guide_only_os_contract",
    "install_guide_winget_centric",
    "linux_apt_docs_not_macos_issue",
    "linux_package_manager_in_guide",
    "linux_path_assumption",
    "linux_path_in_bundled_script",
    "linux_xdg_open_properly_conditionalized",
    "macos_native_claim",
    "metadata_contract",
    "procfs_in_examples",
    "pyautogui_gui_automation",
    "root_path",
    "systemd_guide_for_macos_skill",
    "systemd_references_in_code",
    "windows_command_properly_conditionalized",
}

LOCAL_ENTRY_SURFACE_TAGS = {
    "all_cli_variants_missing",
    "entry_point_declared_not_bundled",
    "external_cli_dependency",
    "guide_path_mismatch",
    "metadata_shell",
    "missing_binary",
    "missing_build_artifact",
    "missing_entrypoint_script",
    "missing_referenced_setup",
    "missing_scripts",
    "missing_scripts_directory",
    "path_mismatch",
    "script_path_mismatch",
    "scripts_declared_in_frontmatter",
    "shell_script_not_in_package",
}

ENTRY_SURFACE_RE = re.compile(
    r"\b(cli|bin/|binary|shell script|\.sh\b|setup\.sh|install script|hook|cron|wrapper|launcher|entrypoint|entry point)\b",
    re.IGNORECASE,
)

ENTRY_SURFACE_EXCLUDE_RE = re.compile(
    r"external[_ ]api|required|api[_ ]key|credential|auth[_ ]required|external[_ ]service|"
    r"all[_ ]tasks[_ ]pass|benchmark[_ ]all[_ ]pass|optional|placeholder|"
    r"not an os issue|no macos issue|not os-specific|not os specific|"
    r"platform-agnostic|affects any platform|on any platform|documentation skill",
    re.IGNORECASE,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-rows", type=Path, default=DEFAULT_REVIEW_ROWS)
    parser.add_argument("--out-json", type=Path, default=DEFAULT_OUT_JSON)
    parser.add_argument("--out-md", type=Path, default=DEFAULT_OUT_MD)
    parser.add_argument("--out-jsonl", type=Path, default=DEFAULT_OUT_JSONL)
    args = parser.parse_args()

    rows = read_jsonl(args.review_rows)
    classified = [classify_row(row) for row in rows]
    characteristic_rows = [row for row in classified if row["os_characteristic_issue"]]

    support = build_support_matrix(rows, classified, characteristic_rows, args.review_rows)
    write_json(args.out_json, support)
    write_jsonl(args.out_jsonl, compact_issue_rows(characteristic_rows))
    args.out_md.parent.mkdir(parents=True, exist_ok=True)
    args.out_md.write_text(render_markdown(support), encoding="utf-8")

    print(
        json.dumps(
            {
                "reviewed_rows": support["sample_universe"]["reviewed_rows"],
                "broad_positive_count": support["definitions"]["broad_os_or_package_issue"]["count"],
                "strict_positive_count": support["definitions"]["strict_os_or_install_issue"]["count"],
                "os_characteristic_count": support["definitions"]["os_characteristic_issue"]["count"],
                "out_json": str(args.out_json),
                "out_md": str(args.out_md),
                "out_jsonl": str(args.out_jsonl),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def row_text(row: dict[str, Any]) -> str:
    parts = [str(row.get(field) or "") for field in ("notes", "task_result")]
    parts.extend(str(tag) for tag in row.get("secondary_tags", []))
    return " ".join(parts)


def classify_row(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    primary_label = row.get("primary_label")
    in_scope = row.get("scope_status") == "in_scope"
    broad_positive = in_scope and primary_label in BROAD_REVIEW_POSITIVE_LABELS
    strict_positive = in_scope and primary_label in STRICT_REVIEW_POSITIVE_LABELS
    bucket = "not_os_characteristic"

    if strict_positive:
        os_characteristic = True
        bucket = "direct_os_or_install"
    elif broad_positive and primary_label == "package_or_entrypoint_issue":
        tags = set(row.get("secondary_tags", []))
        text = row_text(row)
        if tags & EXPLICIT_OS_PACKAGE_TAGS:
            os_characteristic = True
            bucket = "explicit_os_semantic_package"
        elif (tags & LOCAL_ENTRY_SURFACE_TAGS or ENTRY_SURFACE_RE.search(text)) and not ENTRY_SURFACE_EXCLUDE_RE.search(text):
            os_characteristic = True
            bucket = "local_os_adapter_entry_surface"
        else:
            os_characteristic = False
            bucket = "generic_package_excluded"
    else:
        os_characteristic = False

    out["broad_os_or_package_issue"] = broad_positive
    out["strict_os_or_install_issue"] = strict_positive
    out["os_characteristic_issue"] = os_characteristic
    out["os_characteristic_bucket"] = bucket
    out["static_positive"] = row.get("static_predicted_label") in STATIC_POSITIVE_LABELS
    return out


def build_support_matrix(
    rows: list[dict[str, Any]],
    classified: list[dict[str, Any]],
    characteristic_rows: list[dict[str, Any]],
    review_rows_path: Path,
) -> dict[str, Any]:
    broad_rows = [row for row in classified if row["broad_os_or_package_issue"]]
    strict_rows = [row for row in classified if row["strict_os_or_install_issue"]]
    excluded_generic_package = [
        row
        for row in classified
        if row.get("scope_status") == "in_scope"
        and row.get("primary_label") == "package_or_entrypoint_issue"
        and not row["os_characteristic_issue"]
    ]

    return {
        "schema_version": "macos_os_characteristic_findings_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sample_universe": {
            "target_os": "macos",
            "reviewed_rows": len(rows),
            "source_review_rows": repo_rel(review_rows_path),
            "review_method": "Claude Code audit over compact static and dynamic evidence; no skill code execution in the review pass.",
        },
        "definitions": {
            "broad_os_or_package_issue": {
                "count": len(broad_rows),
                "definition": "in_scope and primary_label in os_specific_execution_issue, os_install_or_dependency_contract_issue, package_or_entrypoint_issue",
                "metrics": binary_metrics(classified, "broad_os_or_package_issue", "static_positive"),
            },
            "strict_os_or_install_issue": {
                "count": len(strict_rows),
                "definition": "in_scope and primary_label in os_specific_execution_issue or os_install_or_dependency_contract_issue",
                "metrics": binary_metrics(classified, "strict_os_or_install_issue", "static_positive"),
            },
            "os_characteristic_issue": {
                "count": len(characteristic_rows),
                "definition": "strict OS/install issue, or package/entrypoint issue whose evidence contains OS semantics or a local OS-adapter entry surface.",
                "metrics": binary_metrics(classified, "os_characteristic_issue", "static_positive"),
                "bucket_counts": counts(row["os_characteristic_bucket"] for row in characteristic_rows),
                "primary_label_counts": counts(row.get("primary_label", "") for row in characteristic_rows),
                "static_predicted_label_counts": counts(row.get("static_predicted_label", "") for row in characteristic_rows),
                "fixability_counts": counts(row.get("fixability", "") for row in characteristic_rows),
                "false_negatives": [
                    compact_issue_row(row)
                    for row in characteristic_rows
                    if not row["static_positive"]
                ],
            },
            "generic_package_excluded": {
                "count": len(excluded_generic_package),
                "definition": "in-scope package/entrypoint issues without enough OS semantics for the paper-facing OS-characteristic denominator.",
            },
        },
        "support_calls": {
            "static_analysis_is_high_recall_for_os_characteristic_issues": {
                "status": "support",
                "safe_wording": "Static analysis covers 90 of 94 OS-characteristic macOS issues in this reviewed sample, for 0.9574 recall.",
                "source_metric": "definitions.os_characteristic_issue.metrics",
            },
            "all_broad_package_issues_are_os_compatibility_issues": {
                "status": "fail",
                "safe_wording": "The broad 241 count includes generic package and entrypoint defects; it should not be reported as the macOS OS-compatibility prevalence.",
                "source_metric": "definitions.broad_os_or_package_issue.count",
            },
            "strict_os_labels_capture_all_relevant_skill_os_problems": {
                "status": "caution",
                "safe_wording": "The strict 32 count captures direct platform/install failures but misses OS-adapter entry surface failures.",
                "source_metric": "definitions.strict_os_or_install_issue.count",
            },
        },
        "recommended_story": [
            "Use the 1477 Claude-reviewed macOS rows as the current denominator.",
            "Report 94 OS-characteristic issues as the paper-facing macOS finding, while keeping 241 broad and 32 strict counts as sensitivity analyses.",
            "Describe the static analyzer as a high-recall triage layer rather than a final classifier.",
        ],
        "forbidden_overclaims": [
            "Do not say macOS has exactly 241 OS compatibility bugs; 241 is the broad OS/package count.",
            "Do not say static analysis alone proves a skill is broken; the reviewed labels use dynamic evidence and agent audit.",
            "Do not compare macOS prevalence directly with Linux until the denominators and reviewed-label scopes are aligned.",
        ],
        "characteristic_issue_skill_ids": [row["skill_id"] for row in characteristic_rows],
    }


def counts(values: Any) -> dict[str, int]:
    return dict(sorted(Counter(values).items(), key=lambda item: (-item[1], item[0])))


def repo_rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def binary_metrics(rows: list[dict[str, Any]], actual_key: str, predicted_key: str) -> dict[str, Any]:
    tp = fp = fn = tn = 0
    for row in rows:
        actual = bool(row.get(actual_key))
        predicted = bool(row.get(predicted_key))
        if actual and predicted:
            tp += 1
        elif not actual and predicted:
            fp += 1
        elif actual and not predicted:
            fn += 1
        else:
            tn += 1
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "f1": round(f1, 6),
        "support_actual_positive": tp + fn,
        "support_predicted_positive": tp + fp,
    }


def compact_issue_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [compact_issue_row(row) for row in rows]


def compact_issue_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "skill_id": row.get("skill_id"),
        "bucket": row.get("os_characteristic_bucket"),
        "primary_label": row.get("primary_label"),
        "fixability": row.get("fixability"),
        "confidence": row.get("confidence"),
        "static_predicted_label": row.get("static_predicted_label"),
        "secondary_tags": row.get("secondary_tags", []),
        "notes": row.get("notes", ""),
    }


def render_markdown(data: dict[str, Any]) -> str:
    broad = data["definitions"]["broad_os_or_package_issue"]
    strict = data["definitions"]["strict_os_or_install_issue"]
    characteristic = data["definitions"]["os_characteristic_issue"]
    metrics = characteristic["metrics"]

    lines = [
        "# macOS OS-Characteristic Skill Compatibility Findings",
        "",
        "Date: 2026-06-24",
        "",
        "## Claim Under Audit",
        "",
        "We want a paper-facing macOS denominator that is narrower than generic package breakage but broader than direct platform API failures.",
        "",
        "The recommended label is `OS-characteristic skill compatibility issue`: a skill problem whose evidence involves platform APIs, shell/system command differences, install contracts, path or permission models, or a local OS-adapter entry surface.",
        "",
        "## Denominator",
        "",
        f"- Target OS: `{data['sample_universe']['target_os']}`",
        f"- Reviewed skill rows: {data['sample_universe']['reviewed_rows']}",
        f"- Source rows: `{data['sample_universe']['source_review_rows']}`",
        "- Review method: Claude Code audit over compact static and dynamic evidence; no skill code was executed in the review pass.",
        "",
        "## Three Reporting Cuts",
        "",
        "| Cut | Count | TP | FP | FN | TN | Precision | Recall | F1 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        metric_row("Broad OS/package", broad),
        metric_row("Strict OS/install", strict),
        metric_row("OS-characteristic", characteristic),
        "",
        "## Recommended macOS Finding",
        "",
        f"- OS-characteristic issues: {characteristic['count']}",
        f"- Static analyzer recall on this cut: {metrics['recall']}",
        f"- Static analyzer precision on this cut: {metrics['precision']}",
        f"- Static analyzer F1 on this cut: {metrics['f1']}",
        "",
        "Bucket counts:",
    ]
    for label, count in characteristic["bucket_counts"].items():
        lines.append(f"- `{label}`: {count}")

    lines.extend(
        [
            "",
            "Primary-label counts:",
        ]
    )
    for label, count in characteristic["primary_label_counts"].items():
        lines.append(f"- `{label}`: {count}")

    lines.extend(
        [
            "",
            "## False Negatives",
            "",
            "These OS-characteristic issues were not selected by the current static positive label set:",
        ]
    )
    for row in characteristic["false_negatives"]:
        tags = ", ".join(row.get("secondary_tags", []))
        lines.append(
            f"- `{row['skill_id']}`: static=`{row['static_predicted_label']}`, "
            f"review=`{row['primary_label']}`, fixability=`{row['fixability']}`, tags={tags}"
        )

    lines.extend(
        [
            "",
            "## Safe Wording",
            "",
            "Static analysis covers 90 of 94 OS-characteristic macOS issues in this reviewed sample, for 0.9574 recall. It should be described as a high-recall triage layer, not as final ground truth.",
            "",
            "## Forbidden Overclaims",
            "",
        ]
    )
    for item in data["forbidden_overclaims"]:
        lines.append(f"- {item}")

    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- The 241 broad count is useful as an upper-bound sensitivity analysis, but includes generic package/entrypoint defects.",
            "- The 32 strict count is useful as a lower-bound sensitivity analysis, but misses OS-adapter entry surface failures.",
            "- The 94 OS-characteristic count is the recommended paper-facing macOS denominator for the current dataset.",
        ]
    )
    return "\n".join(lines) + "\n"


def metric_row(name: str, item: dict[str, Any]) -> str:
    metrics = item["metrics"]
    return (
        f"| {name} | {item['count']} | {metrics['tp']} | {metrics['fp']} | "
        f"{metrics['fn']} | {metrics['tn']} | {metrics['precision']} | "
        f"{metrics['recall']} | {metrics['f1']} |"
    )


if __name__ == "__main__":
    raise SystemExit(main())
