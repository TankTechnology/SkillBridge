#!/usr/bin/env python3
"""Mine distinctive Linux OS-compatibility candidates from local skill packages.

This script is intentionally read-only. It does not execute skills; it joins
existing Linux dynamic observations with static package evidence, then emits a
review queue focused on concrete OS mechanisms rather than generic package or
credential failures.
"""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Iterable


REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / ".cache" / "skillcomptest" / "SkillsRepo"
ANALYSIS_DIR = REPO_ROOT / "docs" / "cross-os-testing" / "analysis"
ARCHIVE_DIR = ANALYSIS_DIR / "archive" / "20260625-legacy-exploratory"

EXPLICIT_LABELS = ARCHIVE_DIR / "three-os-explicit-os-issue-labels-20260625.jsonl"
REVIEWED_LABELS = REPO_ROOT / "docs" / "linux-testing" / "datasets" / "os-compat-reviewed-labels.jsonl"

OUT_CSV = ARCHIVE_DIR / "linux-distinctive-os-candidate-mining-20260626.csv"
OUT_JSON = ARCHIVE_DIR / "linux-distinctive-os-candidate-mining-20260626.json"
OUT_MD = ARCHIVE_DIR / "linux-distinctive-os-candidate-mining-20260626.md"

TEXT_SUFFIXES = {
    ".md",
    ".json",
    ".toml",
    ".txt",
    ".sh",
    ".bash",
    ".bat",
    ".cmd",
    ".py",
    ".ps1",
    ".psd1",
    ".psm1",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".yaml",
    ".yml",
}
SKIP_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    "build",
    "cache",
    "dist",
    "fixtures",
    "node_modules",
    "results",
    "storage",
    "test",
    "tests",
    "vendor",
    ".next",
    ".cache",
}
SKIP_FILE_NAMES = {"package-lock.json", "pnpm-lock.yaml", "yarn.lock"}
MAX_FILES_PER_SKILL = 120
MAX_BYTES_PER_FILE = 80_000


@dataclass(frozen=True)
class Rule:
    rule_id: str
    mechanism: str
    pattern: re.Pattern[str]
    weight: int


@dataclass(frozen=True)
class Hit:
    rule_id: str
    mechanism: str
    file: str
    line: int
    text: str
    weight: int


RULES = (
    Rule("win.win32_dll", "windows_only_api_or_workflow", re.compile(r"\b(?:user32|kernel32|gdi32|advapi32)\.dll\b", re.I), 8),
    Rule("win.python_win32", "windows_only_api_or_workflow", re.compile(r"\b(?:win32com|pywinauto|pywin32|winreg|msvcrt)\b", re.I), 8),
    Rule("win.com_uia", "windows_only_api_or_workflow", re.compile(r"\b(?:COM automation|Windows UI Automation|UIA backend|WScript|ProgID|GetObject\()\b", re.I), 7),
    Rule("win.container_stack", "windows_only_api_or_workflow", re.compile(r"\b(?:mcr\.microsoft\.com/windows|windowsservercore|Windows Server Core|\.NET Framework|MSIX|WinGet)\b", re.I), 7),
    Rule("win.shell_tooling", "windows_only_api_or_workflow", re.compile(r"\b(?:powershell|cmd\.exe|winget|choco|reg\.exe|clip\.exe)\b", re.I), 4),
    Rule("win.path_literal", "hardcoded_non_linux_path", re.compile(r"(?:[A-Za-z]:\\\\|%APPDATA%|%LOCALAPPDATA%|\\\\Users\\\\|\\AppData\\)", re.I), 4),
    Rule("mac.apple_script", "macos_only_api_or_workflow", re.compile(r"\b(?:osascript|AppleScript|open -a|tell application)\b", re.I), 8),
    Rule("mac.apple_apps", "macos_only_api_or_workflow", re.compile(r"\b(?:iMessage|Messages\.app|Notes\.app|Reminders\.app|Things(?: 3|\.app)|Find My|Apple Mail|Music\.app|DrawThings|System Events)\b", re.I), 7),
    Rule("mac.native_framework", "macos_only_api_or_workflow", re.compile(r"\b(?:pyobjc|AppKit|Quartz|CoreGraphics|CoreServices|CoreML|MLX|AVFoundation|Vision framework)\b", re.I), 7),
    Rule("mac.permissions", "macos_only_api_or_workflow", re.compile(r"\b(?:TCC|Accessibility permission|Screen Recording|Full Disk Access)\b", re.I), 6),
    Rule("mac.launchd", "os_adapter_entry_surface", re.compile(r"\b(?:launchd|launchctl|LaunchAgents|\.plist)\b", re.I), 6),
    Rule("mac.shell_tools", "macos_only_api_or_workflow", re.compile(r"\b(?:pbcopy|pbpaste|/usr/bin/say|say -v)\b", re.I), 5),
    Rule("mac.homebrew_path", "install_or_path_contract", re.compile(r"(?:/opt/homebrew|/usr/local/Cellar|brew install)", re.I), 3),
    Rule("mac.users_path", "hardcoded_non_linux_path", re.compile(r"(?:/Users/[A-Za-z0-9_.-]+|~/Library|/Library/Messages|Library/Mail)"), 5),
    Rule("shell.bsd_sed_i", "bsd_gnu_shell_mismatch", re.compile(r"\bsed\s+-i\s+(['\"]{2}|''|\"\")", re.I), 7),
    Rule("shell.bsd_date", "bsd_gnu_shell_mismatch", re.compile(r"\bdate\s+(?:-j|-v|-r\s+\d)", re.I), 6),
    Rule("shell.bsd_stat", "bsd_gnu_shell_mismatch", re.compile(r"\bstat\s+-f\b", re.I), 6),
)

