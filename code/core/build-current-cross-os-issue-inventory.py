#!/usr/bin/env python3
"""Build the current clean cross-OS issue inventory and paper-facing summaries.

This script keeps the historical 2026-06-26 inventory intact and writes a v2
inventory that incorporates the Windows common-denominator adjudicated
additions. It also writes a separate three-OS-common-denominator summary so
paper-facing prevalence claims use a single sample universe.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import textwrap
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OLD_INVENTORY = REPO_ROOT / "docs/cross-os-testing/analysis/provenance/three-os-clean-os-issue-inventory-20260626.jsonl"
DEFAULT_WINDOWS_ADDITIONS = REPO_ROOT / "docs/cross-os-testing/analysis/provenance/windows-common-accepted-os-issue-additions-20260626.jsonl"
DEFAULT_DYNAMIC = REPO_ROOT / "docs/cross-os-testing/datasets/unified-dynamic-skill-labels.jsonl"
DEFAULT_OUT_PREFIX = REPO_ROOT / "docs/cross-os-testing/analysis/current/three-os-clean-os-issue-inventory-20260626-v2"
DEFAULT_COMMON_PREFIX = REPO_ROOT / "docs/cross-os-testing/analysis/current/three-os-common-denominator-os-issue-summary-20260626"
DEFAULT_WINDOWS_TABLE = REPO_ROOT / "docs/cross-os-testing/analysis/current/windows-common-os-issue-skills-20260626.csv"


FIELDNAMES = [
    "skill_id",
    "target_os",
    "issue_status",
    "evidence_status",
    "paper_category",
    "paper_category_label",
    "raw_os_issue_class",
    "fixability_class",
    "dynamic_outcome",
    "success_rate",
    "short_issue",
    "repair_implication",
    "source_file",
]

CATEGORY_LABELS = {
    "browser_gui_runtime": "Browser/GUI runtime contract",
    "encoding_locale_path": "Encoding/locale/path assumption",
    "install_dependency_contract": "Install/dependency contract",
    "package_entrypoint_contract": "Package/entrypoint contract",
    "permission_session_model": "Permission/session model",
    "platform_native_api_or_app": "Platform-native API/app dependency",
    "platform_native_api_or_permission": "Platform-native API/permission dependency",
    "shell_path_command_assumption": "Shell/path/command assumption",
    "shell_path_or_command_assumption": "Shell/path/command assumption",
    "target_os_mismatch": "Target OS metadata/contract mismatch",
}

REPAIR_BY_FIXABILITY = {
    "install_contract_fix": "Add a Windows install/dependency contract or document unsupported status explicitly.",
    "metadata_or_target_os_contract_fix": "Correct target-OS metadata or add a Windows-compatible contract.",
    "package_entrypoint_fix": "Ship a discoverable Windows-compatible entrypoint or wrapper.",
    "platform_adapter_or_not_portable_by_design": "Add a Windows platform adapter or explicitly declare the skill non-portable.",
    "runtime_contract_fix": "Define and package the required Windows runtime contract.",
    "script_portability_fix": "Replace POSIX-only shell/path assumptions or add a Windows-native wrapper.",
    "session_or_permission_adapter_fix": "Add a Windows session/permission adapter or document unsupported status.",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-inventory", type=Path, default=DEFAULT_OLD_INVENTORY)
    parser.add_argument("--windows-additions", type=Path, default=DEFAULT_WINDOWS_ADDITIONS)
    parser.add_argument("--dynamic-jsonl", type=Path, default=DEFAULT_DYNAMIC)
    parser.add_argument("--out-prefix", type=Path, default=DEFAULT_OUT_PREFIX)
    parser.add_argument("--common-prefix", type=Path, default=DEFAULT_COMMON_PREFIX)
    parser.add_argument("--windows-table", type=Path, default=DEFAULT_WINDOWS_TABLE)
    args = parser.parse_args()

    dynamic_rows = read_jsonl(args.dynamic_jsonl)
    dynamic_by_key, id_to_families, common_families, dynamic_family_counts = build_dynamic_indexes(dynamic_rows)

    old_rows = read_jsonl(args.old_inventory)
    addition_rows = [
        windows_addition_to_inventory(row, dynamic_by_key)
        for row in read_jsonl(args.windows_additions)
        if row.get("verdict") == "confirmed_os_issue"
    ]

    merged = merge_inventory(old_rows, addition_rows)
    write_jsonl(args.out_prefix.with_suffix(".jsonl"), merged)
    write_csv(args.out_prefix.with_suffix(".csv"), merged)

    common_summary = build_common_summary(merged, old_rows, addition_rows, id_to_families, common_families, dynamic_family_counts)
    write_json(args.common_prefix.with_suffix(".json"), common_summary)
    args.common_prefix.with_suffix(".md").write_text(render_common_summary_md(common_summary), encoding="utf-8")

    inventory_summary = build_inventory_summary(merged, old_rows, addition_rows)
    write_json(args.out_prefix.with_name(args.out_prefix.name + "-summary.json"), inventory_summary)
    args.out_prefix.with_name(args.out_prefix.name + "-summary.md").write_text(
        render_inventory_summary_md(inventory_summary),
        encoding="utf-8",
    )

    windows_rows = windows_common_issue_table(merged, id_to_families, common_families)
    write_csv(args.windows_table, windows_rows)
    args.windows_table.with_suffix(".md").write_text(render_windows_table_md(windows_rows), encoding="utf-8")

    print(
        json.dumps(
            {
                "merged_rows": len(merged),
                "windows_additions": len(addition_rows),
                "out_jsonl": rel(args.out_prefix.with_suffix(".jsonl")),
                "common_summary": rel(args.common_prefix.with_suffix(".json")),
                "windows_table": rel(args.windows_table),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def build_dynamic_indexes(
    rows: list[dict[str, Any]],
) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, set[str]], set[str], dict[str, int]]:
    dynamic_by_key = {}
    id_to_families: dict[str, set[str]] = defaultdict(set)
    families_by_os: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        target_os = str(row.get("target_os") or "")
        skill_id = str(row.get("skill_id") or "")
        family = str(row.get("slug_family") or skill_id)
        if not target_os or not skill_id:
            continue
        dynamic_by_key[(target_os, skill_id)] = row
        id_to_families[skill_id].add(family)
        families_by_os[target_os].add(family)
    common = set.intersection(families_by_os["linux"], families_by_os["macos"], families_by_os["windows"])
    counts = {target_os: len(families) for target_os, families in sorted(families_by_os.items())}
    return dynamic_by_key, id_to_families, common, counts


def windows_addition_to_inventory(row: dict[str, Any], dynamic_by_key: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    skill_id = str(row["skill_id"])
    dynamic = dynamic_by_key.get(("windows", skill_id), {})
    category = str(row.get("os_issue_class") or "uncategorized")
    fixability = str(row.get("fixability") or "")
    return {
        "skill_id": skill_id,
        "target_os": "windows",
        "issue_status": "confirmed",
        "evidence_status": "confirmed_positive_adjudicated",
        "paper_category": category,
        "paper_category_label": CATEGORY_LABELS.get(category, category.replace("_", " ").title()),
        "raw_os_issue_class": category,
        "fixability_class": fixability,
        "dynamic_outcome": str(dynamic.get("dynamic_outcome") or ""),
        "success_rate": "" if dynamic.get("success_rate") is None else str(dynamic.get("success_rate")),
        "short_issue": shorten(str(row.get("reason") or row.get("first_pass_notes") or "")),
        "repair_implication": REPAIR_BY_FIXABILITY.get(fixability, "Review the skill contract and add a Windows-compatible path if appropriate."),
        "source_file": "windows-common-accepted-os-issue-additions-20260626.jsonl",
    }


def merge_inventory(old_rows: list[dict[str, Any]], addition_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    seen: set[tuple[str, str]] = set()
    for row in old_rows + addition_rows:
        key = (str(row.get("target_os")), str(row.get("skill_id")))
        if key in seen:
            continue
        seen.add(key)
        result.append({field: row.get(field, "") for field in FIELDNAMES})
    return sorted(result, key=lambda row: (row["target_os"], row["skill_id"]))


def build_common_summary(
    merged: list[dict[str, Any]],
    old_rows: list[dict[str, Any]],
    addition_rows: list[dict[str, Any]],
    id_to_families: dict[str, set[str]],
    common_families: set[str],
    dynamic_family_counts: dict[str, int],
) -> dict[str, Any]:
    old_common = [row for row in old_rows if family_for(row["skill_id"], id_to_families) in common_families]
    merged_common = [row for row in merged if family_for(row["skill_id"], id_to_families) in common_families]
    additions_common = [row for row in addition_rows if family_for(row["skill_id"], id_to_families) in common_families]
    by_os = Counter(row["target_os"] for row in merged_common)
    strict_common = [row for row in merged_common if is_strict_os_row(row)]
    strict_by_os = Counter(row["target_os"] for row in strict_common)
    macos_package_artifact_common = [
        row
        for row in merged_common
        if row["target_os"] == "macos" and row["paper_category"] == "package_entrypoint_contract"
    ]
    return {
        "schema_version": "three_os_common_denominator_os_issue_summary_v1",
        "denominator": "three_os_slug_family_overlap",
        "three_os_common_family_count": len(common_families),
        "dynamic_family_counts": dynamic_family_counts,
        "old_common_confirmed_counts_by_os": dict(Counter(row["target_os"] for row in old_common)),
        "current_common_confirmed_counts_by_os": dict(by_os),
        "current_common_confirmed_rate_by_os": {
            target_os: round(count / len(common_families), 6) for target_os, count in sorted(by_os.items())
        },
        "current_common_strict_non_package_counts_by_os": dict(strict_by_os),
        "current_common_strict_non_package_rate_by_os": {
            target_os: round(count / len(common_families), 6) for target_os, count in sorted(strict_by_os.items())
        },
        "macos_download_artifact_common_count": len(macos_package_artifact_common),
        "windows_new_additions_in_common": len(additions_common),
        "windows_current_category_counts": dict(
            Counter(row["paper_category"] for row in merged_common if row["target_os"] == "windows")
        ),
        "linux_current_category_counts": dict(
            Counter(row["paper_category"] for row in merged_common if row["target_os"] == "linux")
        ),
        "macos_current_category_counts": dict(
            Counter(row["paper_category"] for row in merged_common if row["target_os"] == "macos")
        ),
        "safe_wording": [
            "On the three-OS shared denominator of 1,090 skill families, the current broad reviewed-label inventory contains 57 Linux, 57 macOS, and 98 Windows issue families.",
            "After excluding generic package/entrypoint labels, the same shared denominator contains 55 Linux, 23 macOS, and 96 Windows strict non-package OS issue families.",
            "The macOS broad count includes 34 package/entrypoint labels that should be treated as download/release-package artifacts rather than native macOS compatibility defects.",
            "Windows shows the highest reviewed OS-compatibility issue prevalence in the shared denominator, driven mainly by POSIX shell/path assumptions and platform-native app/API dependencies.",
            "The full-inventory counts use different OS-specific coverage and should not be used as prevalence comparisons.",
        ],
        "forbidden_wording": [
            "Do not compare full counts 61/92/121 as if they shared one denominator.",
            "Do not report macOS 57/1090 or 92 full-inventory labels as strict macOS OS compatibility issues.",
            "Do not treat macOS package_entrypoint_contract labels caused by an incorrect download as skill-intrinsic OS compatibility defects.",
            "Do not treat raw dynamic failures or first-pass raw positives as confirmed OS issues.",
            "Do not include deferred or rejected Windows raw positives in the confirmed inventory.",
        ],
        "source_files": {
            "old_inventory": "three-os-clean-os-issue-inventory-20260626.jsonl",
            "windows_additions": "windows-common-accepted-os-issue-additions-20260626.jsonl",
            "dynamic_labels": "docs/cross-os-testing/datasets/unified-dynamic-skill-labels.jsonl",
        },
    }


def build_inventory_summary(
    merged: list[dict[str, Any]],
    old_rows: list[dict[str, Any]],
    addition_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    strict_rows = [row for row in merged if is_strict_os_row(row)]
    macos_package_artifacts = [
        row for row in merged if row["target_os"] == "macos" and row["paper_category"] == "package_entrypoint_contract"
    ]
    return {
        "schema_version": "three_os_clean_inventory_v2_summary",
        "unit": "skill package x target OS label",
        "rows_total": len(merged),
        "old_rows_total": len(old_rows),
        "windows_additions": len(addition_rows),
        "counts_by_os": dict(Counter(row["target_os"] for row in merged)),
        "strict_non_package_counts_by_os": dict(Counter(row["target_os"] for row in strict_rows)),
        "macos_download_artifact_count": len(macos_package_artifacts),
        "macos_strict_non_package_count": Counter(row["target_os"] for row in strict_rows).get("macos", 0),
        "category_counts_by_os": {
            target_os: dict(Counter(row["paper_category"] for row in merged if row["target_os"] == target_os))
            for target_os in sorted({row["target_os"] for row in merged})
        },
        "notes": [
            "This v2 inventory supersedes the earlier 2026-06-26 inventory for current reporting.",
            "macOS package_entrypoint_contract labels are retained for audit, but evaluator feedback indicates they are primarily caused by an incorrect original download/release-package artifact.",
            "Use strict_non_package_counts_by_os for paper-facing OS-characteristic compatibility claims.",
            "Use the separate common-denominator summary for cross-OS prevalence claims.",
        ],
    }


def windows_common_issue_table(
    merged: list[dict[str, Any]],
    id_to_families: dict[str, set[str]],
    common_families: set[str],
) -> list[dict[str, Any]]:
    rows = []
    for row in merged:
        if row["target_os"] != "windows":
            continue
        family = family_for(row["skill_id"], id_to_families)
        if family not in common_families:
            continue
        rows.append(
            {
                "skill_id": row["skill_id"],
                "slug_family": family,
                "paper_category": row["paper_category"],
                "fixability_class": row["fixability_class"],
                "dynamic_outcome": row["dynamic_outcome"],
                "success_rate": row["success_rate"],
                "short_issue": row["short_issue"],
                "source_file": row["source_file"],
            }
        )
    return sorted(rows, key=lambda row: (row["paper_category"], row["skill_id"]))


def render_common_summary_md(summary: dict[str, Any]) -> str:
    lines = [
        "# Three-OS Common Denominator OS-Issue Summary",
        "",
        f"Denominator: `{summary['denominator']}`",
        f"Common skill families: {summary['three_os_common_family_count']}",
        "",
        "## Broad Reviewed-Label Counts",
        "",
        "| OS | Broad issue labels | Rate |",
        "|---|---:|---:|",
    ]
    for target_os in ["linux", "macos", "windows"]:
        count = summary["current_common_confirmed_counts_by_os"].get(target_os, 0)
        rate = summary["current_common_confirmed_rate_by_os"].get(target_os, 0)
        lines.append(f"| {target_os} | {count} | {rate:.2%} |")
    lines.extend(
        [
            "",
            "## Strict Non-Package Counts",
            "",
            "| OS | Strict non-package OS issues | Rate |",
            "|---|---:|---:|",
        ]
    )
    for target_os in ["linux", "macos", "windows"]:
        count = summary["current_common_strict_non_package_counts_by_os"].get(target_os, 0)
        rate = summary["current_common_strict_non_package_rate_by_os"].get(target_os, 0)
        lines.append(f"| {target_os} | {count} | {rate:.2%} |")
    lines.extend(
        [
            "",
            "macOS package/entrypoint labels in the shared denominator: "
            f"{summary['macos_download_artifact_common_count']}."
            " Treat these as download/release-package artifacts, not strict macOS compatibility defects.",
            "",
            "## Windows Categories",
            "",
        ]
    )
    for category, count in sorted(summary["windows_current_category_counts"].items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"- `{category}`: {count}")
    lines.extend(["", "## Safe Wording", ""])
    lines.extend(f"- {item}" for item in summary["safe_wording"])
    lines.extend(["", "## Forbidden Wording", ""])
    lines.extend(f"- {item}" for item in summary["forbidden_wording"])
    return "\n".join(lines) + "\n"


def render_inventory_summary_md(summary: dict[str, Any]) -> str:
    lines = [
        "# Three-OS Clean OS-Issue Inventory v2 Summary",
        "",
        f"Unit: `{summary['unit']}`",
        f"Rows total: {summary['rows_total']}",
        f"Previous rows total: {summary['old_rows_total']}",
        f"Windows additions: {summary['windows_additions']}",
        "",
        "## Broad Counts by OS",
        "",
        "| OS | Confirmed labels |",
        "|---|---:|",
    ]
    for target_os, count in sorted(summary["counts_by_os"].items()):
        lines.append(f"| {target_os} | {count} |")
    lines.extend(
        [
            "",
            "## Strict Non-Package Counts by OS",
            "",
            "| OS | Strict non-package labels |",
            "|---|---:|",
        ]
    )
    for target_os, count in sorted(summary["strict_non_package_counts_by_os"].items()):
        lines.append(f"| {target_os} | {count} |")
    lines.extend(
        [
            "",
            "## macOS Download Artifact Split",
            "",
            f"- macOS package/entrypoint labels retained for audit: {summary['macos_download_artifact_count']}",
            f"- macOS strict non-package OS labels: {summary['macos_strict_non_package_count']}",
        ]
    )
    lines.extend(["", "## Notes", ""])
    lines.extend(f"- {item}" for item in summary["notes"])
    return "\n".join(lines) + "\n"


def render_windows_table_md(rows: list[dict[str, Any]]) -> str:
    lines = [
        "# Windows Common OS-Issue Skills",
        "",
        f"Rows: {len(rows)}",
        "",
        "| Skill | Category | Issue |",
        "|---|---|---|",
    ]
    for row in rows:
        issue = str(row["short_issue"]).replace("|", "\\|")
        lines.append(f"| `{row['skill_id']}` | `{row['paper_category']}` | {issue} |")
    return "\n".join(lines) + "\n"


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
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


def family_for(skill_id: str, id_to_families: dict[str, set[str]]) -> str:
    families = id_to_families.get(skill_id, set())
    if families:
        return sorted(families)[0]
    if "/" in skill_id:
        source, package = skill_id.split("/", 1)
        package = re.sub(r"-\d+(?:\.\d+){1,3}(?:-[A-Za-z0-9.]+)?$", "", package)
        return f"{source}/{package}"
    return skill_id


def is_strict_os_row(row: dict[str, Any]) -> bool:
    """Exclude generic package/entrypoint defects from strict OS-characteristic counts."""

    return row.get("paper_category") != "package_entrypoint_contract"


def shorten(text: str, width: int = 320) -> str:
    text = " ".join(text.split())
    return textwrap.shorten(text, width=width, placeholder="...")


def rel(path: Path | str) -> str:
    path = Path(path)
    try:
        return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


if __name__ == "__main__":
    raise SystemExit(main())
