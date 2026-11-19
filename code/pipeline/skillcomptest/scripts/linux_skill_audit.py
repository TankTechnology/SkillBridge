#!/usr/bin/env python3
"""Audit one skill's Linux compatibility result from existing SkillTester data.

This module is intentionally read-only: it parses existing CSV/report evidence
and does not execute skill code, install dependencies, or run generated tasks.
"""

import argparse
import csv
import json
import os
import subprocess
import sys
from pathlib import Path


OS_RELEVANT_CATEGORIES = {
    "os_api_incompatible",
    "path_shell_assumption",
    "permission_model",
    "encoding_locale",
    "runtime_crash",
    "packaging_or_install",
}

ENVIRONMENT_CATEGORIES = {
    "missing_dependency",
    "missing_capability",
    "gui_or_browser_unavailable",
    "network_or_service",
}

MANUAL_REVIEW_CATEGORIES = {
    "logic_error",
    "unknown",
}


def parse_failure_categories(value):
    """Parse a semicolon-delimited category count string into a dict."""
    categories = {}
    if not value:
        return categories
    for part in value.split(";"):
        part = part.strip()
        if not part:
            continue
        name, _, raw_count = part.partition(":")
        name = name.strip()
        if not name:
            continue
        try:
            count = int(raw_count.strip()) if raw_count else 1
        except ValueError:
            count = 1
        categories[name] = categories.get(name, 0) + count
    return categories


def load_csv_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def split_skill_ref(skill_ref, source=None):
    if "/" in skill_ref and source is None:
        source_part, skill_part = skill_ref.split("/", 1)
        return source_part, skill_part
    return source, skill_ref


