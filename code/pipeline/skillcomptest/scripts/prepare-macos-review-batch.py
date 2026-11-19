#!/usr/bin/env python3
"""Prepare compact macOS review evidence for Claude Code.

This script is read-only with respect to skill packages. It selects unreviewed
macOS static OS/package candidates, extracts small evidence snippets, and writes
batch prompts for a reviewer agent. It never executes skill code.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
DATASET_DIR = REPO_ROOT / "docs" / "macos-testing" / "datasets"
REVIEW_DIR = REPO_ROOT / "docs" / "macos-testing" / "reviews"
MACOS_BENCHMARK_EXEC_ROOT = REPO_ROOT.parent / "Compa-Skill" / "data" / "benchmarks" / "macos" / "exec"

OS_POSITIVE_LABELS = {
    "skill_os_fixable",
    "skill_package_or_entrypoint",
    "skill_contract_mismatch",
    "guide_only_os_contract",
    "platform_intrinsic",
}

STRICT_RULES = {
    "windows.command",
    "path.windows_literal",
    "python.windows_import",
    "pkg.linux_package_manager",
    "system.systemd",
    "system.procfs",
    "system.sysfs",
    "shell.linux_free",
    "shell.linux_uptime_pretty",
    "guide.linux_only_system_utilities",
    "shell.gnu_date_d",
    "shell.gnu_readlink_f",
    "shell.gnu_realpath",
    "shell.gnu_xargs_r",
    "shell.linux_xdg_open",
    "shell.linux_clipboard",
    "arch.x86_simd_on_macos",
}

STRONG_DYNAMIC_CATEGORIES = {
    "os_api_incompatible",
    "path_shell_assumption",
    "packaging_or_install",
}

KEYWORD_RE = re.compile(
    r"Windows|PowerShell|WinGet|winget|win32com|pywin32|pywinauto|Win32|UIA|"
    r"macOS|darwin|osascript|Accessibility|Full Disk Access|TCC|"
    r"Linux|systemctl|apt-get|apt install|/proc|/sys|xdg-open|free -h|uptime -p|"
    r"xargs -r|date -d|readlink -f|realpath|C:\\\\",
    re.IGNORECASE,
)

TEXT_SUFFIXES = {
    ".md",
    ".txt",
    ".json",
    ".py",
    ".sh",
    ".js",
    ".ts",
    ".cjs",
    ".mjs",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".ps1",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--run-id", default="broad-static30-20260623")
    parser.add_argument(
        "--candidate-scope",
        choices=("broad-positive", "static-negative", "all"),
        default="broad-positive",
        help="Which static prediction rows to consider.",
    )
    parser.add_argument(
        "--order",
        choices=("priority", "original"),
        default="priority",
        help="Candidate ordering before applying --limit.",
    )
    parser.add_argument("--include-existing-agent-reviews", action="store_true")
    parser.add_argument("--include-reviewed-ground-truth", action="store_true")
    args = parser.parse_args()

    static_rows = read_jsonl(DATASET_DIR / "static-skill-predictions.jsonl")
    dynamic_rows = read_jsonl(DATASET_DIR / "dynamic-skill-labels.jsonl")
    reviewed_rows = read_jsonl(DATASET_DIR / "os-compat-reviewed-labels.jsonl")
    dynamic_by_skill = {row["skill_id"]: row for row in dynamic_rows}
    reviewed_ids = {row["skill_id"] for row in reviewed_rows}
    agent_reviewed_ids = set() if args.include_existing_agent_reviews else load_agent_reviewed_ids(REVIEW_DIR)

    candidates = []
    for original_index, row in enumerate(static_rows):
        if not row_in_scope(row, args.candidate_scope):
            continue
        skill_id = row["skill_id"]
        if not args.include_reviewed_ground_truth and skill_id in reviewed_ids:
            continue
        if skill_id in agent_reviewed_ids:
            continue
        dynamic = dynamic_by_skill.get(skill_id)
        candidates.append((priority(row, dynamic), original_index, skill_id, row, dynamic))

    if args.order == "priority":
        candidates.sort(key=lambda item: (item[0], -item[1], item[2]), reverse=True)
    else:
        candidates.sort(key=lambda item: item[1])
    selected = candidates[: args.limit]

    out_dir = REVIEW_DIR / args.run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    evidence_rows = [
        build_evidence(skill_id=skill_id, static_row=static_row, dynamic_row=dynamic_row, rank=index + 1)
        for index, (_, _, skill_id, static_row, dynamic_row) in enumerate(selected)
    ]

    write_json(out_dir / "candidate-selection-summary.json", selection_summary(candidates, selected, reviewed_ids, agent_reviewed_ids))
    write_jsonl(out_dir / "evidence.jsonl", evidence_rows)

    batches = []
    for batch_index, start in enumerate(range(0, len(evidence_rows), args.batch_size), 1):
        batch_rows = evidence_rows[start : start + args.batch_size]
        prompt_path = out_dir / f"prompt-batch-{batch_index:03d}.md"
        prompt_path.write_text(render_prompt(batch_index, batch_rows), encoding="utf-8")
        batches.append(
            {
                "batch_index": batch_index,
                "prompt_path": str(prompt_path),
                "skill_ids": [row["skill_id"] for row in batch_rows],
            }
        )

    write_json(out_dir / "batch-manifest.json", {"run_id": args.run_id, "batch_size": args.batch_size, "batches": batches})
    print(json.dumps({"run_id": args.run_id, "out_dir": str(out_dir), "selected": len(evidence_rows), "batches": len(batches)}, ensure_ascii=False))
    return 0


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def load_agent_reviewed_ids(review_dir: Path) -> set[str]:
    ids: set[str] = set()
    for path in review_dir.glob("**/*extracted*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for row in data.get("rows", []):
            skill_id = row.get("skill_id")
            if isinstance(skill_id, str):
                ids.add(skill_id)
    return ids


def row_in_scope(row: dict[str, Any], candidate_scope: str) -> bool:
    is_broad_positive = row.get("predicted_label") in OS_POSITIVE_LABELS
    if candidate_scope == "broad-positive":
        return is_broad_positive
    if candidate_scope == "static-negative":
        return not is_broad_positive
    return True


def priority(static_row: dict[str, Any], dynamic_row: dict[str, Any] | None) -> tuple[int, int, str]:
    score = int(static_row.get("score") or 0)
    label = static_row.get("predicted_label", "")
    rules = {e.get("rule_id") for e in static_row.get("evidence", [])}
    dynamic_label = dynamic_row.get("dynamic_label") if dynamic_row else None
    categories = set((dynamic_row.get("failure_categories") or {}).keys()) if dynamic_row else set()
    scope_hint = dynamic_row.get("scope_hint") if dynamic_row else None

    value = score
    value += {
        "skill_os_fixable": 50,
        "platform_intrinsic": 50,
        "guide_only_os_contract": 40,
        "skill_contract_mismatch": 30,
        "skill_package_or_entrypoint": 20,
    }.get(label, 0)
    if rules & STRICT_RULES:
        value += 45
    if categories & STRONG_DYNAMIC_CATEGORIES:
        value += 80
    if dynamic_label and dynamic_label != "no_observed_issue":
        value += 35
    if scope_hint == "possible_os_compatibility_issue":
        value += 20
    elif scope_hint == "needs_review":
        value += 10
    elif scope_hint == "likely_external_service_or_credential":
        value -= 20
    elif dynamic_label == "no_observed_issue":
        value -= 20
    if dynamic_row is None:
        value -= 5
    return (value, score, static_row["skill_id"])


def selection_summary(
    candidates: list[tuple[tuple[int, int, str], int, str, dict[str, Any], dict[str, Any] | None]],
    selected: list[tuple[tuple[int, int, str], int, str, dict[str, Any], dict[str, Any] | None]],
    reviewed_ids: set[str],
    agent_reviewed_ids: set[str],
) -> dict[str, Any]:
    return {
        "schema_version": "macos_review_candidate_selection_v1",
        "candidate_count_after_exclusions": len(candidates),
        "selected_count": len(selected),
        "excluded_reviewed_ground_truth_count": len(reviewed_ids),
        "excluded_existing_agent_review_count": len(agent_reviewed_ids),
        "selected_label_counts": dict(Counter(row.get("predicted_label", "") for _, _, _, row, _ in selected)),
        "selected_dynamic_label_counts": dict(Counter((dyn or {}).get("dynamic_label", "missing_dynamic") for _, _, _, _, dyn in selected)),
        "selected_skill_ids": [skill_id for _, _, skill_id, _, _ in selected],
    }


def build_evidence(skill_id: str, static_row: dict[str, Any], dynamic_row: dict[str, Any] | None, rank: int) -> dict[str, Any]:
    package_root = Path(static_row.get("package_root", ""))
    static_evidence = static_row.get("evidence", [])[:12]
    summary_path = first_summary_path(dynamic_row)
    return {
        "rank": rank,
        "skill_id": skill_id,
        "target_os": "macos",
        "static_prediction": {
            "predicted_label": static_row.get("predicted_label"),
            "predicted_fixability": static_row.get("predicted_fixability"),
            "score": static_row.get("score"),
            "confidence": static_row.get("confidence"),
            "evidence": static_evidence,
            "negative_rules": static_row.get("negative_rules", []),
        },
        "dynamic_label": compact_dynamic_label(dynamic_row),
        "dynamic_summary": load_dynamic_summary(summary_path),
        "package_evidence": {
            "package_root": str(package_root),
            "file_inventory": file_inventory(package_root),
            "metadata": metadata_files(package_root),
            "skill_overview": skill_overview(package_root),
            "static_evidence_snippets": evidence_snippets(package_root, static_evidence),
            "platform_keyword_snippets": platform_keyword_snippets(package_root),
        },
    }


def compact_dynamic_label(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {"dynamic_label": "missing_dynamic"}
    keys = [
        "dynamic_label",
        "scope_hint",
        "task_counts",
        "failure_categories",
        "source_runs",
        "evidence_summary",
    ]
    return {key: row.get(key) for key in keys if key in row}


def first_summary_path(row: dict[str, Any] | None) -> Path | None:
    if not row:
        return None
    runs = row.get("source_runs") or []
    if not runs:
        return None
    path = MACOS_BENCHMARK_EXEC_ROOT / runs[0] / "platform_summary.json"
    return path if path.is_file() else None


def load_dynamic_summary(path: Path | None) -> dict[str, Any]:
    if not path or not path.is_file():
        return {"summary_path": str(path) if path else "", "available": False}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"summary_path": str(path), "available": False, "error": str(exc)}
    keep = {
        "summary_path": str(path),
        "available": True,
    }
    for key in [
        "platform",
        "verdict",
        "overall_verdict",
        "compatibility_verdict",
        "platform_compatibility",
        "overall_assessment",
        "dominant_blocker_type",
        "dominant_failure",
        "dominant_failure_category",
        "primary_failure_category",
        "failure_summary",
        "failure_distribution",
        "failure_categories",
        "failure_category_counts",
        "key_blockers",
        "all_tasks_unsupported_reason",
        "recommendations",
        "task_results",
        "task_outcomes",
        "tasks",
        "results",
        "execution_summary",
        "key_findings",
    ]:
        if key in data:
            keep[key] = truncate_value(data[key], max_chars=6000)
    return keep


def truncate_value(value: Any, max_chars: int) -> Any:
    text = json.dumps(value, ensure_ascii=False)
    if len(text) <= max_chars:
        return value
    return {"truncated_json_prefix": text[:max_chars], "truncated": True}


def file_inventory(package_root: Path) -> dict[str, Any]:
    if not package_root.is_dir():
        return {"available": False}
    files = []
    total = 0
    for path in sorted(p for p in package_root.rglob("*") if p.is_file()):
        total += 1
        if len(files) < 120:
            try:
                size = path.stat().st_size
            except OSError:
                size = None
            files.append({"path": str(path.relative_to(package_root)), "size": size})
    return {"available": True, "total_files": total, "files_prefix": files}


def metadata_files(package_root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in ["_meta.json", "package.json", "pyproject.toml", "requirements.txt"]:
        path = package_root / name
        if path.is_file():
            result[name] = safe_read(path, max_chars=2500)
    return result


def skill_overview(package_root: Path) -> dict[str, Any]:
    path = package_root / "SKILL.md"
    if not path.is_file():
        return {"available": False}
    lines = safe_read_lines(path)
    return {
        "available": True,
        "path": "SKILL.md",
            "first_lines": [{"line": index + 1, "text": truncate_text(line)} for index, line in enumerate(lines[:90])],
    }


def evidence_snippets(package_root: Path, evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    snippets = []
    seen: set[tuple[str, int]] = set()
    for item in evidence:
        rel = item.get("file")
        line = item.get("line")
        if not isinstance(rel, str) or not isinstance(line, int):
            continue
        key = (rel, line)
        if key in seen:
            continue
        seen.add(key)
        path = package_root / rel
        snippets.append(
            {
                "rule_id": item.get("rule_id"),
                "file": rel,
                "line": line,
                "matched_text": item.get("matched_text"),
                "context": line_context(path, line, radius=4),
            }
        )
    return snippets


def platform_keyword_snippets(package_root: Path) -> list[dict[str, Any]]:
    if not package_root.is_dir():
        return []
    result = []
    for path in sorted(p for p in package_root.rglob("*") if p.is_file()):
        if len(result) >= 30:
            break
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            if path.stat().st_size > 512_000:
                continue
        except OSError:
            continue
        lines = safe_read_lines(path)
        for idx, line in enumerate(lines, 1):
            if KEYWORD_RE.search(line):
                result.append(
                    {
                        "file": str(path.relative_to(package_root)),
                        "line": idx,
                        "text": line.strip()[:500],
                    }
                )
                if len(result) >= 30:
                    break
    return result


def line_context(path: Path, line_no: int, radius: int) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    lines = safe_read_lines(path)
    start = max(1, line_no - radius)
    end = min(len(lines), line_no + radius)
    return [{"line": idx, "text": truncate_text(lines[idx - 1])} for idx in range(start, end + 1)]


def safe_read(path: Path, max_chars: int) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"<read error: {exc}>"
    return text[:max_chars]


def safe_read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []


def truncate_text(text: str, max_chars: int = 500) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "...<truncated>"


def render_prompt(batch_index: int, evidence_rows: list[dict[str, Any]]) -> str:
    evidence_json = json.dumps(evidence_rows, ensure_ascii=False, indent=2)
    return f"""You are a read-only reviewer agent for a research dataset about skill OS compatibility.

