#!/usr/bin/env python3
"""Build a canonical repair-facing cohort for the finalized RQ1 skill set.

The output unit is one target-OS x normalized skill-family strong OS issue.
This script writes generated cohort files under data/generated and does not
mutate raw source artifacts.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import csv
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SKILLCOMPTEST = ROOT / "skillcomptest"
NOTES = ROOT / "data" / "generated" / "notes"

DYNAMIC = SKILLCOMPTEST / "docs/cross-os-testing/datasets/unified-dynamic-skill-labels.jsonl"
CURRENT_INVENTORY = (
    SKILLCOMPTEST / "docs/cross-os-testing/analysis/current/three-os-clean-os-issue-inventory-20260626-v2.jsonl"
)
NEW_LABELS = NOTES / "three-os-missing-review-labels-20260627.jsonl"
RQ1_CLEAN = NOTES / "three-os-rq1-clean-data-20260627.json"

OUT_PREFIX = NOTES / "three-os-repair-cohort-20260627"
OUT_JSONL = OUT_PREFIX.with_suffix(".jsonl")
OUT_CSV = OUT_PREFIX.with_suffix(".csv")
OUT_MD = OUT_PREFIX.with_suffix(".md")
OUT_SUMMARY_JSON = OUT_PREFIX.with_suffix(".summary.json")
OUT_FIRST_BATCH_JSONL = NOTES / "three-os-repair-cohort-first-batch-20260627.jsonl"
OUT_FIRST_BATCH_MD = NOTES / "three-os-repair-cohort-first-batch-20260627.md"
OUT_SUPPORT_JSON = NOTES / "three-os-repair-cohort-support-matrix-20260627.json"
OUT_SUPPORT_MD = NOTES / "three-os-repair-cohort-support-matrix-20260627.md"

OS_ORDER = ["windows", "linux", "macos"]
EXPECTED_COUNTS = {"linux": 61, "macos": 38, "windows": 106}
FIRST_BATCH_LIMIT = 40
FIRST_BATCH_OS_QUOTA = {"windows": 16, "linux": 12, "macos": 12}

SOURCE_ROOTS = [
    SKILLCOMPTEST / ".cache/skillcomptest/SkillsRepo",
    SKILLCOMPTEST / ".cache/skillcomptest/top5000-sources",
]


def main() -> int:
    NOTES.mkdir(parents=True, exist_ok=True)
    dynamic = read_jsonl(DYNAMIC)
    common = common_families(dynamic)
    family_index = build_family_index(dynamic)
    dynamic_index = build_dynamic_index(dynamic)

    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for row in read_jsonl(CURRENT_INVENTORY):
        item = from_current_inventory(row, common, family_index, dynamic_index)
        if item:
            rows[(item["target_os"], item["slug_family"])] = item

    for row in read_jsonl(NEW_LABELS):
        item = from_new_label(row, common, dynamic_index)
        if item:
            key = (item["target_os"], item["slug_family"])
            if key in rows:
                rows[key] = merge_duplicate(rows[key], item)
            else:
                rows[key] = item

    cohort = sorted(rows.values(), key=lambda r: (os_rank(r["target_os"]), r["slug_family"]))
    enrich_for_repair(cohort)
    first_batch = select_first_batch(cohort)
    summary = build_summary(cohort, first_batch, common)
    support = build_support_matrix(cohort, summary)

    write_jsonl(OUT_JSONL, cohort)
    write_csv(OUT_CSV, cohort)
    write_json(OUT_SUMMARY_JSON, summary)
    OUT_MD.write_text(render_summary(summary, cohort), encoding="utf-8")
    write_jsonl(OUT_FIRST_BATCH_JSONL, first_batch)
    OUT_FIRST_BATCH_MD.write_text(render_first_batch(first_batch), encoding="utf-8")
    write_json(OUT_SUPPORT_JSON, support)
    OUT_SUPPORT_MD.write_text(render_support(support), encoding="utf-8")

    ok = summary["validation"]["matches_expected_counts"] and not summary["validation"]["duplicate_keys"]
    print(
        json.dumps(
            {
                "rows": len(cohort),
                "by_os": summary["by_os"],
                "first_batch": len(first_batch),
                "matches_expected_counts": summary["validation"]["matches_expected_counts"],
                "duplicate_keys": len(summary["validation"]["duplicate_keys"]),
                "out": rel(OUT_JSONL),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if ok else 1


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "cohort_key",
        "target_os",
        "slug_family",
        "representative_skill_id",
        "all_skill_ids",
        "source_dir",
        "paper_category",
        "raw_os_issue_class",
        "repair_strategy",
        "repair_tier",
        "repair_priority",
        "first_batch_candidate",
        "fixability_class",
        "dynamic_outcome",
        "success_rate",
        "short_issue",
        "repair_plan",
        "source_artifacts",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: csv_value(row.get(field)) for field in fields})


def csv_value(value: Any) -> str:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return "" if value is None else str(value)


def rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


def family(row: dict[str, Any]) -> str:
    return str(row.get("slug_family") or row.get("skill_id") or "")


def common_families(rows: list[dict[str, Any]]) -> set[str]:
    by_os: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        by_os[str(row.get("target_os"))].add(family(row))
    return set.intersection(by_os["linux"], by_os["macos"], by_os["windows"])


def build_family_index(rows: list[dict[str, Any]]) -> dict[tuple[str | None, str], str]:
    index: dict[tuple[str | None, str], str] = {}
    for row in rows:
        skill_id = row.get("skill_id")
        target_os = row.get("target_os")
        if not skill_id:
            continue
        fam = family(row)
        index[(str(target_os), str(skill_id))] = fam
        index[(None, str(skill_id))] = fam
    return index


def build_dynamic_index(rows: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    index: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        index[(str(row.get("target_os")), family(row))].append(row)
    return index


def source_dir(skill_id: str | None) -> str:
    if not skill_id or "/" not in skill_id:
        return ""
    source, package = skill_id.split("/", 1)
    for root in SOURCE_ROOTS:
        path = root / source / package
        if path.exists():
            return rel(path)
    return ""


def from_current_inventory(
    row: dict[str, Any],
    common: set[str],
    family_index: dict[tuple[str | None, str], str],
    dynamic_index: dict[tuple[str, str], list[dict[str, Any]]],
) -> dict[str, Any] | None:
    target_os = str(row.get("target_os") or "")
    skill_id = str(row.get("skill_id") or "")
    fam = family_index.get((target_os, skill_id)) or family_index.get((None, skill_id)) or skill_id
    if not target_os or not skill_id or fam not in common:
        return None
    if row.get("paper_category") == "package_entrypoint_contract":
        return None
    dyn = choose_dynamic_record(dynamic_index.get((target_os, fam), []), skill_id)
    return {
        "schema_version": "three_os_repair_cohort_v1",
        "cohort_key": f"{target_os}:{fam}",
        "target_os": target_os,
        "slug_family": fam,
        "representative_skill_id": skill_id,
        "all_skill_ids": [skill_id],
        "source_dir": source_dir(skill_id),
        "paper_category": normalize_category(str(row.get("paper_category") or "")),
        "paper_category_label": row.get("paper_category_label", ""),
        "raw_os_issue_class": row.get("raw_os_issue_class", ""),
        "fixability_class": row.get("fixability_class", ""),
        "dynamic_outcome": row.get("dynamic_outcome") or dyn.get("dynamic_outcome", ""),
        "success_rate": row.get("success_rate") or dyn.get("success_rate", ""),
        "task_counts": dyn.get("task_counts", {}),
        "short_issue": row.get("short_issue", ""),
        "repair_plan": row.get("repair_implication", ""),
        "source_artifacts": [f"current_inventory:{row.get('source_file', '')}"],
        "label_origin": "current_inventory",
        "review_confidence": "",
        "evidence_refs": [],
    }


def from_new_label(
    row: dict[str, Any],
    common: set[str],
    dynamic_index: dict[tuple[str, str], list[dict[str, Any]]],
) -> dict[str, Any] | None:
    if row.get("verdict") != "confirmed_os_issue":
        return None
    target_os = str(row.get("target_os") or "")
    skill_id = str(row.get("skill_id") or "")
    fam = str(row.get("slug_family") or skill_id)
    if not target_os or not skill_id or fam not in common:
        return None
    category = normalize_category(category_from_issue_class(str(row.get("os_issue_class") or "")))
    if category == "package_entrypoint_contract":
        return None
    dyn = choose_dynamic_record(dynamic_index.get((target_os, fam), []), skill_id)
    return {
        "schema_version": "three_os_repair_cohort_v1",
        "cohort_key": f"{target_os}:{fam}",
        "target_os": target_os,
        "slug_family": fam,
        "representative_skill_id": skill_id,
        "all_skill_ids": [skill_id],
        "source_dir": source_dir(skill_id),
        "paper_category": category,
        "paper_category_label": category_label(category),
        "raw_os_issue_class": row.get("os_issue_class", ""),
        "fixability_class": row.get("fixability_class", ""),
        "dynamic_outcome": dyn.get("dynamic_outcome", ""),
        "success_rate": dyn.get("success_rate", ""),
        "task_counts": dyn.get("task_counts", {}),
        "short_issue": row.get("notes", ""),
        "repair_plan": row.get("suggested_action", ""),
        "source_artifacts": ["new_missing_review:three-os-missing-review-labels-20260627.jsonl"],
        "label_origin": "new_missing_review",
        "review_confidence": row.get("confidence", ""),
        "evidence_refs": row.get("evidence_refs", []),
    }


def merge_duplicate(base: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    merged["all_skill_ids"] = sorted(set(base.get("all_skill_ids", [])) | set(item.get("all_skill_ids", [])))
    merged["source_artifacts"] = sorted(set(base.get("source_artifacts", [])) | set(item.get("source_artifacts", [])))
    if item.get("label_origin") == "new_missing_review":
        merged["label_origin"] = "current_inventory+new_missing_review"
    merged["short_issue"] = combine_text(base.get("short_issue", ""), item.get("short_issue", ""))
    merged["repair_plan"] = combine_text(base.get("repair_plan", ""), item.get("repair_plan", ""))
    return merged


def combine_text(a: str, b: str) -> str:
    if not a:
        return b
    if not b or b in a:
        return a
    return f"{a} | {b}"


def choose_dynamic_record(rows: list[dict[str, Any]], skill_id: str) -> dict[str, Any]:
    if not rows:
        return {}
    exact = [row for row in rows if row.get("skill_id") == skill_id]
    if exact:
        return exact[0]
    nonpass = [row for row in rows if row.get("dynamic_outcome") != "all_pass"]
    return nonpass[0] if nonpass else rows[0]


def category_from_issue_class(klass: str) -> str:
    k = klass.lower()
    if "package_entrypoint" in k:
        return "package_entrypoint_contract"
    if any(token in k for token in ["brew", "install", "dependency", "python3", "secret_service", "keyring"]):
        return "install_dependency_contract"
    if any(token in k for token in ["bash", "gnu", "bsd", "shell", "path", "posix", "cmd", "command", "tempfile", "max_path", "entrypoint", "date", "stat"]):
        return "shell_path_or_command_assumption"
    if any(token in k for token in ["browser", "gui", "display", "x11", "desktop", "window", "app", "xvfb", "fail2ban", "systemd"]):
        return "platform_native_api_or_app"
    if any(token in k for token in ["permission", "tcc", "session", "accessibility"]):
        return "platform_native_api_or_permission"
    if "metadata" in k or "advertised_for" in k or "support_metadata" in k:
        return "target_os_mismatch"
    return "shell_path_or_command_assumption"


def normalize_category(category: str) -> str:
    if category == "shell_path_command_assumption":
        return "shell_path_or_command_assumption"
    if category == "permission_session_model":
        return "platform_native_api_or_permission"
    if category == "browser_gui_runtime":
        return "platform_native_api_or_app"
    return category or "unclassified_confirmed_os_issue"


def category_label(category: str) -> str:
    return {
        "install_dependency_contract": "Install/dependency contract",
        "platform_native_api_or_app": "Platform-native API/app",
        "platform_native_api_or_permission": "Permission/session model",
        "shell_path_or_command_assumption": "Shell/path/command assumption",
        "target_os_mismatch": "Support metadata mismatch",
        "encoding_locale_path": "Encoding/locale/path",
    }.get(category, "Confirmed OS issue")


def enrich_for_repair(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        strategy, tier, priority, plan = classify_repair(row)
        row["repair_strategy"] = strategy
        row["repair_tier"] = tier
        row["repair_priority"] = priority
        if not row.get("repair_plan"):
            row["repair_plan"] = plan
        row["first_batch_candidate"] = tier in {"small_functional_fix", "contract_fix"} and priority in {"P0", "P1"}


def classify_repair(row: dict[str, Any]) -> tuple[str, str, str, str]:
    category = row.get("paper_category", "")
    fixability = str(row.get("fixability_class", "")).lower()
    raw = str(row.get("raw_os_issue_class", "")).lower()
    issue = str(row.get("short_issue", "")).lower()

    if category in {"shell_path_or_command_assumption", "encoding_locale_path"}:
        return (
            "portable_command_path_or_shell_fix",
            "small_functional_fix",
            "P0",
            "Add a portable wrapper or target-OS command branch; remove POSIX/Bash/BSD/GNU/path assumptions.",
        )
    if category == "install_dependency_contract":
        return (
            "dependency_install_contract_fix",
            "contract_fix",
            "P1",
            "Add target-OS install instructions, dependency metadata, runtime checks, or a packaged wrapper.",
        )
    if category == "target_os_mismatch":
        return (
            "metadata_support_contract_fix",
            "contract_fix",
            "P1",
            "Align supported-platform metadata, guide text, and task targeting with the executable contract.",
        )
    if category == "platform_native_api_or_permission":
        return (
            "permission_session_or_environment_contract",
            "environment_or_permission_contract",
            "P2",
            "Document and provision target-OS permissions/session/runtime, or gate unsupported tasks.",
        )
    if category == "platform_native_api_or_app":
        if "not_portable_by_design" in fixability or "macos-only" in issue or "windows-only" in issue:
            return (
                "platform_gating_or_adapter_research",
                "metadata_gating_or_adapter_research",
                "P3",
                "Gate the skill to supported OSes, or design a platform adapter if cross-OS semantics are justified.",
            )
        if any(token in raw for token in ["display", "x11", "browser", "desktop", "gui", "xvfb"]):
            return (
                "gui_runtime_or_platform_adapter",
                "environment_or_adapter_contract",
                "P2",
                "Add display/browser/session provisioning or implement a target-OS platform adapter.",
            )
        return (
            "platform_adapter_or_gating",
            "metadata_gating_or_adapter_research",
            "P3",
            "Add a target-OS adapter where feasible; otherwise make unsupported status explicit.",
        )
    return (
        "manual_repair_triage",
        "needs_manual_repair_triage",
        "P3",
        "Inspect the source and decide whether this is a small fix, contract fix, adapter, or OS gating issue.",
    )


def select_first_batch(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    scored = []
    for row in rows:
        score = 0
        if row.get("first_batch_candidate"):
            score += 100
        if row.get("repair_priority") == "P0":
            score += 30
        elif row.get("repair_priority") == "P1":
            score += 20
        if row.get("dynamic_outcome") in {"all_blocked", "all_unsupported", "mixed"}:
            score += 5
        if row.get("source_dir"):
            score += 3
        scored.append((score, os_rank(row["target_os"]), row["slug_family"], row))
    ranked = sorted(scored, key=lambda item: (-item[0], item[1], item[2]))
    selected: list[dict[str, Any]] = []
    selected_keys: set[str] = set()
    for os_name in OS_ORDER:
        quota = FIRST_BATCH_OS_QUOTA.get(os_name, 0)
        eligible = [row for score, _, _, row in ranked if score >= 100 and row["target_os"] == os_name]
        for row in eligible[:quota]:
            selected.append(row)
            selected_keys.add(row["cohort_key"])
    if len(selected) < FIRST_BATCH_LIMIT:
        for score, _, _, row in ranked:
            if score < 100 or row["cohort_key"] in selected_keys:
                continue
            selected.append(row)
            selected_keys.add(row["cohort_key"])
            if len(selected) >= FIRST_BATCH_LIMIT:
                break
    return selected[:FIRST_BATCH_LIMIT]


def build_summary(rows: list[dict[str, Any]], first_batch: list[dict[str, Any]], common: set[str]) -> dict[str, Any]:
    keys = [(row["target_os"], row["slug_family"]) for row in rows]
    by_os = {os_name: sum(1 for row in rows if row["target_os"] == os_name) for os_name in ["linux", "macos", "windows"]}
    duplicate_keys = [key for key, count in Counter(keys).items() if count > 1]
    return {
        "schema_version": "three_os_repair_cohort_summary_v1",
        "created_at": "2026-06-27",
        "unit": "target_os x normalized skill family confirmed strong OS-related issue",
        "common_family_denominator": len(common),
        "total_rows": len(rows),
        "by_os": by_os,
        "repair_strategy_counts": dict(Counter(row["repair_strategy"] for row in rows)),
        "repair_tier_counts": dict(Counter(row["repair_tier"] for row in rows)),
        "repair_priority_counts": dict(Counter(row["repair_priority"] for row in rows)),
        "paper_category_counts": {
            os_name: dict(Counter(row["paper_category"] for row in rows if row["target_os"] == os_name))
            for os_name in ["linux", "macos", "windows"]
        },
        "label_origin_counts": dict(Counter(row["label_origin"] for row in rows)),
        "source_dir_available_rows": sum(1 for row in rows if row.get("source_dir")),
        "first_batch_rows": len(first_batch),
        "first_batch_by_os": {
            os_name: sum(1 for row in first_batch if row["target_os"] == os_name)
            for os_name in ["linux", "macos", "windows"]
        },
        "validation": {
            "expected_counts": EXPECTED_COUNTS,
            "matches_expected_counts": by_os == EXPECTED_COUNTS,
            "duplicate_keys": [{"target_os": os_name, "slug_family": fam} for os_name, fam in duplicate_keys],
        },
        "sources": {
            "dynamic": rel(DYNAMIC),
            "current_inventory": rel(CURRENT_INVENTORY),
            "new_missing_review_labels": rel(NEW_LABELS),
            "rq1_clean_data": rel(RQ1_CLEAN),
        },
    }


def build_support_matrix(rows: list[dict[str, Any]], summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "three_os_repair_cohort_support_matrix_v1",
        "created_at": "2026-06-27",
        "claims": [
            {
                "claim": "The repair cohort uses the same strong OS issue counts as the paper RQ1 tables.",
                "status": "support" if summary["validation"]["matches_expected_counts"] else "fail",
                "evidence": rel(OUT_SUMMARY_JSON),
                "safe_wording": "Use 205 target-OS x skill-family issue rows: Windows 106, Linux 61, macOS 38.",
                "forbidden_wording": "Do not use dynamic non-pass rows or broad/package artifact counts as the repair cohort.",
            },
            {
                "claim": "Rows are normalized by skill family, not package-version row.",
                "status": "support" if not summary["validation"]["duplicate_keys"] else "fail",
                "evidence": rel(OUT_JSONL),
                "safe_wording": "Each row is keyed by target_os:slug_family.",
                "forbidden_wording": "Do not open separate repair rows for duplicate versions unless the repair implementation needs version-specific tracking.",
            },
            {
                "claim": "The first repair batch is a prioritization layer, not the full problem universe.",
                "status": "support",
                "evidence": rel(OUT_FIRST_BATCH_JSONL),
                "safe_wording": "Use the first batch for initial RQ2 repair execution and keep the full cohort as the denominator.",
                "forbidden_wording": "Do not report first-batch repair success as success over all 205 issue rows.",
            },
        ],
    }


def render_summary(summary: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    lines = [
        "# Three-OS Repair Cohort",
        "",
        f"Unit: `{summary['unit']}`.",
        f"Shared denominator: {summary['common_family_denominator']} normalized skill families.",
        "",
        "## Headline Counts",
        "",
        "| OS | Strong OS issue rows |",
        "| --- | ---: |",
    ]
    for os_name in OS_ORDER:
        lines.append(f"| {os_name} | {summary['by_os'].get(os_name, 0)} |")
    lines.extend(
        [
            f"| total | {summary['total_rows']} |",
            "",
            "Validation: "
            + ("matches paper RQ1 counts." if summary["validation"]["matches_expected_counts"] else "count mismatch; inspect summary JSON."),
            "",
            "## Repair Tiers",
            "",
            "| Tier | Rows |",
            "| --- | ---: |",
        ]
    )
    for tier, count in Counter(row["repair_tier"] for row in rows).most_common():
        lines.append(f"| `{tier}` | {count} |")
    lines.extend(["", "## Repair Strategies", "", "| Strategy | Rows |", "| --- | ---: |"])
    for strategy, count in Counter(row["repair_strategy"] for row in rows).most_common():
        lines.append(f"| `{strategy}` | {count} |")
    lines.extend(["", "## By OS and Category", ""])
    for os_name in OS_ORDER:
        lines.append(f"### {os_name}")
        lines.append("")
        lines.append("| Category | Rows |")
        lines.append("| --- | ---: |")
        for category, count in Counter(row["paper_category"] for row in rows if row["target_os"] == os_name).most_common():
            lines.append(f"| `{category}` | {count} |")
        lines.append("")
    lines.extend(
        [
            "## First Repair Batch",
            "",
            f"First batch rows: {summary['first_batch_rows']}. This is a stratified prioritized execution slice, not the denominator for RQ2.",
            "",
            "Source artifacts:",
            f"- `{summary['sources']['current_inventory']}`",
            f"- `{summary['sources']['new_missing_review_labels']}`",
            f"- `{summary['sources']['rq1_clean_data']}`",
        ]
    )
    return "\n".join(lines) + "\n"


def render_first_batch(rows: list[dict[str, Any]]) -> str:
    lines = [
        "# Three-OS Repair Cohort: First Batch",
        "",
        "This stratified batch prioritizes small functional fixes and contract fixes while keeping coverage across Windows, Linux, and macOS. It should be used for the first RQ2 repair run, while the full 205-row cohort remains the denominator.",
        "",
        "| OS | Skill family | Strategy | Priority | Issue |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        issue = str(row.get("short_issue", "")).replace("|", "\\|")
        if len(issue) > 170:
            issue = issue[:167] + "..."
        lines.append(
            f"| {row['target_os']} | `{row['slug_family']}` | `{row['repair_strategy']}` | "
            f"{row['repair_priority']} | {issue} |"
        )
    return "\n".join(lines) + "\n"


def render_support(support: dict[str, Any]) -> str:
    lines = ["# Three-OS Repair Cohort Support Matrix", "", "| Claim | Status | Safe wording | Forbidden wording |", "| --- | --- | --- | --- |"]
    for row in support["claims"]:
        lines.append(
            f"| {row['claim']} | `{row['status']}` | {row['safe_wording']} | {row['forbidden_wording']} |"
        )
    return "\n".join(lines) + "\n"


def os_rank(os_name: str) -> int:
    return OS_ORDER.index(os_name) if os_name in OS_ORDER else 99


if __name__ == "__main__":
    raise SystemExit(main())
