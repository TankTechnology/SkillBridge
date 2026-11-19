#!/usr/bin/env python3
"""Recompute Windows static-signal metrics from package source.

This script intentionally does not read review prompt evidence such as
`platform_keyword_hits`. It rebuilds static signals from the skill package
files and joins them with reviewed Windows labels.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[2]
ICSE = ROOT / "data" / "generated"
SKILLCOMPTEST = ROOT / "skillcomptest"

LABEL_SOURCES = [
    SKILLCOMPTEST
    / "docs/cross-os-testing/reviews/windows-cc-review-test100-20260626/extracted-reviewed-labels.jsonl",
    SKILLCOMPTEST
    / "docs/cross-os-testing/reviews/windows-common-remaining407-cc-review-20260626/extracted-reviewed-labels.jsonl",
]

PACKAGE_ROOTS = [
    SKILLCOMPTEST / ".cache/skillcomptest/top5000-sources",
    SKILLCOMPTEST / ".cache/skillcomptest/SkillsRepo",
    SKILLCOMPTEST / ".cache/skillcomptest/samples",
]

OUT_JSON = ICSE / "notes/windows-static-source-eval-20260629.json"
OUT_MD = ICSE / "notes/windows-static-source-eval-20260629.md"
OUT_JOINED = ICSE / "notes/windows-static-source-eval-20260629.joined.jsonl"

TEXT_SUFFIXES = {
    ".md",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".sh",
    ".bash",
    ".py",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".ps1",
    ".bat",
    ".cmd",
}
SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", "dist", "build", ".cache"}
MAX_FILES = 100
MAX_BYTES = 80_000

WINDOWS_NATIVE_HINT = re.compile(
    r"\b(windows|win32|powershell|pwsh|\.ps1\b|cmd\.exe|\.bat\b|\.cmd\b|"
    r"winget|choco|chocolatey|scoop|py\s+-3|python\.exe)\b",
    re.I,
)


@dataclass
class Signal:
    rule: str
    severity: str
    file: str
    line: int
    text: str

    def as_dict(self) -> dict[str, object]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "file": self.file,
            "line": self.line,
            "text": self.text[:220],
        }


def read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.strip():
            rows.append(json.loads(raw))
    return rows


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def read_text(path: Path) -> str:
    try:
        return path.read_bytes()[:MAX_BYTES].decode("utf-8", errors="ignore")
    except OSError:
        return ""


def rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def line_no(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def dedupe_labels(rows: Iterable[dict]) -> list[dict]:
    by_skill: dict[str, dict] = {}
    for row in rows:
        if row.get("target_os") != "windows":
            continue
        if row.get("scope_status") != "in_scope":
            continue
        if row.get("primary_label") not in {"confirmed_os_issue", "not_os_issue"}:
            continue
        skill_id = row.get("skill_id")
        if not skill_id:
            continue
        if skill_id not in by_skill:
            by_skill[skill_id] = dict(row)
            by_skill[skill_id]["source_run_ids"] = [row.get("source_run_id", "")]
            continue
        existing = by_skill[skill_id]
        if existing.get("primary_label") != row.get("primary_label"):
            # Prefer confirmed labels if duplicate review artifacts disagree.
            if row.get("primary_label") == "confirmed_os_issue":
                existing.update(row)
        run_id = row.get("source_run_id", "")
        if run_id and run_id not in existing["source_run_ids"]:
            existing["source_run_ids"].append(run_id)
    return sorted(by_skill.values(), key=lambda item: item["skill_id"])


def build_package_index() -> dict[str, Path]:
    index: dict[str, Path] = {}
    for base in PACKAGE_ROOTS:
        if not base.exists():
            continue
        for skill_file in base.glob("*/*/SKILL.md"):
            skill_id = f"{skill_file.parent.parent.name}/{skill_file.parent.name}"
            index.setdefault(skill_id, skill_file.parent)
    return index


def iter_package_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(root).parts
        if any(part in SKIP_DIRS for part in rel_parts[:-1]):
            continue
        if path.suffix.lower() in TEXT_SUFFIXES or path.name in {"SKILL.md", "README.md", "Dockerfile", "Makefile"}:
            files.append(path)
        if len(files) >= MAX_FILES:
            break
    return sorted(files)


def has_windows_native_support(files: list[tuple[Path, str]]) -> bool:
    if any(path.suffix.lower() in {".ps1", ".bat", ".cmd"} for path, _ in files):
        return True
    return any(WINDOWS_NATIVE_HINT.search(text) for _, text in files)


def add_pattern_signals(
    signals: list[Signal],
    files: list[tuple[Path, str]],
    root: Path,
    patterns: list[tuple[str, str, re.Pattern]],
) -> None:
    for path, text in files:
        relative = path.relative_to(root).as_posix()
        for rule, severity, pattern in patterns:
            match = pattern.search(text)
            if match:
                signals.append(Signal(rule, severity, relative, line_no(text, match.start()), match.group(0)))


def analyze_package(root: Path) -> dict:
    files = [(path, read_text(path)) for path in iter_package_files(root)]
    joined = "\n".join(text for _, text in files)
    skill_text = read_text(root / "SKILL.md")
    has_win = has_windows_native_support(files)
    signals: list[Signal] = []

    strict_patterns: list[tuple[str, str, re.Pattern]] = [
        (
            "windows.requires_wsl_or_git_bash",
            "strict",
            re.compile(r"\bWindows\s*\((?:via|using)\s*(?:WSL|Git Bash|MSYS2|Cygwin)", re.I),
        ),
        (
            "macos.native_app_or_api",
            "strict",
            re.compile(
                r"\b(Messages\.app|Things 3|Finder|AppleScript|osascript|Shortcuts\.app|Automator|"
                r"System Events|pbcopy|pbpaste|open\s+-a|/Applications/[^\\n]+\.app)\b",
                re.I,
            ),
        ),
        (
            "linux.native_runtime_or_api",
            "strict",
            re.compile(r"\b(xdotool|wmctrl|xclip|xsel|wl-copy|wl-paste|systemctl|journalctl)\b|/proc/[A-Za-z0-9_/.-]+", re.I),
        ),
    ]
    add_pattern_signals(signals, files, root, strict_patterns)

    top_level_docs = [(path, text) for path, text in files if path.parent == root and path.name.lower() in {"skill.md", "readme.md", "readme.zh-cn.md", "_meta.json", "package.json"}]
    for path, text in top_level_docs:
        relative = path.relative_to(root).as_posix()
        for match in re.finditer(r"(?:\"os\"\s*:\s*\[[^\]]+\]|os:\s*\[[^\]]+\])", text, re.I):
            os_decl = match.group(0).lower()
            if ("darwin" in os_decl or "linux" in os_decl) and not re.search(r"win(?:dows|32)?", os_decl):
                signals.append(Signal("metadata.non_windows_os_only", "strict", relative, line_no(text, match.start()), match.group(0)))
        for match in re.finditer(
            r"\b(?:macos|mac os|darwin|linux|ubuntu)[ -]only\b|"
            r"\bonly (?:works|supported|available) (?:on|for) (?:macos|mac os|darwin|linux|ubuntu)\b|"
            r"\brequires (?:macos|mac os|darwin|linux|ubuntu)\b",
            text,
            re.I,
        ):
            signals.append(Signal("guide.explicit_non_windows_only", "strict", relative, line_no(text, match.start()), match.group(0)))
        if path.name.lower() == "skill.md":
            for match in re.finditer(
                r"(?:^|[\s`'\"])(?:bash|sh)\s+(?:\.?/)?(?:scripts|bin|tools)/[A-Za-z0-9_.@+/-]+\.(?:sh|bash)\b|"
                r"\.\/[A-Za-z0-9_.@+/-]+\.(?:sh|bash)\b",
                text,
                re.I,
            ):
                signals.append(Signal("entrypoint.posix_shell_invoked", "strict", relative, line_no(text, match.start()), match.group(0)))

    # Package-level shell contract: only count it as strict when the package or
    # guide exposes shell scripts as entrypoints and no native Windows alternate
    # is visible.
    shell_entry_files = [
        path
        for path, text in files
        if path.suffix.lower() in {".sh", ".bash"} and re.search(r"^#!.*\b(?:bash|sh)\b", text, re.M)
    ]
    guide_references_shell = re.search(r"(?:bash|sh|\./).{0,80}\.(?:sh|bash)\b", skill_text, re.I)
    if shell_entry_files and guide_references_shell:
        signals.append(
            Signal(
                "entrypoint.package_shell_contract",
                "strict",
                shell_entry_files[0].relative_to(root).as_posix(),
                1,
                "shell entrypoint referenced by guide",
            )
        )

    broad_patterns: list[tuple[str, str, re.Pattern]] = [
        ("posix.path_literal", "broad", re.compile(r"(?:^|[\s`'\"])/(?:home|tmp|usr|var|etc|opt|bin)/[A-Za-z0-9_./-]+", re.M)),
        ("posix.utility_contract", "broad", re.compile(r"\b(?:grep|awk|sed|find|du|wc|head|tail|tree|which|chmod|chown|ln -s)\b", re.I)),
        ("posix.shell_shebang", "broad", re.compile(r"^#!.*\b(?:bash|sh|zsh)\b", re.M)),
        ("macos.homebrew_only", "broad", re.compile(r"\bbrew install\b|/opt/homebrew|/usr/local/Cellar", re.I)),
        ("linux.package_manager_only", "broad", re.compile(r"\b(?:apt-get|apt|yum|dnf|pacman)\s+install\b", re.I)),
        ("entrypoint.python3_only", "broad", re.compile(r"(?:^|[\s`'\"])python3\s+(?:\.?/)?(?:scripts|bin|tools)/[A-Za-z0-9_.@+/-]+\.py\b", re.I)),
        ("entrypoint.posix_shell_invoked", "broad", re.compile(r"(?:^|[\s`'\"])(?:bash|sh)\s+(?:\.?/)?(?:scripts|bin|tools)/[A-Za-z0-9_.@+/-]+\.(?:sh|bash)\b|\.\/[A-Za-z0-9_.@+/-]+\.(?:sh|bash)\b", re.I)),
        ("python.unix_module", "broad", re.compile(r"\b(?:import|from)\s+(?:fcntl|termios|pwd|grp|pty)\b")),
    ]
    add_pattern_signals(signals, files, root, broad_patterns)

    if has_win:
        explicit_blockers = {"windows.requires_wsl_or_git_bash", "metadata.non_windows_os_only", "guide.explicit_non_windows_only"}
        signals = [
            signal
            for signal in signals
            if signal.severity != "strict" or signal.rule in explicit_blockers or signal.rule.startswith("entrypoint.")
        ]

    strict_positive = any(signal.severity == "strict" for signal in signals)
    broad_screen_positive = strict_positive or any(signal.severity == "broad" for signal in signals)
    broad_positive = broad_screen_positive

    # Windows alternatives reduce broad false positives for common cross-platform tools.
    if has_win and not strict_positive:
        broad_positive = False

    return {
        "package_root": rel(root),
        "has_windows_native_hint": has_win,
        "strict_positive": strict_positive,
        "broad_screen_positive": broad_screen_positive,
        "broad_positive": broad_positive,
        "signals": [signal.as_dict() for signal in signals[:30]],
        "strict_rules": sorted({signal.rule for signal in signals if signal.severity == "strict"}),
        "broad_rules": sorted({signal.rule for signal in signals}),
    }


def metrics(rows: list[dict], pred_key: str) -> dict:
    tp = sum(1 for row in rows if row["truth_positive"] and row[pred_key])
    fp = sum(1 for row in rows if not row["truth_positive"] and row[pred_key])
    fn = sum(1 for row in rows if row["truth_positive"] and not row[pred_key])
    tn = sum(1 for row in rows if not row["truth_positive"] and not row[pred_key])
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "n": len(rows),
        "truth_positive": tp + fn,
        "predicted_positive": tp + fp,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }


def compact(row: dict) -> dict:
    return {
        "skill_id": row["skill_id"],
        "truth": row["primary_label"],
        "os_issue_class": row.get("os_issue_class", ""),
        "strict_positive": row.get("strict_positive", False),
        "broad_screen_positive": row.get("broad_screen_positive", False),
        "broad_positive": row.get("broad_positive", False),
        "strict_rules": row.get("strict_rules", []),
        "broad_rules": row.get("broad_rules", [])[:8],
        "package_root": row.get("package_root", ""),
    }


def main() -> int:
    labels = dedupe_labels(row for path in LABEL_SOURCES for row in read_jsonl(path))
    package_index = build_package_index()

    joined: list[dict] = []
    missing_packages: list[str] = []
    for label in labels:
        skill_id = label["skill_id"]
        root = package_index.get(skill_id)
        if root is None:
            missing_packages.append(skill_id)
            continue
        static = analyze_package(root)
        joined.append(
            {
                "skill_id": skill_id,
                "target_os": "windows",
                "primary_label": label.get("primary_label"),
                "scope_status": label.get("scope_status"),
                "os_issue_class": label.get("os_issue_class", ""),
                "confidence": label.get("confidence", ""),
                "truth_positive": label.get("primary_label") == "confirmed_os_issue",
                **static,
            }
        )

    by_run = defaultdict(list)
    for row in joined:
        source = "all"
        by_run[source].append(row)
    summary = {
        "schema_version": "windows_static_source_eval_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "purpose": "Recompute Windows static analysis from package source without using review-time keyword evidence.",
        "label_sources": [rel(path) for path in LABEL_SOURCES],
        "package_roots": [rel(path) for path in PACKAGE_ROOTS],
        "raw_review_rows": sum(len(read_jsonl(path)) for path in LABEL_SOURCES),
        "deduped_in_scope_definitive_labels": len(labels),
        "joined_rows": len(joined),
        "missing_package_count": len(missing_packages),
        "missing_package_skill_ids": missing_packages[:50],
        "truth_label_counts": dict(Counter(row["primary_label"] for row in joined)),
        "truth_class_counts": dict(Counter(row.get("os_issue_class", "") for row in joined if row["truth_positive"])),
        "rule_policy": {
            "strict": "Low-noise diagnostic policy. Fires on explicit non-Windows support, native macOS/Linux APIs, invoked POSIX shell entrypoints, and macOS/Linux-only install contracts.",
            "broad_screen": "Recall-first screening policy. Adds POSIX path/utility/shebang/Homebrew/Unix-module hints without suppressing positives when Windows-native alternatives are visible.",
            "broad_filtered": "Paper-facing Windows source-static policy. Starts from broad_screen, then suppresses broad-only positives when Windows-native alternatives are visible.",
        },
        "metrics": {
            "strict": metrics(joined, "strict_positive"),
            "broad_screen": metrics(joined, "broad_screen_positive"),
            "broad_filtered": metrics(joined, "broad_positive"),
        },
        "missed_strict": [compact(row) for row in joined if row["truth_positive"] and not row["strict_positive"]],
        "false_positive_strict": [compact(row) for row in joined if not row["truth_positive"] and row["strict_positive"]][:80],
        "missed_broad_screen": [compact(row) for row in joined if row["truth_positive"] and not row["broad_screen_positive"]],
        "false_positive_broad_screen": [compact(row) for row in joined if not row["truth_positive"] and row["broad_screen_positive"]][:80],
        "missed_broad": [compact(row) for row in joined if row["truth_positive"] and not row["broad_positive"]],
        "false_positive_broad": [compact(row) for row in joined if not row["truth_positive"] and row["broad_positive"]][:80],
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    write_json(OUT_JSON, summary)
    OUT_JOINED.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in joined),
        encoding="utf-8",
    )

    lines = [
        "# Windows Static Source Evaluation",
        "",
        f"Generated: {summary['generated_at']}",
        "",
        "## Scope",
        "",
        "- Unit: reviewed `skill_id x windows` row, deduped by skill.",
        "- Labels: in-scope Windows reviewed rows whose primary label is `confirmed_os_issue` or `not_os_issue`.",
        "- Static source: package files only. The recomputation does not reuse review-time `platform_keyword_hits`.",
        "",
        "## Metrics",
        "",
        "| Policy | N | Truth+ | Pred+ | TP | FP | FN | TN | Precision | Recall | F1 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name in ("strict", "broad_screen", "broad_filtered"):
        item = summary["metrics"][name]
        lines.append(
            f"| {name} | {item['n']} | {item['truth_positive']} | {item['predicted_positive']} | "
            f"{item['tp']} | {item['fp']} | {item['fn']} | {item['tn']} | "
            f"{item['precision']:.3f} | {item['recall']:.3f} | {item['f1']:.3f} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "- The old 1.000 recall came from a very broad reviewed-nonpass proxy that was too close to review-time evidence.",
            "- The strict policy is a low-noise diagnostic view, but it misses many true Windows OS issues.",
            "- The broad_screen policy is useful for recall-oriented exploration, but its lower precision should not be presented as final diagnosis.",
            "- The broad_filtered policy suppresses broad-only positives when Windows-native alternatives are visible; use this as the paper-facing Windows source-static row.",
            "",
            "## Strict False Negatives",
            "",
        ]
    )
    for row in summary["missed_strict"][:30]:
        lines.append(f"- {row['skill_id']} ({row['os_issue_class']})")
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(json.dumps({"metrics": summary["metrics"], "joined_rows": len(joined), "missing": len(missing_packages)}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