def _int(row, key):
    try:
        return int(row.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def audit_skill(rows, skill_ref, *, source=None, platform="linux"):
    """Return a single-skill audit dictionary for the requested platform."""
    requested_source, skill = split_skill_ref(skill_ref, source=source)
    matches = []
    for row in rows:
        if row.get("platform") != platform:
            continue
        if requested_source and row.get("source") != requested_source:
            continue
        if row.get("skill") == skill:
            matches.append(row)

    if not matches:
        return {
            "skill": skill,
            "source": requested_source or "",
            "platform": platform,
            "runs": 0,
            "tasks": 0,
            "pass": 0,
            "fail": 0,
            "blocked": 0,
            "unsupported": 0,
            "pending": 0,
            "unknown": 0,
            "pass_rate": 0.0,
            "failure_categories": {},
            "os_relevant_categories": {},
            "environment_categories": {},
            "manual_review_categories": {},
            "audit_verdict": "no_linux_result",
            "needs_migration_attention": False,
            "next_action": "No active Linux result was found for this skill.",
            "data_quality_notes": [],
        }

    totals = {
        "tasks": 0,
        "pass": 0,
        "fail": 0,
        "blocked": 0,
        "unsupported": 0,
        "pending": 0,
        "unknown": 0,
    }
    categories = {}
    runners = set()
    task_design_models = set()
    verdicts = set()
    sources = set()

    for row in matches:
        sources.add(row.get("source") or "")
        runners.add(row.get("runner") or "")
        task_design_models.add(row.get("task_design_model") or "")
        if row.get("portability_verdict"):
            verdicts.add(row["portability_verdict"])
        for key in totals:
            totals[key] += _int(row, key)
        for name, count in parse_failure_categories(row.get("top_failure_categories", "")).items():
            categories[name] = categories.get(name, 0) + count

    tasks = totals["tasks"]
    pass_rate = totals["pass"] / tasks if tasks else 0.0
    os_relevant = {k: v for k, v in categories.items() if k in OS_RELEVANT_CATEGORIES}
    environment = {k: v for k, v in categories.items() if k in ENVIRONMENT_CATEGORIES}
    manual_review = {k: v for k, v in categories.items() if k in MANUAL_REVIEW_CATEGORIES}
    data_quality_notes = []
    non_pass = tasks - totals["pass"]

    if totals["pending"] or totals["unknown"]:
        data_quality_notes.append("Run has pending or unknown task statuses.")
    if tasks == 0:
        data_quality_notes.append("Run has zero tasks in the active summary.")

    audit_verdict, needs_attention, next_action = _classify(
        totals, os_relevant, environment, manual_review, non_pass
    )

    return {
        "skill": skill,
        "source": sorted(s for s in sources if s) or ([requested_source] if requested_source else []),
        "platform": platform,
        "runs": len(matches),
        "task_design_models": sorted(m for m in task_design_models if m),
        "runners": sorted(r for r in runners if r),
        "tasks": tasks,
        "pass": totals["pass"],
        "fail": totals["fail"],
        "blocked": totals["blocked"],
        "unsupported": totals["unsupported"],
        "pending": totals["pending"],
        "unknown": totals["unknown"],
        "pass_rate": round(pass_rate, 4),
        "failure_categories": dict(sorted(categories.items())),
        "os_relevant_categories": dict(sorted(os_relevant.items())),
        "environment_categories": dict(sorted(environment.items())),
        "manual_review_categories": dict(sorted(manual_review.items())),
        "observed_portability_verdicts": sorted(verdicts),
        "matched_runs": [
            {
                "source": row.get("source") or "",
                "skill": row.get("skill") or skill,
                "task_design_model": row.get("task_design_model") or "",
                "runner": row.get("runner") or "",
            }
            for row in matches
        ],
        "audit_verdict": audit_verdict,
        "needs_migration_attention": needs_attention,
        "next_action": next_action,
        "data_quality_notes": data_quality_notes,
    }


def _classify(totals, os_relevant, environment, manual_review, non_pass):
    if totals["tasks"] == 0:
        return "data_quality_gap", False, "Inspect why the run has no classified tasks."
    if non_pass == 0:
        return (
            "compatible_in_current_tests",
            False,
            "No migration action is indicated by the current Linux run.",
        )
    if os_relevant:
        return (
            "linux_migration_candidate",
            True,
            "Review task evidence and consider creating a Compa-Skill analysis contract.",
        )
    if manual_review:
        return (
            "manual_review",
            True,
            "Inspect failing task evidence; logic/unknown failures may hide Linux-specific issues.",
        )
    if totals["fail"] == 0 and environment and sum(environment.values()) >= non_pass:
        return (
            "environment_blocked",
            False,
            "Treat as an environment or credential blocker unless task evidence shows an OS issue.",
        )
    if totals["unsupported"] > 0:
        return (
            "manual_review",
            True,
            "Unsupported tasks need evidence review before deciding whether migration is possible.",
        )
    return (
        "manual_review",
        True,
        "Inspect task evidence before deciding whether this is migration work or test noise.",
    )


def render_markdown(audit):
    categories = audit["failure_categories"] or {}
    lines = [
        f"# Linux Skill Audit Brief: {audit['skill']}",
        "",
        "> This is a rule-based brief for routing. Use `--claude` for the evidence-reading audit.",
        "",
        f"- Platform: `{audit['platform']}`",
        f"- Source: `{_format_value(audit['source'])}`",
        f"- Runs: {audit['runs']}",
        f"- Tasks: {audit['tasks']} total; pass={audit['pass']}, fail={audit['fail']}, "
        f"blocked={audit['blocked']}, unsupported={audit['unsupported']}",
        f"- Pass rate: {audit['pass_rate']:.1%}",
        f"- Audit verdict: `{audit['audit_verdict']}`",
        f"- Needs migration attention: `{str(audit['needs_migration_attention']).lower()}`",
        "",
        "## Failure Categories",
        "",
    ]
    if categories:
        for name, count in categories.items():
            lines.append(f"- `{name}`: {count}")
    else:
        lines.append("- None")

    lines.extend([
        "",
        "## Next Action",
        "",
        audit["next_action"],
    ])
    if audit["data_quality_notes"]:
        lines.extend(["", "## Data Quality Notes", ""])
        for note in audit["data_quality_notes"]:
            lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)


