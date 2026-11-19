#!/usr/bin/env python3
"""Run the RQ3 direct single-agent repair baseline.

This runner intentionally avoids SkillFab. It gives one Claude Code developer
agent a local copy of the original package plus the RQ3 OS evidence, records the
static repair output, and packages the repaired skill for later reused-task
replay. It does not run skill behavior or benchmark tasks.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = REPO_ROOT.parent
DEFAULT_BASELINE = REPO_ROOT / "data/releases/skillbridge-rq3-direct-agent-baseline-20260701"
DEFAULT_RUN_DIR = REPO_ROOT / ".agent-runs/rq3-direct-agent-baseline"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "delivery/rq3-direct-agent-baseline-outputs-20260701"
DEFAULT_CLAUDE_ENV = WORKSPACE_ROOT / "skillcomptest/.env"
CLAUDE_ENV_KEYS = {
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    "CLAUDE_SETTING_SOURCES",
    "CLAUDE_MODEL",
    "EXEC_CLAUDE_MODEL",
}
FORBIDDEN_NAMES = {".git", ".env", ".agent-runs", "__MACOSX", ".skillfab-tokens"}
JSON_WRITE_LOCK = Lock()


def main() -> int:
    args = parse_args()
    jobs = read_jsonl(args.baseline_dir / "data/direct_agent_repair_jobs.jsonl")
    selected = select_jobs(jobs, args.issue_keys, args.limit)
    if args.dry_run:
        print(json.dumps({"selected_jobs": len(selected), "issue_keys": [row["issue_key"] for row in selected]}, ensure_ascii=False))
        return 0

    args.run_dir.mkdir(parents=True, exist_ok=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    results_path = args.output_dir / "direct_agent_results.jsonl"
    output_manifest_path = args.output_dir / "direct_agent_output_packages.jsonl"

    completed: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as executor:
        futures = {
            executor.submit(run_one_job, row, args, results_path, output_manifest_path): row
            for row in selected
            if args.force or not output_exists(row, args.output_dir)
        }
        skipped = len(selected) - len(futures)
        for future in as_completed(futures):
            completed.append(future.result())

    summary = build_run_summary(selected, completed, skipped, args)
    write_json(args.output_dir / "summary.json", summary)
    write_json(args.output_dir / "release.json", build_release_json(summary, args))
    (args.output_dir / "README.md").write_text(render_output_readme(summary), encoding="utf-8")
    if args.tarball:
        tar_path = create_tarball(args.output_dir)
        print(f"wrote tarball {tar_path}")
    print(json.dumps(summary["headline_counts"], ensure_ascii=False, sort_keys=True))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-dir", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--issue-keys", nargs="*", default=[])
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--agent-timeout-seconds", type=int, default=30 * 60)
    parser.add_argument("--claude-bin", default=os.environ.get("CLAUDE_BIN", "claude"))
    parser.add_argument("--claude-env-file", type=Path, default=DEFAULT_CLAUDE_ENV)
    parser.add_argument("--claude-model", default=os.environ.get("CLAUDE_MODEL", ""))
    parser.add_argument("--claude-max-budget-usd", default="30")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--tarball", action="store_true")
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def select_jobs(rows: list[dict[str, Any]], issue_keys: list[str], limit: int | None) -> list[dict[str, Any]]:
    if issue_keys:
        wanted = set(issue_keys)
        selected = [row for row in rows if row["issue_key"] in wanted]
        missing = wanted - {row["issue_key"] for row in selected}
        if missing:
            raise RuntimeError(f"issue keys not found: {', '.join(sorted(missing))}")
    else:
        selected = list(rows)
    selected.sort(key=lambda row: row["issue_key"])
    return selected[:limit] if limit else selected


def output_exists(row: dict[str, Any], output_dir: Path) -> bool:
    return (output_dir / "packages" / row["baseline_job_id"] / "package").exists()


def run_one_job(row: dict[str, Any], args: argparse.Namespace, results_path: Path, output_manifest_path: Path) -> dict[str, Any]:
    job_dir = args.run_dir / row["baseline_job_id"]
    package_work = job_dir / "package"
    if job_dir.exists() and args.force:
        shutil.rmtree(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)
    copy_source_package(Path(row["source_package_path"]), package_work)
    write_json(job_dir / "job.json", row)
    init_git(package_work)

    prompt = build_prompt(row, package_work)
    (job_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    started_at = utc_now()
    agent = run_claude(
        prompt=prompt,
        cwd=job_dir,
        package_dir=package_work,
        timeout_seconds=args.agent_timeout_seconds,
        max_budget_usd=args.claude_max_budget_usd,
        claude_bin=args.claude_bin,
        claude_env_file=args.claude_env_file,
        claude_model=args.claude_model,
    )
    ended_at = utc_now()
    diff_check = run_static_diff_check(package_work)
    changed_files = changed_files_from_git(package_work)
    package_dir = args.output_dir / "packages" / row["baseline_job_id"] / "package"
    package_status = "not_packaged"
    package_ref = ""
    if agent.get("ok") and not agent.get("agent_blocked") and changed_files:
        if package_dir.exists():
            shutil.rmtree(package_dir)
        copy_repaired_package(package_work, package_dir)
        package_status = "packaged"
        package_ref = package_dir.relative_to(args.output_dir).as_posix()

    result = {
        "schema_version": "rq3_direct_agent_result_v1",
        "baseline_job_id": row["baseline_job_id"],
        "issue_key": row["issue_key"],
        "skill_id": row["skill_id"],
        "target_os": row["target_os"],
        "started_at": started_at,
        "ended_at": ended_at,
        "wall_time_seconds": agent.get("duration_seconds"),
        "status": classify_result(agent, changed_files, diff_check),
        "agent_blocked": bool(agent.get("agent_blocked")),
        "blocked_reason": agent.get("blocked_reason", ""),
        "changed_files": changed_files,
        "static_validation": {"git_diff_check": diff_check},
        "package_status": package_status,
        "package_ref": package_ref,
        "agent_result": agent.get("result", {}),
        "input_tokens": agent.get("input_tokens"),
        "output_tokens": agent.get("output_tokens"),
        "cache_creation_input_tokens": agent.get("cache_creation_input_tokens"),
        "cache_read_input_tokens": agent.get("cache_read_input_tokens"),
        "total_cost_usd": agent.get("total_cost_usd"),
        "run_artifacts": {
            "run_dir": repo_rel(job_dir),
            "stdout": repo_rel(job_dir / "claude.stdout.json"),
            "stderr": repo_rel(job_dir / "claude.stderr.log"),
        },
        "secrets_redacted": True,
    }
    safe_append_jsonl(results_path, result)
    if package_status == "packaged":
        safe_append_jsonl(
            output_manifest_path,
            {
                "schema_version": "rq3_direct_agent_output_package_v1",
                "baseline_job_id": row["baseline_job_id"],
                "issue_key": row["issue_key"],
                "skill_id": row["skill_id"],
                "target_os": row["target_os"],
                "package_ref": package_ref,
                "source_package_path": row["source_package_path"],
                "changed_files": changed_files,
                "result_ref": "direct_agent_results.jsonl",
            },
        )
    return result


def copy_source_package(source: Path, dest: Path) -> None:
    source_path = source if source.is_absolute() else WORKSPACE_ROOT / source
    if not source_path.exists():
        raise FileNotFoundError(f"missing source package: {source_path}")
    if dest.exists():
        shutil.rmtree(dest)

    def ignore(_dir: str, names: list[str]) -> set[str]:
        ignored = {".git", "node_modules", "__MACOSX"}
        ignored.update(name for name in names if name.startswith("._"))
        return ignored

    shutil.copytree(source_path, dest, ignore=ignore)


def init_git(package_dir: Path) -> None:
    run(["git", "init"], cwd=package_dir)
    run(["git", "config", "user.email", "direct-agent-baseline@example.invalid"], cwd=package_dir)
    run(["git", "config", "user.name", "Direct Agent Baseline"], cwd=package_dir)
    run(["git", "add", "-A"], cwd=package_dir)
    run(["git", "commit", "-m", "baseline source"], cwd=package_dir)


def run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=True)


def build_prompt(row: dict[str, Any], package_dir: Path) -> str:
    return f"""You are the direct single-agent baseline for an OS-compatibility repair.

