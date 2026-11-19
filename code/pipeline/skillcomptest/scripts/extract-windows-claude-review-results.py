#!/usr/bin/env python3
"""Extract Windows Claude Code review outputs into row-level labels."""

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
    "insufficient_evidence",
}
PRIMARY_LABELS = {
    "confirmed_os_issue",
    "not_os_issue",
    "uncertain_insufficient_evidence",
}
OS_ISSUE_CLASSES = {
    "platform_native_api_or_app",
    "shell_path_command_assumption",
    "package_entrypoint_contract",
    "permission_session_model",
    "browser_gui_runtime",
    "target_os_mismatch",
    "install_dependency_contract",
    "encoding_locale_path",
    "not_os_auth_or_service",
    "not_os_generic_dependency",
    "not_os_generic_logic",
    "insufficient_evidence",
}
EVIDENCE_LEVELS = {
    "source_only",
    "sample_only",
    "dynamic_summary",
    "source_and_dynamic",
    "source_sample_dynamic",
}
FIXABILITY = {
    "script_portability_fix",
    "package_entrypoint_fix",
    "install_contract_fix",
    "platform_adapter_or_not_portable_by_design",
    "metadata_or_target_os_contract_fix",
    "runtime_contract_fix",
    "session_or_permission_adapter_fix",
    "need_credentials_or_service",
    "not_os_issue",
    "need_more_evidence",
}
CONFIDENCE = {"low", "medium", "high"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--label-quality", default="claude_code_trial_reviewed")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--summary-json", type=Path)
    parser.add_argument("--summary-md", type=Path)
    args = parser.parse_args()

    run_dir = args.run_dir
    out = args.out or (run_dir / "extracted-reviewed-labels.jsonl")
    summary_json = args.summary_json or (run_dir / "review-summary.json")
    summary_md = args.summary_md or (run_dir / "review-summary.md")

    manifest = load_json_safe(run_dir / "batch-manifest.json")
    expected_skill_ids = [
        skill_id
        for batch in manifest.get("batches", [])
        for skill_id in batch.get("skill_ids", [])
        if isinstance(skill_id, str)
    ]

    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    batch_summaries: list[dict[str, Any]] = []
    for result_path in sorted(run_dir.glob("result-batch-*.json")):
        if not re.fullmatch(r"result-batch-\d{3}\.json", result_path.name):
            continue
        batch_result = load_batch_result(result_path, errors)
        if not batch_result:
            continue
        payload = extract_payload(batch_result, result_path, errors)
        if not payload:
            continue
        source_run_id = str(payload.get("source_run_id") or manifest.get("run_id") or run_dir.name)
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
        batch_summaries.append(batch_summary(batch_result, result_path, batch_index, len(payload_rows)))

    write_jsonl(out, rows)
    confirmed = [row for row in rows if row["scope_status"] == "in_scope" and row["primary_label"] == "confirmed_os_issue"]
    write_jsonl(run_dir / "confirmed-os-issues.jsonl", confirmed)
    summary = build_summary(rows, errors, batch_summaries, expected_skill_ids, out)
    write_json(summary_json, summary)
    summary_md.write_text(render_summary_md(summary), encoding="utf-8")
    write_json(run_dir / "failed-batches.json", {"errors": errors})

    print(
        json.dumps(
            {
                "rows": len(rows),
                "expected": len(expected_skill_ids),
                "confirmed_os_issue": len(confirmed),
                "errors": len(errors),
                "out": rel(out),
                "summary": rel(summary_json),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if not errors and len(rows) == len(expected_skill_ids) else 1


def load_batch_result(path: Path, errors: list[dict[str, Any]]) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        errors.append({"file": path.name, "error": f"invalid Claude output JSON wrapper: {exc}"})
        return None
    if not isinstance(data, dict):
        errors.append({"file": path.name, "error": "Claude output wrapper is not an object"})
        return None
    return data


def extract_payload(wrapper: dict[str, Any], result_path: Path, errors: list[dict[str, Any]]) -> dict[str, Any] | None:
    candidates = []
    if isinstance(wrapper.get("result"), str):
        candidates.append(wrapper["result"])
    if isinstance(wrapper.get("content"), list):
        candidates.extend(str(item.get("text", "")) for item in wrapper["content"] if isinstance(item, dict))
    if isinstance(wrapper.get("message"), dict) and isinstance(wrapper["message"].get("content"), list):
        candidates.extend(str(item.get("text", "")) for item in wrapper["message"]["content"] if isinstance(item, dict))
    if not candidates and "schema_version" in wrapper and "rows" in wrapper:
        return wrapper
    for text in candidates:
        payload = parse_review_json(text, result_path, errors)
        if payload:
            return payload
    errors.append({"file": result_path.name, "error": "could not find parseable review payload"})
    return None


def parse_review_json(text: str, result_path: Path, errors: list[dict[str, Any]]) -> dict[str, Any] | None:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
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
    scope_status = enum_or(raw.get("scope_status"), SCOPE_STATUS, "insufficient_evidence")
    primary_label = enum_or(raw.get("primary_label"), PRIMARY_LABELS, "uncertain_insufficient_evidence")
    os_issue_class = normalize_issue_class(raw.get("os_issue_class"), scope_status, primary_label)
    fixability = normalize_fixability(raw.get("fixability"), scope_status, primary_label)
    if scope_status != "in_scope" and primary_label == "confirmed_os_issue":
        primary_label = "uncertain_insufficient_evidence"
    return {
        "schema_version": "windows_claude_os_review_label_v1",
        "label_quality": label_quality,
        "reviewed_at": date.today().isoformat(),
        "source_run_id": source_run_id,
        "skill_id": str(raw.get("skill_id") or ""),
        "target_os": "windows",
        "scope_status": scope_status,
        "primary_label": primary_label,
        "os_issue_class": os_issue_class,
        "fixability": fixability,
        "confidence": enum_or(raw.get("confidence"), CONFIDENCE, "medium"),
        "evidence_level": enum_or(raw.get("evidence_level"), EVIDENCE_LEVELS, "source_sample_dynamic"),
        "should_add_to_reviewed_labels": bool(raw.get("should_add_to_reviewed_labels", True)),
        "task_result": str(raw.get("task_result") or ""),
        "evidence_refs": string_list(raw.get("evidence_refs")),
        "notes": str(raw.get("notes") or ""),
        "uncertainties": string_list(raw.get("uncertainties")),
        "reviewer": "claude-code",
        "review_result_file": result_path.name,
        "review_batch_index": batch_index,
        "review_batch_position": position,
        "raw_review_row": raw,
    }


def normalize_issue_class(value: Any, scope_status: str, primary_label: str) -> str:
    raw = str(value or "")
    if raw in OS_ISSUE_CLASSES:
        return raw
    if scope_status == "excluded_external_credential_or_service":
        return "not_os_auth_or_service"
    if primary_label == "not_os_issue":
        return "not_os_generic_logic"
    return "insufficient_evidence"


def normalize_fixability(value: Any, scope_status: str, primary_label: str) -> str:
    raw = str(value or "")
    if raw in FIXABILITY:
        return raw
    if scope_status == "excluded_external_credential_or_service":
        return "need_credentials_or_service"
    if primary_label == "not_os_issue":
        return "not_os_issue"
    return "need_more_evidence"


def batch_summary(wrapper: dict[str, Any], result_path: Path, batch_index: Any, row_count: int) -> dict[str, Any]:
    return {
        "result_file": result_path.name,
        "batch_index": batch_index,
        "rows": row_count,
        "duration_s": round(float(wrapper.get("duration_ms") or 0) / 1000, 3),
        "cost_usd": float(wrapper.get("total_cost_usd") or 0),
        "model_usage": wrapper.get("modelUsage", {}),
        "is_error": bool(wrapper.get("is_error", False)),
    }


def build_summary(
    rows: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    batch_summaries: list[dict[str, Any]],
    expected_skill_ids: list[str],
    out: Path,
) -> dict[str, Any]:
    observed = [row["skill_id"] for row in rows]
    observed_counter = Counter(observed)
    missing = sorted(set(expected_skill_ids) - set(observed))
    duplicates = sorted(skill_id for skill_id, count in observed_counter.items() if count > 1)
    in_scope = [row for row in rows if row["scope_status"] == "in_scope"]
    confirmed = [row for row in in_scope if row["primary_label"] == "confirmed_os_issue"]
    return {
        "schema_version": "windows_claude_review_summary_v1",
        "rows": len(rows),
        "expected_rows": len(expected_skill_ids),
        "out": rel(out),
        "batch_count": len(batch_summaries),
        "batch_summaries": batch_summaries,
        "total_cost_usd": round(sum(item["cost_usd"] for item in batch_summaries), 6),
        "total_duration_s": round(sum(item["duration_s"] for item in batch_summaries), 3),
        "scope_counts": dict(Counter(row["scope_status"] for row in rows)),
        "primary_label_counts": dict(Counter(row["primary_label"] for row in rows)),
        "os_issue_class_counts": dict(Counter(row["os_issue_class"] for row in rows)),
        "fixability_counts": dict(Counter(row["fixability"] for row in rows)),
        "confidence_counts": dict(Counter(row["confidence"] for row in rows)),
        "confirmed_os_issue_count": len(confirmed),
        "confirmed_os_issue_skills": sorted(row["skill_id"] for row in confirmed),
        "missing_skill_ids": missing,
        "duplicate_skill_ids": duplicates,
        "error_count": len(errors),
        "errors": errors,
    }


def render_summary_md(summary: dict[str, Any]) -> str:
    lines = [
        "# Windows Claude Review Trial Summary",
        "",
        f"- Rows: {summary['rows']} / {summary['expected_rows']}",
        f"- Batches: {summary['batch_count']}",
        f"- Cost: ${summary['total_cost_usd']}",
        f"- Scope counts: `{summary['scope_counts']}`",
        f"- Primary label counts: `{summary['primary_label_counts']}`",
        f"- OS issue class counts: `{summary['os_issue_class_counts']}`",
        f"- Confirmed OS issue count: {summary['confirmed_os_issue_count']}",
        "",
        "## Confirmed OS Issue Skills",
        "",
    ]
    lines.extend(f"- `{skill_id}`" for skill_id in summary["confirmed_os_issue_skills"])
    if not summary["confirmed_os_issue_skills"]:
        lines.append("- None")
    if summary["missing_skill_ids"]:
        lines.extend(["", "## Missing Skills", ""])
        lines.extend(f"- `{skill_id}`" for skill_id in summary["missing_skill_ids"])
    if summary["error_count"]:
        lines.extend(["", "## Errors", ""])
        lines.extend(f"- `{item.get('file')}`: {item.get('error')}" for item in summary["errors"])
    return "\n".join(lines) + "\n"


def enum_or(value: Any, allowed: set[str], default: str) -> str:
    return str(value) if str(value) in allowed else default


def string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def load_json_safe(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


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