DIRECT_SHELL_RE = re.compile(r"^(?:\./|skills/)?(?P<path>[A-Za-z0-9_.@+/-]+\.sh)\b")
LINUX_WORD_RE = re.compile(r"\b(?:linux|ubuntu|debian|apt(?:-get)?|dnf|yum|pacman|systemd|bash)\b", re.I)
AUTH_OR_SERVICE_HINT_RE = re.compile(r"\b(?:api[_ -]?key|oauth|token|secret|credential|login|browser-based authorization)\b", re.I)
FALSE_POSITIVE_HINT_RE = re.compile(r"\b(?:natural language|not an OS issue|pure credential|OAuth|API key|service-level|platform-agnostic)\b", re.I)


def main() -> None:
    dynamic_by_skill = load_linux_explicit_labels()
    reviewed_by_skill = load_reviewed_labels()
    rows = []

    for skill_root in iter_skill_roots(SKILLS_DIR):
        skill_id = f"{skill_root.parent.name}/{skill_root.name}"
        hits = scan_skill(skill_root)
        hits.extend(find_non_executable_direct_shell_hits(skill_root))
        if not hits:
            continue

        dynamic = dynamic_by_skill.get(skill_id, {})
        reviewed = reviewed_by_skill.get(skill_id, {})
        row = build_candidate_row(skill_id, skill_root, hits, dynamic, reviewed)
        if row["candidate_score"] < 6:
            continue
        rows.append(row)

    rows.sort(key=sort_key)
    summary = build_summary(rows, dynamic_by_skill)
    write_csv(OUT_CSV, rows)
    write_json(OUT_JSON, summary)
    write_text(OUT_MD, render_markdown(summary, rows))

    for path in (OUT_CSV, OUT_JSON, OUT_MD):
        print(f"wrote {path}")


def load_linux_explicit_labels() -> dict[str, dict[str, Any]]:
    rows = {}
    for row in read_jsonl(EXPLICIT_LABELS):
        if row.get("target_os") == "linux":
            rows[row["skill_id"]] = row
    return rows


def load_reviewed_labels() -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for row in read_jsonl(REVIEWED_LABELS):
        if row.get("target_os") == "linux":
            rows[row["skill_id"]] = row
    return rows


def iter_skill_roots(skills_dir: Path) -> Iterable[Path]:
    if not skills_dir.is_dir():
        return []
    roots = set()
    for name in ("SKILL.md", "skill.md"):
        roots.update(path.parent for path in skills_dir.glob(f"*/*/{name}"))
    return sorted(roots)


def find_skill_file(root: Path) -> Path | None:
    for name in ("SKILL.md", "skill.md"):
        path = root / name
        if path.is_file():
            return path
    return None


def scan_skill(root: Path) -> list[Hit]:
    hits = []
    for path in package_files(root):
        text = read_text(path)
        if not text:
            continue
        rel_path = rel(path, root)
        for rule in RULES:
            for match in rule.pattern.finditer(text):
                matched = match.group(0).replace("\n", " ")[:180]
                if is_likely_false_positive(rule, text, match.start()):
                    continue
                hits.append(Hit(rule.rule_id, rule.mechanism, rel_path, line_for_offset(text, match.start()), matched, rule.weight))
                break
    return hits