This is an ablation baseline. Do not use SkillFab, do not create issues or
submissions, and do not request reviewer feedback. Work only in this package:

{package_dir}

Inputs:
- issue_key: {row['issue_key']}
- skill_id: {row['skill_id']}
- target_os: {', '.join(row.get('target_os') or [])}
- affected_os: {', '.join(row.get('affected_os') or [])}
- fixed replay tasks after repair: {row.get('fixed_task_count')}

Observed OS problem:
{row.get('issue_summary') or ''}

Suggested repair intent:
{row.get('unified_fix_plan') or ''}

Pre-repair evidence by OS:
{json.dumps(row.get('rq2_before_by_os') or {}, ensure_ascii=False, indent=2)}

Rules:
- Preserve the original skill identity and intended capability.
- Make the smallest contract-level OS compatibility repair.
- Prefer portable code/guide branches, dependency metadata, wrappers, or honest
  unsupported declarations over unrelated rewrites.
- Do not hard-code benchmark task answers.
- Do not run the skill, generated benchmark tasks, package manager installs,
  Docker, browser automation, GUI automation, or network-heavy workflows.
- Static validation is allowed: inspect files, check SKILL.md frontmatter,
  bash -n, python3 -m py_compile, node --check, and git diff --check.
- If the repair is impossible or unsafe, leave the package unchanged and set
  agent_blocked=true.

