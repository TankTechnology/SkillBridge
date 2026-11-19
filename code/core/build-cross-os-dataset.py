#!/usr/bin/env python3
"""Build a clean cross-OS compatibility dataset from existing artifacts.

The builder is read-only with respect to raw benchmark assets. It parses
existing Linux datasets, macOS release index CSVs, and Windows release
platform summaries, then writes normalized JSONL/JSON/Markdown artifacts.
It never executes skills, generated task commands, setup scripts, or migrated
packages.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from datetime import datetime, timezone
import gzip
import io
import json
from pathlib import Path
import re
import tarfile
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = REPO_ROOT.parent

DEFAULT_OUT = REPO_ROOT / "docs" / "cross-os-testing" / "datasets"
DEFAULT_LINUX_DATASETS = REPO_ROOT / "docs" / "linux-testing" / "datasets"
DEFAULT_MACOS_V06_REPORTS = WORKSPACE_ROOT / "release-work" / "macos-v0.6.0" / "reports"
DEFAULT_MACOS_V06_EXEC_ASSET = REPO_ROOT / ".cache" / "release-audit" / "macos-v0.6.0" / "macOS_exec_ClawHub+SkillSh.tar.gz"
DEFAULT_MACOS_V06_SAMPLES_ASSET = REPO_ROOT / ".cache" / "release-audit" / "macos-v0.6.0" / "macOS_samples.tar.gz"
DEFAULT_WINDOWS_V04_ASSET = (
    WORKSPACE_ROOT / "release-work" / "windows-v0.4.0" / "compa-skill-data-20260624-215344.tar.gz"
)
DEFAULT_WINDOWS_V05_AUDIT_DIR = REPO_ROOT / ".cache" / "release-audit" / "windows-v0.5.0"
DEFAULT_WINDOWS_V03_EXEC_DIR = WORKSPACE_ROOT / ".release-analysis" / "windows-v0.3.0"
DEFAULT_WINDOWS_V02_ASSET = WORKSPACE_ROOT / ".release-analysis" / "compa-skill-data-windows-20260610-103433.tar.gz"

COUNT_KEYS = ("pass", "fail", "blocked", "unsupported", "pending", "unknown", "total")
FIXABLE_LABELS = {
    "os_specific_execution_issue",
    "package_or_entrypoint_issue",
    "os_install_or_dependency_contract_issue",
    "guide_only_os_contract_issue",
}
KNOWN_SOURCES = {
    "clawhub": "ClawHub",
    "skillsh": "SkillSh",
    "anthropic": "Anthropic",
    "github-jeffallan": "GitHub-Jeffallan",
}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def iter_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def canonical_skill_id(skill_id: str) -> str:
    source = source_of(skill_id)
    package_id = package_of(skill_id)
    return f"{source}/{package_id}"


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def canonical_source(source: str) -> str:
    normalized = str(source or "").strip()
    tokens = [token for token in re.split(r"[\\/]+", normalized) if token]
    candidates = tokens if tokens else [normalized]
    for token in candidates:
        lowered = token.lower()
        if lowered.startswith("clawhub"):
            return "ClawHub"
        if lowered in KNOWN_SOURCES:
            return KNOWN_SOURCES[lowered]
    return "unknown" if ("/" in normalized or "\\" in normalized) else normalized or "unknown"


def source_of(skill_id: str) -> str:
    return canonical_source(skill_id.split("/", 1)[0]) if "/" in skill_id else "unknown"


def is_known_source(source: str) -> bool:
    return canonical_source(source) in set(KNOWN_SOURCES.values())


def package_of(skill_id: str) -> str:
    return skill_id.split("/", 1)[1] if "/" in skill_id else skill_id


def slug_family(source: str, package_id: str, fallback_slug: str = "") -> str:
    if fallback_slug:
        return f"{canonical_source(source)}/{fallback_slug}"
    return f"{canonical_source(source)}/{strip_version_suffix(package_id)}"


def strip_version_suffix(name: str) -> str:
    return re.sub(r"[-_]?v?\d+(?:[._-]\d+)*(?:[._-]?\w+)?$", "", name).rstrip("-_") or name


def normalize_counts(raw: dict[str, Any]) -> dict[str, int]:
    counts = {key: 0 for key in COUNT_KEYS}
    counts["pass"] = first_count(
        raw,
        "pass",
        "passed",
        "pass_count",
        "passed_count",
        "passed_tasks",
        "passed_task_ids",
        "tasks_pass",
        "total_pass",
        "total_passed",
    )
    counts["fail"] = first_count(
        raw,
        "fail",
        "failed",
        "fail_count",
        "failed_count",
        "failed_tasks",
        "failed_task_ids",
        "tasks_fail",
        "total_fail",
        "total_failed",
    )
    counts["blocked"] = first_count(
        raw,
        "blocked",
        "blocked_count",
        "blocked_tasks",
        "blocked_task_ids",
        "tasks_blocked",
        "total_blocked",
    )
    counts["unsupported"] = first_count(
        raw,
        "unsupported",
        "unsupported_count",
        "unsupported_tasks",
        "unsupported_task_ids",
        "tasks_unsupported",
        "total_unsupported",
    )
    counts["pending"] = first_count(raw, "pending", "pending_count", "pending_tasks", "pending_task_ids")

    for result_key in (
        "task_results",
        "results",
        "outcomes",
        "task_outcomes",
        "tasks",
        "task_details",
        "summary",
        "result_summary",
        "results_summary",
        "status_summary",
        "task_statuses",
        "task_status_summary",
        "task_summaries",
        "task_summary",
        "tasks_by_status",
        "outcome_summary",
        "outcome_counts",
        "status_counts",
        "status_breakdown",
        "result_counts",
    ):
        extracted = counts_from_result_payload(raw.get(result_key))
        if status_count_sum(extracted) > status_count_sum(counts):
            counts.update(extracted)
        elif extracted.get("total") and not counts["total"]:
            counts["total"] = extracted["total"]

    if not counts["total"]:
        counts["total"] = first_int(raw, "total_tasks", "task_count", "total_functional_tasks") or sum(
            counts[key] for key in ("pass", "fail", "blocked", "unsupported", "pending", "unknown")
        )
    known = sum(counts[key] for key in ("pass", "fail", "blocked", "unsupported", "pending"))
    counts["unknown"] = max(counts["total"] - known, counts["unknown"])
    return counts


def counts_from_result_payload(payload: Any) -> dict[str, int]:
    if isinstance(payload, dict):
        direct = {key: 0 for key in COUNT_KEYS}
        direct["pass"] = first_count(
            payload,
            "pass",
            "passed",
            "pass_count",
            "passed_count",
            "passed_tasks",
            "tasks_pass",
            "total_pass",
        )
        direct["fail"] = first_count(payload, "fail", "failed", "fail_count", "failed_count", "failed_tasks", "total_fail")
        direct["blocked"] = first_count(
            payload,
            "blocked",
            "blocked_count",
            "blocked_tasks",
            "tasks_blocked",
            "total_blocked",
        )
        direct["unsupported"] = first_count(
            payload,
            "unsupported",
            "unsupported_count",
            "unsupported_tasks",
            "tasks_unsupported",
            "total_unsupported",
        )
        direct["pending"] = first_count(payload, "pending", "pending_count", "pending_tasks")
        direct["total"] = first_int(payload, "total", "total_tasks", "task_count", "total_functional_tasks")
        if any(direct[key] for key in ("pass", "fail", "blocked", "unsupported", "pending")) or direct["total"]:
            return direct
        return count_status_values(payload.values())
    if isinstance(payload, list):
        return count_status_values(payload)
    return {}


def count_status_values(values: Any) -> dict[str, int]:
    counts = {key: 0 for key in COUNT_KEYS}
    for value in values:
        if isinstance(value, dict):
            status = str(value.get("status") or value.get("result") or value.get("outcome") or "").lower()
        else:
            status = str(value).lower()
        if status in {"pass", "passed", "success", "ok"}:
            counts["pass"] += 1
        elif status in {"fail", "failed", "error"}:
            counts["fail"] += 1
        elif status == "blocked":
            counts["blocked"] += 1
        elif status == "unsupported":
            counts["unsupported"] += 1
        elif status == "pending":
            counts["pending"] += 1
        elif status:
            counts["unknown"] += 1
    counts["total"] = sum(counts[key] for key in ("pass", "fail", "blocked", "unsupported", "pending", "unknown"))
    return counts


def has_status_counts(counts: dict[str, int]) -> bool:
    return bool(status_count_sum(counts))


def status_count_sum(counts: dict[str, int]) -> int:
    return sum(int(counts.get(key, 0)) for key in ("pass", "fail", "blocked", "unsupported", "pending"))


def int_value(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)
    return 0


def first_int(raw: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = int_value(raw.get(key))
        if value:
            return value
    return 0


def count_value(value: Any) -> int:
    direct = int_value(value)
    if direct:
        return direct
    if isinstance(value, (list, tuple, set)):
        return len(value)
    if isinstance(value, dict):
        return len(value)
    return 0


def first_count(raw: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = count_value(raw.get(key))
        if value:
            return value
    return 0


def parse_success_rate(value: Any, counts: dict[str, int]) -> float:
    if isinstance(value, (int, float)):
        rate = float(value)
        return round(rate / 100, 6) if rate > 1 else round(rate, 6)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.endswith("%"):
            try:
                return round(float(stripped[:-1]) / 100, 6)
            except ValueError:
                pass
        try:
            rate = float(stripped)
            return round(rate / 100, 6) if rate > 1 else round(rate, 6)
        except ValueError:
            pass
    return round(counts["pass"] / counts["total"], 6) if counts.get("total") else 0.0


def dynamic_outcome(counts: dict[str, int]) -> str:
    total = counts.get("total", 0)
    if total == 0:
        return "unresolved"
    if counts.get("pending", 0) == total:
        return "all_pending"
    if counts.get("unsupported", 0) == total:
        return "all_unsupported"
    if counts.get("blocked", 0) == total:
        return "all_blocked"
    if counts.get("fail", 0) == total:
        return "all_fail"
    if counts.get("pass", 0) == total:
        return "all_pass"
    return "mixed"


def bronze_label(counts: dict[str, int]) -> str:
    return "no_observed_issue" if dynamic_outcome(counts) == "all_pass" else (
        "dynamic_unresolved" if counts.get("total", 0) == 0 else "dynamic_nonpass_needs_review"
    )


def read_linux_dynamic_rows(linux_datasets: Path) -> list[dict[str, Any]]:
    observations = iter_jsonl(linux_datasets / "dynamic-task-observations.jsonl")
    aggregate: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "task_counts": Counter(),
            "runs": set(),
            "source": "",
            "package_id": "",
            "runner_models": set(),
            "cost_usd": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
        }
    )
    for row in observations:
        skill_id = str(row.get("skill_id") or "")
        if not skill_id:
            continue
        item = aggregate[skill_id]
        item["source"] = canonical_source(row.get("source") or source_of(skill_id))
        item["package_id"] = str(row.get("skill") or package_of(skill_id))
        item["runs"].add(str(row.get("run_id") or ""))
        model = row.get("agent_model") or row.get("executor_model") or row.get("runner")
        if model:
            item["runner_models"].add(str(model))
        item["cost_usd"] += float(row.get("runner_cost_usd") or 0)
        item["input_tokens"] += int_value(row.get("runner_input_tokens"))
        item["output_tokens"] += int_value(row.get("runner_output_tokens"))
        for key, value in (row.get("task_counts") or {}).items():
            normalized_key = "pass" if key == "passed" else "fail" if key == "failed" else key
            if normalized_key in COUNT_KEYS:
                item["task_counts"][normalized_key] += int_value(value)

    rows = []
    for skill_id, item in sorted(aggregate.items()):
        counts = {key: int(item["task_counts"].get(key, 0)) for key in COUNT_KEYS}
        if not counts["total"]:
            counts["total"] = sum(counts[key] for key in ("pass", "fail", "blocked", "unsupported", "pending", "unknown"))
        source = item["source"] or source_of(skill_id)
        package_id = item["package_id"] or package_of(skill_id)
        rows.append(
            normalized_dynamic_row(
                target_os="linux",
                source=source,
                package_id=package_id,
                skill_id=f"{source}/{package_id}",
                release_tag="local-linux-runs",
                source_artifact="docs/linux-testing/datasets/dynamic-task-observations.jsonl",
                rank=None,
                rank_scope="local_dynamic_history",
                sample_status="unknown",
                exec_status="exec_full" if counts["total"] else "exec_unresolved",
                task_counts=counts,
                success_rate=parse_success_rate(None, counts),
                runner_models=sorted(item["runner_models"]),
                failure_category="",
                raw_paths=sorted(path for path in item["runs"] if path),
                extra={
                    "runner_cost_usd": round(float(item["cost_usd"]), 6),
                    "runner_input_tokens": int(item["input_tokens"]),
                    "runner_output_tokens": int(item["output_tokens"]),
                },
            )
        )
    return rows


def read_macos_v06_rows(reports_root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    denominator_rows: list[dict[str, Any]] = []
    dynamic_rows: list[dict[str, Any]] = []
    csv_paths = [
        ("ClawHub", reports_root / "reports" / "clawhub_top5000_database_index.csv"),
        ("SkillSh", reports_root / "reports" / "skillsh_top5000_database_index.csv"),
    ]
    for source, csv_path in csv_paths:
        if not csv_path.is_file():
            continue
        with csv_path.open(newline="", encoding="utf-8") as handle:
            for raw in csv.DictReader(handle):
                package_id = raw.get("index_top5000_dir") or raw.get("index_matched_db_dir") or raw.get("index_slug") or ""
                family = slug_family(source, package_id, raw.get("index_slug") or "")
                rank = int_value(raw.get("index_rank"))
                status = raw.get("db_test_status") or "unknown"
                denominator_rows.append(
                    {
                        "schema_version": "cross_os_denominator_index_v1",
                        "target_os": "macos",
                        "source": source,
                        "skill_id": f"{source}/{package_id}",
                        "package_id": package_id,
                        "slug_family": family,
                        "rank": rank,
                        "rank_scope": "top5000",
                        "release_tag": "v0.6.0",
                        "release_url": "skillbridge-public-raw-release",
                        "db_test_status": status,
                        "sample_status": "sample_full" if raw.get("db_sample_exists") == "YES" else "sample_missing",
                        "exec_status": "exec_full" if raw.get("db_exec_exists") == "YES" else "exec_missing",
                        "db_sample_path": raw.get("db_sample_path") or "",
                        "db_exec_path": raw.get("db_exec_path") or "",
                    }
                )
                if status != "tested_full":
                    continue
                counts = {
                    "pass": int_value(raw.get("exec_pass_count")),
                    "fail": int_value(raw.get("exec_fail_count")),
                    "blocked": int_value(raw.get("exec_blocked_count")),
                    "unsupported": int_value(raw.get("exec_unsupported_count")),
                    "pending": int_value(raw.get("exec_pending_count")),
                    "unknown": 0,
                    "total": int_value(raw.get("exec_total_tasks")),
                }
                counts["unknown"] = max(
                    counts["total"] - sum(counts[key] for key in ("pass", "fail", "blocked", "unsupported", "pending")),
                    0,
                )
                dynamic_rows.append(
                    normalized_dynamic_row(
                        target_os="macos",
                        source=source,
                        package_id=package_id,
                        skill_id=f"{source}/{package_id}",
                        release_tag="v0.6.0",
                        source_artifact=_display_path(csv_path),
                        rank=rank,
                        rank_scope="top5000",
                        sample_status="sample_full" if raw.get("db_sample_exists") == "YES" else "sample_missing",
                        exec_status="exec_full",
                        task_counts=counts,
                        success_rate=parse_success_rate(raw.get("exec_success_rate_pct"), counts),
                        runner_models=[value for value in (raw.get("exec_task_design_model"), raw.get("exec_executor_model")) if value],
                        failure_category=raw.get("exec_dominant_failure_category") or "",
                        raw_paths=[path for path in (raw.get("db_sample_path"), raw.get("db_exec_path")) if path],
                    )
                )
    return denominator_rows, dynamic_rows


def read_macos_v06_exec_rows(exec_asset: Path, samples_asset: Path | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not exec_asset.is_file():
        return [], {"asset_present": False, "release_tag": "v0.6.0"}

    sample_skill_ids, sample_quality = inspect_macos_samples_asset(samples_asset)
    rows: list[dict[str, Any]] = []
    exec_skill_dirs: set[str] = set()
    platform_summary_skill_ids: set[str] = set()
    platform_summary_parse_ok_skill_ids: set[str] = set()
    metrics_skill_ids: set[str] = set()
    task_metric_files = 0
    parse_errors = 0
    platform_summary_files = 0
    result_bearing_skill_ids: set[str] = set()
    source_counts_summary: Counter[str] = Counter()
    summary_records: list[tuple[str, str, str, dict[str, Any]]] = []
    metrics_counts_by_skill: dict[str, dict[str, int]] = {}

    with tarfile.open(exec_asset, "r:gz") as tar:
        for member in tar:
            parsed = source_package_from_macos_exec_path(member.name)
            if parsed[0] != "unknown" and parsed[1]:
                source = canonical_source(parsed[0])
                skill_id = f"{source}/{parsed[1]}"
                exec_skill_dirs.add(skill_id)
                if member.name.endswith("/metrics.json"):
                    metrics_skill_ids.add(skill_id)
                    fileobj = tar.extractfile(member)
                    if fileobj is not None:
                        try:
                            metrics_counts_by_skill[skill_id] = normalize_counts(json.load(fileobj))
                        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                            pass
                if member.name.endswith("/task_metrics.json"):
                    task_metric_files += 1

            if Path(member.name).name != "platform_summary.json":
                continue
            path_source, fallback_package = parsed
            if path_source == "unknown" or not fallback_package:
                continue
            platform_summary_files += 1
            source = canonical_source(path_source)
            package_id = fallback_package
            skill_id = f"{source}/{package_id}"
            platform_summary_skill_ids.add(skill_id)
            fileobj = tar.extractfile(member)
            if fileobj is None:
                parse_errors += 1
                continue
            try:
                summary = json.load(fileobj)
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                parse_errors += 1
                continue
            platform_summary_parse_ok_skill_ids.add(skill_id)
            source_counts_summary[source] += 1
            summary_records.append((source, package_id, member.name, summary))

    for source, package_id, raw_path, summary in summary_records:
        skill_id = f"{source}/{package_id}"
        summary_counts = normalize_counts(summary)
        metrics_counts = metrics_counts_by_skill.get(skill_id, {})
        counts = (
            metrics_counts
            if status_count_sum(metrics_counts) > status_count_sum(summary_counts)
            or int_value(metrics_counts.get("total")) > int_value(summary_counts.get("total"))
            else summary_counts
        )
        if counts["total"]:
            result_bearing_skill_ids.add(skill_id)
        rows.append(
            normalized_dynamic_row(
                target_os="macos",
                source=source,
                package_id=package_id,
                skill_id=skill_id,
                release_tag="v0.6.0",
                source_artifact=_display_path(exec_asset),
                rank=None,
                rank_scope="release_dynamic_history",
                sample_status="sample_full" if skill_id in sample_skill_ids else "sample_unknown",
                exec_status="exec_full" if counts["total"] else "exec_unresolved",
                task_counts=counts,
                success_rate=parse_success_rate(summary.get("success_rate"), counts),
                runner_models=[
                    value
                    for value in (
                        summary.get("task_design_model"),
                        summary.get("executor_model"),
                        summary.get("runner"),
                    )
                    if value
                ],
                failure_category=dominant_failure_category(summary),
                raw_paths=[raw_path],
                extra={
                    "summary_schema_version": summary.get("schema_version") or "",
                    "count_source": "metrics_json" if skill_id in metrics_counts_by_skill else "platform_summary_json",
                },
            )
        )

    source_counts_dirs = Counter(source_of(skill_id) for skill_id in exec_skill_dirs)
    quality = {
        "asset_present": True,
        "release_tag": "v0.6.0",
        "release_url": "skillbridge-public-raw-release",
        "exec_asset_name": exec_asset.name,
        "exec_asset_bytes": exec_asset.stat().st_size,
        "unique_exec_skill_dirs": len(exec_skill_dirs),
        "platform_summary_files": platform_summary_files,
        "platform_summary_skills": len(platform_summary_skill_ids),
        "platform_summary_parse_ok_skills": len(platform_summary_parse_ok_skill_ids),
        "dynamic_rows": len(rows),
        "result_bearing_dynamic_rows": len(result_bearing_skill_ids),
        "count_source_counts": dict(sorted(Counter(row.get("count_source", "") for row in rows).items())),
        "metrics_skills": len(metrics_skill_ids),
        "task_metric_files": task_metric_files,
        "parse_errors": parse_errors,
        "source_counts_unique_dirs": dict(sorted(source_counts_dirs.items())),
        "source_counts_platform_summary": dict(sorted(source_counts_summary.items())),
        "missing_platform_summary_count": len(exec_skill_dirs - platform_summary_skill_ids),
        "missing_platform_summary": sorted(exec_skill_dirs - platform_summary_skill_ids),
        "missing_or_unparseable_platform_summary_count": len(exec_skill_dirs - platform_summary_parse_ok_skill_ids),
        "missing_or_unparseable_platform_summary": sorted(exec_skill_dirs - platform_summary_parse_ok_skill_ids),
        **sample_quality,
    }
    return rows, quality


def inspect_macos_samples_asset(samples_asset: Path | None) -> tuple[set[str], dict[str, Any]]:
    if samples_asset is None or not samples_asset.is_file():
        return set(), {
            "sample_archive_status": "missing",
            "sample_archive_bytes": 0,
            "sample_skill_dirs": 0,
            "sample_source_counts": {},
        }

    sample_skill_ids: set[str] = set()
    with tarfile.open(samples_asset, "r:gz") as tar:
        for member in tar:
            parsed = source_package_from_samples_path(member.name)
            if parsed[0] != "unknown" and parsed[1]:
                sample_skill_ids.add(f"{canonical_source(parsed[0])}/{parsed[1]}")

    source_counts = Counter(source_of(skill_id) for skill_id in sample_skill_ids)
    return sample_skill_ids, {
        "sample_archive_status": "readable",
        "sample_archive_bytes": samples_asset.stat().st_size,
        "sample_asset_name": samples_asset.name,
        "sample_skill_dirs": len(sample_skill_ids),
        "sample_source_counts": dict(sorted(source_counts.items())),
    }


def read_windows_v04_rows(asset: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not asset.is_file():
        return [], {"asset_present": False}

    rows: list[dict[str, Any]] = []
    quality = {
        "asset_present": True,
        "platform_summary_files": 0,
        "sample_archive_status": "missing",
        "sample_archive_bytes": 0,
        "parse_errors": 0,
    }
    with tarfile.open(asset, "r:gz") as tar:
        for member in tar:
            if member.name.endswith("samples.tar.gz"):
                quality["sample_archive_bytes"] = int(member.size)
                sample_file = tar.extractfile(member)
                quality["sample_archive_status"] = inspect_nested_gzip(sample_file.read() if sample_file else b"")
                continue
            if not member.name.endswith("platform_summary.json"):
                continue
            quality["platform_summary_files"] += 1
            fileobj = tar.extractfile(member)
            if fileobj is None:
                quality["parse_errors"] += 1
                continue
            try:
                summary = json.load(fileobj)
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                quality["parse_errors"] += 1
                continue
            path_source, package_id = source_package_from_windows_path(member.name)
            summary_source = canonical_source(summary.get("skill_source") or summary.get("skill") or "")
            source = canonical_source(path_source) if is_known_source(path_source) else summary_source
            if source == "unknown":
                source = canonical_source(path_source)
            package_id = package_from_summary(summary, source, package_id)
            counts = normalize_counts(summary)
            rows.append(
                normalized_dynamic_row(
                    target_os="windows",
                    source=source,
                    package_id=package_id,
                    skill_id=f"{source}/{package_id}",
                    release_tag="v0.4.0-windows",
                    source_artifact=_display_path(asset),
                    rank=None,
                    rank_scope="release_dynamic_history",
                    sample_status="sample_broken" if quality["sample_archive_status"] == "broken_or_truncated" else "sample_unknown",
                    exec_status="exec_full" if counts["total"] else "exec_unresolved",
                    task_counts=counts,
                    success_rate=parse_success_rate(summary.get("success_rate"), counts),
                    runner_models=[value for value in (summary.get("task_design_model"), summary.get("executor_model"), summary.get("runner")) if value],
                    failure_category=dominant_failure_category(summary),
                    raw_paths=[member.name],
                    extra={
                        "summary_schema_version": summary.get("schema_version") or "",
                    },
                )
            )
    return rows, quality


def read_windows_v05_rows(exec_assets: list[Path], samples_asset: Path | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    assets = [path for path in exec_assets if path.is_file()]
    if not assets:
        return [], {"asset_present": False, "release_tag": "v0.5.0-windows"}

    sample_skill_ids, sample_quality = inspect_windows_samples_asset(samples_asset)
    rows: list[dict[str, Any]] = []
    exec_skill_dirs: set[str] = set()
    platform_summary_skill_ids: set[str] = set()
    platform_summary_file_skill_ids: set[str] = set()
    platform_summary_parse_ok_skill_ids: set[str] = set()
    metrics_skill_ids: set[str] = set()
    task_metrics_skill_ids: set[str] = set()
    source_counts_summary: Counter[str] = Counter()
    parse_errors = 0
    platform_summary_files = 0
    task_metric_files = 0
    asset_details: list[dict[str, Any]] = []

    for asset in assets:
        asset_dirs: set[str] = set()
        asset_summary_file_skill_ids: set[str] = set()
        asset_summary_parse_ok_skill_ids: set[str] = set()
        asset_parse_errors = 0
        asset_task_metric_files = 0
        with tarfile.open(asset, "r:gz") as tar:
            for member in tar:
                parsed = source_package_from_windows_path(member.name)
                if parsed[0] != "unknown" and parsed[1]:
                    source = canonical_source(parsed[0])
                    skill_id = f"{source}/{parsed[1]}"
                    exec_skill_dirs.add(skill_id)
                    asset_dirs.add(skill_id)
                    if member.name.endswith("/metrics.json"):
                        metrics_skill_ids.add(skill_id)
                    if member.name.endswith("/task_metrics.json"):
                        task_metrics_skill_ids.add(skill_id)
                        task_metric_files += 1
                        asset_task_metric_files += 1

                if not member.name.endswith("platform_summary.json"):
                    continue
                platform_summary_files += 1
                path_source, fallback_package = parsed
                path_skill_id = (
                    f"{canonical_source(path_source)}/{fallback_package}"
                    if path_source != "unknown" and fallback_package
                    else ""
                )
                if path_skill_id:
                    platform_summary_file_skill_ids.add(path_skill_id)
                    asset_summary_file_skill_ids.add(path_skill_id)
                fileobj = tar.extractfile(member)
                if fileobj is None:
                    parse_errors += 1
                    asset_parse_errors += 1
                    continue
                try:
                    summary = json.load(fileobj)
                except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                    parse_errors += 1
                    asset_parse_errors += 1
                    continue
                if path_skill_id:
                    platform_summary_parse_ok_skill_ids.add(path_skill_id)
                    asset_summary_parse_ok_skill_ids.add(path_skill_id)
                summary_source = canonical_source(summary.get("skill_source") or summary.get("skill") or "")
                source = canonical_source(path_source) if is_known_source(path_source) else summary_source
                if source == "unknown":
                    source = canonical_source(path_source)
                package_id = fallback_package or package_from_summary(summary, source, fallback_package)
                skill_id = f"{source}/{package_id}"
                platform_summary_skill_ids.add(skill_id)
                source_counts_summary[source] += 1
                counts = normalize_counts(summary)
                rows.append(
                    normalized_dynamic_row(
                        target_os="windows",
                        source=source,
                        package_id=package_id,
                        skill_id=skill_id,
                        release_tag="v0.5.0-windows",
                        source_artifact=_display_path(asset),
                        rank=None,
                        rank_scope="release_dynamic_history",
                        sample_status="sample_full" if skill_id in sample_skill_ids else "sample_unknown",
                        exec_status="exec_full" if counts["total"] else "exec_unresolved",
                        task_counts=counts,
                        success_rate=parse_success_rate(summary.get("success_rate"), counts),
                        runner_models=[
                            value
                            for value in (
                                summary.get("task_design_model"),
                                summary.get("executor_model"),
                                summary.get("runner"),
                            )
                            if value
                        ],
                        failure_category=dominant_failure_category(summary),
                        raw_paths=[member.name],
                        extra={
                            "summary_schema_version": summary.get("schema_version") or "",
                        },
                    )
                )
        asset_details.append(
            {
                "asset_name": asset.name,
                "asset_bytes": asset.stat().st_size,
                "unique_exec_skill_dirs": len(asset_dirs),
                "platform_summary_skills": len(asset_summary_file_skill_ids),
                "platform_summary_parse_ok_skills": len(asset_summary_parse_ok_skill_ids),
                "parse_errors": asset_parse_errors,
                "task_metric_files": asset_task_metric_files,
            }
        )

    source_counts_dirs = Counter(source_of(skill_id) for skill_id in exec_skill_dirs)
    quality = {
        "asset_present": True,
        "release_tag": "v0.5.0-windows",
        "release_url": "skillbridge-public-raw-release",
        "asset_count": len(assets),
        "asset_names": [path.name for path in assets],
        "asset_details": asset_details,
        "unique_exec_skill_dirs": len(exec_skill_dirs),
        "platform_summary_files": platform_summary_files,
        "platform_summary_skills": len(platform_summary_file_skill_ids),
        "platform_summary_parse_ok_skills": len(platform_summary_parse_ok_skill_ids),
        "dynamic_rows": len(rows),
        "summary_field_unique_skill_ids": len(platform_summary_skill_ids),
        "metrics_skills": len(metrics_skill_ids),
        "task_metrics_skill_dirs": len(task_metrics_skill_ids),
        "task_metric_files": task_metric_files,
        "parse_errors": parse_errors,
        "source_counts_unique_dirs": dict(sorted(source_counts_dirs.items())),
        "source_counts_platform_summary": dict(sorted(source_counts_summary.items())),
        "missing_platform_summary_count": len(exec_skill_dirs - platform_summary_file_skill_ids),
        "missing_platform_summary": sorted(exec_skill_dirs - platform_summary_file_skill_ids),
        "missing_or_unparseable_platform_summary_count": len(exec_skill_dirs - platform_summary_parse_ok_skill_ids),
        "missing_or_unparseable_platform_summary": sorted(exec_skill_dirs - platform_summary_parse_ok_skill_ids),
        **sample_quality,
    }
    return rows, quality


def inspect_windows_samples_asset(samples_asset: Path | None) -> tuple[set[str], dict[str, Any]]:
    if samples_asset is None or not samples_asset.is_file():
        return set(), {
            "sample_archive_status": "missing",
            "sample_archive_bytes": 0,
            "sample_skill_dirs": 0,
            "sample_source_counts": {},
        }

    sample_skill_ids: set[str] = set()
    with tarfile.open(samples_asset, "r:gz") as tar:
        for member in tar:
            parts = Path(member.name).parts
            try:
                idx = parts.index("samples")
            except ValueError:
                continue
            if len(parts) <= idx + 2:
                continue
            source = canonical_source(parts[idx + 1])
            package_id = parts[idx + 2]
            if source and package_id:
                sample_skill_ids.add(f"{source}/{package_id}")

    source_counts = Counter(source_of(skill_id) for skill_id in sample_skill_ids)
    return sample_skill_ids, {
        "sample_archive_status": "readable",
        "sample_archive_bytes": samples_asset.stat().st_size,
        "sample_skill_dirs": len(sample_skill_ids),
        "sample_source_counts": dict(sorted(source_counts.items())),
    }


def scan_windows_release_diagnostics(v03_exec_dir: Path, v02_asset: Path) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    v03_paths = sorted(v03_exec_dir.glob("compa-skill-windows-exec-*.tar")) if v03_exec_dir.is_dir() else []
    if v03_paths:
        diagnostics.append(scan_windows_tar_group("v0.3.0-windows", v03_paths, mode="r:"))
    if v02_asset.is_file():
        diagnostics.append(scan_windows_tar_group("v0.2.0-windows", [v02_asset], mode="r:gz"))
    return diagnostics


def scan_windows_tar_group(release_tag: str, paths: list[Path], mode: str) -> dict[str, Any]:
    result = {
        "release_tag": release_tag,
        "asset_names": [path.name for path in paths],
        "platform_summary_files": 0,
        "parse_ok": 0,
        "parse_errors": 0,
        "task_metric_files": 0,
        "source_counts_by_path": {},
        "role": "historical_windows_dynamic_release",
    }
    source_counts: Counter[str] = Counter()
    for path in paths:
        with tarfile.open(path, mode) as tar:
            for member in tar:
                name = member.name
                if name.endswith("task_metrics.json"):
                    result["task_metric_files"] += 1
                if not name.endswith("platform_summary.json"):
                    continue
                result["platform_summary_files"] += 1
                source, _package_id = source_package_from_windows_path(name)
                source_counts[canonical_source(source)] += 1
                fileobj = tar.extractfile(member)
                if fileobj is None:
                    result["parse_errors"] += 1
                    continue
                try:
                    json.load(fileobj)
                    result["parse_ok"] += 1
                except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                    result["parse_errors"] += 1
    result["source_counts_by_path"] = dict(sorted(source_counts.items()))
    return result


def inspect_nested_gzip(payload: bytes) -> str:
    if not payload:
        return "missing"
    try:
        with tarfile.open(fileobj=gzip.GzipFile(fileobj=io.BytesIO(payload)), mode="r:") as tar:
            members = tar.getmembers()
    except (EOFError, OSError, tarfile.TarError):
        return "broken_or_truncated"
    return "readable" if members else "broken_or_truncated"


def source_package_from_windows_path(path: str) -> tuple[str, str]:
    parts = Path(path).parts
    try:
        idx = parts.index("exec")
    except ValueError:
        return "unknown", Path(path).parent.name
    if len(parts) > idx + 1 and parts[idx + 1].lower() in {"windows", "win32"}:
        if len(parts) > idx + 3:
            return parts[idx + 2], parts[idx + 3]
        return "unknown", ""
    if len(parts) > idx + 2:
        return parts[idx + 1], parts[idx + 2]
    return "unknown", Path(path).parent.name


def source_package_from_macos_exec_path(path: str) -> tuple[str, str]:
    parts = Path(path).parts
    if any(part.startswith("._") for part in parts):
        return "unknown", ""
    try:
        idx = parts.index("macos")
    except ValueError:
        return "unknown", ""
    if len(parts) > idx + 2:
        return parts[idx + 1], parts[idx + 2]
    return "unknown", ""


def source_package_from_samples_path(path: str) -> tuple[str, str]:
    parts = Path(path).parts
    if any(part.startswith("._") for part in parts):
        return "unknown", ""
    try:
        idx = parts.index("samples")
    except ValueError:
        return "unknown", ""
    if len(parts) > idx + 2:
        return parts[idx + 1], parts[idx + 2]
    return "unknown", ""


def package_from_summary(summary: dict[str, Any], fallback_source: str, fallback_package: str) -> str:
    for key in ("skill_name", "skill", "skill_source"):
        parsed = parse_package_candidate(summary.get(key), fallback_source)
        if parsed:
            return parsed
    return fallback_package


def parse_package_candidate(value: Any, fallback_source: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    tokens = [token for token in re.split(r"[\\/]+", text) if token]
    if len(tokens) > 1:
        for idx, token in enumerate(tokens[:-1]):
            if canonical_source(token) == canonical_source(fallback_source):
                return tokens[idx + 1]
        return ""
    if canonical_source(text) == canonical_source(fallback_source):
        return ""
    return text


def dominant_failure_category(summary: dict[str, Any]) -> str:
    for key in ("exec_dominant_failure_category", "dominant_failure_category", "dominant_blocker", "dominant_failure_pattern"):
        value = summary.get(key)
        if value:
            return str(value)
    for key in ("failure_distribution", "failures_by_category"):
        raw = summary.get(key)
        if isinstance(raw, dict):
            flattened = flatten_failure_distribution(raw)
            if flattened:
                return flattened.most_common(1)[0][0]
    raw_tasks = summary.get("task_results")
    if isinstance(raw_tasks, list):
        counts = Counter(
            str(item.get("failure_category"))
            for item in raw_tasks
            if isinstance(item, dict) and item.get("failure_category")
        )
        if counts:
            return counts.most_common(1)[0][0]
    return ""


def flatten_failure_distribution(raw: dict[str, Any]) -> Counter[str]:
    counts: Counter[str] = Counter()
    if isinstance(raw.get("category_count"), dict):
        for key, value in raw["category_count"].items():
            counts[str(key or "unknown")] += int_value(value)
    for key, value in raw.items():
        if key in {"category_count", "most_common_category"}:
            continue
        if isinstance(value, (int, float, str)):
            counts[str(key or "unknown")] += int_value(value) or 1
    return counts


def normalized_dynamic_row(
    *,
    target_os: str,
    source: str,
    package_id: str,
    skill_id: str,
    release_tag: str,
    source_artifact: str,
    rank: int | None,
    rank_scope: str,
    sample_status: str,
    exec_status: str,
    task_counts: dict[str, int],
    success_rate: float,
    runner_models: list[str],
    failure_category: str,
    raw_paths: list[str],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source = canonical_source(source)
    package_id = package_id or package_of(skill_id)
    counts = {key: int(task_counts.get(key, 0)) for key in COUNT_KEYS}
    row = {
        "schema_version": "cross_os_dynamic_skill_label_v1",
        "target_os": target_os,
        "source": source,
        "skill_id": f"{source}/{package_id}",
        "package_id": package_id,
        "slug_family": slug_family(source, package_id),
        "rank": rank,
        "rank_scope": rank_scope,
        "release_tag": release_tag,
        "source_artifact": source_artifact,
        "sample_status": sample_status,
        "exec_status": exec_status,
        "task_counts": counts,
        "success_rate": success_rate,
        "dynamic_outcome": dynamic_outcome(counts),
        "bronze_dynamic_label": bronze_label(counts),
        "dominant_failure_category": failure_category,
        "runner_models": sorted(set(str(model) for model in runner_models if model)),
        "raw_paths": sorted(set(raw_paths)),
        "label_quality": "bronze_raw",
        "root_cause_note": "Dynamic observation only; not an OS compatibility ground truth label.",
    }
    if extra:
        row.update(extra)
    return row


def dedupe_dynamic_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    kept: dict[tuple[str, str], dict[str, Any]] = {}
    duplicate_counts: Counter[tuple[str, str]] = Counter()
    dropped_by_os: Counter[str] = Counter()

    for row in rows:
        key = (str(row.get("target_os") or ""), str(row.get("skill_id") or ""))
        if key not in kept:
            kept[key] = row
            continue

        duplicate_counts[key] += 1
        current = kept[key]
        if dynamic_row_preference(row) > dynamic_row_preference(current):
            dropped_by_os[str(current.get("target_os") or "")] += 1
            kept[key] = row
        else:
            dropped_by_os[str(row.get("target_os") or "")] += 1

    deduped = sorted(kept.values(), key=lambda row: (row["target_os"], row["source"], row["package_id"]))
    stats = {
        "duplicate_dynamic_rows_dropped": sum(duplicate_counts.values()),
        "duplicate_dynamic_keys": len(duplicate_counts),
        "duplicate_dynamic_rows_dropped_by_os": dict(sorted(dropped_by_os.items())),
    }
    return deduped, stats


def dynamic_row_preference(row: dict[str, Any]) -> tuple[int, int, int, int]:
    exec_score = {"exec_full": 3, "exec_unresolved": 2, "exec_missing": 1}.get(str(row.get("exec_status") or ""), 0)
    sample_score = {"sample_full": 3, "sample_unknown": 2, "sample_missing": 1, "sample_broken": 0}.get(
        str(row.get("sample_status") or ""),
        0,
    )
    rank = int_value(row.get("rank"))
    rank_score = -rank if rank else -1_000_000
    total = int_value((row.get("task_counts") or {}).get("total"))
    return total, exec_score, sample_score, rank_score


def read_reviewed_rows(repo_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    macos_characteristic_ids: set[str] = set()
    macos_characteristic_path = (
        repo_root / "docs" / "macos-testing" / "analysis" / "macos-os-characteristic-findings-20260624.json"
    )
    if macos_characteristic_path.is_file():
        for skill_id in load_json(macos_characteristic_path).get("characteristic_issue_skill_ids", []):
            macos_characteristic_ids.add(str(skill_id))
            macos_characteristic_ids.add(canonical_skill_id(str(skill_id)))

    review_sources: list[tuple[str, Path, str]] = [
        (
            "linux",
            repo_root / "docs" / "linux-testing" / "datasets" / "os-compat-reviewed-labels.jsonl",
            "reviewed_seed",
        )
    ]
    macos_full_review = (
        repo_root
        / "docs"
        / "macos-testing"
        / "reviews"
        / "full-static1477-cc-review-20260624"
        / "full-cc-review-rows.jsonl"
    )
    if macos_full_review.is_file():
        review_sources.append(("macos", macos_full_review, "cc_full_review"))
    else:
        review_sources.append(
            (
                "macos",
                repo_root / "docs" / "macos-testing" / "datasets" / "os-compat-reviewed-labels.jsonl",
                "reviewed_seed",
            )
        )

    for target_os, path, label_source in review_sources:
        for raw in iter_jsonl(path):
            skill_id = str(raw.get("skill_id") or "")
            source = source_of(skill_id)
            package_id = package_of(skill_id)
            primary_label = str(raw.get("primary_label") or "")
            normalized_skill_id = f"{source}/{package_id}"
            in_scope = raw.get("scope_status") == "in_scope"
            strict_os_install = in_scope and primary_label in {
                "os_specific_execution_issue",
                "os_install_or_dependency_contract_issue",
            }
            rows.append(
                {
                    "schema_version": "cross_os_reviewed_label_v1",
                    "target_os": raw.get("target_os") or target_os,
                    "source": source,
                    "skill_id": normalized_skill_id,
                    "package_id": package_id,
                    "slug_family": slug_family(source, package_id),
                    "scope_status": raw.get("scope_status") or "",
                    "primary_label": primary_label,
                    "secondary_tags": raw.get("secondary_tags") or [],
                    "is_os_compat_issue": in_scope and primary_label != "not_os_issue",
                    "is_strict_os_install_issue": strict_os_install,
                    "is_package_entrypoint_issue": in_scope and primary_label == "package_or_entrypoint_issue",
                    "is_legacy_macos_os_characteristic_issue": target_os == "macos"
                    and normalized_skill_id in macos_characteristic_ids,
                    "is_paper_facing_os_characteristic_issue": target_os == "macos" and strict_os_install,
                    "fixability": raw.get("fixability") or "",
                    "confidence": raw.get("confidence") or "",
                    "evidence_level": raw.get("evidence_level") or "",
                    "label_quality": raw.get("label_quality") or label_source,
                    "label_source": label_source,
                    "source_run_id": raw.get("source_run_id") or raw.get("_candidate_scope") or label_source,
                    "reviewed_at": raw.get("reviewed_at") or ("2026-06-24" if label_source == "cc_full_review" else ""),
                    "notes": raw.get("notes") or "",
                    "raw_record": raw,
                }
            )
    return sorted(rows, key=lambda row: (row["target_os"], row["source"], row["package_id"]))


def build_summary(
    dynamic_rows: list[dict[str, Any]],
    reviewed_rows: list[dict[str, Any]],
    denominator_rows: list[dict[str, Any]],
    macos_quality: dict[str, Any],
    windows_quality: dict[str, Any],
    windows_historical_diagnostics: list[dict[str, Any]],
    dedupe_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    by_os: dict[str, dict[str, Any]] = {}
    for target_os in ("linux", "macos", "windows"):
        rows = [row for row in dynamic_rows if row["target_os"] == target_os]
        review = [row for row in reviewed_rows if row["target_os"] == target_os]
        counts = {key: sum(int(row["task_counts"].get(key, 0)) for row in rows) for key in COUNT_KEYS}
        by_os[target_os] = {
            "dynamic_skill_rows": len(rows),
            "reviewed_label_rows": len(review),
            "reviewed_in_scope_rows": sum(1 for row in review if row["scope_status"] == "in_scope"),
            "reviewed_os_issue_rows": sum(1 for row in review if row["is_os_compat_issue"]),
            "reviewed_package_entrypoint_rows": sum(1 for row in review if row.get("is_package_entrypoint_issue")),
            "reviewed_strict_os_install_rows": sum(1 for row in review if row.get("is_strict_os_install_issue")),
            "reviewed_legacy_macos_os_characteristic_rows": sum(
                1 for row in review if row.get("is_legacy_macos_os_characteristic_issue")
            ),
            "reviewed_paper_facing_os_characteristic_rows": sum(
                1 for row in review if row.get("is_paper_facing_os_characteristic_issue")
            ),
            "task_counts": counts,
            "dynamic_outcome_counts": dict(sorted(Counter(row["dynamic_outcome"] for row in rows).items())),
            "source_counts": dict(sorted(Counter(row["source"] for row in rows).items())),
            "dominant_failure_category_counts": dict(
                sorted(Counter(row["dominant_failure_category"] or "" for row in rows).items())
            ),
        }

    macos_denominator = [row for row in denominator_rows if row["target_os"] == "macos"]
    windows_release_tag = windows_quality.get("release_tag") or "v0.4.0-windows"
    windows_release_url = windows_quality.get("release_url") or "skillbridge-public-raw-release"
    windows_quality_note = (
        "Windows v0.5.0 draft has multi-asset exec/sample data; clean dynamic rows use parseable platform summaries."
        if windows_release_tag == "v0.5.0-windows"
        else "Windows v0.4.0 sample archive is marked separately from parsed execution summaries."
    )
    return {
        "schema_version": "cross_os_dataset_summary_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "unit": "skill package version on one target OS",
        "dynamic_rows": len(dynamic_rows),
        "reviewed_rows": len(reviewed_rows),
        "denominator_rows": len(denominator_rows),
        "dedupe": dedupe_stats or {
            "duplicate_dynamic_rows_dropped": 0,
            "duplicate_dynamic_keys": 0,
            "duplicate_dynamic_rows_dropped_by_os": {},
        },
        "by_os": by_os,
        "denominators": {
            "macos_v0.6.0_top5000_rows": len(macos_denominator),
            "macos_v0.6.0_top5000_tested_full_rows": sum(
                1 for row in macos_denominator if row["db_test_status"] == "tested_full"
            ),
            "macos_v0.6.0_top5000_sample_only_rows": sum(
                1 for row in macos_denominator if row["db_test_status"] == "tested_sample_only"
            ),
            "macos_v0.6.0_top5000_not_tested_rows": sum(
                1 for row in macos_denominator if row["db_test_status"] == "not_tested"
            ),
        },
        "release_inventory": [
            {
                "target_os": "linux",
                "release_tag": "local-linux-runs",
                "role": "local dynamic run history",
                "source": "docs/linux-testing/datasets/dynamic-task-observations.jsonl",
                "quality": "clean local dataset; reviewed labels stored separately",
            },
            {
                "target_os": "macos",
                "release_tag": "v0.6.0",
                "role": "unified ClawHub + SkillSh macOS full exec summaries plus top5000 index denominator",
                "url": "skillbridge-public-raw-release",
                "quality": "preferred macOS source; full exec asset is used for dynamic rows when available",
                "asset_quality": macos_quality,
            },
            {
                "target_os": "windows",
                "release_tag": windows_release_tag,
                "role": "Windows dynamic execution summaries",
                "url": windows_release_url,
                "quality": windows_quality_note,
                "asset_quality": windows_quality,
            },
        ],
        "historical_release_diagnostics": {
            "windows": windows_historical_diagnostics,
        },
        "notes": [
            "Dynamic rows are symptoms and must not be treated as OS root-cause ground truth.",
            "This dynamic builder emits symptom rows; current reviewed labels are joined by the review/inventory stage.",
            "macOS reviewed labels use the 1477-row full Claude Code audit when available; the older 25-row reviewed seed is kept only as historical taxonomy evidence.",
            "macOS reviewed_os_issue_rows is the broad OS/package reviewed count; use reviewed_strict_os_install_rows or reviewed_paper_facing_os_characteristic_rows for the current strict paper-facing cut.",
            "macOS reviewed_legacy_macos_os_characteristic_rows keeps the former 94-row sensitivity cut, which includes package/entrypoint rows now treated as incorrect-download artifacts.",
            "macOS v0.6.0 full exec asset dynamic rows and top5000-index tested rows are different denominators.",
            windows_quality_note,
            "Historical Windows release diagnostics are kept separate from the default dynamic rows.",
        ],
    }


def render_readme(summary: dict[str, Any]) -> str:
    rows = summary["by_os"]
    windows_release_tag = summary["release_inventory"][2].get("release_tag", "Windows release")
    dedupe = summary.get("dedupe", {})
    lines = [
        "# Cross-OS Compatibility Clean Dataset",
        "",
        "Generated by `python3 scripts/build-cross-os-dataset.py`.",
        "",
        "This directory is the normalized layer for comparing Linux, macOS, and Windows skill compatibility data. It does not contain raw release tarballs and it does not execute skill code.",
        "",
        "## Files",
        "",
        "- `unified-dynamic-skill-labels.jsonl`: bronze dynamic observations, one skill package per target OS.",
        "- `unified-reviewed-os-labels.jsonl`: reviewed OS-compatibility labels, currently Linux and macOS only.",
        "- `unified-denominator-index.jsonl`: denominator/ranking rows currently available from macOS v0.6.0 top5000 release reports.",
        "- `cross-os-dataset-summary.json`: machine-readable counts, release inventory, and quality warnings.",
        "- `cross-os-data-quality-support-matrix.json`: machine-readable support/caution/fail calls.",
        "- `cross-os-data-quality-report.md`: paper-facing data quality note.",
        "",
        "## Current Counts",
        "",
        "| OS | Dynamic rows | Reviewed labels | Broad OS/package issues | Strict OS/install | Total tasks | Pass | Fail | Blocked | Unsupported |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for target_os in ("linux", "macos", "windows"):
        item = rows[target_os]
        counts = item["task_counts"]
        lines.append(
            f"| {target_os} | {item['dynamic_skill_rows']} | {item['reviewed_label_rows']} | "
            f"{item['reviewed_os_issue_rows']} | {item['reviewed_paper_facing_os_characteristic_rows']} | "
            f"{counts['total']} | {counts['pass']} | "
            f"{counts['fail']} | {counts['blocked']} | {counts['unsupported']} |"
        )
    lines.extend(
        [
            "",
            "## Denominator Rules",
            "",
            "- Linux dynamic rows come from local run history exported under `docs/linux-testing/datasets/`.",
            "- macOS uses release `v0.6.0` full exec summaries for dynamic rows and release reports for the ClawHub/SkillSh top5000 denominator.",
            "- macOS reviewed labels use the 1477-row full Claude Code audit when available. The older 25-row seed remains a historical taxonomy sanity check.",
            "- For macOS, `Broad OS/package issues` is the broad reviewed positive count. `Strict OS/install` is the current paper-facing cut after excluding package/entrypoint rows caused by incorrect downloads.",
            f"- Windows uses release `{windows_release_tag}` execution summaries for the dynamic symptom layer; current reviewed labels are added by the review/inventory stage.",
            f"- The builder enforces one dynamic row per `(target_os, skill_id)`; current duplicate dynamic rows dropped: {dedupe.get('duplicate_dynamic_rows_dropped', 0)}.",
            "- Dynamic non-pass is not an OS compatibility label. Use reviewed labels for precision/recall and root-cause claims.",
            "",
        ]
    )
    return "\n".join(lines)


def render_quality_report(summary: dict[str, Any]) -> str:
    macos_quality = summary["release_inventory"][1].get("asset_quality", {})
    windows_quality = summary["release_inventory"][2]["asset_quality"]
    windows_release_tag = summary["release_inventory"][2].get("release_tag", "Windows release")
    denominators = summary["denominators"]
    dedupe = summary.get("dedupe", {})
    lines = [
        "# Cross-OS Data Quality Report",
        "",
        "## Support Matrix",
        "",
        "| Claim | Status | Safe Wording | Banned Wording |",
        "| --- | --- | --- | --- |",
        "| We have normalized Linux, macOS, and Windows dynamic observations into one schema. | support | The clean layer contains three-OS bronze dynamic rows with explicit source releases. | All three OS datasets are equally complete. |",
        "| macOS v0.6.0 is the preferred ClawHub/SkillSh aligned macOS source. | support | macOS v0.6.0 gives a cleaner top5000-indexed corpus for ClawHub/SkillSh. | macOS v0.4.0 and v0.6.0 can be mixed as one denominator. |",
        "| macOS full Claude Code review supersedes the 25-row seed for reviewed-label counts. | support | Use the 1477-row full review for current macOS reviewed-label accounting and keep the 25-row seed as historical taxonomy evidence. | macOS has only 25 reviewed labels. |",
        "| macOS broad OS/package positives are the same as paper-facing OS-characteristic issues. | caution | The broad count includes generic package/entrypoint defects; report the narrower OS-characteristic cut for paper-facing OS claims. | Every macOS package/entrypoint defect is an OS-characteristic compatibility issue. |",
        f"| {windows_release_tag} provides the Windows dynamic symptom layer. | caution | Use the current review/inventory stage for Windows root-cause labels and source-static evaluation. | {windows_release_tag} dynamic failures are OS compatibility ground truth. |",
        "| Dynamic failures are OS compatibility issues. | fail | Dynamic failures are symptoms that need reviewed root-cause labels. | Every blocked or failed task is an OS bug. |",
        "",
        "## Key Diagnostics",
        "",
        f"- macOS v0.6.0 top5000 denominator rows: {denominators['macos_v0.6.0_top5000_rows']}.",
        f"- macOS v0.6.0 top5000 tested-full rows: {denominators['macos_v0.6.0_top5000_tested_full_rows']}.",
        f"- macOS v0.6.0 full exec skill dirs found: {macos_quality.get('unique_exec_skill_dirs', 'n/a')}.",
        f"- macOS v0.6.0 platform summaries parse ok: {macos_quality.get('platform_summary_parse_ok_skills', 'n/a')}.",
        f"- macOS v0.6.0 result-bearing dynamic rows: {macos_quality.get('result_bearing_dynamic_rows', 'n/a')}.",
        f"- macOS v0.6.0 count sources: {macos_quality.get('count_source_counts', {})}.",
        f"- macOS v0.6.0 sample skill dirs found: {macos_quality.get('sample_skill_dirs', 'n/a')}.",
        f"- {windows_release_tag} exec skill dirs found: {windows_quality.get('unique_exec_skill_dirs', 'n/a')}.",
        f"- {windows_release_tag} platform summary files found: {windows_quality.get('platform_summary_files', 0)}.",
        f"- {windows_release_tag} platform summary parse errors: {windows_quality.get('parse_errors', 0)}.",
        f"- {windows_release_tag} samples archive status: {windows_quality.get('sample_archive_status', 'unknown')} ({windows_quality.get('sample_archive_bytes', 0)} bytes).",
        f"- {windows_release_tag} exec dirs missing platform summary: {windows_quality.get('missing_platform_summary_count', 'n/a')}.",
        f"- Duplicate dynamic rows dropped before export: {dedupe.get('duplicate_dynamic_rows_dropped', 0)} across {dedupe.get('duplicate_dynamic_keys', 0)} keys.",
    ]
    for item in summary.get("historical_release_diagnostics", {}).get("windows", []):
        lines.append(
            f"- Historical {item['release_tag']} platform summaries: "
            f"{item['platform_summary_files']} found, {item['parse_ok']} parse ok, "
            f"{item['parse_errors']} parse errors."
        )
    lines.extend(
        [
            "",
            "## Remaining Cleanup",
            "",
            "1. Add Windows reviewed OS-compat labels before using Windows for static-analysis precision/recall.",
            "2. Decide whether Linux scheduling should align to macOS v0.6.0 top5000 rows instead of the older macOS v0.4.0 denominator.",
            "3. Keep raw release assets out of git; record release tag, asset name, and quality status in the clean summary.",
            "",
        ]
    )
    return "\n".join(lines)


def build_support_matrix(summary: dict[str, Any]) -> dict[str, Any]:
    windows_release_tag = summary["release_inventory"][2].get("release_tag", "Windows release")
    return {
        "schema_version": "cross_os_data_quality_support_matrix_v1",
        "generated_at": summary["generated_at"],
        "claims": [
            {
                "claim": "We have normalized Linux, macOS, and Windows dynamic observations into one schema.",
                "status": "support",
                "safe_wording": "The clean layer contains three-OS bronze dynamic rows with explicit source releases.",
                "banned_wording": "All three OS datasets are equally complete.",
                "evidence": [
                    "docs/cross-os-testing/datasets/unified-dynamic-skill-labels.jsonl",
                    "docs/cross-os-testing/datasets/cross-os-dataset-summary.json",
                ],
            },
            {
                "claim": "macOS v0.6.0 is the preferred ClawHub/SkillSh aligned macOS source.",
                "status": "support",
                "safe_wording": "macOS v0.6.0 gives a cleaner top5000-indexed corpus for ClawHub/SkillSh.",
                "banned_wording": "macOS v0.4.0 and v0.6.0 can be mixed as one denominator.",
                "evidence": [
                    "release-work/macos-v0.6.0/reports/reports/clawhub_top5000_database_index.csv",
                    "release-work/macos-v0.6.0/reports/reports/skillsh_top5000_database_index.csv",
                ],
            },
            {
                "claim": f"{windows_release_tag} is paper-ready ground truth.",
                "status": "caution",
                "safe_wording": f"{windows_release_tag} provides parseable dynamic summaries; use the current review/inventory stage for Windows root-cause labels.",
                "banned_wording": f"{windows_release_tag} dynamic failures are OS compatibility ground truth.",
                "evidence": [
                    "docs/cross-os-testing/datasets/cross-os-dataset-summary.json",
                ],
            },
            {
                "claim": "Dynamic failures are OS compatibility issues.",
                "status": "fail",
                "safe_wording": "Dynamic failures are symptoms that need reviewed root-cause labels.",
                "banned_wording": "Every blocked or failed task is an OS bug.",
                "evidence": [
                    "docs/cross-os-testing/datasets/unified-reviewed-os-labels.jsonl",
                ],
            },
        ],
    }


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        try:
            return path.resolve().relative_to(WORKSPACE_ROOT.resolve()).as_posix()
        except ValueError:
            return path.as_posix()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--linux-datasets", type=Path, default=DEFAULT_LINUX_DATASETS)
    parser.add_argument("--macos-v06-reports", type=Path, default=DEFAULT_MACOS_V06_REPORTS)
    parser.add_argument("--macos-v06-exec-asset", type=Path, default=DEFAULT_MACOS_V06_EXEC_ASSET)
    parser.add_argument("--macos-v06-samples-asset", type=Path, default=DEFAULT_MACOS_V06_SAMPLES_ASSET)
    parser.add_argument("--windows-v04-asset", type=Path, default=DEFAULT_WINDOWS_V04_ASSET)
    parser.add_argument(
        "--windows-v05-exec-asset",
        type=Path,
        action="append",
        default=[],
        help="Windows v0.5.0 exec tar.gz asset. Can be repeated.",
    )
    parser.add_argument(
        "--windows-v05-samples-asset",
        type=Path,
        default=None,
        help="Optional Windows v0.5.0 samples tar.gz asset.",
    )
    parser.add_argument("--windows-v03-exec-dir", type=Path, default=DEFAULT_WINDOWS_V03_EXEC_DIR)
    parser.add_argument("--windows-v02-asset", type=Path, default=DEFAULT_WINDOWS_V02_ASSET)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    linux_rows = read_linux_dynamic_rows(args.linux_datasets)
    macos_denominator_rows, macos_report_rows = read_macos_v06_rows(args.macos_v06_reports)
    macos_asset_rows, macos_quality = read_macos_v06_exec_rows(
        args.macos_v06_exec_asset,
        samples_asset=args.macos_v06_samples_asset,
    )
    macos_rows = macos_asset_rows or macos_report_rows
    if not macos_asset_rows:
        macos_quality = {
            **macos_quality,
            "dynamic_source": "top5000_reports",
            "dynamic_rows": len(macos_report_rows),
        }
    else:
        macos_quality = {
            **macos_quality,
            "dynamic_source": "full_exec_asset",
        }
    if args.windows_v05_exec_asset:
        windows_rows, windows_quality = read_windows_v05_rows(
            args.windows_v05_exec_asset,
            samples_asset=args.windows_v05_samples_asset,
        )
    else:
        windows_rows, windows_quality = read_windows_v04_rows(args.windows_v04_asset)
    windows_historical_diagnostics = scan_windows_release_diagnostics(args.windows_v03_exec_dir, args.windows_v02_asset)
    reviewed_rows = read_reviewed_rows(args.repo_root)

    dynamic_rows, dedupe_stats = dedupe_dynamic_rows(linux_rows + macos_rows + windows_rows)
    denominator_rows = sorted(
        macos_denominator_rows,
        key=lambda row: (row["target_os"], row["source"], row["rank"], row["package_id"]),
    )
    summary = build_summary(
        dynamic_rows,
        reviewed_rows,
        denominator_rows,
        macos_quality,
        windows_quality,
        windows_historical_diagnostics,
        dedupe_stats,
    )
    support_matrix = build_support_matrix(summary)

    write_jsonl(args.out_dir / "unified-dynamic-skill-labels.jsonl", dynamic_rows)
    write_jsonl(args.out_dir / "unified-reviewed-os-labels.jsonl", reviewed_rows)
    write_jsonl(args.out_dir / "unified-denominator-index.jsonl", denominator_rows)
    write_json(args.out_dir / "cross-os-dataset-summary.json", summary)
    write_json(args.out_dir / "cross-os-data-quality-support-matrix.json", support_matrix)
    write_text(args.out_dir / "README.md", render_readme(summary))
    write_text(args.out_dir / "cross-os-data-quality-report.md", render_quality_report(summary))

    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