def package_files(root: Path) -> list[Path]:
    files = []
    for path in root.rglob("*"):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts[:-1]):
            continue
        if not path.is_file():
            continue
        if len(path.parts) - len(root.parts) > 5:
            continue
        if path.name in SKIP_FILE_NAMES:
            continue
        if path.name.startswith("._"):
            continue
        if path.suffix.lower() in TEXT_SUFFIXES or path.name in {"Dockerfile", "Makefile"}:
            files.append(path)
        if len(files) >= MAX_FILES_PER_SKILL:
            break
    return sorted(files)


def is_likely_false_positive(rule: Rule, text: str, offset: int) -> bool:
    window = text[max(0, offset - 180) : offset + 260]
    if rule.rule_id == "mac.shell_tools":
        return " say " in window.lower() and not re.search(r"(/usr/bin/say|say\s+-[a-z]|`say`|\bsay\s+[\"'])", window, re.I)
    if rule.rule_id == "win.shell_tooling":
        return "powershell" in window.lower() and "optional" in window.lower() and LINUX_WORD_RE.search(window)
    return False


def find_non_executable_direct_shell_hits(root: Path) -> list[Hit]:
    skill = find_skill_file(root)
    if not skill:
        return []
    text = read_text(skill)
    hits = []
    seen = set()
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith(("bash ", "sh ", "source ", "python ", "python3 ", "node ", "#")):
            continue
        match = DIRECT_SHELL_RE.search(stripped)
        if not match:
            continue
        ref = match.group("path").lstrip("./")
        if ref in seen or " " in ref:
            continue
        seen.add(ref)
        candidates = [root / ref, root / "scripts" / Path(ref).name]
        target = next((path for path in candidates if path.is_file()), None)
        if target and target.suffix == ".sh" and not is_executable(target):
            hits.append(Hit("entry.non_executable_shell_script", "os_adapter_entry_surface", rel(skill, root), line_no, ref, 6))
    return hits


def build_candidate_row(
    skill_id: str,
    skill_root: Path,
    hits: list[Hit],
    dynamic: dict[str, Any],
    reviewed: dict[str, Any],
) -> dict[str, Any]:
    mechanism_counts = Counter(hit.mechanism for hit in hits)
    rule_counts = Counter(hit.rule_id for hit in hits)
    raw_score = sum(max(hit.weight for hit in hits if hit.mechanism == mech) for mech in mechanism_counts)
    raw_score += min(8, len(mechanism_counts) * 2)
    raw_score += platform_name_bonus(skill_id, mechanism_counts)

    label_status = dynamic.get("label_status", "not_in_current_linux_dynamic")
    dynamic_outcome = dynamic.get("dynamic_outcome", "not_seen")
    if dynamic_outcome in {"mixed", "all_blocked", "all_fail", "all_unsupported", "unresolved"}:
        raw_score += 6
    if label_status == "confirmed_os_issue":
        raw_score += 10
    if reviewed.get("primary_label") == "not_os_issue":
        raw_score -= reviewed_non_os_penalty(reviewed)

    candidate_status = candidate_status_for(label_status, reviewed, raw_score)
    confidence = confidence_for(candidate_status, raw_score, dynamic_outcome, mechanism_counts)
    return {
        "skill_id": skill_id,
        "candidate_status": candidate_status,
        "confidence": confidence,
        "candidate_score": max(raw_score, 0),
        "dynamic_outcome": dynamic_outcome,
        "success_rate": dynamic.get("success_rate", ""),
        "label_status": label_status,
        "reviewed_primary_label": reviewed.get("primary_label", ""),
        "reviewed_scope_status": reviewed.get("scope_status", ""),
        "mechanisms": ";".join(sorted(mechanism_counts)),
        "top_rules": ";".join(rule for rule, _ in rule_counts.most_common(8)),
        "evidence_excerpt": summarize_hits(hits),
        "package_root": rel(skill_root, REPO_ROOT),
        "review_recommendation": recommendation_for(candidate_status, mechanism_counts, dynamic_outcome),
    }


def platform_name_bonus(skill_id: str, mechanism_counts: Counter[str]) -> int:
    lower = skill_id.lower()
    bonus = 0
    if any(token in lower for token in ("windows", "winapp", "win-", "aspnet-framework")) and "windows_only_api_or_workflow" in mechanism_counts:
        bonus += 6
    if any(token in lower for token in ("mac", "apple", "imessage", "things", "findmy", "peekaboo")) and "macos_only_api_or_workflow" in mechanism_counts:
        bonus += 6
    return bonus


def reviewed_non_os_penalty(reviewed: dict[str, Any]) -> int:
    text = " ".join(str(reviewed.get(key, "")) for key in ("notes", "task_result", "fixability", "scope_status"))
    if FALSE_POSITIVE_HINT_RE.search(text):
        return 14
    return 8


