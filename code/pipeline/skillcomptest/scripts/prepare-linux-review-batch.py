#!/usr/bin/env python3
"""Prepare compact Linux dynamic-review evidence for Claude Code.

The script is read-only with respect to skill packages and execution artifacts.
It does not run skill code. It packages existing dynamic execution summaries,
task metrics, static predictions, and small package snippets into prompt batches
for a reviewer agent.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RUN = REPO_ROOT / "docs/linux-testing/runs/linux-macos-v06-next300-20260625-s1x16-s2x16.json"
DEFAULT_STATIC = REPO_ROOT / "docs/linux-testing/reviews/linux-macos-v06-next300-20260625/static-predictions.jsonl"
DEFAULT_REVIEW_ROOT = REPO_ROOT / "docs/linux-testing/reviews"
DEFAULT_EXEC_ROOT = REPO_ROOT / ".cache/skillcomptest/exec/linux"
DEFAULT_SAMPLE_ROOT = REPO_ROOT / ".cache/skillcomptest/samples"
DEFAULT_SKILL_ROOTS = [
    REPO_ROOT / ".cache/skillcomptest/top5000-sources",
    REPO_ROOT / ".cache/skillcomptest/SkillsRepo",
]

OS_POSITIVE_LABELS = {
    "skill_os_fixable",
    "skill_package_or_entrypoint",
    "skill_contract_mismatch",
    "guide_only_os_contract",
    "platform_intrinsic",
}

TEXT_SUFFIXES = {
    ".md",
    ".txt",
    ".json",
    ".py",
    ".sh",
    ".js",
    ".ts",
    ".mjs",
    ".cjs",
    ".yaml",
    ".yml",
    ".toml",
}

PLATFORM_KEYWORD_RE = re.compile(
    r"macOS|Mac OS|Darwin|osascript|AppleScript|pbcopy|pbpaste|open -a|"
    r"Homebrew|brew install|/Applications|/Library|~/Library|"
    r"Windows|PowerShell|cmd\\.exe|winget|choco|win32|Win32|pywin32|"
    r"C:\\\\|%APPDATA%|AppData|registry|clip\\.exe",
    re.IGNORECASE,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-summary", type=Path, default=DEFAULT_RUN)
    parser.add_argument("--static-predictions", type=Path, default=DEFAULT_STATIC)
    parser.add_argument("--run-id", default="linux-macos-v06-next300-review-20260625")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument(
        "--candidate-scope",
        choices=("all", "dynamic-nonpass", "static-positive", "static-positive-or-nonpass"),
        default="all",
    )
    parser.add_argument("--include-existing-reviewed", action="store_true")
    parser.add_argument("--exec-root", type=Path, default=DEFAULT_EXEC_ROOT)
    parser.add_argument("--sample-root", type=Path, default=DEFAULT_SAMPLE_ROOT)
    parser.add_argument("--skill-root", action="append", type=Path, default=[])
    args = parser.parse_args()

    skill_roots = args.skill_root or DEFAULT_SKILL_ROOTS
    run = load_json(args.run_summary)
    static_by_skill = {row["skill_id"]: row for row in read_jsonl(args.static_predictions)}
    reviewed_ids = set() if args.include_existing_reviewed else load_existing_reviewed_ids()

    candidates = []
    for index, result in enumerate(run.get("results", [])):
        skill_id = f"{result['source']}/{result['skill']}"
        if skill_id in reviewed_ids:
            continue
        static_row = static_by_skill.get(skill_id)
        if not row_in_scope(result, static_row, args.candidate_scope):
            continue
        candidates.append((priority(result, static_row), index, skill_id, result, static_row))

    candidates.sort(key=lambda item: (item[0], -item[1], item[2]), reverse=True)
    selected = candidates[: args.limit] if args.limit else candidates

    out_dir = DEFAULT_REVIEW_ROOT / args.run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    evidence_rows = [
        build_evidence(
            skill_id=skill_id,
            run_result=result,
            static_row=static_row,
            rank=rank,
            exec_root=args.exec_root,
            sample_root=args.sample_root,
            skill_roots=skill_roots,
        )
        for rank, (_, _, skill_id, result, static_row) in enumerate(selected, 1)
    ]

    write_jsonl(out_dir / "evidence.jsonl", evidence_rows)
    write_json(out_dir / "candidate-selection-summary.json", selection_summary(run, candidates, selected, reviewed_ids))

    batches = []
    for batch_index, start in enumerate(range(0, len(evidence_rows), args.batch_size), 1):
        batch_rows = evidence_rows[start : start + args.batch_size]
        prompt_path = out_dir / f"prompt-batch-{batch_index:03d}.md"
        prompt_path.write_text(render_prompt(batch_index, batch_rows, source_run_id=run["run_id"]), encoding="utf-8")
        batches.append(
            {
                "batch_index": batch_index,
                "prompt_path": rel(prompt_path),
                "skill_ids": [row["skill_id"] for row in batch_rows],
            }
        )

    write_json(
        out_dir / "batch-manifest.json",
        {
            "schema_version": "linux_review_batch_manifest_v1",
            "run_id": args.run_id,
            "source_run_id": run["run_id"],
            "batch_size": args.batch_size,
            "batches": batches,
        },
    )
    print(json.dumps({"run_id": args.run_id, "out_dir": rel(out_dir), "selected": len(evidence_rows), "batches": len(batches)}, ensure_ascii=False, sort_keys=True))
    return 0


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def load_existing_reviewed_ids() -> set[str]:
    path = REPO_ROOT / "docs/linux-testing/datasets/os-compat-reviewed-labels.jsonl"
    return {row["skill_id"] for row in read_jsonl(path)}


def row_in_scope(result: dict[str, Any], static_row: dict[str, Any] | None, candidate_scope: str) -> bool:
    dynamic_nonpass = int(result.get("tasks_fail") or 0) + int(result.get("tasks_blocked") or 0) + int(result.get("tasks_unsupported") or 0) > 0
    static_positive = bool(static_row and static_row.get("predicted_label") in OS_POSITIVE_LABELS)
    if candidate_scope == "all":
        return True
    if candidate_scope == "dynamic-nonpass":
        return dynamic_nonpass
    if candidate_scope == "static-positive":
        return static_positive
    return dynamic_nonpass or static_positive


def priority(result: dict[str, Any], static_row: dict[str, Any] | None) -> tuple[int, int]:
    score = int((static_row or {}).get("score") or 0)
    nonpass = int(result.get("tasks_fail") or 0) + int(result.get("tasks_blocked") or 0) + int(result.get("tasks_unsupported") or 0)
    if (static_row or {}).get("predicted_label") in OS_POSITIVE_LABELS:
        score += 80
    score += nonpass * 10
    if result.get("portability_verdict") == "blocked":
        score += 15
    return score, nonpass


def selection_summary(
    run: dict[str, Any],
    candidates: list[tuple[tuple[int, int], int, str, dict[str, Any], dict[str, Any] | None]],
    selected: list[tuple[tuple[int, int], int, str, dict[str, Any], dict[str, Any] | None]],
    reviewed_ids: set[str],
) -> dict[str, Any]:
    return {
        "schema_version": "linux_review_candidate_selection_v1",
        "source_run_id": run.get("run_id"),
        "candidate_count_after_exclusions": len(candidates),
        "selected_count": len(selected),
        "excluded_existing_reviewed_count": len(reviewed_ids),
        "selected_static_label_counts": dict(Counter((static or {}).get("predicted_label", "missing_static") for _, _, _, _, static in selected)),
        "selected_verdict_counts": dict(Counter(result.get("portability_verdict", "") for _, _, _, result, _ in selected)),
        "selected_skill_ids": [skill_id for _, _, skill_id, _, _ in selected],
    }


def build_evidence(
    *,
    skill_id: str,
    run_result: dict[str, Any],
    static_row: dict[str, Any] | None,
    rank: int,
    exec_root: Path,
    sample_root: Path,
    skill_roots: list[Path],
) -> dict[str, Any]:
    source, package = skill_id.split("/", 1)
    package_root = find_package_root(skill_roots, source, package)
    platform_summary = load_platform_summary(exec_root, source, package)
    tasks = load_task_evidence(platform_summary.get("summary_path", ""), max_tasks=6)
    sample_manifest = load_sample_manifest(sample_root, source, package)
    static_prediction = static_row or {
        "predicted_label": "missing_static_prediction",
        "predicted_fixability": "unknown",
        "score": 0,
        "confidence": "none",
        "evidence": [],
        "negative_rules": [],
    }
    return {
        "rank": rank,
        "skill_id": skill_id,
        "target_os": "linux",
        "run_result": {
            key: run_result.get(key)
            for key in [
                "status",
                "tasks_generated",
                "tasks_pass",
                "tasks_fail",
                "tasks_blocked",
                "tasks_unsupported",
                "success_rate",
                "portability_verdict",
                "exec_duration_s",
            ]
        },
        "static_prediction": {
            "predicted_label": static_prediction.get("predicted_label"),
            "predicted_fixability": static_prediction.get("predicted_fixability"),
            "score": static_prediction.get("score"),
            "confidence": static_prediction.get("confidence"),
            "evidence": static_prediction.get("evidence", [])[:12],
            "negative_rules": static_prediction.get("negative_rules", []),
        },
        "dynamic_summary": platform_summary,
        "task_evidence": tasks,
        "sample_manifest": sample_manifest,
        "package_evidence": {
            "package_root": rel(package_root) if package_root else "",
            "file_inventory": file_inventory(package_root),
            "metadata": metadata_files(package_root),
            "skill_overview": skill_overview(package_root),
            "static_evidence_snippets": evidence_snippets(package_root, static_prediction.get("evidence", [])[:12]),
            "platform_keyword_snippets": platform_keyword_snippets(package_root),
        },
    }


def find_package_root(skill_roots: list[Path], source: str, package: str) -> Path | None:
    for root in skill_roots:
        candidate = root / source / package
        if find_skill_file(candidate):
            return candidate
    return None


def find_skill_file(package_root: Path | None) -> Path | None:
    if not package_root:
        return None
    for name in ("SKILL.md", "skill.md"):
        path = package_root / name
        if path.is_file():
            return path
    return None


def load_platform_summary(exec_root: Path, source: str, package: str) -> dict[str, Any]:
    base = exec_root / source / package
    candidates = sorted(base.glob("**/platform_summary.json"), key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)
    if not candidates:
        return {"available": False, "summary_path": ""}
    path = candidates[0]
    try:
        data = load_json(path)
    except Exception as exc:
        return {"available": False, "summary_path": rel(path), "error": str(exc)}
    keep = {"available": True, "summary_path": rel(path)}
    for key in [
        "skill",
        "platform",
        "pass_count",
        "fail_count",
        "blocked_count",
        "unsupported_count",
        "unknown_count",
        "expected_task_count",
        "platform_success_rate",
        "portability_verdict",
        "failure_categories_observed",
        "portability_blocker_types",
        "environment_summary",
    ]:
        if key in data:
            keep[key] = truncate_value(data[key], 5000)
    return keep


def load_task_evidence(summary_path: str, max_tasks: int) -> list[dict[str, Any]]:
    if not summary_path:
        return []
    summary = REPO_ROOT / summary_path
    tasks_root = summary.parent / "tasks"
    if not tasks_root.is_dir():
        return []
    rows = []
    for task_dir in sorted(path for path in tasks_root.iterdir() if path.is_dir())[:max_tasks]:
        metric_path = task_dir / "task_metrics.json"
        output_path = task_dir / "results/task_output.json"
        row = {"task_id": task_dir.name}
        if metric_path.is_file():
            row["task_metrics"] = compact_task_metrics(load_json_safe(metric_path))
        if output_path.is_file():
            row["task_output"] = truncate_value(load_json_safe(output_path), 1200)
        rows.append(row)
    return rows


def compact_task_metrics(data: dict[str, Any]) -> dict[str, Any]:
    keep = {}
    for key in ["task_id", "status", "failure_category", "summary", "environment_evidence"]:
        if key in data:
            keep[key] = truncate_value(data[key], 2500)
    return keep


def load_sample_manifest(sample_root: Path, source: str, package: str) -> dict[str, Any]:
    base = sample_root / source / package
    candidates = sorted(base.glob("*/benchmark_manifest.json"), key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)
    if not candidates:
        return {"available": False}
    path = candidates[0]
    data = load_json_safe(path)
    keep = {"available": True, "path": rel(path)}
    for key in ["skill_name", "skill_type", "required_capabilities", "summary", "functional_tasks"]:
        if key in data:
            keep[key] = truncate_value(data[key], 3500)
    return keep


def file_inventory(package_root: Path | None) -> dict[str, Any]:
    if not package_root or not package_root.is_dir():
        return {"available": False}
    files = []
    total = 0
    for path in sorted(p for p in package_root.rglob("*") if p.is_file()):
        total += 1
        if len(files) < 100:
            files.append({"path": path.relative_to(package_root).as_posix(), "size": safe_size(path)})
    return {"available": True, "total_files": total, "files_prefix": files}


def metadata_files(package_root: Path | None) -> dict[str, str]:
    if not package_root:
        return {}
    result = {}
    for name in ["_meta.json", "package.json", "pyproject.toml", "requirements.txt"]:
        path = package_root / name
        if path.is_file():
            result[name] = safe_read(path, 2500)
    return result


def skill_overview(package_root: Path | None) -> dict[str, Any]:
    if not package_root:
        return {"available": False}
    path = find_skill_file(package_root)
    if not path:
        return {"available": False}
    lines = safe_read(path, 12000).splitlines()
    return {
        "available": True,
        "path": path.relative_to(package_root).as_posix(),
        "first_lines": [{"line": index + 1, "text": line[:500]} for index, line in enumerate(lines[:90])],
    }


def evidence_snippets(package_root: Path | None, evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not package_root:
        return []
    snippets = []
    seen: set[tuple[str, int]] = set()
    for item in evidence:
        rel_path = item.get("file")
        line = item.get("line")
        if not isinstance(rel_path, str) or not isinstance(line, int):
            continue
        key = (rel_path, line)
        if key in seen:
            continue
        seen.add(key)
        snippets.append(
            {
                "rule_id": item.get("rule_id"),
                "file": rel_path,
                "line": line,
                "matched_text": item.get("matched_text"),
                "context": line_context(package_root / rel_path, line, radius=3),
            }
        )
    return snippets


def platform_keyword_snippets(package_root: Path | None) -> list[dict[str, Any]]:
    if not package_root or not package_root.is_dir():
        return []
    result = []
    for path in sorted(p for p in package_root.rglob("*") if p.is_file()):
        if len(result) >= 30:
            break
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if safe_size(path) > 512_000:
            continue
        for idx, line in enumerate(safe_read(path, 64_000).splitlines(), 1):
            if PLATFORM_KEYWORD_RE.search(line):
                result.append({"file": path.relative_to(package_root).as_posix(), "line": idx, "text": line.strip()[:500]})
                if len(result) >= 30:
                    break
    return result


def line_context(path: Path, line_no: int, radius: int) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    lines = safe_read(path, 64_000).splitlines()
    start = max(1, line_no - radius)
    end = min(len(lines), line_no + radius)
    return [{"line": idx, "text": lines[idx - 1][:500]} for idx in range(start, end + 1)]


def load_json_safe(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"value": data}
    except Exception as exc:
        return {"error": str(exc)}


def safe_read(path: Path, max_chars: int) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:max_chars]
    except Exception as exc:
        return f"<read error: {exc}>"


def safe_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def truncate_value(value: Any, max_chars: int) -> Any:
    text = json.dumps(value, ensure_ascii=False)
    if len(text) <= max_chars:
        return value
    return {"truncated_json_prefix": text[:max_chars], "truncated": True}


def rel(path: Path | str) -> str:
    path = Path(path)
    try:
        return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def render_prompt(batch_index: int, evidence_rows: list[dict[str, Any]], source_run_id: str) -> str:
    evidence_json = json.dumps(evidence_rows, ensure_ascii=False, indent=2)
    return f"""You are a read-only reviewer agent for a research dataset about skill OS compatibility.

