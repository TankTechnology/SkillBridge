#!/usr/bin/env python3
"""Lightweight static OS-compatibility analysis for skill packages.

This scanner is intentionally simple and read-only. It does not run skill code.
It emits one JSONL prediction per skill package so dynamic results can evaluate
the static triage layer.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import re
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SKILLS_DIR = REPO_ROOT / ".cache" / "skillcomptest" / "SkillsRepo"
DEFAULT_OUT_BY_TARGET = {
    "linux": REPO_ROOT / "docs" / "linux-testing" / "datasets" / "static-skill-predictions.jsonl",
    "macos": REPO_ROOT / "docs" / "macos-testing" / "datasets" / "static-skill-predictions.jsonl",
}

TEXT_FILE_SUFFIXES = {
    ".md",
    ".json",
    ".toml",
    ".txt",
    ".sh",
    ".bash",
    ".py",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".yaml",
    ".yml",
}

SCRIPT_SUFFIXES = {".sh", ".bash", ".py", ".js", ".mjs", ".cjs", ".ts"}
SKIP_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "vendor",
    ".next",
    ".cache",
}
MAX_FILES_PER_SKILL = 80
MAX_TEXT_BYTES = 64_000

AUTH_TOKENS = (
    "api_key",
    "api key",
    "apikey",
    "auth",
    "authentication",
    "authorization",
    "oauth",
    "login",
    "token",
    "secret",
    "credentials",
    "bearer",
)

ENV_DEP_TOKENS = (
    "sqlite3",
    "tree ",
    "pandas",
    "ffmpeg",
    "imagemagick",
    "convert ",
    "dotnet",
    "java ",
    "go ",
    "cargo",
    "ruby",
    "bundle",
    "npm ",
    "node ",
    "python ",
)

TARGET_OS_PATTERNS = {
    "linux": (
    ("shell.macos_say", "platform_api", re.compile(r"\bsay\b"), "skill_os_fixable", 28),
    ("shell.apple_automation", "platform_api", re.compile(r"\b(osascript|applescript|pbcopy|pbpaste|open -a)\b", re.I), "skill_os_fixable", 38),
    ("path.homebrew_only", "file_path", re.compile(r"/opt/homebrew|/usr/local/Cellar|brew install", re.I), "skill_os_fixable", 24),
    ("shell.macos_date_j_f", "process_shell", re.compile(r"date\s+-j\b|date\s+.*\s+-f\s+", re.I), "skill_os_fixable", 40),
    ("windows.command", "platform_api", re.compile(r"\b(powershell|cmd\.exe|winget|choco|clip\.exe|reg\.exe)\b", re.I), "skill_os_fixable", 34),
    ("path.windows_literal", "file_path", re.compile(r"[A-Za-z]:\\\\|%APPDATA%|\\AppData\\", re.I), "skill_os_fixable", 32),
    ("python.windows_import", "platform_api", re.compile(r"\b(import|from)\s+(winreg|msvcrt)\b"), "skill_os_fixable", 40),
    ("python.unix_api", "platform_api", re.compile(r"\b(import|from)\s+(fcntl|termios|pwd|grp)\b"), "platform_intrinsic", 16),
    ("system.procfs", "system_info", re.compile(r"/proc/[A-Za-z0-9_/.-]+"), "platform_intrinsic", 16),
    ("node.unix_env_script", "process_shell", re.compile(r"NODE_ENV=|[A-Z_][A-Z0-9_]*=.*\bnpm\b"), "skill_os_fixable", 18),
    ("shell.sed_i", "process_shell", re.compile(r"\bsed\s+-i(\s+['\"]{2})?"), "skill_os_fixable", 12),
    ),
    "macos": (
        ("shell.linux_xdg_open", "process_shell", re.compile(r"\bxdg-open\b"), "skill_os_fixable", 34),
        ("pkg.linux_package_manager", "dependency_contract", re.compile(r"\b(apt-get|apt|dnf|yum|pacman|zypper)\s+(?:install|add|update)\b", re.I), "skill_os_fixable", 28),
        ("system.procfs", "system_info", re.compile(r"/proc/[A-Za-z0-9_/.-]+"), "platform_intrinsic", 34),
        ("system.sysfs", "system_info", re.compile(r"/sys/[A-Za-z0-9_/.-]+"), "platform_intrinsic", 34),
        ("system.systemd", "platform_api", re.compile(r"\b(systemctl|journalctl)\b|/etc/systemd", re.I), "skill_os_fixable", 36),
        ("shell.linux_free", "process_shell", re.compile(r"\bfree\s+-(?:h|m|g|k|b)\b|['\"]free['\"]", re.I), "skill_os_fixable", 40),
        ("shell.linux_uptime_pretty", "process_shell", re.compile(r"\buptime\s+-p\b", re.I), "skill_os_fixable", 38),
        ("guide.linux_only_system_utilities", "guide_only_os_contract", re.compile(r"standard Linux utilities|Linux-specific utilit", re.I), "skill_os_fixable", 34),
        ("shell.gnu_date_d", "process_shell", re.compile(r"\bdate\s+-d\b", re.I), "skill_os_fixable", 38),
        ("shell.gnu_readlink_f", "process_shell", re.compile(r"\breadlink\s+-f\b", re.I), "skill_os_fixable", 28),
        ("shell.gnu_realpath", "process_shell", re.compile(r"\brealpath\s+--(?:relative-to|canonicalize)", re.I), "skill_os_fixable", 28),
        ("shell.gnu_xargs_r", "process_shell", re.compile(r"\bxargs\s+-r\b", re.I), "skill_os_fixable", 28),
        ("shell.linux_clipboard", "process_shell", re.compile(r"\b(xclip|xsel|wl-copy|wl-paste)\b", re.I), "skill_os_fixable", 28),
        ("python.linux_import", "platform_api", re.compile(r"\b(import|from)\s+(inotify_simple|pyinotify|systemd|prctl)\b"), "skill_os_fixable", 36),
        ("arch.x86_simd_on_macos", "platform_api", re.compile(r"\b(AVX2?|__m256|_mm256_|immintrin\.h|x86-64|x86_64)\b", re.I), "platform_intrinsic", 24),
        ("windows.command", "platform_api", re.compile(r"\b(powershell|cmd\.exe|winget|choco|clip\.exe|reg\.exe)\b", re.I), "skill_os_fixable", 34),
        ("path.windows_literal", "file_path", re.compile(r"[A-Za-z]:\\\\|%APPDATA%|\\AppData\\", re.I), "skill_os_fixable", 32),
        ("python.windows_import", "platform_api", re.compile(r"\b(import|from)\s+(winreg|msvcrt)\b"), "skill_os_fixable", 40),
    ),
}

REFERENCED_SCRIPT_RE = re.compile(
    r"(?P<path>(?:\.?/)?(?:scripts|bin|tools)/[A-Za-z0-9_.@+/-]+\.(?:sh|bash|py|js|mjs|cjs|ts)|\./[A-Za-z0-9_.@+/-]+\.(?:sh|bash|py|js|mjs|cjs|ts))"
)
COMMAND_SCRIPT_RE = re.compile(
    r"\b(?:bash|sh|python3?|node|tsx|ts-node)\s+(?P<path>[A-Za-z0-9_.@+/-]+\.(?:sh|bash|py|js|mjs|cjs|ts))"
)
EXTERNAL_CLI_RE = re.compile(r"\b(?P<cmd>gemini|oracle|mcporter|fedit)\b")


@dataclass(frozen=True)
class Evidence:
    rule_id: str
    category: str
    file: str
    line: int
    matched_text: str
    predicted_label: str
    score: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "category": self.category,
            "file": self.file,
            "line": self.line,
            "matched_text": self.matched_text[:240],
            "predicted_label": self.predicted_label,
            "score": self.score,
        }


def iter_skill_files(skills_dir: Path) -> list[Path]:
    return sorted(skills_dir.glob("*/*/SKILL.md"))


def read_skill_list(path: Path | None) -> set[str]:
    if path is None:
        return set()
    skill_ids: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        skill_ids.add(line)
    return skill_ids


def filter_skill_files(skill_files: list[Path], wanted_ids: set[str]) -> list[Path]:
    if not wanted_ids:
        return skill_files
    selected = []
    for skill_file in skill_files:
        skill_id = f"{skill_file.parent.parent.name}/{skill_file.parent.name}"
        if skill_id in wanted_ids:
            selected.append(skill_file)
    return selected


def read_text(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_TEXT_BYTES)
        return data.decode("utf-8", errors="ignore")
    except OSError:
        return ""


def rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def line_for_offset(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def package_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts[:-1]):
            continue
        if not path.is_file():
            continue
        if len(path.parts) - len(root.parts) > 4:
            continue
        if path.name.startswith(".") and path.name not in {".env.example"}:
            continue
        if path.suffix.lower() in TEXT_FILE_SUFFIXES or path.name in {"Dockerfile", "Makefile"}:
            files.append(path)
            if len(files) >= MAX_FILES_PER_SKILL:
                break
    return sorted(files)


def analyze_skill(skill_file: Path, target_os: str = "linux", package_root_base: Path | None = None) -> dict[str, Any]:
    root = skill_file.parent
    source = root.parent.name
    name = root.name
    skill_id = f"{source}/{name}"
    files = package_files(root)
    evidence: list[Evidence] = []

    skill_text = read_text(skill_file)
    evidence.extend(find_missing_referenced_scripts(root, skill_file, skill_text))
    evidence.extend(find_package_entrypoint_gaps(root))
    evidence.extend(find_os_patterns(root, files, target_os))
    evidence.extend(find_external_cli_contracts(root, files))
    evidence.extend(find_guide_platform_lock(root, skill_file, skill_text, target_os))

    auth_hits = token_hits("\n".join(read_text(p)[:12000] for p in files[:40]), AUTH_TOKENS)
    env_hits = token_hits("\n".join(read_text(p)[:12000] for p in files[:40]), ENV_DEP_TOKENS)
    negative_rules = []
    if auth_hits >= 5:
        evidence.append(Evidence("auth.api_key_heavy", "auth_or_service", "SKILL.md", 1, f"{auth_hits} auth tokens", "auth_or_service_blocked", 22))
    else:
        negative_rules.append("auth.api_key_heavy")
    if env_hits >= 4:
        evidence.append(Evidence("dep.generic_installable_tools", "environment_capability", "SKILL.md", 1, f"{env_hits} dependency tokens", "env_bindable_dependency", 14))

    predicted_label, score = choose_label(evidence)
    prediction = {
        "schema_version": "static_skill_prediction_v1",
        "skill_id": skill_id,
        "source": source,
        "skill": name,
        "package_root": rel(root, package_root_base or REPO_ROOT),
        "platform": target_os,
        "predicted_label": predicted_label,
        "predicted_os_related": predicted_label in {
            "skill_os_fixable",
            "skill_package_or_entrypoint",
            "skill_contract_mismatch",
            "guide_only_os_contract",
            "platform_intrinsic",
        },
        "predicted_fixability": fixability(predicted_label, target_os),
        "confidence": confidence(score),
        "score": score,
        "evidence": [item.to_dict() for item in sorted(evidence, key=lambda x: -x.score)[:20]],
        "negative_rules": negative_rules,
    }
    return prediction


def find_missing_referenced_scripts(root: Path, skill_file: Path, text: str) -> list[Evidence]:
    evidence: list[Evidence] = []
    seen: set[str] = set()
    for pattern in (REFERENCED_SCRIPT_RE, COMMAND_SCRIPT_RE):
        for match in pattern.finditer(text):
            ref = match.group("path").lstrip("./")
            if ref in seen:
                continue
            seen.add(ref)
            candidate = root / ref
            if not candidate.is_file():
                evidence.append(
                    Evidence(
                        "pkg.missing_referenced_script",
                        "package_entrypoint",
                        rel(skill_file, root),
                        line_for_offset(text, match.start()),
                        ref,
                        "skill_package_or_entrypoint",
                        65,
                    )
                )
    return evidence


def find_external_cli_contracts(root: Path, files: list[Path]) -> list[Evidence]:
    evidence: list[Evidence] = []
    declared_text = "\n".join(read_text(root / name) for name in ("package.json", "_meta.json", "pyproject.toml"))
    for path in files:
        text = read_text(path)
        relative = rel(path, root)
        for match in EXTERNAL_CLI_RE.finditer(text):
            cmd = match.group("cmd")
            if cmd in declared_text:
                continue
            evidence.append(
                Evidence(
                    "contract.undeclared_external_cli",
                    "dependency_contract",
                    relative,
                    line_for_offset(text, match.start()),
                    cmd,
                    "skill_contract_mismatch",
                    42,
                )
            )
            break
    return evidence


def find_package_entrypoint_gaps(root: Path) -> list[Evidence]:
    evidence: list[Evidence] = []
    package_json = root / "package.json"
    if package_json.is_file():
        try:
            data = json.loads(read_text(package_json))
        except json.JSONDecodeError:
            data = {}
        if isinstance(data, dict):
            for key, value in _package_bin_entries(data.get("bin")).items():
                if not (root / value).is_file():
                    evidence.append(Evidence("pkg.missing_declared_bin", "package_entrypoint", "package.json", 1, f"{key}: {value}", "skill_package_or_entrypoint", 70))
            scripts = data.get("scripts")
            if isinstance(scripts, dict):
                for key, value in scripts.items():
                    if isinstance(value, str) and re.search(r"\b(sh|bash|python|node|tsx|ts-node)\s+([^\s;&|]+)", value):
                        target = re.search(r"\b(?:sh|bash|python|node|tsx|ts-node)\s+([^\s;&|]+)", value)
                        if target and not (root / target.group(1)).exists():
                            evidence.append(Evidence("pkg.missing_package_script_target", "package_entrypoint", "package.json", 1, f"{key}: {value}", "skill_package_or_entrypoint", 55))
    return evidence


def _package_bin_entries(value: Any) -> dict[str, str]:
    if isinstance(value, str):
        return {"bin": value}
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items() if isinstance(v, str)}
    return {}


def find_os_patterns(root: Path, files: list[Path], target_os: str) -> list[Evidence]:
    evidence: list[Evidence] = []
    patterns = TARGET_OS_PATTERNS[target_os]
    for path in files:
        text = read_text(path)
        if not text:
            continue
        relative = rel(path, root)
        for rule_id, category, pattern, label, score in patterns:
            for match in pattern.finditer(text):
                evidence.append(Evidence(rule_id, category, relative, line_for_offset(text, match.start()), match.group(0), label, score))
                break
    return evidence


def find_guide_platform_lock(root: Path, skill_file: Path, text: str, target_os: str) -> list[Evidence]:
    lower = text.lower()
    evidence: list[Evidence] = []
    mac = sum(lower.count(token) for token in ("macos", "mac os", "darwin", "finder", "homebrew"))
    win = sum(lower.count(token) for token in ("windows", "powershell", "cmd.exe", "registry"))
    linux = sum(lower.count(token) for token in ("linux", "ubuntu", "debian", "apt "))
    scripts = any(p.suffix.lower() in SCRIPT_SUFFIXES for p in root.rglob("*") if p.is_file())

    target_mentions = {"linux": linux, "macos": mac}[target_os]
    non_target_mentions = {"linux": mac + win, "macos": linux + win}[target_os]
    target_name = {"linux": "Linux", "macos": "macOS"}[target_os]

    if non_target_mentions >= 3 and target_mentions == 0:
        label = "guide_only_os_contract" if not scripts else "skill_os_fixable"
        evidence.append(
            Evidence(
                "guide.platform_locked_steps",
                "guide_only_os_contract",
                rel(skill_file, root),
                1,
                f"non-target mentions={non_target_mentions}, {target_name} mentions=0",
                label,
                26,
            )
        )

    if target_os == "linux":
        if re.search(r"if\s+(?:mac|darwin|windows|win32)", lower) and "linux" not in lower:
            evidence.append(Evidence("guide.os_dispatch_missing_linux", "guide_only_os_contract", rel(skill_file, root), 1, "OS dispatch without Linux mention", "skill_os_fixable", 36))
    elif re.search(r"if\s+(?:linux|windows|win32)", lower) and not re.search(r"\b(?:macos|mac os|darwin|mac)\b", lower):
        evidence.append(Evidence("guide.os_dispatch_missing_macos", "guide_only_os_contract", rel(skill_file, root), 1, "OS dispatch without macOS mention", "skill_os_fixable", 36))
    return evidence


def token_hits(text: str, tokens: tuple[str, ...]) -> int:
    lower = text.lower()
    return sum(1 for token in tokens if token in lower)


def choose_label(evidence: list[Evidence]) -> tuple[str, int]:
    if not evidence:
        return "no_static_risk_detected", 0
    label_scores: dict[str, int] = {}
    for item in evidence:
        label_scores[item.predicted_label] = label_scores.get(item.predicted_label, 0) + item.score
    priority = {
        "skill_package_or_entrypoint": 5,
        "skill_os_fixable": 4,
        "platform_intrinsic": 3,
        "guide_only_os_contract": 3,
        "env_bindable_dependency": 2,
        "auth_or_service_blocked": 1,
    }
    label = max(label_scores, key=lambda item: (label_scores[item], priority.get(item, 0)))
    return label, min(sum(item.score for item in evidence), 100)


def fixability(label: str, target_os: str = "linux") -> str:
    return {
        "skill_package_or_entrypoint": "fixable_skill_package",
        "skill_os_fixable": "fixable_skill_package",
        "guide_only_os_contract": "fixable_skill_guide",
        "env_bindable_dependency": "fixable_environment",
        "auth_or_service_blocked": "not_os_issue",
        "platform_intrinsic": f"not_functionally_fixable_for_{target_os}",
        "no_static_risk_detected": "unknown",
    }.get(label, "unknown")


def confidence(score: int) -> str:
    if score >= 60:
        return "high"
    if score >= 25:
        return "medium"
    if score > 0:
        return "low"
    return "none"


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skills-dir", type=Path, default=DEFAULT_SKILLS_DIR)
    parser.add_argument("--target-os", choices=sorted(DEFAULT_OUT_BY_TARGET), default="linux")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--skill-list",
        type=Path,
        default=None,
        help="Optional newline-delimited source/package skill ids to analyze.",
    )
    args = parser.parse_args(argv)

    out = args.out or DEFAULT_OUT_BY_TARGET[args.target_os]
    wanted_ids = read_skill_list(args.skill_list)
    skill_files = filter_skill_files(iter_skill_files(args.skills_dir), wanted_ids)
    if args.limit:
        skill_files = skill_files[: args.limit]
    rows = [analyze_skill(path, args.target_os, args.skills_dir) for path in skill_files]
    write_jsonl(out, rows)
    summary = {
        "skills_analyzed": len(rows),
        "target_os": args.target_os,
        "out": rel(out, REPO_ROOT),
        "requested_skill_count": len(wanted_ids) if wanted_ids else None,
        "missing_requested_skill_count": len(wanted_ids - {row["skill_id"] for row in rows}) if wanted_ids else 0,
        "missing_requested_skill_ids": sorted(wanted_ids - {row["skill_id"] for row in rows})[:50] if wanted_ids else [],
        "label_counts": _counts(row["predicted_label"] for row in rows),
        "confidence_counts": _counts(row["confidence"] for row in rows),
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


def _counts(values) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


if __name__ == "__main__":
    raise SystemExit(main())