def candidate_status_for(label_status: str, reviewed: dict[str, Any], score: int) -> str:
    if label_status == "confirmed_os_issue":
        return "already_confirmed_os_issue"
    if reviewed.get("primary_label") == "not_os_issue":
        return "reviewed_non_os_but_static_os_signal"
    if label_status in {"unreviewed_dynamic_nonpass", "static_dynamic_os_candidate_unreviewed", "subagent_reviewed_uncertain_os_issue"}:
        return "needs_root_cause_review_dynamic_nonpass"
    if label_status == "no_observed_dynamic_issue_unreviewed":
        return "latent_os_issue_candidate_dynamic_pass"
    if label_status == "not_in_current_linux_dynamic":
        return "static_only_candidate_not_yet_dynamic"
    if score >= 18:
        return "needs_root_cause_review"
    return "low_priority_static_signal"


def confidence_for(
    candidate_status: str,
    score: int,
    dynamic_outcome: str,
    mechanism_counts: Counter[str],
) -> str:
    if candidate_status == "already_confirmed_os_issue":
        return "confirmed"
    if candidate_status == "reviewed_non_os_but_static_os_signal":
        return "audit_only"
    if score >= 22 and dynamic_outcome != "all_pass":
        return "high"
    if score >= 18 and ("windows_only_api_or_workflow" in mechanism_counts or "macos_only_api_or_workflow" in mechanism_counts):
        return "high"
    if score >= 14:
        return "medium"
    return "low"


def recommendation_for(candidate_status: str, mechanism_counts: Counter[str], dynamic_outcome: str) -> str:
    if candidate_status == "already_confirmed_os_issue":
        return "Keep as confirmed evidence."
    if candidate_status == "reviewed_non_os_but_static_os_signal":
        return "Do not count without re-review; useful for false-positive analysis."
    if dynamic_outcome != "all_pass":
        return "Review first: dynamic non-pass plus concrete OS evidence."
    if "macos_only_api_or_workflow" in mechanism_counts or "windows_only_api_or_workflow" in mechanism_counts:
        return "Review as latent issue: dynamic tasks may have missed primary platform-bound workflow."
    return "Review after higher-priority platform-bound cases."


def build_summary(rows: list[dict[str, Any]], dynamic_by_skill: dict[str, dict[str, Any]]) -> dict[str, Any]:
    non_audit_rows = [row for row in rows if row["candidate_status"] != "reviewed_non_os_but_static_os_signal"]
    new_rows = [row for row in non_audit_rows if row["candidate_status"] != "already_confirmed_os_issue"]
    high_or_medium_new = [row for row in new_rows if row["confidence"] in {"high", "medium"}]
    return {
        "schema_version": "linux_distinctive_os_candidate_mining_v1",
        "generated_at": now_utc(),
        "unit": "skill_id",
        "source_files": {
            "packages": rel(SKILLS_DIR, REPO_ROOT),
            "explicit_linux_labels": rel(EXPLICIT_LABELS, REPO_ROOT),
            "reviewed_linux_labels": rel(REVIEWED_LABELS, REPO_ROOT),
        },
        "skills_in_local_package_repo": len(list(iter_skill_roots(SKILLS_DIR))),
        "linux_dynamic_rows": len(dynamic_by_skill),
        "candidate_rows_total": len(rows),
        "candidate_rows_excluding_audit_only": len(non_audit_rows),
        "new_candidate_rows_excluding_confirmed_and_audit_only": len(new_rows),
        "new_high_or_medium_candidate_rows": len(high_or_medium_new),
        "status_counts": counter_dict(row["candidate_status"] for row in rows),
        "confidence_counts": counter_dict(row["confidence"] for row in rows),
        "dynamic_outcome_counts": counter_dict(str(row["dynamic_outcome"]) for row in rows),
        "mechanism_counts": mechanism_counts(rows),
        "claim_audit": {
            "status": "caution",
            "safe_wording": "The mining pass found additional high/medium-confidence Linux OS-compatibility candidates with concrete OS mechanisms; they should be treated as review targets, not confirmed positives.",
            "forbidden_wording": "All mined candidates are ground-truth Linux OS issues.",
        },
        "top_new_candidates": [
            {
                "skill_id": row["skill_id"],
                "confidence": row["confidence"],
                "candidate_status": row["candidate_status"],
                "dynamic_outcome": row["dynamic_outcome"],
                "mechanisms": row["mechanisms"],
                "top_rules": row["top_rules"],
            }
            for row in high_or_medium_new[:40]
        ],
    }


