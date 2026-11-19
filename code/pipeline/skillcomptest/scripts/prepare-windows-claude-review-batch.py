#!/usr/bin/env python3
"""Prepare compact Windows OS-compatibility review batches for Claude Code.

This is a read-only packaging step. It never executes skill code or installers;
it only collects compact source/sample/dynamic summaries into prompt files so
Claude Code can review the evidence without tool access.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter
import json
from pathlib import Path
import re
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROUTING = REPO_ROOT / "docs/cross-os-testing/analysis/archive/20260625-legacy-exploratory/windows-b-nonpass-highyield-routing-20260626.csv"
DEFAULT_PROGRESS = REPO_ROOT / "docs/cross-os-testing/analysis/archive/20260625-legacy-exploratory/windows-review-progress-20260626.json"
DEFAULT_REVIEW_ROOT = REPO_ROOT / "docs/cross-os-testing/reviews"
DEFAULT_RUN_ID = "windows-cc-review-test100-20260626"

TEXT_SUFFIXES = {
    ".bat",
    ".cmd",
    ".cjs",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".mjs",
    ".ps1",
    ".py",
    ".sh",
    ".toml",
    ".ts",
    ".txt",
    ".yaml",
    ".yml",
}

KEY_FILE_NAMES = [
    "SKILL.md",
    "skill-card.md",
    "README.md",
    "_meta.json",
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "skill.yaml",
    "skill.yml",
]

SKIP_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    ".next",
    ".cache",
}

PLATFORM_KEYWORD_RE = re.compile(
    r"macOS|Mac OS|Darwin|osascript|AppleScript|pbcopy|pbpaste|open -a|"
    r"Homebrew|brew install|/Applications|/Library|~/Library|"
    r"Linux|systemctl|apt-get|apt install|yum install|dnf install|"
    r"/usr/|/etc/|/tmp/|/var/|chmod|chown|sudo|"
    r"Windows|PowerShell|cmd\.exe|winget|choco|Win32|pywin32|registry|"
    r"C:\\|%APPDATA%|AppData|clip\.exe|\.bat|\.cmd",
    re.IGNORECASE,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--routing-csv", type=Path, default=DEFAULT_ROUTING)
    parser.add_argument("--progress-json", type=Path, default=DEFAULT_PROGRESS)
    parser.add_argument("--run-id", default=DEFAULT_RUN_ID)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--review-root", type=Path, default=DEFAULT_REVIEW_ROOT)
    parser.add_argument("--include-reviewed", action="store_true")
    parser.add_argument("--include-missing-source", action="store_true")
    args = parser.parse_args()

    routing_rows = read_csv(args.routing_csv)
    reviewed_ids = set() if args.include_reviewed else load_reviewed_skill_ids(args.progress_json)
    selected = select_rows(
        routing_rows,
        reviewed_ids=reviewed_ids,
        limit=args.limit,
        include_missing_source=args.include_missing_source,
    )

    out_dir = args.review_root / args.run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    evidence_rows = [build_evidence(row, index) for index, row in enumerate(selected, 1)]
    write_jsonl(out_dir / "evidence.jsonl", evidence_rows)
    write_json(out_dir / "candidate-selection-summary.json", selection_summary(routing_rows, selected, reviewed_ids))

    batches = []
    for batch_index, start in enumerate(range(0, len(evidence_rows), args.batch_size), 1):
        batch_rows = evidence_rows[start : start + args.batch_size]
        prompt_path = out_dir / f"prompt-batch-{batch_index:03d}.md"
        prompt_path.write_text(render_prompt(batch_index, batch_rows, args.run_id), encoding="utf-8")
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
            "schema_version": "windows_claude_review_batch_manifest_v1",
            "run_id": args.run_id,
            "target_os": "windows",
            "routing_csv": rel(args.routing_csv),
            "progress_json": rel(args.progress_json),
            "batch_size": args.batch_size,
            "selected_count": len(evidence_rows),
            "batches": batches,
        },
    )
    print(
        json.dumps(
            {
                "run_id": args.run_id,
                "out_dir": rel(out_dir),
                "selected": len(evidence_rows),
                "batches": len(batches),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def load_reviewed_skill_ids(progress_json: Path) -> set[str]:
    reviewed: set[str] = set()
    if progress_json.is_file():
        data = load_json(progress_json)
        reviewed.update(str(skill_id) for skill_id in data.get("reviewed", {}).get("reviewed_skill_ids", []))
    for path in [
        REPO_ROOT / "docs/cross-os-testing/analysis/provenance/windows-agent-reviewed-os-candidates-20260626.jsonl",
        REPO_ROOT / "docs/cross-os-testing/analysis/provenance/windows-agent-confirmed-os-issue-additions-wave2-highyield-20260626.jsonl",
    ]:
        for row in read_jsonl(path):
            if row.get("skill_id"):
                reviewed.add(str(row["skill_id"]))
    return reviewed


def select_rows(
    rows: list[dict[str, str]],
    *,
    reviewed_ids: set[str],
    limit: int,
    include_missing_source: bool,
) -> list[dict[str, str]]:
    selected = []
    for row in rows:
        skill_id = row.get("skill_id", "")
        if skill_id in reviewed_ids:
            continue
        if row.get("review_track") != "B_dynamic_nonpass_unreviewed":
            continue
        if not include_missing_source and row.get("has_source") != "True":
            continue
        selected.append(row)
        if limit > 0 and len(selected) >= limit:
            break
    return selected


def selection_summary(
    all_rows: list[dict[str, str]],
    selected: list[dict[str, str]],
    reviewed_ids: set[str],
) -> dict[str, Any]:
    remaining = [
        row
        for row in all_rows
        if row.get("review_track") == "B_dynamic_nonpass_unreviewed" and row.get("skill_id", "") not in reviewed_ids
    ]
    return {
        "schema_version": "windows_claude_review_candidate_selection_v1",
        "routing_rows": len(all_rows),
        "reviewed_exclusion_count": len(reviewed_ids),
        "remaining_unreviewed_b_records": len(remaining),
        "remaining_unreviewed_b_with_source": sum(1 for row in remaining if row.get("has_source") == "True"),
        "selected_count": len(selected),
        "selected_outcome_counts": dict(Counter(row.get("dynamic_outcome", "") for row in selected)),
        "selected_failure_counts": dict(Counter(row.get("dominant_failure_category", "") for row in selected)),
        "selected_skill_ids": [row.get("skill_id", "") for row in selected],
    }


def build_evidence(row: dict[str, str], rank: int) -> dict[str, Any]:
    source_path = Path(row.get("source_path", ""))
    sample_path = Path(row.get("sample_path", ""))
    windows_exec_path = Path(row.get("windows_exec_path", ""))
    return {
        "rank_in_this_review": rank,
        "skill_id": row.get("skill_id", ""),
        "target_os": "windows",
        "queue_record": {
            key: row.get(key, "")
            for key in [
                "review_rank",
                "review_track",
                "source",
                "package_id",
                "slug_family",
                "dynamic_outcome",
                "label_status",
                "dominant_failure_category",
                "success_rate",
                "task_total",
                "task_pass",
                "task_fail",
                "task_blocked",
                "task_unsupported",
                "dynamic_note",
                "evidence_level_before_review",
                "routing_score",
                "routing_signals",
            ]
        },
        "package_evidence": package_evidence(source_path, row.get("routing_evidence_files", "")),
        "sample_evidence": sample_evidence(sample_path),
        "windows_execution_evidence": execution_evidence(windows_exec_path),
    }


def package_evidence(package_root: Path, routing_evidence_files: str) -> dict[str, Any]:
    if not package_root.is_dir():
        return {"available": False, "package_root": str(package_root)}
    selected_paths = choose_package_files(package_root, routing_evidence_files)
    snippets = []
    total_chars = 0
    for path in selected_paths:
        if len(snippets) >= 7 or total_chars >= 5_500:
            break
        if not is_text_file(path):
            continue
        text = safe_read(path, 950)
        total_chars += len(text)
        snippets.append(
            {
                "path": path.relative_to(package_root).as_posix(),
                "size": safe_size(path),
                "text_prefix": text,
            }
        )
    return {
        "available": True,
        "package_root": rel(package_root),
        "file_inventory": file_inventory(package_root),
        "selected_file_snippets": snippets,
        "platform_keyword_hits": platform_keyword_hits(package_root, selected_paths),
    }


def choose_package_files(package_root: Path, routing_evidence_files: str) -> list[Path]:
    paths: list[Path] = []
    for name in KEY_FILE_NAMES:
        candidate = package_root / name
        if candidate.is_file():
            paths.append(candidate)
    for raw in routing_evidence_files.split(";"):
        raw = raw.strip()
        if not raw:
            continue
        candidate = Path(raw)
        if candidate.is_absolute():
            try:
                candidate.relative_to(package_root)
            except ValueError:
                continue
        else:
            candidate = package_root / raw
        if candidate.is_file():
            paths.append(candidate)
    for folder_name in ["scripts", "script", "bin", "hooks", "src"]:
        folder = package_root / folder_name
        if not folder.is_dir():
            continue
        for path in iter_files_limited(folder, max_files=32):
            if len(paths) >= 24:
                break
            if is_text_file(path) and safe_size(path) and safe_size(path) <= 256_000:
                paths.append(path)
        if len(paths) >= 24:
            break
    return dedupe_paths(paths)


def file_inventory(package_root: Path) -> dict[str, Any]:
    files = []
    total = 0
    truncated = False
    for path in iter_files_limited(package_root, max_files=90):
        total += 1
        if len(files) < 60 and not is_appledouble_file(path):
            files.append({"path": path.relative_to(package_root).as_posix(), "size": safe_size(path)})
    # We intentionally avoid counting every file in large package trees; this
    # inventory is only orienting evidence for reviewers.
    try:
        truncated = len(files) >= 60
    except Exception:
        truncated = True
    return {"scanned_files": total, "truncated": truncated, "files_prefix": files}


def platform_keyword_hits(package_root: Path, selected_paths: list[Path]) -> list[dict[str, Any]]:
    hits = []
    for path in selected_paths:
        if len(hits) >= 15 or not is_text_file(path):
            break
        text = safe_read(path, 64_000)
        for line_no, line in enumerate(text.splitlines(), 1):
            if PLATFORM_KEYWORD_RE.search(line):
                hits.append(
                    {
                        "path": path.relative_to(package_root).as_posix(),
                        "line": line_no,
                        "text": line.strip()[:500],
                    }
                )
                if len(hits) >= 15:
                    break
    return hits


def sample_evidence(sample_root: Path) -> dict[str, Any]:
    if not sample_root.is_dir():
        return {"available": False, "sample_root": str(sample_root)}
    snippets = []
    for pattern in ["*/benchmark_manifest.json", "*/samples_description.md", "benchmark_manifest.json", "samples_description.md"]:
        for path in sorted(sample_root.glob(pattern)):
            if path.is_file() and is_text_file(path):
                snippets.append({"path": rel(path), "text_prefix": safe_read(path, 1_200)})
            if len(snippets) >= 4:
                break
        if len(snippets) >= 4:
            break
    return {
        "available": True,
        "sample_root": rel(sample_root),
        "selected_file_snippets": snippets,
    }


def execution_evidence(exec_path: Path) -> dict[str, Any]:
    if not str(exec_path):
        return {"available": False}
    if exec_path.is_file():
        return {"available": True, "path": rel(exec_path), "text_prefix": safe_read(exec_path, 2_500)}
    if not exec_path.is_dir():
        return {"available": False, "path": str(exec_path)}
    snippets = []
    for name in ["platform_summary.json", "summary.json", "run.log", "stderr.log", "stdout.log"]:
        path = exec_path / name
        if path.is_file() and is_text_file(path):
            snippets.append({"path": rel(path), "text_prefix": safe_read(path, 2_500)})
    return {"available": bool(snippets), "path": rel(exec_path), "selected_file_snippets": snippets}


def render_prompt(batch_index: int, evidence_rows: list[dict[str, Any]], run_id: str) -> str:
    evidence_json = json.dumps(evidence_rows, ensure_ascii=False, indent=2)
    return f"""You are a read-only reviewer agent for a research dataset about skill OS compatibility.

