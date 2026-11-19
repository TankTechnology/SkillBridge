#!/usr/bin/env python3
"""Build a layered repairability view over strict OS issue labels.

This script is data-only. It does not execute skill code, setup scripts,
generated tasks, package managers, browsers, or GUI commands.
"""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
import json
from pathlib import Path
import re
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = REPO_ROOT.parent
DEFAULT_TEST_REPO = WORKSPACE_ROOT / "skillcomptest"
DATE_STAMP = "20260627"

DEFAULT_STRICT_LABELS = (
    DEFAULT_TEST_REPO
    / "docs/cross-os-testing/analysis/current/strict-os-issue-labels-20260627.jsonl"
)
DEFAULT_LEGACY_EXCLUSIONS = (
    REPO_ROOT / "docs/repair-workflow/legacy-repair-exclusions-20260626.txt"
)
DEFAULT_OUT_DIR = REPO_ROOT / "docs/repair-workflow"

SMALL_REPAIR_FIXABILITIES = {
    "install_contract_fix",
    "metadata_or_target_os_contract_fix",
    "runtime_contract_fix",
    "script_portability_fix",
    "small_script_or_path_fix",
    "small_skill_guide_fix",
    "small_skill_script_fix",
}

CONTRACT_OR_ADAPTER_RESEARCH = {
    ("linux", "ClawHub/desktop-control-win-1.0.1"),
    ("linux", "ClawHub/drawthings-1.0.0"),
    ("linux", "ClawHub/feishu-bridge-1.0.0"),
    ("linux", "ClawHub/linkedin-inbox-0.1.0"),
    ("linux", "ClawHub/windows-control-1.0.0"),
    ("linux", "SkillSh/containerize-aspnet-framework"),
    ("linux", "SkillSh/swiftui-expert-skill"),
    ("macos", "ClawHub/tiangong-wps-word-automation-0.1.0"),
    ("macos", "ClawHub/windows-control-1.0.0"),
    ("windows", "ClawHub/apple-mail-search-1.0.0"),
    ("windows", "ClawHub/apple-notes-1.0.0"),
    ("windows", "ClawHub/claude-code-wingman-0.3.0"),
    ("windows", "ClawHub/feishu-bridge-1.0.0"),
    ("windows", "ClawHub/linux-desktop-1.0.0"),
    ("windows", "ClawHub/macos-calendar-1.2.0"),
    ("windows", "ClawHub/peekaboo-1.0.0"),
    ("windows", "ClawHub/record-0.2.0"),
    ("windows", "ClawHub/turix-cua-1.0.8"),
    ("windows", "ClawHub/wechat-sender-1.0.1"),
    ("windows", "ClawHub/wps-office-1.0.1"),
    ("windows", "ClawHub/xlsx-cn-1.0.1"),
    ("windows", "ClawHub/youtube-transcript-1.0.1"),
    ("windows", "ClawHub/filesystem-1.0.0"),
    ("windows", "ClawHub/second-brain-0.1.4"),
}

FIELDNAMES = [
    "schema_version",
    "skill_id",
    "target_os",
    "paper_category",
    "fixability_class",
    "base_repair_candidate",
    "repairability_tier",
    "repair_claim_type",
    "count_for_active_queue",
    "count_for_near_term_functional_repair",
    "count_for_broader_skillfab_action",
    "count_for_harness_or_metadata_action",
    "recommended_action",
    "short_issue",
    "repair_implication",
    "source_file",
]


def main() -> int:
    rows = read_jsonl(DEFAULT_STRICT_LABELS)
    legacy = read_legacy_exclusions(DEFAULT_LEGACY_EXCLUSIONS)
    expanded = [expand_row(row, legacy) for row in rows]
    expanded.sort(key=lambda row: (row["target_os"], row["repairability_tier"], row["skill_id"]))
    summary = build_summary(expanded)

    DEFAULT_OUT_DIR.mkdir(parents=True, exist_ok=True)
    write_jsonl(DEFAULT_OUT_DIR / f"strict-repairability-expanded-{DATE_STAMP}.jsonl", expanded)
    write_csv(DEFAULT_OUT_DIR / f"strict-repairability-expanded-{DATE_STAMP}.csv", expanded)
    write_json(DEFAULT_OUT_DIR / f"strict-repairability-expanded-summary-{DATE_STAMP}.json", summary)
    (DEFAULT_OUT_DIR / f"strict-repairability-expanded-summary-{DATE_STAMP}.md").write_text(
        render_summary_md(summary), encoding="utf-8"
    )
    print(json.dumps({"rows": len(expanded), "summary": summary["headline_counts"]}, sort_keys=True))
    return 0


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def read_legacy_exclusions(path: Path) -> set[str]:
    values = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.split("#", 1)[0].strip()
        if value:
            values.add(value)
    return values


