#!/usr/bin/env python3
"""SkillFab repair loop runner.

This script coordinates the repair workflow used by the OS-compatibility study:
create/link one SkillFab issue, run Claude Code as the developer agent, let the
default SkillFab review agent review the submission, and re-run Claude Code with
review feedback until the repair is approved, the agent gives up, or the round
limit is reached.

The runner never executes skill behavior. Agent prompts explicitly allow only
static validation such as syntax/package checks.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKSPACE_ROOT = REPO_ROOT.parent
DEFAULT_SKILLFAB_ROOT = DEFAULT_WORKSPACE_ROOT / "SkillFab"
DEFAULT_MANIFEST = REPO_ROOT / "docs/repair-workflow/skillfab-issue-manifest-20260627.jsonl"
DEFAULT_SOURCE_ROOT = DEFAULT_WORKSPACE_ROOT / "skillcomptest/.cache/skillcomptest/top5000-sources-v0.7.0"
DEFAULT_RUN_DIR = REPO_ROOT / ".agent-runs/skillfab-repair-loop"
DEFAULT_EVENTS = REPO_ROOT / "docs/repair-workflow/skillfab-tracking/events.jsonl"
DEFAULT_ROUNDS = REPO_ROOT / "docs/repair-workflow/skillfab-tracking/rounds.jsonl"
DEFAULT_CLAUDE_ENV = DEFAULT_WORKSPACE_ROOT / "skillcomptest/.env"
DEFAULT_TARGETS = REPO_ROOT / "docs/repair-workflow/skillfab-repair-targets-20260628.jsonl"
MAX_REPAIR_ROUNDS = 5
JSON_WRITE_LOCK = Lock()
CLAUDE_ENV_KEYS = {
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    "CLAUDE_SETTING_SOURCES",
    "CLAUDE_MODEL",
    "SAMPLE_CLAUDE_MODEL",
    "EXEC_CLAUDE_MODEL",
}
ACTIONABLE_NEXT_ACTIONS = {"run_first_repair_round", "run_next_repair_round", "resume_dev_agent_submission"}


@dataclass(frozen=True)
class RepairLoopState:
    round_index: int
    submission_status: str | None
    latest_review_result: str | None
    agent_blocked: bool = False


@dataclass(frozen=True)
class RepairLoopDecision:
    action: str
    reason: str
    next_round_index: int | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def load_developer_token(skillfab_root: Path) -> str:
    if os.environ.get("SKILLFAB_DEVELOPER_TOKEN"):
        return os.environ["SKILLFAB_DEVELOPER_TOKEN"]
    tokens = parse_env_file(skillfab_root / ".env.access-tokens")
    token = tokens.get("SKILLFAB_DEVELOPER_TOKEN")
    if not token:
        raise RuntimeError(f"SKILLFAB_DEVELOPER_TOKEN not found in {skillfab_root / '.env.access-tokens'}")
    return token


def build_claude_env(env_file: Path | None, requested_model: str | None) -> tuple[dict[str, str], str | None]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    loaded = parse_env_file(env_file) if env_file and env_file.exists() else {}
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


def resolved_claude_model_label(args: argparse.Namespace) -> str:
    env_file = Path(args.claude_env_file) if args.claude_env_file else None
    _, model = build_claude_env(env_file, args.claude_model)
    return model or "claude-code-default"


def review_agent_model_label(args: argparse.Namespace) -> str:
    return str(args.review_agent_model or "").strip() or "claude-code-default"


def review_agent_provider_label(args: argparse.Namespace) -> str:
    return str(args.review_agent_provider or "").strip() or "claude-code-configured"


def review_agent_name_label(args: argparse.Namespace, fallback: str | None = None) -> str:
    return str(args.review_agent_name or "").strip() or str(fallback or "").strip() or "claude-code-reviewer"


def redact_secrets(text: str, secrets: list[str] | tuple[str, ...]) -> str:
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "<redacted-token>")
    redacted = re.sub(r"https?://[^/\s@]+@", lambda m: re.sub(r"://.*@", "://<redacted>@", m.group(0)), redacted)
    redacted = re.sub(r"Bearer\s+[A-Za-z0-9._-]+", "Bearer <redacted-token>", redacted)
    redacted = re.sub(r"sk-[A-Za-z0-9]{12,}", "<redacted-token>", redacted)
    return redacted


def decide_next_repair_action(state: RepairLoopState, max_rounds: int = MAX_REPAIR_ROUNDS) -> RepairLoopDecision:
    status = (state.submission_status or "").lower()
    review = (state.latest_review_result or "").lower()
    if state.agent_blocked:
        return RepairLoopDecision(action="stop", reason="agent_blocked")
    if status in {"approved", "published"}:
        return RepairLoopDecision(action="complete", reason=status)
    if review == "approved":
        return RepairLoopDecision(action="complete", reason="approved")
    if review == "rejected" or status == "rejected":
        return RepairLoopDecision(action="stop", reason="review_rejected")
    if review in {"needs_work", "changes_requested"} or status == "needs_work":
        if state.round_index >= max_rounds:
            return RepairLoopDecision(action="stop", reason="max_rounds_reached")
        return RepairLoopDecision(action="continue_repair", reason="review_needs_work", next_round_index=state.round_index + 1)
    return RepairLoopDecision(action="wait_review", reason="review_pending")


def effective_review_result(latest_review: dict[str, Any], state: RepairLoopState) -> str | None:
    if (state.submission_status or "").lower() in {"approved", "published"}:
        return "approved"
    result = latest_review.get("review_result")
    if result:
        return result
    return None


def load_resume_context(rounds_path: Path, issue_key: str) -> dict[str, Any] | None:
    if not rounds_path.exists():
        return None
    rounds: list[dict[str, Any]] = []
    for line in rounds_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("issue_key") == issue_key:
            rounds.append(row)
    if not rounds:
        return None
    latest = sorted(rounds, key=lambda row: int(row.get("round_index") or 0))[-1]
    verdict = (latest.get("review") or {}).get("verdict")
    if verdict not in {"needs_work", "changes_requested"}:
        return None
    findings = (latest.get("review") or {}).get("findings") or []
    return {
        "issue_id": int(latest.get("skillfab_issue_id")),
        "submission_id": int(latest.get("skillfab_submission_id")),
        "previous_review_count": int(latest.get("round_index") or len(rounds)),
        "start_round": int(latest.get("round_index") or 0) + 1,
        "review_comment": "\n\n".join(str(item) for item in findings if item),
        "source": "rounds_jsonl",
    }


def target_resume_context(row: dict[str, Any]) -> dict[str, Any] | None:
    issue_id = row.get("skillfab_issue_id")
    if not issue_id:
        return None
    context: dict[str, Any] = {
        "issue_id": int(issue_id),
        "previous_review_count": 0,
        "start_round": 1,
        "review_comment": "",
        "source": "target_row",
    }
    submission_id = row.get("skillfab_submission_id")
    if submission_id:
        context["submission_id"] = int(submission_id)
    latest_review_result = row.get("latest_review_result") or ""
    latest_review_excerpt = row.get("latest_review_excerpt") or ""
    if latest_review_result in {"needs_work", "changes_requested"} and latest_review_excerpt:
        context["review_comment"] = latest_review_excerpt
    return context


def load_manifest(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def select_manifest_rows(rows: list[dict[str, Any]], issue_keys: list[str], limit: int | None) -> list[dict[str, Any]]:
    if issue_keys:
        wanted = set(issue_keys)
        selected = [row for row in rows if row.get("issue_key") in wanted]
        missing = wanted - {row.get("issue_key") for row in selected}
        if missing:
            raise RuntimeError(f"Manifest issue_key values not found: {', '.join(sorted(missing))}")
        return selected
    selected = [row for row in rows if row.get("open_skillfab_issue") is True]
    return selected[:limit] if limit else selected


def select_target_rows(rows: list[dict[str, Any]], issue_keys: list[str], limit: int | None) -> list[dict[str, Any]]:
    if issue_keys:
        wanted = set(issue_keys)
        selected = [row for row in rows if row.get("issue_key") in wanted]
        missing = wanted - {row.get("issue_key") for row in selected}
        if missing:
            raise RuntimeError(f"Target issue_key values not found: {', '.join(sorted(missing))}")
    else:
        selected = [row for row in rows if row.get("next_action") in ACTIONABLE_NEXT_ACTIONS]
    unsafe = [f"{row.get('issue_key')}:{row.get('next_action')}" for row in selected if row.get("next_action") not in ACTIONABLE_NEXT_ACTIONS]
    if unsafe:
        raise RuntimeError(f"Selected target rows are not actionable: {', '.join(sorted(unsafe))}")
    return selected[:limit] if limit else selected


def safe_append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with JSON_WRITE_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{id(value)}")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def repo_relative_path(path: Path) -> str:
    resolved = path if path.is_absolute() else (REPO_ROOT / path)
    try:
        return str(resolved.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def event_id(issue_key: str, event_type: str, round_index: int) -> str:
    names = {
        "issue_created": "ISS",
        "dev_round_started": "DEVSTART",
        "dev_round_completed": "DEV",
        "review_completed": "REV",
        "repair_stopped": "STOP",
    }
    return f"{issue_key}-R{round_index:02d}-{names.get(event_type, event_type.upper())}"


def build_issue_description(row: dict[str, Any]) -> str:
    return "\n".join(
        [
            "Repair target from Compa-Skill OS compatibility manifest.",
            "",
            f"Issue key: {row['issue_key']}",
            f"Skill package: {row['skill_id']}",
            f"Affected OS: {', '.join(row.get('affected_os', []))}",
            f"Repair OS: {', '.join(row.get('repair_os', []))}",
            "",
            f"Observed dynamic baseline: {row.get('pre_repair_baseline', {}).get('outcome_summary', 'unknown')}",
            "",
            "Problem summary:",
            row.get("issue_summary", ""),
            "",
            "Unified fix plan:",
            row.get("unified_fix_plan", ""),
            "",
            "Safety constraints: repair the package statically. Do not run the skill, scripts, installers, Docker, browser automation, or network tasks.",
        ]
    )


def build_acceptance_criteria(row: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"Preserve the original skill identity and package semantics for {row['skill_id']}.",
            f"Address the OS compatibility issue for: {', '.join(row.get('repair_os', []))}.",
            "Prefer portable logic, OS-specific branches, or accurate OS metadata/guide contracts over unrelated rewrites.",
            "Remove or avoid AppleDouble files, __MACOSX folders, raw credentials, and token-bearing URLs.",
            "Do not execute skill behavior; use static validation only, such as package inspection, markdown/frontmatter checks, bash -n, python py_compile, or node --check.",
            "Submit the repaired package through SkillFab and request review from the default review agent.",
        ]
    )


def build_claude_repair_prompt(
    manifest_row: dict[str, Any],
    issue_id: int,
    submission_id: int | None,
    round_index: int,
    source_dir: Path,
    review_comment: str | None = None,
    max_rounds: int = MAX_REPAIR_ROUNDS,
) -> str:
    review_block = ""
    if submission_id and review_comment:
        review_block = f"""