Review target: Windows.
Review run id: {run_id}
Batch index: {batch_index}

Hard safety rules:
- Do not execute any skill script, package code, installer, test, browser, GUI command, network command, or package manager command.
- Do not modify files.
- Use only the compact evidence JSON below.
- Do not use network access.
- Do not print or infer secret environment variable values.

Goal:
For each skill, decide whether the observed Windows dynamic failure is a skill-side OS compatibility issue, a non-OS issue, or uncertain. Prefer high recall for clear OS-specific problems, but do not label API keys, OAuth, paid/private services, generic missing dependencies, generic task bugs, or benchmark fixture gaps as OS issues.

Scope status:
- in_scope: the evidence can support a Windows OS compatibility judgment.
- excluded_external_credential_or_service: the best explanation is API key, OAuth, account, paid provider, remote service state, CAPTCHA, or private data.
- excluded_benchmark_capability_gap: the best explanation is missing benchmark capability such as GUI/browser/device/local daemon, and the evidence does not show a skill-side Windows defect.
- insufficient_evidence: source/sample/dynamic evidence is too thin for a useful reviewed label.

Primary labels:
- confirmed_os_issue: core skill/task path has a Windows-incompatible assumption or lacks a Windows-compatible contract.
- not_os_issue: failure is better explained by credentials, service availability, generic dependency, generic logic/task bug, or benchmark fixture.
- uncertain_insufficient_evidence: plausible but not enough evidence.

