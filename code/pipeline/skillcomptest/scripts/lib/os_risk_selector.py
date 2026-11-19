"""Rank skills by static OS-compatibility risk signals.

The selector is intentionally conservative: it does not execute skill code. It
uses SKILL.md text, nearby script files, and existing run state to prioritize
skills that are more likely to reveal fixable OS portability issues.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path

from scripts.lib.batch_queue import (
    RunProfile,
    SkillCandidate,
    classify_candidate,
    scan_skills,
)


STRONG_SIGNALS = {
    "setup_script": 35,
    "script_entrypoint": 25,
    "platform_metadata": 22,
    "requires_bins": 18,
    "install_metadata": 16,
    "macos_specific": 18,
    "windows_specific": 18,
    "linux_specific": 12,
    "shell_specific": 14,
    "path_specific": 14,
    "compiled_tool": 10,
}

API_OR_AUTH_PENALTY = 36

MACOS_TOKENS = (
    "osascript",
    "open -a",
    "finder",
    "~/library",
    "pbcopy",
    "pbpaste",
    "darwin",
    "macos",
)
WINDOWS_TOKENS = (
    "powershell",
    "cmd.exe",
    "windows",
    "win32",
    "appdata",
    "%appdata%",
    "c:\\",
    "registry",
    "winget",
    "choco",
    "clip.exe",
)
LINUX_TOKENS = (
    "apt install",
    "apt-get",
    "dnf install",
    "pacman -s",
    "xdg-open",
    "/usr/bin",
    "/etc/",
    "linux",
)
SHELL_TOKENS = (
    "#!/usr/bin/env bash",
    "#!/bin/bash",
    "set -e",
    "chmod +x",
    "bash ",
    ".sh",
)
PATH_TOKENS = (
    "~/",
    "/tmp/",
    "/var/",
    "\\\\",
)
COMPILED_TOKENS = (
    "go install",
    "cargo install",
    "npm install -g",
    "pipx install",
    "binary",
)
API_TOKENS = (
    "api_key",
    "api key",
    "apikey",
    "auth",
    "authenticate",
    "authentication",
    "authorization",
    "credentials",
    "cookie",
    "cookies",
    "session",
    "oauth",
    "login",
    "sign in",
    "signin",
    "bearer token",
    "access token",
    "cloud",
    "stripe",
    "google api",
    "gemini",
    "openrouter",
    "slack",
    "notion",
    "tencentcloud",
    "twitter",
    "x.com",
    "wechat",
    "youtube",
)

AUTH_REASONS = {"api_or_auth_heavy", "api_or_auth_mixed"}

EXECUTABLE_FILE_SUFFIXES = {".sh", ".bash", ".py", ".js", ".mjs", ".cjs", ".ts"}


def _package_json_has_executable_surface(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    if data.get("bin"):
        return True
    scripts = data.get("scripts")
    return isinstance(scripts, dict) and bool(scripts)


@dataclass(frozen=True)
class ScoredSkill:
    source: str
    name: str
    skill_file: str
    score: int
    reasons: list[str] = field(default_factory=list)
    state: str = ""
    sample_tasks: int = 0
    exec_tasks: int = 0
    final_exec_tasks: int = 0
    executable_surface: bool = False
    executable_surface_reasons: list[str] = field(default_factory=list)

    @property
    def skill_id(self) -> str:
        return f"{self.source}/{self.name}"

    def to_json_dict(self) -> dict:
        return asdict(self) | {"skill_id": self.skill_id}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _has_any(text: str, tokens: tuple[str, ...]) -> bool:
    lower = text.lower()
    return any(token in lower for token in tokens)


def _add(reasons: list[str], reason: str, score: int) -> int:
    if reason not in reasons:
        reasons.append(reason)
        return score
    return 0


def detect_executable_surface(skill_file: Path | str) -> list[str]:
    """Return concrete signals that the skill has local executable behavior.

    Guide-only OS wording is useful for lightweight documentation audits, but
    the main Linux benchmark should prioritize skills with scripts, wrappers,
    package entrypoints, install contracts, or declared local binaries.
    """
    path = Path(skill_file)
    root = path.parent
    text = _read_text(path)
    lower = text.lower()
    reasons: list[str] = []

    if (root / "setup.sh").is_file():
        reasons.append("setup_script")

    scripts_dir = root / "scripts"
    if scripts_dir.is_dir() and any(p.is_file() for p in scripts_dir.rglob("*")):
        reasons.append("script_entrypoint")

    package_json = root / "package.json"
    if package_json.is_file() and _package_json_has_executable_surface(package_json):
        reasons.append("package_entrypoint")

    if any(p.is_file() and p.suffix.lower() in EXECUTABLE_FILE_SUFFIXES for p in root.iterdir()):
        reasons.append("root_executable_file")

    if '"bins"' in lower or '"anybins"' in lower or "requires:" in lower or '"requires"' in lower:
        reasons.append("requires_bins")

    if '"install"' in lower or "brew install" in lower or "apt install" in lower or "go install" in lower:
        reasons.append("install_metadata")

    if _has_any(text, COMPILED_TOKENS):
        reasons.append("compiled_tool")

    return reasons


def score_skill(skill_file: Path | str) -> ScoredSkill:
    path = Path(skill_file)
    root = path.parent
    text = _read_text(path)
    lower = text.lower()
    reasons: list[str] = []
    executable_surface_reasons = detect_executable_surface(path)
    score = 0

    if (root / "setup.sh").is_file():
        score += _add(reasons, "setup_script", STRONG_SIGNALS["setup_script"])

    scripts_dir = root / "scripts"
    script_files = []
    if scripts_dir.is_dir():
        script_files = [p for p in scripts_dir.rglob("*") if p.is_file()]
        if script_files:
            score += _add(reasons, "script_entrypoint", STRONG_SIGNALS["script_entrypoint"])

    script_text = "\n".join(_read_text(p)[:4096] for p in script_files[:8])
    combined = "\n".join([text, script_text])
    combined_lower = combined.lower()

    if "platforms:" in lower or '"os"' in lower or '"platform' in lower:
        score += _add(reasons, "platform_metadata", STRONG_SIGNALS["platform_metadata"])
    if '"bins"' in lower or '"anybins"' in lower or "requires:" in lower or '"requires"' in lower:
        score += _add(reasons, "requires_bins", STRONG_SIGNALS["requires_bins"])
    if '"install"' in lower or "brew install" in lower or "apt install" in lower or "go install" in lower:
        score += _add(reasons, "install_metadata", STRONG_SIGNALS["install_metadata"])

    if _has_any(combined, MACOS_TOKENS):
        score += _add(reasons, "macos_specific", STRONG_SIGNALS["macos_specific"])
    if _has_any(combined, WINDOWS_TOKENS):
        score += _add(reasons, "windows_specific", STRONG_SIGNALS["windows_specific"])
    if _has_any(combined, LINUX_TOKENS):
        score += _add(reasons, "linux_specific", STRONG_SIGNALS["linux_specific"])
    if _has_any(combined, SHELL_TOKENS):
        score += _add(reasons, "shell_specific", STRONG_SIGNALS["shell_specific"])
    if _has_any(combined, PATH_TOKENS):
        score += _add(reasons, "path_specific", STRONG_SIGNALS["path_specific"])
    if _has_any(combined, COMPILED_TOKENS):
        score += _add(reasons, "compiled_tool", STRONG_SIGNALS["compiled_tool"])

    api_hits = sum(1 for token in API_TOKENS if token in combined_lower)
    if api_hits >= 3 and len(reasons) <= 2:
        score -= API_OR_AUTH_PENALTY
        reasons.append("api_or_auth_heavy")
    elif api_hits >= 3:
        score -= API_OR_AUTH_PENALTY // 2
        reasons.append("api_or_auth_mixed")

    source = root.parent.name
    return ScoredSkill(
        source=source,
        name=root.name,
        skill_file=str(path),
        score=max(score, 0),
        reasons=reasons,
        executable_surface=bool(executable_surface_reasons),
        executable_surface_reasons=executable_surface_reasons,
    )


def select_os_risk_skills(
    *,
    skills_dir: Path | str,
    samples_dir: Path | str,
    exec_dir: Path | str,
    profile: RunProfile,
    limit: int,
    source: str = "",
    include_complete: bool = False,
    exclude_auth_heavy: bool = False,
    require_executable_surface: bool = False,
) -> list[ScoredSkill]:
    selected: list[ScoredSkill] = []
    for candidate in scan_skills(skills_dir, source=source):
        state = classify_candidate(candidate, samples_dir, exec_dir, profile)
        if state.state == "exec_complete" and not include_complete:
            continue
        scored = score_skill(candidate.skill_file)
        if require_executable_surface and not scored.executable_surface:
            continue
        if exclude_auth_heavy and AUTH_REASONS.intersection(scored.reasons):
            continue
        selected.append(
            ScoredSkill(
                source=scored.source,
                name=scored.name,
                skill_file=scored.skill_file,
                score=scored.score,
                reasons=scored.reasons,
                state=state.state,
                sample_tasks=state.sample_tasks,
                exec_tasks=state.exec_tasks,
                final_exec_tasks=state.final_exec_tasks,
                executable_surface=scored.executable_surface,
                executable_surface_reasons=scored.executable_surface_reasons,
            )
        )

    selected.sort(key=lambda item: (-item.score, item.source, item.name))
    return selected[:limit]


def write_selection_artifacts(
    *,
    selected: list[ScoredSkill],
    list_path: Path | str,
    metadata_path: Path | str,
    shard_dir: Path | str | None = None,
    shard_size: int = 20,
) -> None:
    list_out = Path(list_path)
    meta_out = Path(metadata_path)
    list_out.parent.mkdir(parents=True, exist_ok=True)
    meta_out.parent.mkdir(parents=True, exist_ok=True)

    list_out.write_text("".join(f"{item.skill_id}\n" for item in selected), encoding="utf-8")
    meta_out.write_text(
        json.dumps([item.to_json_dict() for item in selected], indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    if shard_dir is None:
        return
    shard_root = Path(shard_dir)
    shard_root.mkdir(parents=True, exist_ok=True)
    stem = list_out.stem
    for idx in range(0, len(selected), shard_size):
        shard_num = idx // shard_size + 1
        shard = selected[idx:idx + shard_size]
        shard_path = shard_root / f"{stem}.shard{shard_num:02d}.list"
        shard_path.write_text("".join(f"{item.skill_id}\n" for item in shard), encoding="utf-8")