def find_evidence_paths(audit, *, data_store=None, repo_root=None, max_paths=120):
    """Return existing evidence paths Claude Code should inspect for this skill."""
    if data_store is None:
        repo_root = Path(repo_root) if repo_root else Path(__file__).resolve().parent.parent
        data_store = repo_root / ".cache" / "skillcomptest"
    data_store = Path(data_store)
    paths = []
    seen = set()

    def add(kind, path):
        path = Path(path)
        if not path.is_file():
            return
        resolved = path.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        try:
            relative = str(path.relative_to(data_store))
        except ValueError:
            relative = str(path)
        paths.append({
            "kind": kind,
            "path": str(path),
            "relative_path": relative,
        })

    runs = audit.get("matched_runs") or []
    if not runs and audit.get("source") and audit.get("skill"):
        sources = audit["source"] if isinstance(audit["source"], list) else [audit["source"]]
        runs = [
            {
                "source": source,
                "skill": audit["skill"],
                "task_design_model": "",
                "runner": "",
            }
            for source in sources if source
        ]

    for run in runs:
        source = run.get("source")
        skill = run.get("skill")
        design_model = run.get("task_design_model")
        runner = run.get("runner")
        if not source or not skill:
            continue

        exec_roots = []
        if design_model and runner:
            exec_roots.append(
                data_store / "exec" / audit["platform"] / source / skill / design_model / runner
            )
        exec_parent = data_store / "exec" / audit["platform"] / source / skill
        if exec_parent.is_dir():
            exec_roots.extend(p for p in exec_parent.glob("*/*") if p.is_dir())

        for exec_root in exec_roots:
            add("platform_report", exec_root / "platform_report.md")
            add("platform_summary", exec_root / "platform_summary.json")
            add("metrics", exec_root / "metrics.json")
            add("run_manifest", exec_root / "run_manifest.json")
            for task_dir in sorted((exec_root / "tasks").glob("T_*")):
                add("task_metrics", task_dir / "task_metrics.json")
                add("task_execution_log", task_dir / "execution.log")
                add("task_worklog", task_dir / "worklog.log")
                for result in sorted((task_dir / "results").glob("*")):
                    if result.is_file() and result.suffix.lower() in {".json", ".txt", ".md", ".log"}:
                        add("task_result", result)

        sample_roots = []
        if design_model:
            sample_roots.append(data_store / "samples" / source / skill / design_model)
        sample_parent = data_store / "samples" / source / skill
        if sample_parent.is_dir():
            sample_roots.extend(p for p in sample_parent.glob("*") if p.is_dir())

        for sample_root in sample_roots:
            add("benchmark_manifest", sample_root / "benchmark_manifest.json")
            add("samples_description", sample_root / "samples_description.md")
            for task_dir in sorted((sample_root / "tasks").glob("T_*")):
                add("task_description", task_dir / "task_description.md")
                add("spec_check", task_dir / "SpecCheck.md")

    return paths[:max_paths]


def build_claude_audit_prompt(audit, evidence_paths):
    """Build the prompt used by Claude Code for evidence-reading audit."""
    evidence_lines = "\n".join(
        f"- {item['kind']}: `{item['path']}`" for item in evidence_paths
    ) or "- No evidence paths were found."
    audit_json = json.dumps(audit, indent=2, ensure_ascii=False)
    return f"""You are LinuxSkillAuditAgent.

Your job is to read the existing execution evidence for exactly one skill and
write an audit note. The rule-based summary below is only a starting brief; it
is not the final diagnosis.

Do not execute skill code, generated task commands, setup/install commands,
package scripts, examples, or migrated skill validation. Do not edit files. Use
read-only inspection of the listed evidence files.

Keep the canonical failure taxonomy compatible with macOS/Windows results.
Do not invent new `failure_category` values for Linux-only nuances. If needed, use
these interpretation-only labels in prose:

- `guide_portability_gap`: the guide only documents macOS/Windows paths even
  though the feature could plausibly work on Linux.
- `os_dispatch_missing_linux`: the skill dispatches to macOS/Windows scripts
  but lacks a Linux branch for a portable feature.
- `script_platform_locked`: the script or binary is explicitly tied to another
  OS.
- `task_design_issue`: the benchmark task assumption, not the skill, caused the
  observed failure.

Skill summary:

```json
{audit_json}
```

Evidence files to inspect:

{evidence_lines}

Reading order:

1. Start with `task_metrics.json`, `benchmark_manifest.json`, task descriptions,
   and `SpecCheck.md`.
2. Read execution/work logs for non-pass tasks or tasks whose metrics are
   inconsistent with the summary.
3. Read pass-task logs only when needed for comparison.

Testing model:

- Tasks should come from the skill itself: `SKILL.md`, scripts, examples,
  declared capabilities, and existing usage guidance.
- Task execution should happen on the target OS.
- Task correctness should be judged by programmatic verifiers whenever possible:
  JSON/schema checks, file assertions, exit code, stdout regex, artifact diff,
  or custom checker.
- Your role is root-cause interpretation after results exist, not replacing the
  verifier's pass/fail judgment.

Audit questions:

1. Which task(s) failed, were blocked, or were unsupported?
2. Does the execution evidence show a Linux OS compatibility issue, or is it an
   environment/service/dependency/test-design issue?
3. If the CSV category says `logic_error` or `unknown`, does the task-level
   evidence reveal a more specific root cause?
4. Should this skill be handed to Compa-Skill for a migration analysis contract?
5. If the skill uses OS dispatch such as "mac script for macOS, Windows script
   for Windows", is there a Linux path for a feature that is plausibly portable?

Write exactly this markdown structure:

## Linux Skill Audit

skill:
source:
platform:
verdict: compatible_in_current_tests | linux_migration_candidate | manual_review | environment_blocked | no_linux_result | data_quality_gap
needs_migration_attention: true | false

## Evidence Read

List the specific files you read.

## Task Findings

Use one bullet per non-pass task. Include task id, status, category, and the
short evidence-based reason.

## Interpretation

Explain whether this is likely a Linux compatibility problem. Be conservative:
API keys, external services, missing installable dependencies, and missing GUI
or browser access are not migration issues unless the evidence shows an OS bug.

## Next Action

State whether to create a Compa-Skill analysis contract, mark as environment
blocked, or leave as compatible/no-action.

## Caveats

Mention missing evidence, inconsistent metrics, or anything that limits
confidence.
"""