Review target: Linux.
Source dynamic run: {source_run_id}
Batch index: {batch_index}

Hard safety rules:
- Do not execute any skill script, package code, installer, test, browser, GUI command, network command, or package manager command.
- Do not modify files.
- Use only the compact evidence JSON below.
- Do not use network access.
- Do not print or infer secret environment variable values.

Primary labels:
- os_specific_execution_issue: OS-specific command/path/shell/platform API/permission/architecture assumption conflicts with Linux. Use this for macOS-only commands such as osascript, pbcopy, pbpaste, open -a, say, Homebrew-only execution paths, Windows COM/Win32/PowerShell/registry/drive-letter assumptions, or scripts that dispatch on macOS/Windows but omit Linux.
- os_install_or_dependency_contract_issue: the capability could work on Linux, but install/dependency instructions omit a usable Linux path or only provide macOS/Windows installation contracts.
- package_or_entrypoint_issue: referenced script/wrapper/binary/entrypoint/path is absent or undiscoverable from the package. This can be OS-relevant when Linux agents cannot invoke the skill through the documented entry surface.
- not_os_issue: evidence points to API key/OAuth/account/private service, generic task bug, generic logic bug, benchmark fixture gap, or all observed tasks pass.

Scope:
- in_scope: evidence can test Linux behavior or package/entrypoint/guide contract.
- excluded_external_credential_or_service: blocked by API key, OAuth, private account, remote service state, or paid provider.
- excluded_benchmark_capability_gap: blocked by benchmark runner capability such as GUI/browser/device/local service fixture, where evidence does not show a skill-side Linux defect.

Allowed fixability values:
- code_fix
- code_fix_non_os
- guide_fix
- package_fix
- install_contract_fix
- env_fix
- runner_or_task_fix
- need_credentials_or_service
- need_external_service_or_distribution
- not_skill_fix
- uncertain
- requires_environment_provisioning
- not_os_issue
- need_more_evidence

Output rules:
- Return raw JSON only. The first character must be {{ and the last character must be }}.
- No markdown fences, no explanatory prose outside JSON.
- Use this schema:
{{
  "schema_version": "claude_linux_review_batch_v1",
  "reviewer": "claude-code",
  "target_os": "linux",
  "source_run_id": "{source_run_id}",
  "batch_index": {batch_index},
  "rows": [
    {{
      "skill_id": "...",
      "scope_status": "in_scope | excluded_external_credential_or_service | excluded_benchmark_capability_gap",
      "primary_label": "os_specific_execution_issue | os_install_or_dependency_contract_issue | package_or_entrypoint_issue | not_os_issue",
      "secondary_tags": ["..."],
      "evidence_level": "guide_only | static_only | dynamic_execution | both",
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
