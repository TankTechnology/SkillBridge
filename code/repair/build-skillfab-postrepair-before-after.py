#!/usr/bin/env python3
"""Build before/after comparisons for the SkillFab OS repair batch.

The comparison cohort is the final repaired SkillFab delivery manifest.  For
each OS we compare the original benchmark execution evidence ("before") with
the post-repair execution release ("after").  The script prefers task-level
matches by source, slug, task designer, executor, and task id, and also emits
count-level pass-rate deltas for the complete selected run.

This is a release-level diagnostic over matched OS/skill records. Its broad
targeted and affected-OS views must not be used as the paper's canonical 175
target-OS replay denominator without the separate comparable-target filter.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import tarfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = (
    REPO_ROOT / "delivery/skillfab-os-repair-final-20260628/manifest.jsonl"
)
DEFAULT_OUT_DIR = (
    REPO_ROOT / "data/releases/skillfab-os-repair-postrepair-20260629/before-after"
)

KNOWN_STATUSES = {"pass", "fail", "blocked", "unsupported", "pending", "unknown"}
SFOS_RE = re.compile(r"^(SFOS-\d+)__(?P<source>[^_]+)__(?P<slug>.+)$")


@dataclass
class TaskRunGroup:
    os_name: str
    source: str
    source_group: str
    slug: str
    task_design_model: str
    executor_model: str
    origin: str
    paths: list[str] = field(default_factory=list)
    tasks: dict[str, str] = field(default_factory=dict)
    failure_categories: Counter = field(default_factory=Counter)
    parse_errors: int = 0

    def counts(self) -> Counter:
        counts: Counter = Counter()
        for status in self.tasks.values():
            counts[normalize_status(status)] += 1
        return counts

    @property
    def total(self) -> int:
        return len(self.tasks)

    @property
    def pass_rate(self) -> float | None:
        if not self.total:
            return None
        return self.counts()["pass"] / self.total


def normalize_source(source: str) -> str:
    value = (source or "").strip()
    lower = value.lower()
    if lower == "skillsh" or lower.startswith("skillsh"):
        return "SkillSh"
    if "clawhub" in lower:
        return "ClawHub"
    if lower == "anthropic":
        return "Anthropic"
    return value


def source_group_priority(source: str, source_group: str) -> int:
    if source != "ClawHub":
        return 10 if source_group == source else 0
    order = {
        "ClawHub": 50,
        "ClawhubTop1000": 40,
        "ClawhubSelectScripts": 35,
        "ClawhubSelect": 30,
        "ClawHubTop1000": 40,
    }
    return order.get(source_group, 10 if "Claw" in source_group else 0)


def normalize_status(status: object) -> str:
    value = str(status or "").strip().lower()
    return value if value in KNOWN_STATUSES else "unknown"


def parse_sfos_skill_dir(skill_dir: str) -> tuple[str | None, str | None, str | None]:
    match = SFOS_RE.match(skill_dir or "")
    if not match:
        return None, None, None
    return skill_dir.split("__", 1)[0], normalize_source(match.group("source")), match.group("slug")


def load_manifest(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                row["source"] = normalize_source(row.get("source", ""))
                rows.append(row)
    rows.sort(key=lambda r: r.get("issue_key", ""))
    return rows


def member_parts(name: str) -> list[str]:
    return [part for part in name.split("/") if part]


def is_task_metrics_member(member: tarfile.TarInfo) -> bool:
    if not member.isfile():
        return False
    name = member.name
    if not name.endswith("/task_metrics.json"):
        return False
    parts = member_parts(name)
    return not any(part.startswith("._") for part in parts)


def parse_before_path(name: str) -> tuple[str, str, str, str, str] | None:
    parts = member_parts(name)
    try:
        exec_idx = parts.index("exec")
    except ValueError:
        return None
    base = exec_idx + 1
    if base < len(parts) and parts[base] in {"linux", "macos", "windows"}:
        base += 1
    # {source}/{slug}/{task_design}/{executor}/tasks/{task_id}/task_metrics.json
    if len(parts) <= base + 6 or parts[base + 4] != "tasks":
        return None
    return (
        parts[base],
        parts[base + 1],
        parts[base + 2],
        parts[base + 3],
        parts[base + 5],
    )


def parse_after_path(name: str) -> tuple[str, str, str, str, str, str] | None:
    parts = member_parts(name)
    try:
        exec_idx = parts.index("exec")
    except ValueError:
        return None
    base = exec_idx + 1
    # SkillFabOSRepairFinal20260628/{SFOS__source__slug}/{task_design}/{executor}/tasks/{task_id}/task_metrics.json
    if len(parts) <= base + 6 or parts[base + 4] != "tasks":
        return None
    collection = parts[base]
    skill_dir = parts[base + 1]
    _sfos, source, slug = parse_sfos_skill_dir(skill_dir)
    if not source or not slug:
        return None
    return (
        collection,
        source,
        slug,
        parts[base + 2],
        parts[base + 3],
        parts[base + 5],
    )


def read_task_metric(tf: tarfile.TarFile, member: tarfile.TarInfo) -> tuple[str, str]:
    raw = tf.extractfile(member)
    if raw is None:
        return "unknown", ""
    data = raw.read()
    try:
        payload = json.loads(data.decode("utf-8-sig"))
    except Exception:
        return "unknown", ""
    return normalize_status(payload.get("status")), str(payload.get("failure_category") or "")


def scan_task_tars(
    tar_paths: Iterable[Path],
    *,
    os_name: str,
    cohort_keys: set[tuple[str, str]],
    after: bool,
) -> dict[tuple[str, str], list[TaskRunGroup]]:
    groups: dict[tuple[str, str, str, str, str], TaskRunGroup] = {}
    for tar_path in tar_paths:
        if not tar_path.exists():
            continue
        with tarfile.open(tar_path, "r:gz") as tf:
            for member in tf:
                if not is_task_metrics_member(member):
                    continue
                if after:
                    parsed_after = parse_after_path(member.name)
                    if not parsed_after:
                        continue
                    source_group, source, slug, task_design, executor, task_id = parsed_after
                else:
                    parsed_before = parse_before_path(member.name)
                    if not parsed_before:
                        continue
                    source_group, slug, task_design, executor, task_id = parsed_before
                    source = normalize_source(source_group)
                if (source, slug) not in cohort_keys:
                    continue
                group_key = (source, source_group, slug, task_design, executor)
                group = groups.get(group_key)
                if group is None:
                    group = TaskRunGroup(
                        os_name=os_name,
                        source=source,
                        source_group=source_group,
                        slug=slug,
                        task_design_model=task_design,
                        executor_model=executor,
                        origin=str(tar_path),
                    )
                    groups[group_key] = group
                status, failure_category = read_task_metric(tf, member)
                if status == "unknown":
                    # Unknown may be a real status or a parse failure. Track the
                    # exact count because malformed JSON exists in these releases.
                    try:
                        json.loads(tf.extractfile(member).read().decode("utf-8-sig"))  # type: ignore[union-attr]
                    except Exception:
                        group.parse_errors += 1
                group.tasks[task_id] = status
                if failure_category:
                    group.failure_categories[failure_category] += 1
                group.paths.append(member.name)
    by_skill: dict[tuple[str, str], list[TaskRunGroup]] = defaultdict(list)
    for group in groups.values():
        by_skill[(group.source, group.slug)].append(group)
    return by_skill


def choose_after_group(groups: list[TaskRunGroup]) -> TaskRunGroup | None:
    if not groups:
        return None
    return max(
        groups,
        key=lambda g: (
            g.total,
            g.counts()["pass"],
            source_group_priority(g.source, g.source_group),
            g.task_design_model,
        ),
    )


def choose_before_group(
    groups: list[TaskRunGroup],
    after_group: TaskRunGroup | None,
) -> TaskRunGroup | None:
    if not groups:
        return None
    after_tasks = set(after_group.tasks) if after_group else set()
    after_td = after_group.task_design_model if after_group else ""
    after_exec = after_group.executor_model if after_group else ""
    return max(
        groups,
        key=lambda g: (
            1 if after_td and g.task_design_model == after_td else 0,
            1 if after_exec and g.executor_model == after_exec else 0,
            len(after_tasks & set(g.tasks)),
            source_group_priority(g.source, g.source_group),
            g.total,
            g.counts()["pass"],
        ),
    )


def format_rate(rate: float | None) -> str:
    return "" if rate is None else f"{rate:.4f}"


def count_value(counts: Counter, status: str) -> int:
    return int(counts.get(status, 0))


def compare_one(
    *,
    os_name: str,
    manifest_rows: list[dict],
    before: dict[tuple[str, str], list[TaskRunGroup]],
    after: dict[tuple[str, str], list[TaskRunGroup]],
) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    aggregate = {
        "os": os_name,
        "cohort_skills": len(manifest_rows),
        "targeted_skills": 0,
        "matched_skills": 0,
        "missing_before": 0,
        "missing_after": 0,
        "improved_skills": 0,
        "unchanged_skills": 0,
        "worse_skills": 0,
        "targeted_matched_skills": 0,
        "targeted_improved_skills": 0,
        "targeted_unchanged_skills": 0,
        "targeted_worse_skills": 0,
        "before_total": 0,
        "before_pass": 0,
        "after_total": 0,
        "after_pass": 0,
        "common_tasks": 0,
        "common_fixed": 0,
        "common_unresolved": 0,
        "common_no_regression": 0,
        "common_regression": 0,
    }
    for manifest in manifest_rows:
        source = manifest["source"]
        slug = manifest["slug"]
        key = (source, slug)
        target_os = {str(x).lower() for x in manifest.get("target_os", [])}
        affected_os = {str(x).lower() for x in manifest.get("affected_os", [])}
        targeted = os_name.lower() in (target_os | affected_os)
        if targeted:
            aggregate["targeted_skills"] += 1

        after_group = choose_after_group(after.get(key, []))
        before_group = choose_before_group(before.get(key, []), after_group)
        before_counts = before_group.counts() if before_group else Counter()
        after_counts = after_group.counts() if after_group else Counter()

        if not before_group:
            aggregate["missing_before"] += 1
        if not after_group:
            aggregate["missing_after"] += 1
        if before_group and after_group:
            aggregate["matched_skills"] += 1
            if targeted:
                aggregate["targeted_matched_skills"] += 1
            aggregate["before_total"] += before_group.total
            aggregate["before_pass"] += count_value(before_counts, "pass")
            aggregate["after_total"] += after_group.total
            aggregate["after_pass"] += count_value(after_counts, "pass")

        before_rate = before_group.pass_rate if before_group else None
        after_rate = after_group.pass_rate if after_group else None
        delta_rate = (
            None if before_rate is None or after_rate is None else after_rate - before_rate
        )
        trend = "missing"
        if delta_rate is not None:
            if delta_rate > 1e-9:
                trend = "improved"
                aggregate["improved_skills"] += 1
                if targeted:
                    aggregate["targeted_improved_skills"] += 1
            elif delta_rate < -1e-9:
                trend = "worse"
                aggregate["worse_skills"] += 1
                if targeted:
                    aggregate["targeted_worse_skills"] += 1
            else:
                trend = "unchanged"
                aggregate["unchanged_skills"] += 1
                if targeted:
                    aggregate["targeted_unchanged_skills"] += 1

        common = set(before_group.tasks if before_group else {}) & set(
            after_group.tasks if after_group else {}
        )
        common_fixed = common_unresolved = common_no_regression = common_regression = 0
        for task_id in sorted(common):
            b = normalize_status(before_group.tasks[task_id])  # type: ignore[union-attr]
            a = normalize_status(after_group.tasks[task_id])  # type: ignore[union-attr]
            if b != "pass" and a == "pass":
                common_fixed += 1
            elif b != "pass" and a != "pass":
                common_unresolved += 1
            elif b == "pass" and a == "pass":
                common_no_regression += 1
            elif b == "pass" and a != "pass":
                common_regression += 1
        aggregate["common_tasks"] += len(common)
        aggregate["common_fixed"] += common_fixed
        aggregate["common_unresolved"] += common_unresolved
        aggregate["common_no_regression"] += common_no_regression
        aggregate["common_regression"] += common_regression

        rows.append(
            {
                "os": os_name,
                "sfos_id": manifest.get("issue_key", ""),
                "skill_id": manifest.get("skill_id", f"{source}/{slug}"),
                "source": source,
                "slug": slug,
                "targeted_for_os": str(targeted).lower(),
                "target_os": ";".join(manifest.get("target_os", [])),
                "affected_os": ";".join(manifest.get("affected_os", [])),
                "before_source_group": before_group.source_group if before_group else "",
                "before_task_design_model": before_group.task_design_model if before_group else "",
                "before_executor_model": before_group.executor_model if before_group else "",
                "after_task_design_model": after_group.task_design_model if after_group else "",
                "after_executor_model": after_group.executor_model if after_group else "",
                "before_total": before_group.total if before_group else 0,
                "before_pass": count_value(before_counts, "pass"),
                "before_fail": count_value(before_counts, "fail"),
                "before_blocked": count_value(before_counts, "blocked"),
                "before_unsupported": count_value(before_counts, "unsupported"),
                "before_pending": count_value(before_counts, "pending"),
                "before_unknown": count_value(before_counts, "unknown"),
                "before_pass_rate": format_rate(before_rate),
                "after_total": after_group.total if after_group else 0,
                "after_pass": count_value(after_counts, "pass"),
                "after_fail": count_value(after_counts, "fail"),
                "after_blocked": count_value(after_counts, "blocked"),
                "after_unsupported": count_value(after_counts, "unsupported"),
                "after_pending": count_value(after_counts, "pending"),
                "after_unknown": count_value(after_counts, "unknown"),
                "after_pass_rate": format_rate(after_rate),
                "delta_pass": count_value(after_counts, "pass") - count_value(before_counts, "pass"),
                "delta_pass_rate": format_rate(delta_rate),
                "trend": trend,
                "common_task_ids": len(common),
                "common_fixed": common_fixed,
                "common_unresolved": common_unresolved,
                "common_no_regression": common_no_regression,
                "common_regression": common_regression,
                "before_parse_errors": before_group.parse_errors if before_group else 0,
                "after_parse_errors": after_group.parse_errors if after_group else 0,
            }
        )
    for prefix in ("before", "after"):
        total = aggregate[f"{prefix}_total"]
        passed = aggregate[f"{prefix}_pass"]
        aggregate[f"{prefix}_pass_rate"] = None if not total else passed / total
    return rows, aggregate


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def clean_effect(row: dict) -> str:
    before_total = int(row["before_total"])
    after_total = int(row["after_total"])
    if before_total == 0 and after_total == 0:
        return "missing_both"
    if before_total == 0:
        return "missing_before"
    if after_total == 0:
        return "missing_after"
    if row["trend"] == "unchanged":
        return "same"
    return row["trend"]


def data_quality(row: dict) -> str:
    flags: list[str] = []
    if int(row["before_total"]) == 0:
        flags.append("missing_before")
    if int(row["after_total"]) == 0:
        flags.append("missing_after")
    if int(row["before_parse_errors"]) or int(row["after_parse_errors"]):
        flags.append("parse_error")
    if int(row["common_task_ids"]) == 0 and int(row["before_total"]) and int(row["after_total"]):
        flags.append("no_common_task_ids")
    return ";".join(flags) if flags else "ok"


def run_key(row: dict, prefix: str) -> str:
    if prefix == "before":
        parts = [
            row.get("before_source_group", ""),
            row.get("before_task_design_model", ""),
            row.get("before_executor_model", ""),
        ]
    else:
        parts = [
            "SkillFabOSRepairFinal20260628",
            row.get("after_task_design_model", ""),
            row.get("after_executor_model", ""),
        ]
    return "/".join(part for part in parts if part)


def clean_skill_row(row: dict) -> dict:
    before_total = int(row["before_total"])
    after_total = int(row["after_total"])
    before_pass = int(row["before_pass"])
    after_pass = int(row["after_pass"])
    return {
        "os": row["os"],
        "sfos_id": row["sfos_id"],
        "skill_id": row["skill_id"],
        "source": row["source"],
        "slug": row["slug"],
        "is_target_os": row["targeted_for_os"],
        "target_os": row["target_os"],
        "affected_os": row["affected_os"],
        "before_tasks": before_total,
        "before_pass": before_pass,
        "before_fail": int(row["before_fail"]),
        "before_blocked": int(row["before_blocked"]),
        "before_unsupported": int(row["before_unsupported"]),
        "before_pending": int(row["before_pending"]),
        "before_unknown": int(row["before_unknown"]),
        "before_nonpass": max(0, before_total - before_pass),
        "before_pass_rate": row["before_pass_rate"],
        "after_tasks": after_total,
        "after_pass": after_pass,
        "after_fail": int(row["after_fail"]),
        "after_blocked": int(row["after_blocked"]),
        "after_unsupported": int(row["after_unsupported"]),
        "after_pending": int(row["after_pending"]),
        "after_unknown": int(row["after_unknown"]),
        "after_nonpass": max(0, after_total - after_pass),
        "after_pass_rate": row["after_pass_rate"],
        "delta_pass": int(row["delta_pass"]),
        "delta_pass_rate": row["delta_pass_rate"],
        "effect": clean_effect(row),
        "common_tasks": int(row["common_task_ids"]),
        "common_fixed": int(row["common_fixed"]),
        "common_regression": int(row["common_regression"]),
        "common_stable_pass": int(row["common_no_regression"]),
        "common_still_nonpass": int(row["common_unresolved"]),
        "data_quality": data_quality(row),
        "selected_before_run": run_key(row, "before"),
        "selected_after_run": run_key(row, "after"),
    }


def summarize_clean_rows(os_name: str, rows: list[dict], scope: str) -> dict:
    if scope == "all_matched":
        scoped = [
            row
            for row in rows
            if row["effect"] not in {"missing_before", "missing_after", "missing_both"}
        ]
    elif scope == "targeted_matched":
        scoped = [
            row
            for row in rows
            if row["is_target_os"] == "true"
            and row["effect"] not in {"missing_before", "missing_after", "missing_both"}
        ]
    elif scope == "full_cohort":
        scoped = rows
    else:
        raise ValueError(f"unknown scope: {scope}")

    before_tasks = sum(int(row["before_tasks"]) for row in scoped)
    before_pass = sum(int(row["before_pass"]) for row in scoped)
    after_tasks = sum(int(row["after_tasks"]) for row in scoped)
    after_pass = sum(int(row["after_pass"]) for row in scoped)
    before_rate = "" if not before_tasks else f"{before_pass / before_tasks:.4f}"
    after_rate = "" if not after_tasks else f"{after_pass / after_tasks:.4f}"
    delta_rate = (
        ""
        if not before_rate or not after_rate
        else f"{float(after_rate) - float(before_rate):.4f}"
    )
    return {
        "os": os_name,
        "scope": scope,
        "skills": len(scoped),
        "cohort_skills": len(rows),
        "targeted_skills": sum(1 for row in rows if row["is_target_os"] == "true"),
        "missing_before": sum(1 for row in rows if row["effect"] == "missing_before"),
        "missing_after": sum(1 for row in rows if row["effect"] == "missing_after"),
        "before_tasks": before_tasks,
        "before_pass": before_pass,
        "before_pass_rate": before_rate,
        "after_tasks": after_tasks,
        "after_pass": after_pass,
        "after_pass_rate": after_rate,
        "delta_pass": after_pass - before_pass,
        "delta_pass_rate": delta_rate,
        "improved_skills": sum(1 for row in scoped if row["effect"] == "improved"),
        "same_skills": sum(1 for row in scoped if row["effect"] == "same"),
        "worse_skills": sum(1 for row in scoped if row["effect"] == "worse"),
        "common_tasks": sum(int(row["common_tasks"]) for row in scoped),
        "common_fixed": sum(int(row["common_fixed"]) for row in scoped),
        "common_regression": sum(int(row["common_regression"]) for row in scoped),
    }


def missing_evidence_rows(rows: list[dict]) -> list[dict]:
    missing: list[dict] = []
    for row in rows:
        if row["effect"] not in {"missing_before", "missing_after", "missing_both"}:
            continue
        if row["effect"] == "missing_both":
            side = "both"
        elif row["effect"] == "missing_before":
            side = "before"
        else:
            side = "after"
        missing.append(
            {
                "os": row["os"],
                "sfos_id": row["sfos_id"],
                "skill_id": row["skill_id"],
                "is_target_os": row["is_target_os"],
                "missing_side": side,
                "before_tasks": row["before_tasks"],
                "after_tasks": row["after_tasks"],
                "before_pass": row["before_pass"],
                "after_pass": row["after_pass"],
                "note": row["data_quality"],
            }
        )
    return missing


FIELD_DICTIONARY = [
    ("os", "Execution OS for the comparison row: macos or windows."),
    ("sfos_id", "Stable SkillFab OS repair batch identifier."),
    ("skill_id", "Canonical source/slug skill id."),
    ("source", "Normalized source family, currently ClawHub or SkillSh."),
    ("slug", "Skill package slug."),
    ("is_target_os", "true when this OS was listed in target_os or affected_os for the repair item."),
    ("target_os", "Semicolon-separated OS list from the final repair manifest."),
    ("affected_os", "Semicolon-separated affected OS list from the final repair manifest."),
    ("before_tasks", "Number of task_metrics selected from the original benchmark run."),
    ("before_pass", "Before-run tasks with status pass."),
    ("before_fail", "Before-run tasks with status fail."),
    ("before_blocked", "Before-run tasks with status blocked."),
    ("before_unsupported", "Before-run tasks with status unsupported."),
    ("before_pending", "Before-run tasks with status pending."),
    ("before_unknown", "Before-run tasks with unknown or malformed status."),
    ("before_nonpass", "before_tasks - before_pass."),
    ("before_pass_rate", "before_pass / before_tasks; blank when before_tasks is zero."),
    ("after_tasks", "Number of task_metrics selected from the post-repair run."),
    ("after_pass", "After-run tasks with status pass."),
    ("after_fail", "After-run tasks with status fail."),
    ("after_blocked", "After-run tasks with status blocked."),
    ("after_unsupported", "After-run tasks with status unsupported."),
    ("after_pending", "After-run tasks with status pending."),
    ("after_unknown", "After-run tasks with unknown or malformed status."),
    ("after_nonpass", "after_tasks - after_pass."),
    ("after_pass_rate", "after_pass / after_tasks; blank when after_tasks is zero."),
    ("delta_pass", "after_pass - before_pass."),
    ("delta_pass_rate", "after_pass_rate - before_pass_rate; blank when either side is missing."),
    ("effect", "improved, same, worse, missing_before, missing_after, or missing_both."),
    ("common_tasks", "Number of task IDs appearing in both selected before and after runs."),
    ("common_fixed", "Common task IDs whose status changed from non-pass to pass."),
    ("common_regression", "Common task IDs whose status changed from pass to non-pass."),
    ("common_stable_pass", "Common task IDs that passed both before and after."),
    ("common_still_nonpass", "Common task IDs that were non-pass both before and after."),
    ("data_quality", "ok or semicolon-separated flags: missing_before, missing_after, parse_error, no_common_task_ids."),
    ("selected_before_run", "Selected source group/task designer/executor used for the before side."),
    ("selected_after_run", "Selected collection/task designer/executor used for the after side."),
]


def write_field_dictionary(path: Path) -> None:
    rows = [{"field": field, "description": description} for field, description in FIELD_DICTIONARY]
    write_csv(path.with_suffix(".csv"), rows)
    lines = [
        "# Field Dictionary",
        "",
        "All clean tables use lower_snake_case fields. Counts are integer task counts; rates are decimal fractions in [0, 1] and are blank when the denominator is unavailable.",
        "",
        "| Field | Description |",
        "|---|---|",
    ]
    for field, description in FIELD_DICTIONARY:
        lines.append(f"| `{field}` | {description} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_support_matrix(path: Path, summary_rows: list[dict]) -> None:
    payload = {
        "schema_version": "skillfab_postrepair_before_after_support_matrix_v1",
        "sample_universe": "153 final approved SkillFab OS repair packages evaluated against original macOS/Windows benchmark executions and post-repair macOS/Windows executions.",
        "unit_of_analysis": "os-skill pair",
        "primary_denominator": "matched before/after os-skill pairs; missing evidence is reported separately and excluded from pass-rate deltas.",
        "metric_definition": {
            "before_pass_rate": "before_pass / before_tasks",
            "after_pass_rate": "after_pass / after_tasks",
            "delta_pass_rate": "after_pass_rate - before_pass_rate",
            "common_fixed": "common task IDs changing from non-pass to pass",
            "common_regression": "common task IDs changing from pass to non-pass",
        },
        "source_files": [
            "data/releases/skillfab-os-repair-postrepair-20260629/before-after/repair_effect_by_skill.csv",
            "data/releases/skillfab-os-repair-postrepair-20260629/before-after/repair_effect_summary_by_os.csv",
            "data/releases/skillfab-os-repair-postrepair-20260629/before-after/repair_effect_missing_evidence.csv",
        ],
        "claims": [
            {
                "claim": "The final repaired batch improves aggregate task pass rate on matched macOS and Windows evidence.",
                "status": "support",
                "safe_wording": "On matched before/after evidence, pass rates increase for both macOS and Windows.",
                "forbidden_wording": "Every repaired skill improved on every OS.",
            },
            {
                "claim": "The targeted OS subset is the cleanest estimate of repair effectiveness.",
                "status": "support",
                "safe_wording": "Report targeted-subset results separately from full-cohort regression checks.",
                "forbidden_wording": "Full-cohort macOS/Windows rows are all direct repair targets.",
            },
            {
                "claim": "Worse rows prove repair-induced regressions.",
                "status": "caution",
                "safe_wording": "Worse rows are audit targets because benchmark instability, service state, or task-count differences may also explain them.",
                "forbidden_wording": "All worse rows are caused by the repair.",
            },
        ],
        "summary_rows": summary_rows,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_clean_outputs(out_dir: Path, rows_by_os: dict[str, list[dict]]) -> None:
    clean_rows: list[dict] = []
    for os_name in ("macos", "windows"):
        clean_rows.extend(clean_skill_row(row) for row in rows_by_os[os_name])
    clean_rows.sort(key=lambda r: (r["os"], r["sfos_id"], r["skill_id"]))

    summary_rows: list[dict] = []
    for os_name in ("macos", "windows"):
        os_rows = [row for row in clean_rows if row["os"] == os_name]
        summary_rows.extend(
            summarize_clean_rows(os_name, os_rows, scope)
            for scope in ("all_matched", "targeted_matched", "full_cohort")
        )

    missing_rows = missing_evidence_rows(clean_rows)
    write_csv(out_dir / "repair_effect_by_skill.csv", clean_rows)
    write_csv(out_dir / "repair_effect_summary_by_os.csv", summary_rows)
    write_csv(out_dir / "repair_effect_missing_evidence.csv", missing_rows)
    write_field_dictionary(out_dir / "FIELD_DICTIONARY.md")
    write_support_matrix(out_dir / "repair_effect_support_matrix.json", summary_rows)


def write_markdown(path: Path, summary: dict, rows_by_os: dict[str, list[dict]]) -> None:
    lines = [
        "# SkillFab OS Repair Before/After Comparison",
        "",
        "This comparison uses the final repaired SkillFab delivery as the cohort.",
        "Before results come from the original macOS/Windows benchmark releases; after results come from the post-repair execution releases.",
        "Metrics are task-status counts selected by source, slug, task designer, executor, and task-id overlap where available.",
        "",
        "## Aggregate",
        "",
        "| OS | Cohort | Targeted | Matched before/after | Before pass | After pass | Before pass rate | After pass rate | Improved | Same | Worse | Common fixed | Common regression | Missing before | Missing after |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for os_name in ("macos", "windows"):
        agg = summary[os_name]
        lines.append(
            "| {os} | {cohort} | {targeted} | {matched} | {bp} | {ap} | {bpr:.2%} | {apr:.2%} | {imp} | {same} | {worse} | {fixed} | {reg} | {mb} | {ma} |".format(
                os=os_name,
                cohort=agg["cohort_skills"],
                targeted=agg["targeted_skills"],
                matched=agg["matched_skills"],
                bp=agg["before_pass"],
                ap=agg["after_pass"],
                bpr=agg["before_pass_rate"] or 0.0,
                apr=agg["after_pass_rate"] or 0.0,
                imp=agg["improved_skills"],
                same=agg["unchanged_skills"],
                worse=agg["worse_skills"],
                fixed=agg["common_fixed"],
                reg=agg["common_regression"],
                mb=agg["missing_before"],
                ma=agg["missing_after"],
            )
        )
    lines.extend(
        [
            "",
            "## Targeted OS Subset",
            "",
            "| OS | Targeted matched | Improved | Same | Worse |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for os_name in ("macos", "windows"):
        agg = summary[os_name]
        lines.append(
            f"| {os_name} | {agg['targeted_matched_skills']} | {agg['targeted_improved_skills']} | {agg['targeted_unchanged_skills']} | {agg['targeted_worse_skills']} |"
        )
    lines.extend(
        [
            "",
            "## Top Improvements",
            "",
        ]
    )
    for os_name in ("macos", "windows"):
        lines.extend([f"### {os_name}", ""])
        rows = [
            row
            for row in rows_by_os[os_name]
            if row["trend"] == "improved" and row["delta_pass_rate"] != ""
        ]
        rows.sort(
            key=lambda r: (
                float(r["delta_pass_rate"] or 0),
                int(r["delta_pass"] or 0),
            ),
            reverse=True,
        )
        lines.extend(
            [
                "| Skill | Targeted | Before | After | Delta pass | Delta rate | Common fixed |",
                "|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for row in rows[:25]:
            lines.append(
                f"| `{row['skill_id']}` | {row['targeted_for_os']} | {row['before_pass']}/{row['before_total']} | {row['after_pass']}/{row['after_total']} | {row['delta_pass']} | {float(row['delta_pass_rate']):.2%} | {row['common_fixed']} |"
            )
        if not rows:
            lines.append("| _none_ |  |  |  |  |  |  |")
        lines.append("")
    lines.extend(
        [
            "## Caveats",
            "",
            "- This table is a repair-batch comparison, not a full benchmark re-analysis.",
            "- The primary aggregate uses selected run-level pass-rate deltas. The `common_*` columns are stricter task-id comparisons when the before and after task IDs overlap.",
            "- Missing before/after rows are reported explicitly and are not counted as failures.",
            "",
            "## Clean Tables",
            "",
            "- `repair_effect_by_skill.csv`: primary clean per-OS/per-skill table.",
            "- `repair_effect_summary_by_os.csv`: aggregate and targeted-subset summary.",
            "- `repair_effect_missing_evidence.csv`: rows excluded from matched before/after pass-rate deltas.",
            "- `FIELD_DICTIONARY.md`: field definitions and table contract.",
            "- `repair_effect_support_matrix.json`: claim-to-evidence audit matrix.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument(
        "--macos-before",
        type=Path,
        default=Path(
            "/path/to/original-macos-baseline-release.tar.gz"
        ),
    )
    parser.add_argument(
        "--macos-after",
        type=Path,
        default=REPO_ROOT
        / "data/releases/skillfab-os-repair-postrepair-20260629/macos/compa-skill-skillfab-os-repair-final-macos-exec-20260629.tar.gz",
    )
    parser.add_argument(
        "--windows-before",
        type=Path,
        nargs="+",
        default=[
            REPO_ROOT
            / "data/releases/windows-v0.5.0-baseline/compa-skill-exec-clawhub-remain-20260625-112803.tar.gz",
            REPO_ROOT
            / "data/releases/windows-v0.5.0-baseline/compa-skill-exec-skills-20260625-112803.tar.gz",
            REPO_ROOT
            / "data/releases/windows-v0.5.0-baseline/compa-skill-exec-heavy-20260625-112803.tar.gz",
        ],
    )
    parser.add_argument(
        "--windows-after",
        type=Path,
        default=REPO_ROOT
        / "data/releases/skillfab-os-repair-postrepair-20260629/windows/compa-skill-windows-exec-20260628.tar.gz",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest_rows = load_manifest(args.manifest)
    cohort_keys = {(row["source"], row["slug"]) for row in manifest_rows}
    args.out_dir.mkdir(parents=True, exist_ok=True)

    scan_inputs = {
        "macos": {
            "before": [args.macos_before],
            "after": [args.macos_after],
        },
        "windows": {
            "before": list(args.windows_before),
            "after": [args.windows_after],
        },
    }
    rows_by_os: dict[str, list[dict]] = {}
    summary: dict[str, dict] = {}
    for os_name, paths in scan_inputs.items():
        before = scan_task_tars(
            paths["before"],
            os_name=os_name,
            cohort_keys=cohort_keys,
            after=False,
        )
        after = scan_task_tars(
            paths["after"],
            os_name=os_name,
            cohort_keys=cohort_keys,
            after=True,
        )
        rows, aggregate = compare_one(
            os_name=os_name,
            manifest_rows=manifest_rows,
            before=before,
            after=after,
        )
        rows_by_os[os_name] = rows
        summary[os_name] = aggregate
        write_csv(args.out_dir / f"{os_name}.before_after.csv", rows)

    write_clean_outputs(args.out_dir, rows_by_os)
    summary_payload = {
        "schema_version": "skillfab_postrepair_before_after_v1",
        "cohort_manifest": str(args.manifest),
        "comparison_level": "selected-run counts plus common task-id deltas",
        "inputs": {
            "macos_before": [str(args.macos_before)],
            "macos_after": [str(args.macos_after)],
            "windows_before": [str(p) for p in args.windows_before],
            "windows_after": [str(args.windows_after)],
        },
        "summary": summary,
    }
    (args.out_dir / "summary.json").write_text(
        json.dumps(summary_payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    write_markdown(args.out_dir / "README.md", summary, rows_by_os)
    print(json.dumps(summary_payload["summary"], indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