Review target: macOS.
Batch index: {batch_index}

Hard safety rules:
- Do not execute any skill script, package code, installer, test, browser, GUI command, network command, or package manager command.
- Do not modify files.
- Prefer the compact evidence JSON below. Do not inspect package directories unless the compact evidence is insufficient.
- If extra inspection is absolutely necessary, use only read-only commands such as rg, sed, head, find, ls, or JSON parsing.
- Do not use network access.
- Do not print or inspect secret environment variable values.

Primary labels:
- os_specific_execution_issue: OS-specific command/path/shell/platform API/permission/architecture assumption conflicts with macOS. Use this for Windows COM, Win32, pywin32, pywinauto/UIA, PowerShell-only, registry, Windows SDK, Linux X11, systemd, /proc, Linux-only shell commands, GNU/BSD incompatibilities, or hardcoded Linux paths when they affect macOS.
- os_install_or_dependency_contract_issue: capability may work on macOS, but install/dependency instructions omit a usable macOS path.
- package_or_entrypoint_issue: referenced script/wrapper/binary/entrypoint/path is absent or undiscoverable from the package.
- not_os_issue: evidence points to auth/service, task bug, generic logic bug, or all observed tasks pass.

Scope:
- in_scope: evidence can test macOS behavior or package/entrypoint/guide contract.
- excluded_external_credential_or_service: blocked by API key/OAuth/private account/remote service.
- excluded_benchmark_capability_gap: benchmark runner lacks GUI/browser/account/permission fixture or managed local service; not a skill-side macOS bug.

