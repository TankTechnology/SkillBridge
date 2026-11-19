#!/usr/bin/env python3
"""Extract Linux Claude review batch outputs into reviewed-label JSONL."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
import json
from pathlib import Path
import re
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent

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

FIXABILITY_ALIASES = {
    "small_skill_script_fix": "code_fix",
    "small_skill_guide_fix": "guide_fix",
    "small_skill_metadata_fix": "guide_fix",
    "package_entrypoint_fix": "package_fix",
    "install_contract_fix": "install_contract_fix",
    "requires_environment_provisioning": "requires_environment_provisioning",
    "not_functionally_fixable_for_linux": "not_skill_fix",
    "not_functionally_fixable_for_macos": "not_skill_fix",
    "not_os_issue": "not_os_issue",
    "need_more_evidence": "need_more_evidence",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--label-quality", default="silver_reviewed")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--summary-json", type=Path)
    parser.add_argument("--summary-md", type=Path)
    args = parser.parse_args()

    run_dir = args.run_dir
    out = args.out or (run_dir / "extracted-reviewed-labels.jsonl")
    summary_json = args.summary_json or (run_dir / "review-summary.json")
    summary_md = args.summary_md or (run_dir / "review-summary.md")

    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    batch_summaries: list[dict[str, Any]] = []
    for result_path in sorted(
        path for path in run_dir.glob("result-batch-*.json") if re.fullmatch(r"result-batch-\d{3}\.json", path.name)
    ):
        batch_result = load_json(result_path, errors)
        if not batch_result:
            continue
        payload = extract_payload(batch_result.get("result", ""), result_path, errors)
        if not payload:
            continue
        source_run_id = payload.get("source_run_id") or read_source_run_id(run_dir)
        batch_index = payload.get("batch_index")
        payload_rows = payload.get("rows", [])
        if not isinstance(payload_rows, list):
            errors.append({"file": result_path.name, "error": "payload rows is not a list"})
            continue
        for position, raw_row in enumerate(payload_rows, 1):
            if not isinstance(raw_row, dict):
                errors.append({"file": result_path.name, "error": f"row {position} is not an object"})
                continue
            rows.append(normalize_row(raw_row, result_path, source_run_id, batch_index, position, args.label_quality))
        batch_summaries.append(
            {
                "result_file": result_path.name,
                "batch_index": batch_index,
                "rows": len(payload_rows),
                "duration_s": round(float(batch_result.get("duration_ms") or 0) / 1000, 3),
                "cost_usd": float(batch_result.get("total_cost_usd") or 0),
                "model_usage": batch_result.get("modelUsage", {}),
            }
        )

    write_jsonl(out, rows)
    summary = build_summary(rows, errors, batch_summaries, out)
    write_json(summary_json, summary)
    summary_md.write_text(render_summary_md(summary), encoding="utf-8")
    print(json.dumps({"rows": len(rows), "errors": len(errors), "out": rel(out), "summary": rel(summary_json)}, ensure_ascii=False, sort_keys=True))
    return 0 if not errors else 1


def load_json(path: Path, errors: list[dict[str, Any]]) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        errors.append({"file": path.name, "error": str(exc)})
        return None
    return data if isinstance(data, dict) else None


def extract_payload(text: str, result_path: Path, errors: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not isinstance(text, str):
        errors.append({"file": result_path.name, "error": "result is not a string"})
        return None
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        errors.append({"file": result_path.name, "error": "could not find JSON object in result"})
        return None
    try:
        data = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError as exc:
        errors.append({"file": result_path.name, "error": f"invalid review JSON: {exc}"})
        return None
    return data if isinstance(data, dict) else None


def normalize_row(
    raw: dict[str, Any],
    result_path: Path,
    source_run_id: str,
    batch_index: Any,
    position: int,
    label_quality: str,
) -> dict[str, Any]:
    skill_id = str(raw.get("skill_id") or "")
    scope_status = enum_or(raw.get("scope_status"), SCOPE_STATUS, "in_scope")
    primary_label = enum_or(raw.get("primary_label"), PRIMARY_LABELS, "not_os_issue")
    fixability = normalize_fixability(raw.get("fixability"), scope_status, primary_label)
    row = {
        "schema_version": "os_compat_reviewed_label_v1",
        "label_quality": label_quality,
        "reviewed_at": date.today().isoformat(),
        "source_run_id": source_run_id,
        "skill_id": skill_id,
        "target_os": "linux",
        "task_result": str(raw.get("task_result") or ""),
        "scope_status": scope_status,
        "primary_label": "not_os_issue" if scope_status != "in_scope" else primary_label,
        "secondary_tags": string_list(raw.get("secondary_tags")),
        "evidence_level": enum_or(raw.get("evidence_level"), EVIDENCE_LEVELS, "both"),
        "fixability": fixability,
        "confidence": enum_or(raw.get("confidence"), CONFIDENCE, "medium"),
        "evidence_refs": string_list(raw.get("evidence_refs")),
        "notes": str(raw.get("notes") or ""),
        "uncertainties": string_list(raw.get("uncertainties")),
        "reviewer": "claude-code",
        "review_result_file": result_path.name,
        "review_batch_index": batch_index,
        "review_batch_position": position,
        "should_add_to_reviewed_labels": bool(raw.get("should_add_to_reviewed_labels", True)),
        "raw_review_row": raw,
    }
    return row


def enum_or(value: Any, allowed: set[str], default: str) -> str:
    return str(value) if str(value) in allowed else default


def string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def normalize_fixability(value: Any, scope_status: str, primary_label: str) -> str:
    raw = str(value or "")
    mapped = FIXABILITY_ALIASES.get(raw, raw)
    if scope_status == "excluded_external_credential_or_service":
        return mapped if mapped in {"need_credentials_or_service", "need_external_service_or_distribution", "not_skill_fix", "uncertain"} else "need_credentials_or_service"
    if scope_status == "excluded_benchmark_capability_gap":
        return mapped if mapped in {"requires_environment_provisioning", "runner_or_task_fix", "not_skill_fix", "uncertain"} else "requires_environment_provisioning"
    if primary_label == "not_os_issue":
        return mapped if mapped in FIXABILITY else "not_os_issue"
    if mapped in {"not_applicable", "need_credentials_or_service", "need_external_service_or_distribution", "not_os_issue"}:
        return "uncertain"
    return mapped if mapped in FIXABILITY else "uncertain"


def read_source_run_id(run_dir: Path) -> str:
    manifest = run_dir / "batch-manifest.json"
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if isinstance(data.get("source_run_id"), str):
                return data["source_run_id"]
        except Exception:
            pass
    return run_dir.name


def build_summary(
    rows: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    batch_summaries: list[dict[str, Any]],
    out: Path,
) -> dict[str, Any]:
    in_scope = [row for row in rows if row["scope_status"] == "in_scope"]
    os_positive = {
        "os_specific_execution_issue",
        "os_install_or_dependency_contract_issue",
        "package_or_entrypoint_issue",
    }
    return {
        "schema_version": "linux_agent_review_summary_v1",
        "rows": len(rows),
        "out": rel(out),
        "batch_count": len(batch_summaries),
        "batch_summaries": batch_summaries,
        "total_cost_usd": round(sum(item["cost_usd"] for item in batch_summaries), 6),
        "total_duration_s": round(sum(item["duration_s"] for item in batch_summaries), 3),
        "scope_counts": dict(Counter(row["scope_status"] for row in rows)),
        "primary_label_counts": dict(Counter(row["primary_label"] for row in rows)),
        "in_scope_primary_label_counts": dict(Counter(row["primary_label"] for row in in_scope)),
        "fixability_counts": dict(Counter(row["fixability"] for row in rows)),
        "confidence_counts": dict(Counter(row["confidence"] for row in rows)),
        "os_issue_count": sum(1 for row in in_scope if row["primary_label"] in os_positive),
        "os_issue_skills": sorted(row["skill_id"] for row in in_scope if row["primary_label"] in os_positive),
        "error_count": len(errors),
        "errors": errors,
    }


def render_summary_md(summary: dict[str, Any]) -> str:
    lines = [
        "# Linux Agent Review Summary",
        "",
        f"- Rows: {summary['rows']}",
        f"- Batches: {summary['batch_count']}",
        f"- Cost: ${summary['total_cost_usd']}",
        f"- Scope counts: `{summary['scope_counts']}`",
        f"- Primary label counts: `{summary['primary_label_counts']}`",
        f"- In-scope primary label counts: `{summary['in_scope_primary_label_counts']}`",
        f"- OS issue count: {summary['os_issue_count']}",
        "",
        "## OS Issue Skills",
        "",
    ]
    lines.extend(f"- `{skill_id}`" for skill_id in summary["os_issue_skills"])
    if not summary["os_issue_skills"]:
        lines.append("- None")
    if summary["error_count"]:
        lines.extend(["", "## Errors", ""])
        lines.extend(f"- `{item.get('file')}`: {item.get('error')}" for item in summary["errors"])
    return "\n".join(lines) + "\n"


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def rel(path: Path | str) -> str:
    path = Path(path)
    try:
        return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


if __name__ == "__main__":
    raise SystemExit(main())