Reviewer feedback to address:
{review_comment}

Before editing, call get_review_history(submission_id={submission_id}) and use the latest feedback as the source of truth.
"""
    if submission_id:
        workflow = f"""
Required workflow for revision round:
1. Call get_workflow_state(issue_id={issue_id}) and get_review_history(submission_id={submission_id}).
2. Call get_git_push_url(submission_id={submission_id}) if you need to recover the repository URL.
3. Clone or update the existing submission repository in ./skill-work.
4. Apply only the reviewer-requested repair plus any directly required static cleanup.
5. Commit, push to the existing submission branch, call verify_push(submission_id={submission_id}), then request_review(submission_id={submission_id}, response_summary="<what changed>").
"""
    else:
        workflow = f"""
Required workflow for initial round:
1. Call get_workflow_state(issue_id={issue_id}) and get_issue_detail(issue_id={issue_id}).
2. Call submit_repo_to_issue(issue_id={issue_id}) to create or link the implementation repository.
3. Clone the returned push_url into ./skill-work. Do not print the push_url.
4. Copy the original package from the immutable source package into ./skill-work, excluding .git, node_modules, __MACOSX, and AppleDouble files named ._*.
5. Implement the smallest OS-compatibility repair that preserves the skill identity and semantics.
6. Commit, push, call verify_push, then request_review on the returned submission_id.
"""
    submission_json = str(submission_id) if submission_id else "null"
    return f"""You are the SkillFab developer agent for one OS compatibility repair, round {round_index} of {max_rounds}.