Allowed os_issue_class values:
- platform_native_api_or_app: macOS/Linux-only app, OS API, daemon, GUI automation, launch service, registry/COM mismatch, or platform-native integration without Windows support.
- shell_path_command_assumption: POSIX shell commands, Unix paths, chmod/chown/sudo, /tmp-/usr-/etc-style paths, shebang/wrapper assumptions, or path separator assumptions that break on native Windows.
- package_entrypoint_contract: documented script/wrapper/binary entrypoint is absent, undiscoverable, or packaged in a way Windows agents cannot invoke.
- permission_session_model: Unix permission/session assumptions or OS-specific auth/session model conflict with native Windows.
- browser_gui_runtime: browser/GUI automation depends on platform-specific runtime details; use excluded_benchmark_capability_gap if only the benchmark lacks GUI and skill has no Windows-specific defect.
- target_os_mismatch: skill explicitly targets macOS/Linux only, or branches for macOS/Linux but omits Windows.
- install_dependency_contract: install/dependency instructions only provide non-Windows paths or omit a feasible Windows installation contract for an otherwise portable capability.
- encoding_locale_path: Windows path, encoding, newline, shell quoting, Unicode/codepage, or reserved-name assumption.
- not_os_auth_or_service
- not_os_generic_dependency
- not_os_generic_logic
- insufficient_evidence

