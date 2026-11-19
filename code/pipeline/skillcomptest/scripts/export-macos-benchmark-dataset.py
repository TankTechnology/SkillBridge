#!/usr/bin/env python3
"""Export macOS benchmark summaries into static-analysis evaluation datasets.

The exporter is read-only. It parses existing benchmark summary JSON files and
does not execute skill code, task commands, setup scripts, or migrated packages.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BENCHMARKS_ROOT = REPO_ROOT.parent / "Compa-Skill" / "data" / "benchmarks" / "macos"
DEFAULT_OUT = REPO_ROOT / "docs" / "macos-testing" / "datasets"

MACOS_RELEASE_TAG = "v0.4.0"
MACOS_RELEASE_NOTES_COUNTS = {
    "tested_skills": 2383,
    "total_tasks": 20305,
    "pass": 12220,
    "fail": 588,
    "blocked": 6834,
    "unsupported": 430,
}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def iter_platform_summaries(benchmarks_root: Path) -> list[Path]:
    return sorted((benchmarks_root / "exec").rglob("platform_summary.json"))


def export_task_observations(benchmarks_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for summary_path in iter_platform_summaries(benchmarks_root):
        try:
            summary = load_json(summary_path)
        except (OSError, json.JSONDecodeError) as exc:
            rows.append(_error_observation(benchmarks_root, summary_path, exc))
            continue

        run_manifest = _optional_json(summary_path.parent / "run_manifest.json")
        metrics = _optional_json(summary_path.parent / "metrics.json")
        source, skill = _source_skill_from_path(benchmarks_root, summary_path)
        skill_id = _skill_id(summary, run_manifest, source, skill)
        counts = _task_counts(summary, metrics, run_manifest)
        failure_categories = _failure_categories(summary, metrics)
        total = counts["total"]
        pass_count = counts["pass"]

        rows.append(
            {
                "schema_version": "macos_dynamic_task_observation_v1",
                "skill_id": skill_id,
                "source": _source(skill_id) or source,
                "skill": _name(skill_id) or skill,
                "platform": "macos",
                "run_id": _run_id(benchmarks_root, summary_path),
                "run_summary": _rel_to(summary_path, benchmarks_root),
                "task_counts": counts,
                "success_rate": _success_rate(summary, metrics, pass_count, total),
                "runner": _first_text(summary, metrics, run_manifest, keys=("runner",)),
                "task_design_model": _first_text(summary, metrics, run_manifest, keys=("task_design_model", "Task Design Model")),
                "executor_model": _first_text(summary, metrics, run_manifest, keys=("executor_model", "Executor Model")),
                "portability_verdict": str(summary.get("portability_verdict") or ""),
                "portability_verdict_detail": str(summary.get("portability_verdict_detail") or ""),
                "dominant_failure_pattern": str(summary.get("dominant_failure_pattern") or metrics.get("dominant_blocker") or ""),
                "failure_categories": failure_categories,
                "label_quality": "bronze_raw_macos",
            }
        )
    return rows


def export_dynamic_labels(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    aggregate = _aggregate_observations(observations)
    labels: list[dict[str, Any]] = []
    for skill_id, item in sorted(aggregate.items()):
        counts = item["task_counts"]
        scope_hint = _scope_hint(item["failure_categories"])
        labels.append(
            {
                "schema_version": "macos_dynamic_skill_label_v1",
                "skill_id": skill_id,
                "source": _source(skill_id),
                "skill": _name(skill_id),
                "platform": "macos",
                "task_counts": counts,
                "dynamic_label": _bronze_dynamic_label(counts),
                "scope_hint": scope_hint,
                "os_related": False,
                "fixability": "unknown",
                "category": "unreviewed_bronze",
                "confidence": "low",
                "evidence_summary": "Aggregated macOS benchmark outcome; not manually reviewed for OS root cause.",
                "failure_categories": dict(sorted(item["failure_categories"].items())),
                "source_runs": sorted(item["runs"]),
                "label_quality": "bronze_raw_macos",
            }
        )
    return labels


def _optional_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = load_json(path)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _error_observation(benchmarks_root: Path, path: Path, exc: Exception) -> dict[str, Any]:
    source, skill = _source_skill_from_path(benchmarks_root, path)
    return {
        "schema_version": "macos_dynamic_task_observation_v1",
        "skill_id": f"{source}/{skill}" if source and skill else path.parent.name,
        "source": source,
        "skill": skill,
        "platform": "macos",
        "run_id": _run_id(benchmarks_root, path),
        "run_summary": _rel_to(path, benchmarks_root),
        "task_counts": {"pass": 0, "fail": 0, "blocked": 0, "unsupported": 0, "unknown": 0, "total": 0},
        "success_rate": 0.0,
        "runner": "",
        "task_design_model": "",
        "executor_model": "",
        "portability_verdict": "",
        "portability_verdict_detail": "",
        "dominant_failure_pattern": f"summary_parse_error: {exc}",
        "failure_categories": {"summary_parse_error": 1},
        "label_quality": "bronze_raw_macos",
    }


def _source_skill_from_path(benchmarks_root: Path, summary_path: Path) -> tuple[str, str]:
    try:
        rel = summary_path.relative_to(benchmarks_root)
    except ValueError:
        return "", ""
    parts = rel.parts
    if len(parts) >= 5 and parts[0] == "exec":
        return parts[1], parts[2]
    return "", ""


def _skill_id(summary: dict[str, Any], run_manifest: dict[str, Any], source: str, skill: str) -> str:
    if source and skill:
        return f"{source}/{skill}"
    for data in (summary, run_manifest):
        value = str(data.get("skill_source") or data.get("skill") or "")
        if "/" in value and not value.lstrip().startswith("{"):
            return value
    skill_name = str(summary.get("skill") or run_manifest.get("skill_name") or skill)
    skill_source = str(summary.get("skill_source") or run_manifest.get("skill_source") or source)
    return f"{skill_source}/{skill_name}" if skill_source and skill_name else skill_name or skill_source


def _task_counts(summary: dict[str, Any], metrics: dict[str, Any], run_manifest: dict[str, Any]) -> dict[str, int]:
    raw = summary.get("task_results")
    if isinstance(raw, dict):
        direct = {
            "pass": _int(raw.get("pass") or raw.get("passed")),
            "fail": _int(raw.get("fail") or raw.get("failed")),
            "blocked": _int(raw.get("blocked")),
            "unsupported": _int(raw.get("unsupported")),
        }
        if any(direct.values()) or _int(raw.get("total")):
            direct["total"] = _int(raw.get("total")) or sum(direct.values())
            direct["unknown"] = max(direct["total"] - sum(direct[key] for key in ("pass", "fail", "blocked", "unsupported")), 0)
            return direct
        from_statuses = _counts_from_status_dict(raw)
        if from_statuses["total"]:
            return from_statuses

    counts = {
        "pass": _first_int(summary, metrics, keys=("pass_count", "passed")),
        "fail": _first_int(summary, metrics, keys=("fail_count", "failed")),
        "blocked": _first_int(summary, metrics, keys=("blocked_count", "blocked")),
        "unsupported": _first_int(summary, metrics, keys=("unsupported_count", "unsupported")),
    }
    total = _first_int(summary, metrics, run_manifest, keys=("total_tasks", "task_count", "total_functional_tasks"))
    counts["total"] = total or sum(counts.values())
    counts["unknown"] = max(counts["total"] - sum(counts[key] for key in ("pass", "fail", "blocked", "unsupported")), 0)
    return counts


def _counts_from_status_dict(raw: dict[str, Any]) -> dict[str, int]:
    counts = {"pass": 0, "fail": 0, "blocked": 0, "unsupported": 0, "unknown": 0, "total": 0}
    for value in raw.values():
        status = ""
        if isinstance(value, dict):
            status = str(value.get("status") or value.get("result") or "")
        else:
            status = str(value)
        if status in counts:
            counts[status] += 1
            counts["total"] += 1
        elif status:
            counts["unknown"] += 1
            counts["total"] += 1
    return counts


def _failure_categories(summary: dict[str, Any], metrics: dict[str, Any]) -> dict[str, int]:
    result: dict[str, int] = defaultdict(int)
    for key in ("failures_by_category", "failure_distribution"):
        for data in (summary, metrics):
            raw = data.get(key)
            if isinstance(raw, dict):
                _add_failure_distribution(result, raw)

    for key in ("task_results", "metrics_per_task"):
        for data in (summary, metrics):
            raw = data.get(key)
            if not isinstance(raw, dict):
                continue
            for value in raw.values():
                if isinstance(value, dict):
                    category = value.get("failure_category") or value.get("category")
                    status = value.get("status")
                    if category and status != "pass":
                        result[str(category)] += 1

    raw_task_metrics = metrics.get("task_metrics") or summary.get("task_metrics")
    if isinstance(raw_task_metrics, list):
        for item in raw_task_metrics:
            if isinstance(item, dict) and item.get("failure_category") and item.get("status") != "pass":
                result[str(item["failure_category"])] += 1

    dominant = summary.get("dominant_failure_pattern") or metrics.get("dominant_blocker")
    if dominant and not result:
        result[str(dominant)] += 1
    return dict(result)


def _add_failure_distribution(result: dict[str, int], raw: dict[str, Any]) -> None:
    if isinstance(raw.get("category_count"), dict):
        for category, count in raw["category_count"].items():
            _add_category_count(result, str(category or "unknown"), count)

    for category, count in raw.items():
        if category in {"category_count", "most_common_category"}:
            continue
        if _looks_like_task_id(str(category)) and isinstance(count, str):
            _add_category_count(result, count or "unknown", 1)
            continue
        _add_category_count(result, str(category or "unknown"), count)


def _add_category_count(result: dict[str, int], category: str, count: Any) -> None:
    if isinstance(count, bool) or count is None:
        return
    if isinstance(count, dict):
        _add_category_count(result, category, count.get("count"))
        return
    if isinstance(count, (int, float)):
        if int(count) > 0:
            result[category] += int(count)
        return
    if isinstance(count, str):
        if count.strip().isdigit():
            value = int(count.strip())
            if value > 0:
                result[category] += value
        elif count.strip():
            result[count.strip()] += 1


def _looks_like_task_id(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Z]+_\d+", value))


def _aggregate_observations(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    aggregate: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"task_counts": defaultdict(int), "runs": set(), "failure_categories": defaultdict(int)}
    )
    for row in rows:
        item = aggregate[row["skill_id"]]
        item["runs"].add(row["run_id"])
        for key, value in row["task_counts"].items():
            item["task_counts"][key] += int(value or 0)
        for key, value in row.get("failure_categories", {}).items():
            item["failure_categories"][key] += int(value or 0)
    return {
        skill_id: {
            "task_counts": dict(item["task_counts"]),
            "runs": item["runs"],
            "failure_categories": dict(item["failure_categories"]),
        }
        for skill_id, item in aggregate.items()
    }


def _bronze_dynamic_label(counts: dict[str, int]) -> str:
    total = counts.get("total", 0)
    if total == 0:
        return "dynamic_unresolved"
    if counts.get("unknown", 0) == total:
        return "dynamic_unresolved"
    if counts.get("pass", 0) == total:
        return "no_observed_issue"
    return "dynamic_nonpass_needs_review"


def _scope_hint(failure_categories: dict[str, int]) -> str:
    joined = " ".join(failure_categories).lower()
    if not joined:
        return "no_failure_category"
    if any(token in joined for token in ("auth", "credential", "token", "network", "service", "api")):
        return "likely_external_service_or_credential"
    if any(token in joined for token in ("dependency", "missing_dependency", "environment")):
        return "likely_environment_dependency"
    if any(token in joined for token in ("os", "platform", "permission", "path", "shell", "api_incompatible")):
        return "possible_os_compatibility_issue"
    return "needs_review"


def _success_rate(summary: dict[str, Any], metrics: dict[str, Any], pass_count: int, total: int) -> float:
    for data in (summary, metrics):
        for key in ("platform_success_rate", "success_rate", "overall_success_rate"):
            value = data.get(key)
            if isinstance(value, (int, float)):
                return round(float(value), 6)
    return round(pass_count / total, 6) if total else 0.0


def _first_text(*items: dict[str, Any], keys: tuple[str, ...]) -> str:
    for item in items:
        for key in keys:
            value = item.get(key)
            if value not in (None, ""):
                return str(value)
    return ""


def _first_int(*items: dict[str, Any], keys: tuple[str, ...]) -> int:
    for item in items:
        for key in keys:
            value = _int(item.get(key))
            if value:
                return value
    return 0


def _int(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0


def _source(skill_id: str) -> str:
    return skill_id.split("/", 1)[0] if "/" in skill_id else ""


def _name(skill_id: str) -> str:
    return skill_id.split("/", 1)[1] if "/" in skill_id else skill_id


def _run_id(benchmarks_root: Path, summary_path: Path) -> str:
    try:
        return summary_path.parent.relative_to(benchmarks_root / "exec").as_posix()
    except ValueError:
        return summary_path.parent.as_posix()


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _rel_to(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return _rel(path)


def _display_external_root(path: Path) -> str:
    parts = path.resolve().parts
    if "Compa-Skill" in parts:
        start = parts.index("Compa-Skill")
        return "/".join(parts[start:])
    return _rel(path)


def build_metadata(benchmarks_root: Path, observations: list[dict[str, Any]], labels: list[dict[str, Any]]) -> dict[str, Any]:
    count_totals = {"pass": 0, "fail": 0, "blocked": 0, "unsupported": 0, "unknown": 0, "total": 0}
    label_counts: dict[str, int] = defaultdict(int)
    scope_counts: dict[str, int] = defaultdict(int)
    source_counts: dict[str, int] = defaultdict(int)
    for row in labels:
        for key in count_totals:
            count_totals[key] += int(row["task_counts"].get(key, 0))
        label_counts[row["dynamic_label"]] += 1
        scope_counts[row["scope_hint"]] += 1
        source_counts[row.get("source") or "unknown"] += 1

    return {
        "schema_version": "macos_dynamic_skill_dataset_metadata_v1",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "unit": "skill package version on macos",
        "benchmarks_root": _display_external_root(benchmarks_root),
        "release_reference": MACOS_RELEASE_TAG,
        "release_notes_counts": MACOS_RELEASE_NOTES_COUNTS,
        "local_platform_summary_files": len(observations),
        "skill_labels": len(labels),
        "task_count_totals_from_local_files": count_totals,
        "dynamic_label_counts": dict(sorted(label_counts.items())),
        "scope_hint_counts": dict(sorted(scope_counts.items())),
        "source_counts": dict(sorted(source_counts.items())),
        "notes": [
            "bronze_raw_macos labels are mechanical run aggregations and are not OS root-cause ground truth.",
            "Use reviewed labels before computing precision/recall for macOS OS-compatibility claims.",
            "The exporter is read-only and does not execute skill code.",
            "The local extracted benchmark snapshot may be smaller than the published v0.4.0 release asset.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmarks-root", type=Path, default=DEFAULT_BENCHMARKS_ROOT)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    observations = export_task_observations(args.benchmarks_root)
    labels = export_dynamic_labels(observations)
    metadata = build_metadata(args.benchmarks_root, observations, labels)

    write_jsonl(args.out_dir / "dynamic-task-observations.jsonl", observations)
    write_jsonl(args.out_dir / "dynamic-skill-labels.jsonl", labels)
    write_json(args.out_dir / "dynamic-dataset-metadata.json", metadata)

    print(json.dumps(metadata, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
