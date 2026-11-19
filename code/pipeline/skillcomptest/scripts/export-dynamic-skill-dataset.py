#!/usr/bin/env python3
"""Export dynamic Linux skill observations and reviewed labels.

The exporter is read-only. It turns existing run summaries and compatibility
audits into stable JSONL datasets that can be joined with static predictions.
It does not execute skills or inspect raw task workspaces.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DOCS = REPO_ROOT / "docs" / "linux-testing"
DEFAULT_OUT = DEFAULT_DOCS / "datasets"

OS_LABELS = {
    "real_platform_incompatibility": "platform_intrinsic",
    "platform_target_mismatch": "platform_intrinsic",
    "package_entrypoint_plus_auth": "skill_package_or_entrypoint",
    "script_platform_locked": "skill_os_fixable",
    "path_shell_assumption": "skill_os_fixable",
    "guide_portability_gap": "guide_only_os_contract",
    "os_dispatch_missing_linux": "skill_os_fixable",
}

NON_OS_LABELS = {
    "linux_dependency_gap": "env_bindable_dependency",
    "linux_dependency_gap_plus_service_version": "env_bindable_dependency",
    "external_auth_service_blocker": "auth_or_service_blocked",
    "script_surface_but_auth_blocked": "auth_or_service_blocked",
    "library_api_drift": "task_or_service_drift",
    "toolchain_version_and_generated_code_issue": "uncertain_need_more_dynamic",
}

FIXABILITY_TO_LABEL = {
    "small_skill_fallback": "skill_os_fixable",
    "small_skill_packaging": "skill_package_or_entrypoint",
    "moderate_skill_packaging": "skill_package_or_entrypoint",
    "small_to_moderate_skill_metadata": "skill_contract_mismatch",
    "partially_fixable_skill_or_package": "skill_package_or_entrypoint",
    "not_functionally_fixable_for_linux": "platform_intrinsic",
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


def iter_run_summaries(docs: Path) -> list[Path]:
    return sorted((docs / "runs").glob("*.json"))


def export_task_observations(docs: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in iter_run_summaries(docs):
        summary = load_json(path)
        run_id = summary.get("run_id", path.stem)
        profile = summary.get("profile") or {}
        for result in summary.get("results") or []:
            if not isinstance(result, dict):
                continue
            skill_id = result.get("skill_id") or _skill_id(result)
            counts = {
                "pass": int(result.get("tasks_pass") or 0),
                "fail": int(result.get("tasks_fail") or 0),
                "blocked": int(result.get("tasks_blocked") or 0),
                "unsupported": int(result.get("tasks_unsupported") or 0),
            }
            total = sum(counts.values())
            rows.append(
                {
                    "schema_version": "dynamic_task_observation_v1",
                    "skill_id": skill_id,
                    "source": result.get("source") or _source(skill_id),
                    "skill": result.get("skill") or _name(skill_id),
                    "platform": summary.get("platform", "linux"),
                    "run_id": run_id,
                    "run_summary": _rel(path),
                    "task_counts": counts | {"total": total},
                    "status": result.get("status", ""),
                    "portability_verdict": result.get("portability_verdict", ""),
                    "success_rate": result.get("success_rate", 0),
                    "agent_model": profile.get("claude_model", ""),
                    "runner": profile.get("runner", ""),
                    "task_design_model": profile.get("task_design_model", ""),
                    "runner_cost_usd": result.get("runner_cost_usd"),
                    "runner_input_tokens": result.get("runner_input_tokens"),
                    "runner_output_tokens": result.get("runner_output_tokens"),
                    "label_quality": "bronze_raw",
                }
            )
    return rows


def export_dynamic_labels(docs: Path, observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    labels: dict[str, dict[str, Any]] = {}
    aggregate = _aggregate_observations(observations)
    for skill_id, item in aggregate.items():
        labels[skill_id] = {
            "schema_version": "dynamic_skill_label_v1",
            "skill_id": skill_id,
            "platform": "linux",
            "task_counts": item["task_counts"],
            "dynamic_label": _bronze_label(item["task_counts"]),
            "os_related": False,
            "fixability": "unknown",
            "category": "unknown",
            "subcategory": "",
            "confidence": "low",
            "evidence_summary": "Aggregated raw run result; not yet reviewed for OS root cause.",
            "negative_evidence": [],
            "recommended_fix_pattern": [],
            "label_quality": "bronze_raw",
            "source_runs": sorted(item["runs"]),
        }

    for review in _review_rows(docs):
        skill_id = review["skill_id"]
        base = labels.get(skill_id) or {
            "schema_version": "dynamic_skill_label_v1",
            "skill_id": skill_id,
            "platform": "linux",
            "task_counts": {},
            "source_runs": [],
        }
        labels[skill_id] = base | review

    return sorted(labels.values(), key=lambda row: row["skill_id"])


def _review_rows(docs: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted((docs / "analyses").glob("*os-compat-matrix.json")):
        data = load_json(path)
        for item in data.get("matrix") or []:
            if isinstance(item, dict) and item.get("skill_id"):
                rows.append(_matrix_item_to_label(item, path, data.get("run_id", "")))

    audit = docs / "analysis" / "linux-osrisk100-fast-20260618-s1x10-s2x10-resume1-os-compat-audit.json"
    if audit.is_file():
        data = load_json(audit)
        for item in data.get("support_calls") or []:
            if isinstance(item, dict) and item.get("skill"):
                rows.append(_support_call_to_label(item, audit, data.get("run_id", "")))
    return rows


def _matrix_item_to_label(item: dict[str, Any], path: Path, run_id: str) -> dict[str, Any]:
    os_related = item.get("os_related")
    category = str(item.get("category") or "unknown")
    fixability = str(item.get("fixability") or "unknown")
    dynamic_label = FIXABILITY_TO_LABEL.get(fixability)
    if dynamic_label is None:
        if os_related is True:
            dynamic_label = OS_LABELS.get(category, "skill_os_fixable")
        elif "packaging" in str(os_related) or "dependency_contract" in str(os_related):
            dynamic_label = FIXABILITY_TO_LABEL.get(fixability, "skill_package_or_entrypoint")
        elif "environment" in str(os_related):
            dynamic_label = "env_bindable_dependency"
        else:
            dynamic_label = NON_OS_LABELS.get(category, "auth_or_service_blocked" if os_related is False else "uncertain_need_more_dynamic")

    return {
        "schema_version": "dynamic_skill_label_v1",
        "skill_id": item["skill_id"],
        "platform": "linux",
        "dynamic_label": dynamic_label,
        "os_related": dynamic_label in {
            "skill_os_fixable",
            "skill_package_or_entrypoint",
            "skill_contract_mismatch",
            "guide_only_os_contract",
            "platform_intrinsic",
        },
        "fixability": fixability,
        "category": category,
        "subcategory": "",
        "confidence": "high" if item.get("evidence") else "medium",
        "evidence_summary": item.get("evidence", ""),
        "negative_evidence": [],
        "recommended_fix_pattern": [item.get("recommended_action", "")] if item.get("recommended_action") else [],
        "label_quality": "silver_reviewed",
        "source_runs": [run_id] if run_id else [],
        "review_source": _rel(path),
    }


def _support_call_to_label(item: dict[str, Any], path: Path, run_id: str) -> dict[str, Any]:
    classification = str(item.get("classification") or "unknown")
    fixability = str(item.get("fixability") or "unknown")
    dynamic_label = OS_LABELS.get(classification) or NON_OS_LABELS.get(classification)
    if dynamic_label is None:
        if "package" in classification or "entrypoint" in classification:
            dynamic_label = "skill_package_or_entrypoint"
        elif "auth" in classification or "service" in classification:
            dynamic_label = "auth_or_service_blocked"
        elif "dependency" in classification:
            dynamic_label = "env_bindable_dependency"
        elif "platform" in classification:
            dynamic_label = "platform_intrinsic"
        else:
            dynamic_label = "uncertain_need_more_dynamic"

    return {
        "schema_version": "dynamic_skill_label_v1",
        "skill_id": item["skill"],
        "platform": "linux",
        "dynamic_label": dynamic_label,
        "os_related": dynamic_label in {
            "skill_os_fixable",
            "skill_package_or_entrypoint",
            "skill_contract_mismatch",
            "guide_only_os_contract",
            "platform_intrinsic",
        },
        "fixability": fixability,
        "category": classification,
        "subcategory": "",
        "confidence": "high" if item.get("support") == "support" else "medium",
        "evidence_summary": item.get("evidence", ""),
        "negative_evidence": [item.get("forbidden_wording", "")] if item.get("forbidden_wording") else [],
        "recommended_fix_pattern": [item.get("safe_wording", "")] if item.get("safe_wording") else [],
        "label_quality": "silver_reviewed",
        "source_runs": [run_id] if run_id else [],
        "review_source": _rel(path),
    }


def _aggregate_observations(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    aggregate: dict[str, dict[str, Any]] = defaultdict(lambda: {"task_counts": defaultdict(int), "runs": set()})
    for row in rows:
        item = aggregate[row["skill_id"]]
        item["runs"].add(row["run_id"])
        for key, value in row["task_counts"].items():
            item["task_counts"][key] += int(value or 0)
    return {
        skill_id: {"task_counts": dict(item["task_counts"]), "runs": item["runs"]}
        for skill_id, item in aggregate.items()
    }


def _bronze_label(counts: dict[str, int]) -> str:
    total = counts.get("total", 0)
    non_pass = total - counts.get("pass", 0)
    if total == 0:
        return "uncertain_need_more_dynamic"
    if non_pass == 0:
        return "no_observed_issue"
    return "uncertain_need_more_dynamic"


def _skill_id(result: dict[str, Any]) -> str:
    source = str(result.get("source") or "")
    skill = str(result.get("skill") or "")
    return f"{source}/{skill}" if source and skill else skill or source


def _source(skill_id: str) -> str:
    return skill_id.split("/", 1)[0] if "/" in skill_id else ""


def _name(skill_id: str) -> str:
    return skill_id.split("/", 1)[1] if "/" in skill_id else skill_id


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def build_metadata(observations: list[dict[str, Any]], labels: list[dict[str, Any]]) -> dict[str, Any]:
    quality_counts: dict[str, int] = defaultdict(int)
    label_counts: dict[str, int] = defaultdict(int)
    for row in labels:
        quality_counts[row.get("label_quality", "unknown")] += 1
        label_counts[row.get("dynamic_label", "unknown")] += 1
    return {
        "schema_version": "dynamic_skill_dataset_metadata_v1",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "unit": "skill package version on linux",
        "task_observations": len(observations),
        "skill_labels": len(labels),
        "label_quality_counts": dict(sorted(quality_counts.items())),
        "dynamic_label_counts": dict(sorted(label_counts.items())),
        "notes": [
            "bronze_raw labels are aggregated run outcomes and should not be used as OS ground truth.",
            "silver_reviewed labels come from existing compatibility matrix/audit artifacts.",
            "The exporter is read-only and does not execute skill code.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docs", type=Path, default=DEFAULT_DOCS)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    observations = export_task_observations(args.docs)
    labels = export_dynamic_labels(args.docs, observations)
    metadata = build_metadata(observations, labels)

    write_jsonl(args.out_dir / "dynamic-task-observations.jsonl", observations)
    write_jsonl(args.out_dir / "dynamic-skill-labels.jsonl", labels)
    write_json(args.out_dir / "dynamic-dataset-metadata.json", metadata)

    print(json.dumps(metadata, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