def expand_row(row: dict[str, Any], legacy: set[str]) -> dict[str, Any]:
    skill_id = str(row["skill_id"])
    target_os = str(row["target_os"])
    fixability = str(row.get("fixability_class") or "")
    base_repair_candidate = bool(row.get("repair_candidate"))
    legacy_excluded = matches_legacy_exclusion(skill_id, legacy)
    tier = classify_tier(row, legacy_excluded)
    claim_type = claim_type_for(tier)
    return {
        "schema_version": "strict_repairability_expanded_v1",
        "skill_id": skill_id,
        "target_os": target_os,
        "paper_category": row.get("paper_category", ""),
        "fixability_class": fixability,
        "base_repair_candidate": base_repair_candidate,
        "legacy_excluded": legacy_excluded,
        "repairability_tier": tier,
        "repair_claim_type": claim_type,
        "count_for_active_queue": tier == "current_candidate",
        "count_for_near_term_functional_repair": tier
        in {"current_candidate", "recoverable_small_or_legacy"},
        "count_for_broader_skillfab_action": tier
        in {
            "current_candidate",
            "recoverable_small_or_legacy",
            "contract_gating_or_adapter_research",
            "adapter_research_candidate",
        },
        "count_for_harness_or_metadata_action": tier == "already_platform_scoped_or_harness_gating",
        "recommended_action": recommended_action(tier, row),
        "short_issue": row.get("short_issue", ""),
        "repair_implication": row.get("repair_implication", ""),
        "source_file": row.get("source_file", ""),
    }


def classify_tier(row: dict[str, Any], legacy_excluded: bool) -> str:
    if row.get("repair_candidate"):
        return "current_candidate"
    fixability = str(row.get("fixability_class") or "")
    key = (str(row["target_os"]), str(row["skill_id"]))
    if legacy_excluded and fixability in SMALL_REPAIR_FIXABILITIES:
        return "recoverable_small_or_legacy"
    if fixability == "platform_adapter_needed":
        return "adapter_research_candidate"
    if key in CONTRACT_OR_ADAPTER_RESEARCH:
        return "contract_gating_or_adapter_research"
    if fixability == "requires_environment_provisioning":
        return "environment_or_permission_provisioning"
    return "already_platform_scoped_or_harness_gating"


def claim_type_for(tier: str) -> str:
    return {
        "current_candidate": "active_skillfab_repair_queue",
        "recoverable_small_or_legacy": "near_term_functional_repair_if_reopened",
        "contract_gating_or_adapter_research": "broader_contract_or_adapter_action",
        "adapter_research_candidate": "larger_adapter_research_action",
        "already_platform_scoped_or_harness_gating": "metadata_or_evaluator_gating_action",
        "environment_or_permission_provisioning": "environment_or_permission_provisioning",
    }[tier]


def recommended_action(tier: str, row: dict[str, Any]) -> str:
    if tier == "current_candidate":
        return str(row.get("repair_implication") or "Open a SkillFab repair issue.")
    if tier == "recoverable_small_or_legacy":
        return "Reopen or reuse the legacy repair work and validate it against the current source snapshot."
    if tier == "contract_gating_or_adapter_research":
        return "Open a scoped SkillFab issue for OS contract/gating first; implement an adapter only if the skill semantics justify cross-OS support."
    if tier == "adapter_research_candidate":
        return "Treat as a larger adapter migration candidate rather than a small script fix."
    if tier == "environment_or_permission_provisioning":
        return "Handle through benchmark environment provisioning or explicit permission documentation before counting as a skill patch."
    return "Do not count as a functional migration target; make OS support metadata and task targeting explicit."