Allowed fixability values:
- script_portability_fix
- package_entrypoint_fix
- install_contract_fix
- platform_adapter_or_not_portable_by_design
- metadata_or_target_os_contract_fix
- runtime_contract_fix
- session_or_permission_adapter_fix
- need_credentials_or_service
- not_os_issue
- need_more_evidence

Output rules:
- Return raw JSON only. The first character must be {{ and the last character must be }}.
- No markdown fences, no explanatory prose outside JSON.
- Use this schema:
{{
  "schema_version": "claude_windows_review_batch_v1",
  "reviewer": "claude-code",
  "target_os": "windows",
  "source_run_id": "{run_id}",
  "batch_index": {batch_index},
  "rows": [
    {{
      "skill_id": "...",
      "scope_status": "in_scope | excluded_external_credential_or_service | excluded_benchmark_capability_gap | insufficient_evidence",
      "primary_label": "confirmed_os_issue | not_os_issue | uncertain_insufficient_evidence",
      "os_issue_class": "...",
      "confidence": "low | medium | high",
      "should_add_to_reviewed_labels": true,
      "evidence_level": "source_only | sample_only | dynamic_summary | source_and_dynamic | source_sample_dynamic",
      "fixability": "...",
      "task_result": "...",
      "evidence_refs": ["..."],
      "notes": "...",
      "uncertainties": ["..."]
    }}
  ],
  "review_quality_notes": "..."
}}

Important:
- should_add_to_reviewed_labels means useful for the reviewed ground-truth layer, including controls and exclusions. It does not mean the row is a positive OS bug.
- If a skill mentions Windows only as a supported environment, that is not by itself an OS issue.
- If source code has an explicit win32/Windows/PowerShell/CMD branch for the same critical path, do not mark it as an OS issue merely because macOS/Linux branches also exist.
- Be careful with domain words such as vehicle "windows"; only the Microsoft Windows operating system is relevant here.
- If a normal dependency could be installed on Windows and the evidence only says it is missing from the test image, prefer not_os_generic_dependency or excluded_benchmark_capability_gap.
- If the primary blocker is credential/OAuth/API/account/service access, prefer excluded_external_credential_or_service even when examples are written in bash. Only mark an additional OS issue if a Windows agent would still lack a usable entrypoint after credentials are supplied.

Compact evidence JSON:
{evidence_json}

End of compact evidence.

Final output reminder:
- Return exactly one JSON object and nothing else.
- Do not return markdown, a table, a narrative summary, or bullet points.
- Include exactly one row per skill_id in this batch.
- If a skill is not an OS issue, still include it with primary_label="not_os_issue".
"""


def is_text_file(path: Path) -> bool:
    return not is_appledouble_file(path) and (path.suffix.lower() in TEXT_SUFFIXES or path.name in KEY_FILE_NAMES)


def is_appledouble_file(path: Path) -> bool:
    return path.name.startswith("._") or "/._" in path.as_posix()


def iter_files_limited(root: Path, max_files: int) -> list[Path]:
    result: list[Path] = []
    stack = [root]
    while stack and len(result) < max_files:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda path: (path.is_file(), path.name.lower()))
        except OSError:
            continue
        for entry in entries:
            if len(result) >= max_files:
                break
            if entry.is_dir():
                if entry.name in SKIP_DIR_NAMES or entry.name.startswith(".") and entry.name not in {".github"}:
                    continue
                stack.append(entry)
            elif entry.is_file():
                result.append(entry)
    return result


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


def dedupe_paths(paths: list[Path]) -> list[Path]:
    result = []
    seen = set()
    for path in paths:
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    return rows


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


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