Before finishing, run `git diff --check` from the package directory if possible.

Final answer must be one JSON object and nothing else:
{{
  "issue_key": "{row['issue_key']}",
  "skill_id": "{row['skill_id']}",
  "agent_blocked": <true|false>,
  "blocked_reason": "<empty unless blocked>",
  "changed_files": ["relative/path"],
  "static_validation": {{
    "git_diff_check": "pass|fail|not_run",
    "syntax_checks": "pass|fail|not_run",
    "package_validation": "pass|fail|not_run"
  }},
  "summary": "<brief repair summary>"
}}
"""


def parse_env_file(path: Path | None) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path or not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def build_claude_env(env_file: Path | None, requested_model: str | None) -> tuple[dict[str, str], str | None]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    loaded = parse_env_file(env_file)
    for key in CLAUDE_ENV_KEYS:
        if key in loaded:
            env[key] = loaded[key]
    model = (
        (requested_model or "").strip()
        or loaded.get("EXEC_CLAUDE_MODEL", "").strip()
        or loaded.get("CLAUDE_MODEL", "").strip()
        or env.get("EXEC_CLAUDE_MODEL", "").strip()
        or env.get("CLAUDE_MODEL", "").strip()
    )
    if model:
        env["CLAUDE_MODEL"] = model
    return env, model or None


def run_claude(
    *,
    prompt: str,
    cwd: Path,
    package_dir: Path,
    timeout_seconds: int,
    max_budget_usd: str,
    claude_bin: str,
    claude_env_file: Path | None,
    claude_model: str | None,
) -> dict[str, Any]:
    stdout_path = cwd / "claude.stdout.json"
    stderr_path = cwd / "claude.stderr.log"
    env, model = build_claude_env(claude_env_file, claude_model)
    args = [
        claude_bin,
        "--print",
        "--output-format",
        "json",
        "--input-format",
        "text",
        "--no-session-persistence",
        "--exclude-dynamic-system-prompt-sections",
        "--permission-mode",
        "bypassPermissions",
        "--effort",
        "low",
        "--max-budget-usd",
        max_budget_usd,
        "--add-dir",
        str(package_dir.resolve()),
    ]
    if model:
        args.extend(["--model", model])
    started = time.monotonic()
    try:
        completed = subprocess.run(
            args,
            input=prompt,
            text=True,
            cwd=cwd,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        duration = int(time.monotonic() - started)
        stdout_path.write_text(redact(str(exc.stdout or ""), env), encoding="utf-8")
        stderr_path.write_text(redact(str(exc.stderr or "agent timed out"), env), encoding="utf-8")
        return {"ok": False, "agent_blocked": True, "blocked_reason": "agent_timeout", "duration_seconds": duration}

    duration = int(time.monotonic() - started)
    stdout_path.write_text(redact(completed.stdout, env), encoding="utf-8")
    stderr_path.write_text(redact(completed.stderr, env), encoding="utf-8")
    outer = parse_json_maybe(completed.stdout)
    usage = claude_usage_fields(outer)
    if completed.returncode != 0:
        return {
            "ok": False,
            "agent_blocked": True,
            "blocked_reason": classify_claude_failure(outer, completed.returncode),
            "duration_seconds": duration,
            **usage,
        }
    result_parse_error = ""
    try:
        result = parse_claude_result(outer)
    except (json.JSONDecodeError, ValueError) as exc:
        result = {"agent_blocked": False, "parse_error": str(exc)}
        result_parse_error = str(exc)
    return {"ok": True, "result": result, "result_parse_error": result_parse_error, "duration_seconds": duration, **usage}


def redact(text: str, env: dict[str, str]) -> str:
    redacted = text
    for key in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
        secret = env.get(key)
        if secret:
            redacted = redacted.replace(secret, "<redacted-token>")
    redacted = re.sub(r"sk-[A-Za-z0-9_-]{12,}", "<redacted-token>", redacted)
    return redacted


def parse_json_maybe(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text.strip())
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}\s*$", text.strip())
        if match:
            value = json.loads(match.group(0))
            return value if isinstance(value, dict) else {}
    return {}


def parse_claude_result(outer: dict[str, Any]) -> dict[str, Any]:
    result: Any = outer.get("result") or outer.get("response") or outer.get("message") or outer
    if isinstance(result, str):
        text = result.strip()
        fenced = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
        if fenced:
            text = fenced.group(1).strip()
        elif "{" in text and "}" in text:
            text = text[text.index("{") : text.rindex("}") + 1]
        result = json.loads(text)
    return result if isinstance(result, dict) else {}


def claude_usage_fields(outer: dict[str, Any]) -> dict[str, Any]:
    usage = outer.get("usage", {}) if isinstance(outer, dict) else {}
    return {
        "input_tokens": usage.get("input_tokens") or usage.get("input"),
        "output_tokens": usage.get("output_tokens") or usage.get("output"),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
        "total_cost_usd": outer.get("total_cost_usd") or outer.get("cost_usd") if isinstance(outer, dict) else None,
    }


def classify_claude_failure(outer: dict[str, Any], returncode: int) -> str:
    api_error_status = outer.get("api_error_status")
    result_text = str(outer.get("result") or "")
    if api_error_status:
        if str(api_error_status) == "402" or "Insufficient Balance" in result_text:
            return "api_insufficient_balance"
        return f"api_error_{api_error_status}"
    subtype = str(outer.get("subtype") or "")
    errors = " ".join(str(item) for item in outer.get("errors") or [])
    if subtype == "error_max_budget_usd" or "Reached maximum budget" in errors:
        return "budget_exhausted"
    return f"claude_exited_{returncode}"


def run_static_diff_check(package_dir: Path) -> str:
    completed = subprocess.run(["git", "diff", "--check"], cwd=package_dir, text=True, capture_output=True, check=False)
    (package_dir.parent / "git-diff-check.stdout").write_text(completed.stdout, encoding="utf-8")
    (package_dir.parent / "git-diff-check.stderr").write_text(completed.stderr, encoding="utf-8")
    return "pass" if completed.returncode == 0 else "fail"


def changed_files_from_git(package_dir: Path) -> list[str]:
    completed = subprocess.run(["git", "status", "--porcelain"], cwd=package_dir, text=True, capture_output=True, check=True)
    files = []
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        files.append(line[3:].strip())
    return sorted(files)


def copy_repaired_package(src: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)

    def ignore(_dir: str, names: list[str]) -> set[str]:
        ignored = set(FORBIDDEN_NAMES)
        ignored.update(name for name in names if name.startswith("._"))
        return ignored

    shutil.copytree(src, dest, ignore=ignore)
    forbidden = scan_for_forbidden_files(dest)
    if forbidden:
        shutil.rmtree(dest)
        raise ValueError(f"forbidden files in repaired package: {forbidden[:10]}")


def scan_for_forbidden_files(package_dir: Path) -> list[str]:
    findings = []
    for path in package_dir.rglob("*"):
        rel = path.relative_to(package_dir).as_posix()
        parts = set(path.relative_to(package_dir).parts)
        if path.name in FORBIDDEN_NAMES or parts.intersection(FORBIDDEN_NAMES):
            findings.append(rel)
        if path.name.startswith("._"):
            findings.append(rel)
    return sorted(set(findings))


def classify_result(agent: dict[str, Any], changed_files: list[str], diff_check: str) -> str:
    if not agent.get("ok"):
        return str(agent.get("blocked_reason") or "agent_failed")
    if agent.get("agent_blocked") or agent.get("result", {}).get("agent_blocked"):
        return "agent_blocked"
    if not changed_files:
        return "no_changes"
    if diff_check != "pass":
        return "static_validation_failed"
    if agent.get("result_parse_error"):
        return "package_ready_with_malformed_agent_result"
    return "package_ready_for_replay"


def safe_append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with JSON_WRITE_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_run_summary(
    selected: list[dict[str, Any]],
    completed: list[dict[str, Any]],
    skipped: int,
    args: argparse.Namespace,
) -> dict[str, Any]:
    status_counts = {}
    for row in completed:
        status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
    return {
        "schema_version": "rq3_direct_agent_run_summary_v1",
        "generated_at": utc_now(),
        "headline_counts": {
            "selected_jobs": len(selected),
            "completed_this_run": len(completed),
            "skipped_existing": skipped,
            "status_counts": dict(sorted(status_counts.items())),
        },
        "baseline_dir": repo_rel(args.baseline_dir),
        "run_dir": repo_rel(args.run_dir),
        "output_dir": repo_rel(args.output_dir),
        "runner_contract": {
            "uses_skillfab": False,
            "uses_review_agent": False,
            "runs_skill_behavior": False,
            "runs_dynamic_tasks": False,
        },
    }


def build_release_json(summary: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    return {
        "schema_version": "compa_skill_release_inventory_v1",
        "tag": args.output_dir.name,
        "name": "SkillBridge RQ3 Direct Single-Agent Baseline Outputs",
        "is_prerelease": True,
        "purpose": "Output packages from the no-SkillFab direct-agent RQ3 baseline",
        "summary": summary["headline_counts"],
        "entrypoints": {
            "results": "direct_agent_results.jsonl",
            "packages": "packages/",
            "package_manifest": "direct_agent_output_packages.jsonl",
        },
    }


def render_output_readme(summary: dict[str, Any]) -> str:
    counts = summary["headline_counts"]
    return f"""# RQ3 Direct Single-Agent Baseline Outputs

This directory contains direct-agent repaired packages for the RQ3 baseline.

Selected jobs: {counts['selected_jobs']}
Completed in this run: {counts['completed_this_run']}
Skipped existing: {counts['skipped_existing']}

The runner does not use SkillFab, does not use a ReviewAgent, and does not run
skill behavior or benchmark tasks. Reuse the fixed task manifest from the
baseline release to evaluate these packages.
"""


def create_tarball(output_dir: Path) -> Path:
    tar_path = output_dir.with_suffix(".tar.gz")
    if tar_path.exists():
        tar_path.unlink()
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(output_dir, arcname=output_dir.name)
    return tar_path


def repo_rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        try:
            return path.resolve().relative_to(WORKSPACE_ROOT).as_posix()
        except ValueError:
            return path.as_posix()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