def build_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_tier = Counter(row["repairability_tier"] for row in rows)
    by_os_tier: dict[str, dict[str, int]] = {}
    for target_os in sorted({row["target_os"] for row in rows}):
        by_os_tier[target_os] = dict(
            Counter(row["repairability_tier"] for row in rows if row["target_os"] == target_os)
        )
    headline_counts = {
        "strict_labels": len(rows),
        "current_active_repair_queue": count_bool(rows, "count_for_active_queue"),
        "near_term_functional_repair_candidates": count_bool(rows, "count_for_near_term_functional_repair"),
        "broader_skillfab_actionable_candidates": count_bool(rows, "count_for_broader_skillfab_action"),
        "harness_or_metadata_gating_actions": count_bool(rows, "count_for_harness_or_metadata_action"),
        "environment_or_permission_provisioning": by_tier.get("environment_or_permission_provisioning", 0),
    }
    return {
        "schema_version": "strict_repairability_expanded_summary_v1",
        "generated_at": DATE_STAMP,
        "unit": "strict skill package x target OS issue label",
        "source_labels": rel(DEFAULT_STRICT_LABELS),
        "legacy_exclusions": rel(DEFAULT_LEGACY_EXCLUSIONS),
        "headline_counts": headline_counts,
        "tiers": dict(by_tier),
        "tiers_by_os": by_os_tier,
        "claim_boundaries": [
            "Use current_active_repair_queue for the conservative RQ2 active batch.",
            "Use near_term_functional_repair_candidates when legacy small fixes are allowed to be reopened.",
            "Use broader_skillfab_actionable_candidates only if the paper counts contract/gating and adapter research as SkillFab actions.",
            "Do not present harness_or_metadata_gating_actions as functional cross-OS repairs.",
            "Environment or permission provisioning rows should be reported separately from skill-package patches.",
        ],
        "example_rows_by_tier": examples_by_tier(rows),
    }


def examples_by_tier(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        bucket = result[row["repairability_tier"]]
        if len(bucket) >= 8:
            continue
        bucket.append(
            {
                "skill_id": row["skill_id"],
                "target_os": row["target_os"],
                "fixability_class": row["fixability_class"],
                "short_issue": row["short_issue"],
            }
        )
    return dict(result)


def render_summary_md(summary: dict[str, Any]) -> str:
    lines = [
        "# Strict Repairability Expanded Summary (2026-06-27)",
        "",
        f"Unit: `{summary['unit']}`",
        f"Source labels: `{summary['source_labels']}`",
        f"Legacy exclusions: `{summary['legacy_exclusions']}`",
        "",
        "## Headline Counts",
        "",
        "| Claim scope | Labels |",
        "|---|---:|",
    ]
    labels = {
        "strict_labels": "Strict non-package OS labels",
        "current_active_repair_queue": "Current active repair queue",
        "near_term_functional_repair_candidates": "Near-term functional repair candidates",
        "broader_skillfab_actionable_candidates": "Broader SkillFab-actionable candidates",
        "harness_or_metadata_gating_actions": "Harness or metadata gating actions",
        "environment_or_permission_provisioning": "Environment/permission provisioning",
    }
    for key, label in labels.items():
        lines.append(f"| {label} | {summary['headline_counts'].get(key, 0)} |")

    lines.extend(["", "## Tiers By OS", "", "| OS | Current | Near-term legacy | Contract/adapter | Adapter research | Gating | Env/provisioning |", "|---|---:|---:|---:|---:|---:|---:|"])
    for target_os, counts in sorted(summary["tiers_by_os"].items()):
        lines.append(
            "| {os} | {current} | {legacy} | {contract} | {adapter} | {gating} | {env} |".format(
                os=target_os,
                current=counts.get("current_candidate", 0),
                legacy=counts.get("recoverable_small_or_legacy", 0),
                contract=counts.get("contract_gating_or_adapter_research", 0),
                adapter=counts.get("adapter_research_candidate", 0),
                gating=counts.get("already_platform_scoped_or_harness_gating", 0),
                env=counts.get("environment_or_permission_provisioning", 0),
            )
        )

    lines.extend(["", "## Claim Boundaries", ""])
    lines.extend(f"- {item}" for item in summary["claim_boundaries"])

    lines.extend(["", "## Examples", ""])
    for tier, examples in sorted(summary["example_rows_by_tier"].items()):
        lines.extend([f"### `{tier}`", ""])
        for row in examples:
            issue = row["short_issue"].replace("\n", " ")
            if len(issue) > 180:
                issue = issue[:177] + "..."
            lines.append(
                f"- `{row['skill_id']}` on `{row['target_os']}` (`{row['fixability_class']}`): {issue}"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def count_bool(rows: list[dict[str, Any]], field: str) -> int:
    return sum(1 for row in rows if row[field])


def matches_legacy_exclusion(skill_id: str, legacy: set[str]) -> bool:
    source = source_of(skill_id)
    package = package_of(skill_id)
    slug = strip_version_suffix(package)
    candidates = {skill_id, package, slug, f"{source}/{slug}"}
    return bool(candidates & legacy)


def strip_version_suffix(name: str) -> str:
    return re.sub(r"[-_]?v?\d+(?:[._-]\d+)*(?:[._-]?\w+)?$", "", name).rstrip("-_") or name


def source_of(skill_id: str) -> str:
    return skill_id.split("/", 1)[0] if "/" in skill_id else "unknown"


def package_of(skill_id: str) -> str:
    return skill_id.split("/", 1)[1] if "/" in skill_id else skill_id


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in FIELDNAMES})


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