def run_claude_audit(prompt, *, model=None, timeout_s=900, max_budget_usd=None):
    """Run Claude Code in read-only tool mode and return stdout."""
    cmd = [
        "claude",
        "-p",
        "--no-session-persistence",
        "--permission-mode",
        "dontAsk",
        "--tools",
        "Read,Glob,Grep",
    ]
    if model:
        cmd.extend(["--model", model])
    if max_budget_usd is not None:
        cmd.extend(["--max-budget-usd", str(max_budget_usd)])
    cmd.append(prompt)
    proc = subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        timeout=timeout_s,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "claude audit failed")
    return proc.stdout


def write_or_print(output, output_path=None):
    """Write output to a path, creating parents, or print to stdout."""
    if output_path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output, encoding="utf-8")
    else:
        sys.stdout.write(output)


def _format_value(value):
    if isinstance(value, list):
        return ", ".join(value) if value else ""
    return str(value)


def _default_csv_path():
    docs_dir = Path(__file__).resolve().parent.parent / "docs"
    candidates = sorted(docs_dir.glob("current-skillcomptest-test-runs-*.csv"))
    if not candidates:
        return docs_dir / "archive/project-notes-20260626/current-skillcomptest-test-runs-2026-06-09.csv"
    return candidates[-1]


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Audit one skill's Linux compatibility result without running skill code."
    )
    parser.add_argument("skill", help="Skill name or source/skill, e.g. ClawHub/browser-use-1.0.2")
    parser.add_argument("--source", help="Optional source filter, e.g. ClawHub")
    parser.add_argument("--platform", default="linux", help="Target platform to audit")
    parser.add_argument("--csv", default=str(_default_csv_path()), help="Current test-runs CSV path")
    parser.add_argument("--data-store", help="SkillTester data store path; defaults to .cache/skillcomptest")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--prompt-only", action="store_true", help="Print the Claude Code audit prompt")
    parser.add_argument("--claude", action="store_true", help="Run Claude Code to read evidence and audit")
    parser.add_argument(
        "--model",
        default=os.environ.get("CLAUDE_MODEL", "deepseek-v4-flash"),
        help="Claude Code model",
    )
    parser.add_argument("--timeout", type=int, default=900, help="Claude Code timeout in seconds")
    parser.add_argument("--max-budget-usd", type=float, help="Optional Claude Code print-mode budget cap")
    parser.add_argument("--output", help="Optional output file")
    args = parser.parse_args(argv)

    rows = load_csv_rows(args.csv)
    audit = audit_skill(rows, args.skill, source=args.source, platform=args.platform)
    evidence_paths = find_evidence_paths(audit, data_store=args.data_store)
    if args.prompt_only or args.claude:
        prompt = build_claude_audit_prompt(audit, evidence_paths)
        if args.prompt_only:
            output = prompt
        else:
            output = run_claude_audit(
                prompt,
                model=args.model,
                timeout_s=args.timeout,
                max_budget_usd=args.max_budget_usd,
            )
    elif args.format == "json":
        output = json.dumps(audit, indent=2, ensure_ascii=False) + "\n"
    else:
        output = render_markdown(audit)

    write_or_print(output, args.output)


if __name__ == "__main__":
    main()
