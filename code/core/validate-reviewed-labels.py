#!/usr/bin/env python3
"""Validate reviewed OS compatibility labels.

The validator is read-only. It checks JSONL syntax, required fields, enum
values, duplicate rows, and the scope/label consistency rules used by the
reviewed OS compatibility dataset.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LABELS = REPO_ROOT / "docs" / "linux-testing" / "datasets" / "os-compat-reviewed-labels.jsonl"

REQUIRED_FIELDS = {
    "schema_version",
    "label_quality",
    "reviewed_at",
    "source_run_id",
    "skill_id",
    "target_os",
    "task_result",
    "scope_status",
    "primary_label",
    "evidence_level",
    "fixability",
    "confidence",
    "evidence_refs",
    "notes",
}

TARGET_OS = {"linux", "macos", "windows"}
SCOPE_STATUS = {
    "in_scope",
    "excluded_external_credential_or_service",
    "excluded_benchmark_capability_gap",
}
PRIMARY_LABELS = {
    "os_specific_execution_issue",
    "os_install_or_dependency_contract_issue",
    "package_or_entrypoint_issue",
    "not_os_issue",
}
EVIDENCE_LEVELS = {"guide_only", "dynamic_execution", "both", "static_only"}
CONFIDENCE = {"low", "medium", "high"}
LABEL_QUALITY = {"reviewed_seed", "silver_reviewed", "gold_reviewed"}
FIXABILITY = {
    "not_applicable",
    "code_fix",
    "code_fix_non_os",
    "guide_fix",
    "package_fix",
    "install_contract_fix",
    "env_fix",
    "runner_or_task_fix",
    "need_credentials_or_service",
    "need_external_service_or_distribution",
    "not_skill_fix",
    "uncertain",
    "small_skill_script_fix",
    "small_skill_guide_fix",
    "small_skill_metadata_fix",
    "package_entrypoint_fix",
    "requires_environment_provisioning",
    "not_functionally_fixable_for_macos",
    "not_os_issue",
    "need_more_evidence",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "labels",
        nargs="?",
        type=Path,
        default=DEFAULT_LABELS,
        help="Reviewed labels JSONL path.",
    )
    parser.add_argument("--summary-json", type=Path, help="Optional path to write validation summary JSON.")
    args = parser.parse_args()

    errors: list[str] = []
    rows = load_rows(args.labels, errors)
    validate_rows(rows, errors)

    summary = build_summary(rows, errors)
    if args.summary_json:
        args.summary_json.parent.mkdir(parents=True, exist_ok=True)
        args.summary_json.write_text(json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")

    print_summary(summary)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


def load_rows(path: Path, errors: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        errors.append(f"labels file not found: {path}")
        return rows

    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"line {line_no}: invalid JSON: {exc}")
            continue
        if not isinstance(row, dict):
            errors.append(f"line {line_no}: row must be a JSON object")
            continue
        row["_line_no"] = line_no
        rows.append(row)
    return rows


def validate_rows(rows: list[dict[str, Any]], errors: list[str]) -> None:
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        line = row.get("_line_no", "?")
        missing = sorted(REQUIRED_FIELDS - row.keys())
        if missing:
            errors.append(f"line {line}: missing required fields: {', '.join(missing)}")
            continue

        check_enum(row, "schema_version", {"os_compat_reviewed_label_v1"}, errors)
        check_enum(row, "label_quality", LABEL_QUALITY, errors)
        check_enum(row, "target_os", TARGET_OS, errors)
        check_enum(row, "scope_status", SCOPE_STATUS, errors)
        check_enum(row, "primary_label", PRIMARY_LABELS, errors)
        check_enum(row, "evidence_level", EVIDENCE_LEVELS, errors)
        check_enum(row, "fixability", FIXABILITY, errors)
        check_enum(row, "confidence", CONFIDENCE, errors)

        key = (str(row["source_run_id"]), str(row["target_os"]), str(row["skill_id"]))
        if key in seen:
            errors.append(f"line {line}: duplicate source_run_id/target_os/skill_id: {key}")
        seen.add(key)

        if row["scope_status"] == "excluded_external_credential_or_service":
            if row["primary_label"] != "not_os_issue":
                errors.append(f"line {line}: excluded rows must use primary_label=not_os_issue")
            if row["fixability"] not in {"need_credentials_or_service", "need_external_service_or_distribution", "not_skill_fix", "uncertain"}:
                errors.append(f"line {line}: excluded rows should use an external-service fixability")

        if row["scope_status"] == "excluded_benchmark_capability_gap":
            if row["primary_label"] != "not_os_issue":
                errors.append(f"line {line}: benchmark-capability exclusions must use primary_label=not_os_issue")
            if row["fixability"] not in {"requires_environment_provisioning", "runner_or_task_fix", "not_skill_fix", "uncertain"}:
                errors.append(f"line {line}: benchmark-capability exclusions should use an environment/runner fixability")

        if row["scope_status"] == "in_scope" and row["primary_label"] != "not_os_issue":
            if row["fixability"] in {"not_applicable", "need_credentials_or_service", "need_external_service_or_distribution"}:
                errors.append(f"line {line}: OS issue rows need a skill/env/runner fixability, not {row['fixability']}")

        if not isinstance(row["evidence_refs"], list) or not all(isinstance(item, str) for item in row["evidence_refs"]):
            errors.append(f"line {line}: evidence_refs must be a list of strings")


def check_enum(row: dict[str, Any], field: str, allowed: set[str], errors: list[str]) -> None:
    value = row.get(field)
    if value not in allowed:
        line = row.get("_line_no", "?")
        errors.append(f"line {line}: invalid {field}={value!r}; allowed={sorted(allowed)}")


def build_summary(rows: list[dict[str, Any]], errors: list[str]) -> dict[str, Any]:
    clean_rows = [{k: v for k, v in row.items() if k != "_line_no"} for row in rows]
    in_scope = [row for row in clean_rows if row.get("scope_status") == "in_scope"]
    os_issue_labels = {
        "os_specific_execution_issue",
        "os_install_or_dependency_contract_issue",
        "package_or_entrypoint_issue",
    }
    return {
        "schema_version": "reviewed_label_validation_summary_v1",
        "rows": len(clean_rows),
        "unique_skills": len({(row.get("target_os"), row.get("skill_id")) for row in clean_rows}),
        "scope_counts": dict(Counter(row.get("scope_status") for row in clean_rows)),
        "primary_label_counts": dict(Counter(row.get("primary_label") for row in clean_rows)),
        "in_scope_primary_label_counts": dict(Counter(row.get("primary_label") for row in in_scope)),
        "os_issue_count": sum(1 for row in in_scope if row.get("primary_label") in os_issue_labels),
        "os_issue_skills": sorted(row["skill_id"] for row in in_scope if row.get("primary_label") in os_issue_labels),
        "error_count": len(errors),
    }


def print_summary(summary: dict[str, Any]) -> None:
    print(f"rows: {summary['rows']}")
    print(f"unique_skills: {summary['unique_skills']}")
    print(f"scope_counts: {summary['scope_counts']}")
    print(f"in_scope_primary_label_counts: {summary['in_scope_primary_label_counts']}")
    print(f"os_issue_count: {summary['os_issue_count']}")
    if summary["os_issue_skills"]:
        print("os_issue_skills:")
        for skill_id in summary["os_issue_skills"]:
            print(f"  - {skill_id}")
    print(f"error_count: {summary['error_count']}")


if __name__ == "__main__":
    raise SystemExit(main())