def render_markdown(summary: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    lines = [
        "# Linux Distinctive OS Candidate Mining",
        "",
        "Date: 2026-06-26",
        "",
        "This is a review queue, not ground truth. It is designed to find more Linux-facing skill compatibility issues with concrete OS mechanisms, while keeping generic auth/service/package failures out of confirmed counts.",
        "",
        "## Summary",
        "",
        f"- Local packages scanned: {summary['skills_in_local_package_repo']}",
        f"- Linux dynamic rows available: {summary['linux_dynamic_rows']}",
        f"- Candidate rows total: {summary['candidate_rows_total']}",
        f"- Candidate rows excluding audit-only reviewed non-OS: {summary['candidate_rows_excluding_audit_only']}",
        f"- New candidates excluding already confirmed and audit-only: {summary['new_candidate_rows_excluding_confirmed_and_audit_only']}",
        f"- New high/medium candidates: {summary['new_high_or_medium_candidate_rows']}",
        "",
        "## Candidate Status Counts",
        "",
    ]
    for key, value in summary["status_counts"].items():
        lines.append(f"- `{key}`: {value}")

    lines.extend(["", "## Mechanism Counts", ""])
    for key, value in summary["mechanism_counts"].items():
        lines.append(f"- `{key}`: {value}")

    lines.extend(
        [
            "",
            "## Top New High/Medium Candidates",
            "",
            "| Skill | Confidence | Status | Dynamic | Mechanisms | Evidence |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    shown = 0
    for row in rows:
        if row["candidate_status"] in {"already_confirmed_os_issue", "reviewed_non_os_but_static_os_signal"}:
            continue
        if row["confidence"] not in {"high", "medium"}:
            continue
        lines.append(
            "| `{}` | {} | {} | {} | {} | {} |".format(
                row["skill_id"],
                row["confidence"],
                row["candidate_status"],
                row["dynamic_outcome"],
                row["mechanisms"],
                row["evidence_excerpt"].replace("|", "\\|"),
            )
        )
        shown += 1
        if shown >= 80:
            break

    lines.extend(
        [
            "",
            "## Safe Interpretation",
            "",
            "These rows are best used as the next agent-review queue. A candidate should be promoted to confirmed only after root-cause review verifies that the primary skill workflow fails or is misleading on Linux because of the OS mechanism shown here.",
            "",
            "Strong candidates include Windows-only APIs/workflows, macOS-only app automation, BSD/GNU shell mismatches, hardcoded non-Linux paths, and install contracts that document only a non-Linux installation path for required tooling.",
            "",
        ]
    )
    return "\n".join(lines)


def summarize_hits(hits: list[Hit], limit: int = 8) -> str:
    ordered = sorted(hits, key=lambda hit: (-hit.weight, hit.mechanism, hit.rule_id, hit.file, hit.line))
    return " | ".join(f"{hit.rule_id}:{hit.file}:{hit.line}:{hit.text}" for hit in ordered[:limit])


def mechanism_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in rows:
        for mechanism in row["mechanisms"].split(";"):
            if mechanism:
                counts[mechanism] += 1
    return dict(counts.most_common())


def sort_key(row: dict[str, Any]) -> tuple[int, int, str]:
    status_rank = {
        "needs_root_cause_review_dynamic_nonpass": 0,
        "latent_os_issue_candidate_dynamic_pass": 1,
        "static_only_candidate_not_yet_dynamic": 2,
        "already_confirmed_os_issue": 3,
        "needs_root_cause_review": 4,
        "low_priority_static_signal": 5,
        "reviewed_non_os_but_static_os_signal": 6,
    }.get(row["candidate_status"], 9)
    confidence_rank = {"confirmed": 0, "high": 1, "medium": 2, "low": 3, "audit_only": 4}.get(row["confidence"], 9)
    return (status_rank, confidence_rank, -int(row["candidate_score"]), row["skill_id"])


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "skill_id",
        "candidate_status",
        "confidence",
        "candidate_score",
        "dynamic_outcome",
        "success_rate",
        "label_status",
        "reviewed_primary_label",
        "reviewed_scope_status",
        "mechanisms",
        "top_rules",
        "evidence_excerpt",
        "package_root",
        "review_recommendation",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def counter_dict(values: Iterable[str]) -> dict[str, int]:
    return dict(Counter(values).most_common())


def read_text(path: Path) -> str:
    try:
        return path.read_bytes()[:MAX_BYTES_PER_FILE].decode("utf-8", errors="ignore")
    except OSError:
        return ""


def line_for_offset(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def is_executable(path: Path) -> bool:
    try:
        return bool(path.stat().st_mode & 0o111)
    except OSError:
        return False


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


if __name__ == "__main__":
    main()