Fixability examples:
- small_skill_script_fix
- small_skill_guide_fix
- small_skill_metadata_fix
- package_entrypoint_fix
- install_contract_fix
- requires_environment_provisioning
- not_functionally_fixable_for_macos
- not_os_issue
- need_more_evidence
- need_credentials_or_service
- need_external_service_or_distribution

Output rules:
- Return raw JSON only. The first character must be {{ and the last character must be }}.
- No markdown fences, no explanatory prose outside JSON.
- Use this schema:
{{
  "schema_version": "claude_macos_review_batch_v1",
  "reviewer": "claude-code",
  "target_os": "macos",
  "batch_index": {batch_index},
  "rows": [
    {{
      "skill_id": "...",
      "scope_status": "in_scope | excluded_external_credential_or_service | excluded_benchmark_capability_gap",
      "primary_label": "os_specific_execution_issue | os_install_or_dependency_contract_issue | package_or_entrypoint_issue | not_os_issue",
      "secondary_tags": ["..."],
      "evidence_level": "guide_only | static_only | dynamic_execution | both | manual_source_review",
      "fixability": "...",
      "confidence": "low | medium | high",
      "should_add_to_reviewed_labels": true,
      "task_result": "...",
      "evidence_refs": ["..."],
      "notes": "...",
      "uncertainties": ["..."]
    }}
  ],
  "review_quality_notes": "..."
}}

Important: should_add_to_reviewed_labels means useful for the reviewed ground-truth layer, including controls and exclusions. It does not mean the row is a positive OS bug.

Compact evidence JSON:
{evidence_json}
"""


if __name__ == "__main__":
    raise SystemExit(main())