Hard boundaries:
- You are a developer agent only. Do not submit reviews, approve, publish, certify, or act as maintainer.
- Work only in the temporary working directory and the SkillFab repository returned by MCP.
- Do not edit SkillFab, skillcomptest, or Compa-Skill.
- Do not run the skill, its scripts, installers, Docker commands, browser automation, network tasks, or package manager installs.
- Static validation is allowed: inspect files, check frontmatter/metadata, bash -n, python3 -m py_compile, node --check, git diff --check.
- Never output secrets, raw push URLs, or token-bearing URLs.
- If you believe the repair is impossible or unsafe, stop and return JSON with "agent_blocked": true and a concise blocked_reason.

SkillFab issue:
- issue_id: {issue_id}
- existing submission_id: {submission_json}
- manifest issue_key: {manifest_row['issue_key']}
- skill package: {manifest_row['skill_id']}
- repair OS: {', '.join(manifest_row.get('repair_os', []))}

Original immutable package is available at:
{source_dir}

Observed problem:
{manifest_row.get('issue_summary', '')}

Unified fix plan:
{manifest_row.get('unified_fix_plan', '')}
{review_block}
{workflow}

Final answer must be one JSON object, no markdown fence:
{{
  "issue_key": "{manifest_row['issue_key']}",
  "issue_id": {issue_id},
  "submission_id": <number or null>,
  "commit_sha": "<sha or empty>",
  "requested_review": <true|false>,
  "agent_blocked": <true|false>,
  "blocked_reason": "<empty unless blocked>",
  "static_validation": {{
    "syntax_checks": "pass|fail|not_run",
    "git_diff_check": "pass|fail|not_run",
    "package_validation": "pass|fail|not_run"
  }},
  "summary": "<brief repair summary>"
}}
"""


class SkillFabClient:
    def __init__(self, app_url: str, token: str):
        self.app_url = app_url.rstrip("/")
        self.token = token

    def call(self, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments or {}},
                "id": 1,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.app_url}/mcp",
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"MCP {tool} HTTP {exc.code}: {body[:500]}") from exc
        text = data.get("result", {}).get("content", [{}])[0].get("text")
        if not text:
            raise RuntimeError(f"MCP {tool} returned no text: {str(data)[:500]}")
        if data.get("result", {}).get("isError"):
            raise RuntimeError(f"MCP {tool} failed: {text[:1000]}")
        return json.loads(text)


def write_mcp_config(path: Path, app_url: str, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    config = {
        "mcpServers": {
            "skillfab": {
                "type": "http",
                "transport": "http",
                "url": f"{app_url.rstrip('/')}/mcp",
                "headers": {"Authorization": f"Bearer {token}"},
            }
        }
    }
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def copy_source_snapshot(source_dir: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)

    def ignore(_dir: str, names: list[str]) -> set[str]:
        ignored = {".git", "node_modules", "__MACOSX"}
        ignored.update(name for name in names if name.startswith("._"))
        return ignored

    shutil.copytree(source_dir, dest, ignore=ignore)


def parse_claude_outer(stdout: str) -> dict[str, Any]:
    try:
        return json.loads(stdout.strip())
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}\s*$", stdout.strip())
        if not match:
            raise
        return json.loads(match.group(0))


def claude_usage_fields(outer: dict[str, Any]) -> dict[str, Any]:
    usage = outer.get("usage", {}) if isinstance(outer, dict) else {}
    return {
        "input_tokens": usage.get("input_tokens") or usage.get("input"),
        "output_tokens": usage.get("output_tokens") or usage.get("output"),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
        "total_cost_usd": outer.get("total_cost_usd") or outer.get("cost_usd") if isinstance(outer, dict) else None,
    }


def classify_claude_failure(outer: dict[str, Any] | None, returncode: int) -> str:
    if isinstance(outer, dict):
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
        if outer.get("is_error"):
            return "claude_api_error"
    return f"claude_exited_{returncode}"


def parse_claude_output(stdout: str) -> tuple[dict[str, Any], dict[str, Any]]:
    outer = parse_claude_outer(stdout)
    result: Any = outer.get("result") or outer.get("response") or outer.get("message") or outer
    if isinstance(result, str):
        text = result.strip()
        fenced = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
        if fenced:
            text = fenced.group(1).strip()
        elif "{" in text and "}" in text:
            text = text[text.index("{") : text.rindex("}") + 1]
        result = json.loads(text)
    if not isinstance(result, dict):
        raise RuntimeError("Claude output result was not a JSON object")
    return outer, result


def run_claude_agent(
    *,
    prompt: str,
    run_dir: Path,
    source_dir: Path,
    mcp_config: Path,
    token: str,
    timeout_seconds: int,
    max_budget_usd: str,
    claude_bin: str,
    claude_env_file: Path | None,
    claude_model: str | None,
) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    stdout_path = run_dir / "claude.stdout.json"
    stderr_path = run_dir / "claude.stderr.log"
    claude_env, resolved_model = build_claude_env(claude_env_file, claude_model)
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
        "--mcp-config",
        str(mcp_config.resolve()),
        "--add-dir",
        str(source_dir.resolve()),
    ]
    if resolved_model:
        args.extend(["--model", resolved_model])
    started = time.monotonic()
    try:
        completed = subprocess.run(
            args,
            input=prompt,
            text=True,
            cwd=run_dir,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
            env=claude_env,
        )
    except subprocess.TimeoutExpired as exc:
        duration = int(time.monotonic() - started)
        stdout_path.write_text(redact_secrets(str(exc.stdout or ""), [token]), encoding="utf-8")
        stderr_path.write_text(redact_secrets(str(exc.stderr or "agent timed out"), [token]), encoding="utf-8")
        return {
            "ok": False,
            "agent_blocked": True,
            "blocked_reason": "agent_timeout",
            "duration_seconds": duration,
            "input_tokens": None,
            "output_tokens": None,
            "cache_creation_input_tokens": None,
            "cache_read_input_tokens": None,
            "total_cost_usd": None,
        }
    duration = int(time.monotonic() - started)
    secrets = [token, claude_env.get("ANTHROPIC_AUTH_TOKEN", ""), claude_env.get("ANTHROPIC_API_KEY", "")]
    stdout_path.write_text(redact_secrets(completed.stdout, secrets), encoding="utf-8")
    stderr_path.write_text(redact_secrets(completed.stderr, secrets), encoding="utf-8")
    if completed.returncode != 0:
        outer: dict[str, Any] | None = None
        try:
            outer = parse_claude_outer(completed.stdout)
        except Exception:
            outer = None
        return {
            "ok": False,
            "agent_blocked": True,
            "blocked_reason": classify_claude_failure(outer, completed.returncode),
            "duration_seconds": duration,
            **claude_usage_fields(outer or {}),
        }
    outer, result = parse_claude_output(completed.stdout)
    return {
        "ok": True,
        "result": result,
        "duration_seconds": duration,
        **claude_usage_fields(outer),
    }


def latest_review_state(history: dict[str, Any]) -> RepairLoopState:
    latest = history.get("latest_review") or {}
    facts = history.get("facts") or {}
    obj = history.get("object") or {}
    return RepairLoopState(
        round_index=int(facts.get("review_count") or history.get("count") or 0),
        submission_status=facts.get("status") or obj.get("status"),
        latest_review_result=latest.get("review_result"),
        agent_blocked=False,
    )


def wait_for_review(
    client: SkillFabClient,
    submission_id: int,
    previous_review_count: int,
    timeout_seconds: int,
    poll_seconds: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = client.call("get_review_history", {"submission_id": submission_id})
        count = int(last.get("facts", {}).get("review_count") or last.get("count") or 0)
        status = (last.get("facts", {}).get("status") or last.get("object", {}).get("status") or "").lower()
        if count > previous_review_count or status in {"approved", "published", "rejected"}:
            return last
        time.sleep(poll_seconds)
    return last or client.call("get_review_history", {"submission_id": submission_id})


def create_or_reuse_issue(
    client: SkillFabClient,
    row: dict[str, Any],
    created_path: Path,
    events_path: Path,
    existing_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    issue_key = row["issue_key"]
    with JSON_WRITE_LOCK:
        created = read_json(created_path, {})
        if created.get(issue_key, {}).get("issue_id"):
            return created[issue_key]
        if existing_context and existing_context.get("issue_id"):
            created[issue_key] = {
                "issue_id": int(existing_context["issue_id"]),
                "skill_id": row["skill_id"],
                "target_os": row.get("repair_os", []),
                "created_at": utc_now(),
                "source": "tracking_resume",
            }
            write_json(created_path, created)
            return created[issue_key]
    now = utc_now()
    response = client.call(
        "create_issue",
        {
            "title": f"[AUTO-REPAIR][{issue_key}] {row['issue_title']}",
            "description": build_issue_description(row),
            "acceptance_criteria": build_acceptance_criteria(row),
        },
    )
    issue_id = int(response.get("issue_id") or response.get("facts", {}).get("issue_id"))
    with JSON_WRITE_LOCK:
        created = read_json(created_path, {})
        created[issue_key] = {
            "issue_id": issue_id,
            "skill_id": row["skill_id"],
            "target_os": row.get("repair_os", []),
            "created_at": now,
        }
        write_json(created_path, created)
    safe_append_jsonl(
        events_path,
        {
            "schema_version": "skillfab_interaction_event_v1",
            "event_id": event_id(issue_key, "issue_created", 0),
            "issue_key": issue_key,
            "skill_id": row["skill_id"],
            "event_type": "issue_created",
            "actor_role": "analysis_agent",
            "agent_name": "skillfab-repair-loop",
            "model": "",
            "api_provider": "local",
            "started_at": now,
            "ended_at": now,
            "duration_seconds": 0,
            "input_tokens": None,
            "output_tokens": None,
            "cache_creation_input_tokens": None,
            "cache_read_input_tokens": None,
            "total_cost_usd": None,
            "artifact_refs": [f"skillfab://issue/{issue_id}", f"docs/repair-workflow/skillfab-issue-manifest-20260627.jsonl#{issue_key}"],
            "summary": f"Created SkillFab issue #{issue_id} for automated repair loop.",
            "decision": "issue scoped",
            "secrets_redacted": True,
        },
    )
    return created[issue_key]


def baseline_counts(row: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    by_os = row.get("pre_repair_baseline", {}).get("by_os", {})
    for target_os in row.get("repair_os", []):
        counts = by_os.get(target_os, {}).get("task_counts")
        if counts:
            output[target_os] = counts
    return output


def run_one_skill(row: dict[str, Any], args: argparse.Namespace, token: str) -> dict[str, Any]:
    client = SkillFabClient(args.app_url, token)
    issue_key = row["issue_key"]
    dev_model = resolved_claude_model_label(args)
    review_model = review_agent_model_label(args)
    review_provider = review_agent_provider_label(args)
    combined_provider = f"claude-code-configured + {review_provider}"
    source_dir = Path(args.source_root) / row["skill_id"]
    if not (source_dir / "SKILL.md").exists():
        raise RuntimeError(f"Source package missing SKILL.md: {source_dir}")
    created_path = Path(args.run_dir) / "created-issues.json"
    results_path = Path(args.run_dir) / "repair-results.jsonl"
    resume_context = load_resume_context(Path(args.rounds_path), issue_key) or target_resume_context(row)
    issue_info = create_or_reuse_issue(client, row, created_path, Path(args.events_path), existing_context=resume_context)
    issue_id = int(issue_info["issue_id"])
    submission_id: int | None = resume_context.get("submission_id") if resume_context else None
    review_comment: str | None = resume_context.get("review_comment") if resume_context else None
    previous_review_count = int(resume_context.get("previous_review_count") or 0) if resume_context else 0
    start_round = int(resume_context.get("start_round") or 1) if resume_context else 1
    final_result: dict[str, Any] = {"issue_key": issue_key, "skill_id": row["skill_id"], "issue_id": issue_id}
    if submission_id:
        try:
            history = client.call("get_review_history", {"submission_id": submission_id})
            previous_review_count = max(
                previous_review_count,
                int(history.get("facts", {}).get("review_count") or history.get("count") or 0),
            )
            latest = history.get("latest_review") or {}
            if not review_comment and latest.get("review_result") in {"needs_work", "changes_requested"}:
                review_comment = latest.get("comment") or ""
        except Exception as exc:
            safe_append_jsonl(
                Path(args.events_path),
                {
                    "schema_version": "skillfab_interaction_event_v1",
                    "event_id": event_id(issue_key, "resume_context_warning", 0),
                    "issue_key": issue_key,
                    "skill_id": row["skill_id"],
                    "event_type": "human_note",
                    "actor_role": "runner",
                    "agent_name": "skillfab-repair-loop",
                    "model": "",
                    "api_provider": "local",
                    "started_at": utc_now(),
                    "ended_at": utc_now(),
                    "duration_seconds": 0,
                    "artifact_refs": [f"skillfab://issue/{issue_id}", f"skillfab://submission/{submission_id}"],
                    "summary": f"Could not refresh existing submission review history before repair: {str(exc)[:300]}",
                    "decision": "resume_context_warning",
                    "secrets_redacted": True,
                },
            )

    for round_index in range(start_round, args.max_rounds + 1):
        round_dir = Path(args.run_dir) / issue_key / f"round-{round_index:02d}"
        round_dir.mkdir(parents=True, exist_ok=True)
        snapshot_dir = round_dir / "source-snapshot"
        copy_source_snapshot(source_dir, snapshot_dir)
        mcp_config = round_dir / "mcp.skillfab.json"
        write_mcp_config(mcp_config, args.app_url, token)
        prompt = build_claude_repair_prompt(
            manifest_row=row,
            issue_id=issue_id,
            submission_id=submission_id,
            round_index=round_index,
            source_dir=snapshot_dir,
            review_comment=review_comment,
            max_rounds=args.max_rounds,
        )
        start_time = utc_now()
        safe_append_jsonl(
            Path(args.events_path),
            {
                "schema_version": "skillfab_interaction_event_v1",
                "event_id": event_id(issue_key, "dev_round_started", round_index),
                "issue_key": issue_key,
                "skill_id": row["skill_id"],
                "event_type": "dev_round_started",
                "actor_role": "dev_agent",
                "agent_name": "claude-code",
                "model": dev_model,
                "api_provider": "claude-code-configured",
                "started_at": start_time,
                "ended_at": start_time,
                "duration_seconds": 0,
                "artifact_refs": [f"skillfab://issue/{issue_id}", repo_relative_path(round_dir)],
                "summary": f"Started Claude Code repair round {round_index}.",
                "decision": "started",
                "secrets_redacted": True,
            },
        )
        agent = run_claude_agent(
            prompt=prompt,
            run_dir=round_dir,
            source_dir=snapshot_dir,
            mcp_config=mcp_config,
            token=token,
            timeout_seconds=args.agent_timeout_seconds,
            max_budget_usd=args.claude_max_budget_usd,
            claude_bin=args.claude_bin,
            claude_env_file=Path(args.claude_env_file) if args.claude_env_file else None,
            claude_model=args.claude_model,
        )
        end_time = utc_now()
        agent_result = agent.get("result", {}) if agent.get("ok") else {}
        agent_blocked = bool(agent.get("agent_blocked") or agent_result.get("agent_blocked"))
        if agent_blocked:
            stop_reason = str(agent_result.get("blocked_reason") or agent.get("blocked_reason") or "agent_blocked")
            final_result.update({"status": "stopped", "reason": stop_reason, "round_index": round_index})
            safe_append_jsonl(Path(args.events_path), stop_event(row, issue_id, round_index, stop_reason, end_time, agent=agent, started_at=start_time))
            safe_append_jsonl(
                Path(args.rounds_path),
                {
                    "schema_version": "skillfab_repair_round_v1",
                    "issue_key": issue_key,
                    "skill_id": row["skill_id"],
                    "round_index": round_index,
                    "target_os": row.get("repair_os", []),
                    "review_agent_used": True,
                    "dev_agent_model": dev_model,
                    "review_agent_model": review_model,
                    "api_provider": combined_provider,
                    "started_at": start_time,
                    "ended_at": end_time,
                    "wall_time_seconds": agent.get("duration_seconds"),
                    "input_tokens": agent.get("input_tokens"),
                    "output_tokens": agent.get("output_tokens"),
                    "cache_creation_input_tokens": agent.get("cache_creation_input_tokens"),
                    "cache_read_input_tokens": agent.get("cache_read_input_tokens"),
                    "total_cost_usd": agent.get("total_cost_usd"),
                    "skillfab_issue_id": str(issue_id),
                    "skillfab_submission_id": "",
                    "submission_commit": "",
                    "published_package_ref": "",
                    "static_validation": agent_result.get("static_validation") or {},
                    "review": {"verdict": "not_reviewed", "findings": [stop_reason]},
                    "pre_repair": baseline_counts(row),
                    "post_repair": None,
                    "task_reuse": {"same_task_ids": None, "same_task_samples": None},
                    "round_result": stop_reason,
                    "notes": f"Automated SkillFab repair loop stopped before review: {stop_reason}; no unsafe skill execution or post-repair dynamic re-evaluation was run.",
                },
            )
            break
        submission_id = int(agent_result.get("submission_id") or submission_id or 0)
        commit_sha = str(agent_result.get("commit_sha") or "")
        safe_append_jsonl(
            Path(args.events_path),
            {
                "schema_version": "skillfab_interaction_event_v1",
                "event_id": event_id(issue_key, "dev_round_completed", round_index),
                "issue_key": issue_key,
                "skill_id": row["skill_id"],
                "event_type": "dev_round_completed",
                "actor_role": "dev_agent",
                "agent_name": "claude-code",
                "model": dev_model,
                "api_provider": "claude-code-configured",
                "started_at": start_time,
                "ended_at": end_time,
                "duration_seconds": agent.get("duration_seconds"),
                "input_tokens": agent.get("input_tokens"),
                "output_tokens": agent.get("output_tokens"),
                "cache_creation_input_tokens": agent.get("cache_creation_input_tokens"),
                "cache_read_input_tokens": agent.get("cache_read_input_tokens"),
                "total_cost_usd": agent.get("total_cost_usd"),
                "artifact_refs": [f"skillfab://issue/{issue_id}", f"skillfab://submission/{submission_id}", f"commit:{commit_sha}", repo_relative_path(round_dir)],
                "summary": agent_result.get("summary", f"Claude Code completed repair round {round_index}."),
                "decision": "review_requested" if agent_result.get("requested_review") else "dev_completed",
                "secrets_redacted": True,
            },
        )
        if not submission_id:
            final_result.update({"status": "stopped", "reason": "missing_submission_id", "round_index": round_index})
            safe_append_jsonl(Path(args.events_path), stop_event(row, issue_id, round_index, "missing_submission_id", utc_now()))
            safe_append_jsonl(
                Path(args.rounds_path),
                {
                    "schema_version": "skillfab_repair_round_v1",
                    "issue_key": issue_key,
                    "skill_id": row["skill_id"],
                    "round_index": round_index,
                    "target_os": row.get("repair_os", []),
                    "review_agent_used": True,
                    "dev_agent_model": dev_model,
                    "review_agent_model": review_model,
                    "api_provider": combined_provider,
                    "started_at": start_time,
                    "ended_at": utc_now(),
                    "wall_time_seconds": agent.get("duration_seconds"),
                    "input_tokens": agent.get("input_tokens"),
                    "output_tokens": agent.get("output_tokens"),
                    "cache_creation_input_tokens": agent.get("cache_creation_input_tokens"),
                    "cache_read_input_tokens": agent.get("cache_read_input_tokens"),
                    "total_cost_usd": agent.get("total_cost_usd"),
                    "skillfab_issue_id": str(issue_id),
                    "skillfab_submission_id": "",
                    "submission_commit": commit_sha,
                    "published_package_ref": "",
                    "static_validation": agent_result.get("static_validation") or {},
                    "review": {"verdict": "not_reviewed", "findings": ["missing_submission_id"]},
                    "pre_repair": baseline_counts(row),
                    "post_repair": None,
                    "task_reuse": {"same_task_ids": None, "same_task_samples": None},
                    "round_result": "missing_submission_id",
                    "notes": "Automated SkillFab repair loop stopped because the Dev Agent did not return a valid submission_id.",
                },
            )
            break
        history = wait_for_review(client, submission_id, previous_review_count, args.review_wait_seconds, args.review_poll_seconds)
        latest = history.get("latest_review") or {}
        previous_review_count = int(history.get("facts", {}).get("review_count") or history.get("count") or previous_review_count)
        state = latest_review_state(history)
        state = RepairLoopState(round_index=round_index, submission_status=state.submission_status, latest_review_result=state.latest_review_result, agent_blocked=False)
        decision = decide_next_repair_action(state, args.max_rounds)
        review_comment = latest.get("comment")
        review_result = effective_review_result(latest, state)
        review_at = latest.get("created_at") or utc_now()
        if review_result:
            safe_append_jsonl(
                Path(args.events_path),
                {
                    "schema_version": "skillfab_interaction_event_v1",
                    "event_id": event_id(issue_key, "review_completed", round_index),
                    "issue_key": issue_key,
                    "skill_id": row["skill_id"],
                    "event_type": "review_completed",
                    "actor_role": "review_agent",
                    "agent_name": review_agent_name_label(args, latest.get("reviewer_name")),
                    "model": review_model,
                    "api_provider": review_provider,
                    "started_at": review_at,
                    "ended_at": review_at,
                    "duration_seconds": None,
                    "input_tokens": None,
                    "output_tokens": None,
                    "cache_creation_input_tokens": None,
                    "cache_read_input_tokens": None,
                    "total_cost_usd": None,
                    "artifact_refs": [f"skillfab://submission/{submission_id}", f"commit:{commit_sha}"],
                    "summary": f"{review_agent_name_label(args, latest.get('reviewer_name'))} review completed with verdict {review_result}.",
                    "decision": review_result,
                    "secrets_redacted": True,
                },
            )
        safe_append_jsonl(
            Path(args.rounds_path),
            {
                "schema_version": "skillfab_repair_round_v1",
                "issue_key": issue_key,
                "skill_id": row["skill_id"],
                "round_index": round_index,
                "target_os": row.get("repair_os", []),
                "review_agent_used": True,
                "dev_agent_model": dev_model,
                "review_agent_model": review_model,
                "api_provider": combined_provider,
                "started_at": start_time,
                "ended_at": review_at,
                "wall_time_seconds": agent.get("duration_seconds"),
                "input_tokens": agent.get("input_tokens"),
                "output_tokens": agent.get("output_tokens"),
                "cache_creation_input_tokens": agent.get("cache_creation_input_tokens"),
                "cache_read_input_tokens": agent.get("cache_read_input_tokens"),
                "total_cost_usd": agent.get("total_cost_usd"),
                "skillfab_issue_id": str(issue_id),
                "skillfab_submission_id": str(submission_id),
                "submission_commit": commit_sha,
                "published_package_ref": "",
                "static_validation": agent_result.get("static_validation") or {},
                "review": {"verdict": review_result or "pending", "findings": [review_comment] if review_comment else []},
                "pre_repair": baseline_counts(row),
                "post_repair": None,
                "task_reuse": {"same_task_ids": None, "same_task_samples": None},
                "round_result": "not_evaluated",
                "notes": f"Automated SkillFab repair loop round {round_index}; no unsafe skill execution or post-repair dynamic re-evaluation was run.",
            },
        )
        final_result.update({"submission_id": submission_id, "latest_review_result": review_result, "submission_status": state.submission_status})
        if decision.action == "complete":
            final_result.update({"status": "complete", "reason": decision.reason, "round_index": round_index})
            break
        if decision.action == "stop":
            final_result.update({"status": "stopped", "reason": decision.reason, "round_index": round_index})
            safe_append_jsonl(Path(args.events_path), stop_event(row, issue_id, round_index, decision.reason, utc_now()))
            break
        if decision.action == "wait_review":
            final_result.update({"status": "stopped", "reason": "review_timeout_or_pending", "round_index": round_index})
            safe_append_jsonl(Path(args.events_path), stop_event(row, issue_id, round_index, "review_timeout_or_pending", utc_now()))
            break
    else:
        final_result.update({"status": "stopped", "reason": "max_rounds_reached", "round_index": args.max_rounds})

    safe_append_jsonl(results_path, final_result)
    return final_result


def stop_event(
    row: dict[str, Any],
    issue_id: int,
    round_index: int,
    reason: str,
    now: str,
    *,
    agent: dict[str, Any] | None = None,
    started_at: str | None = None,
) -> dict[str, Any]:
    agent = agent or {}
    return {
        "schema_version": "skillfab_interaction_event_v1",
        "event_id": event_id(row["issue_key"], "repair_stopped", round_index),
        "issue_key": row["issue_key"],
        "skill_id": row["skill_id"],
        "event_type": "human_note",
        "actor_role": "system",
        "agent_name": "skillfab-repair-loop",
        "model": "",
        "api_provider": "local",
        "started_at": started_at or now,
        "ended_at": now,
        "duration_seconds": agent.get("duration_seconds", 0),
        "input_tokens": agent.get("input_tokens"),
        "output_tokens": agent.get("output_tokens"),
        "cache_creation_input_tokens": agent.get("cache_creation_input_tokens"),
        "cache_read_input_tokens": agent.get("cache_read_input_tokens"),
        "total_cost_usd": agent.get("total_cost_usd"),
        "artifact_refs": [f"skillfab://issue/{issue_id}"],
        "summary": f"Automated repair loop stopped: {reason}.",
        "decision": reason,
        "secrets_redacted": True,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run SkillFab repair loop with Claude dev agent and configurable review feedback.")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--targets-jsonl", default=str(DEFAULT_TARGETS))
    parser.add_argument("--source-root", default=str(DEFAULT_SOURCE_ROOT))
    parser.add_argument("--run-dir", default=str(DEFAULT_RUN_DIR))
    parser.add_argument("--events-path", default=str(DEFAULT_EVENTS))
    parser.add_argument("--rounds-path", default=str(DEFAULT_ROUNDS))
    parser.add_argument("--skillfab-root", default=str(DEFAULT_SKILLFAB_ROOT))
    parser.add_argument("--app-url", default=os.environ.get("SKILLFAB_APP_URL", "http://localhost:3000"))
    parser.add_argument("--issue-keys", nargs="*", default=[])
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--max-rounds", type=int, default=MAX_REPAIR_ROUNDS)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--agent-timeout-seconds", type=int, default=30 * 60)
    parser.add_argument("--review-wait-seconds", type=int, default=5 * 60)
    parser.add_argument("--review-poll-seconds", type=int, default=30)
    parser.add_argument("--claude-max-budget-usd", default="30")
    parser.add_argument("--claude-bin", default=os.environ.get("CLAUDE_BIN", "claude"))
    parser.add_argument("--claude-env-file", default=str(DEFAULT_CLAUDE_ENV))
    parser.add_argument("--claude-model", default=os.environ.get("CLAUDE_MODEL", ""))
    parser.add_argument("--review-agent-name", default=os.environ.get("SKILLFAB_REVIEW_AGENT_NAME", "claude-code-reviewer"))
    parser.add_argument("--review-agent-model", default=os.environ.get("SKILLFAB_REVIEW_AGENT_MODEL", "claude-code-default"))
    parser.add_argument("--review-agent-provider", default=os.environ.get("SKILLFAB_REVIEW_AGENT_PROVIDER", "claude-code-configured"))
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.max_rounds < 1:
        parser.error("--max-rounds must be positive")
    if args.max_rounds > MAX_REPAIR_ROUNDS:
        parser.error(f"--max-rounds cannot exceed {MAX_REPAIR_ROUNDS}")
    if args.targets_jsonl:
        rows = select_target_rows(load_manifest(Path(args.targets_jsonl)), args.issue_keys, args.limit)
    else:
        rows = select_manifest_rows(load_manifest(Path(args.manifest)), args.issue_keys, args.limit)
    if not rows:
        print("No manifest rows selected.", file=sys.stderr)
        return 1
    token = load_developer_token(Path(args.skillfab_root))
    Path(args.run_dir).mkdir(parents=True, exist_ok=True)
    print(f"Selected {len(rows)} skills; concurrency={args.concurrency}; max_rounds={args.max_rounds}")
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futures = {pool.submit(run_one_skill, row, args, token): row for row in rows}
        for future in as_completed(futures):
            row = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {"issue_key": row.get("issue_key"), "skill_id": row.get("skill_id"), "status": "error", "reason": str(exc)}
                safe_append_jsonl(Path(args.run_dir) / "repair-results.jsonl", result)
            results.append(result)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    summary = {
        "generated_at": utc_now(),
        "selected": len(rows),
        "complete": sum(1 for row in results if row.get("status") == "complete"),
        "stopped": sum(1 for row in results if row.get("status") == "stopped"),
        "error": sum(1 for row in results if row.get("status") == "error"),
    }
    write_json(Path(args.run_dir) / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if summary["error"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
