#!/usr/bin/env python3
"""Build a round-level SkillFab OS-repair inventory.

This script is metadata-only. It joins the canonical delivery manifest,
SkillFab repair tracking rows, interaction events, and SkillFab SQLite state.
It does not checkout, import, install, or execute repaired skill code.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = REPO_ROOT.parent
DEFAULT_DELIVERY = REPO_ROOT / "docs/repair-workflow/skillfab-os-repair-delivery-20260628.jsonl"
DEFAULT_TARGETS = REPO_ROOT / "docs/repair-workflow/skillfab-repair-targets-20260628.jsonl"
DEFAULT_ROUNDS = REPO_ROOT / "docs/repair-workflow/skillfab-tracking/rounds.jsonl"
DEFAULT_EVENTS = REPO_ROOT / "docs/repair-workflow/skillfab-tracking/events.jsonl"
DEFAULT_SKILLFAB_DB = WORKSPACE_ROOT / "SkillFab/data/skillfab.db"
DEFAULT_SKILLFAB_REPO_ROOT = WORKSPACE_ROOT / "SkillFab/skill-dev-repos"
DEFAULT_OUT_PREFIX = REPO_ROOT / "docs/repair-workflow/skillfab-os-repair-rounds-20260628"

TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])sk-[A-Za-z0-9_-]{20,}")

CSV_FIELDS = [
    "round_record_id",
    "record_source",
    "issue_key",
    "skill_id",
    "source",
    "slug",
    "target_os",
    "affected_os",
    "skillfab_issue_id",
    "skillfab_submission_id",
    "skillfab_repo_id",
    "skillfab_repo_name",
    "round_index",
    "round_attempt_index",
    "submission_commit",
    "candidate_package_ref",
    "candidate_package_available",
    "is_selected_delivery_issue",
    "is_selected_delivery_submission",
    "is_final_delivery_package",
    "matches_delivery_commit",
    "is_terminal_round_for_selected_issue",
    "delivery_effective_review_result",
    "delivery_effective_review_source",
    "review_verdict",
    "round_result",
    "review_agent_used",
    "dev_agent_model",
    "review_agent_model",
    "api_provider",
    "started_at",
    "ended_at",
    "wall_time_seconds",
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "total_cost_usd",
    "static_validation_status",
    "db_review_result",
    "db_review_created_at",
    "db_review_comment_id",
    "review_excerpt",
    "pre_repair_outcome_summary",
    "pre_repair_task_totals",
    "post_repair",
    "task_reuse",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delivery", type=Path, default=DEFAULT_DELIVERY)
    parser.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    parser.add_argument("--rounds", type=Path, default=DEFAULT_ROUNDS)
    parser.add_argument("--events", type=Path, default=DEFAULT_EVENTS)
    parser.add_argument("--skillfab-db", type=Path, default=DEFAULT_SKILLFAB_DB)
    parser.add_argument("--skillfab-repo-root", type=Path, default=DEFAULT_SKILLFAB_REPO_ROOT)
    parser.add_argument("--out-prefix", type=Path, default=DEFAULT_OUT_PREFIX)
    parser.add_argument(
        "--skip-commit-check",
        action="store_true",
        help="Do not verify whether candidate commits exist in SkillFab bare repos.",
    )
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: sanitize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        return TOKEN_RE.sub("[REDACTED_SK_TOKEN]", value)
    return value


def csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(sanitize(value), ensure_ascii=False, sort_keys=True)
    return str(sanitize(value))


def split_skill_id(skill_id: str) -> tuple[str, str]:
    if "/" not in skill_id:
        return "", skill_id
    source, slug = skill_id.split("/", 1)
    return source, slug


def read_submission_index(db_path: Path) -> dict[int, dict[str, Any]]:
    if not db_path.exists():
        return {}
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT
              ss.id AS submission_id,
              ss.issue_id,
              ss.repo_id,
              ss.status AS submission_status,
              ss.commit_sha AS latest_submission_commit,
              ss.git_branch,
              ss.created_at,
              ss.updated_at,
              ss.skill_id AS published_skill_id,
              rp.name AS repo_name,
              rp.default_branch,
              sk.name AS published_skill_name,
              sk.status AS published_skill_status,
              sk.certification_level
            FROM submissions ss
            LEFT JOIN repos rp ON rp.id = ss.repo_id
            LEFT JOIN skills sk ON sk.id = ss.skill_id
            """
        ).fetchall()
    finally:
        conn.close()
    return {int(row["submission_id"]): dict(row) for row in rows}


def read_review_indexes(db_path: Path) -> dict[str, dict[Any, dict[str, Any]]]:
    if not db_path.exists():
        return {"by_exact": {}, "by_submission": {}, "by_issue_round": {}}
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT
              id,
              issue_id,
              submission_id,
              review_result,
              commit_sha,
              review_round,
              created_at,
              comment
            FROM review_comments
            ORDER BY id ASC
            """
        ).fetchall()
    finally:
        conn.close()

    by_exact: dict[tuple[str, str, str], dict[str, Any]] = {}
    by_submission: dict[str, dict[str, Any]] = {}
    by_issue_round: dict[tuple[str, int], dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        issue_id = str(item.get("issue_id") or "")
        submission_id = str(item.get("submission_id") or "")
        commit_sha = str(item.get("commit_sha") or "")
        review_round = item.get("review_round")
        if issue_id and submission_id and commit_sha:
            by_exact[(issue_id, submission_id, commit_sha)] = item
        if submission_id:
            by_submission[submission_id] = item
        if issue_id and review_round:
            by_issue_round[(issue_id, int(review_round))] = item
    return {"by_exact": by_exact, "by_submission": by_submission, "by_issue_round": by_issue_round}


def match_db_review(
    issue_id: str,
    submission_id: str,
    commit_sha: str,
    round_index: int,
    review_indexes: dict[str, dict[Any, dict[str, Any]]],
) -> dict[str, Any] | None:
    if issue_id and submission_id and commit_sha:
        exact = review_indexes["by_exact"].get((issue_id, submission_id, commit_sha))
        if exact:
            return exact
    if issue_id and round_index:
        by_round = review_indexes["by_issue_round"].get((issue_id, round_index))
        if by_round:
            return by_round
    if submission_id:
        return review_indexes["by_submission"].get(submission_id)
    return None


def event_issue_id(row: dict[str, Any]) -> str:
    return artifact_value(row, "skillfab://issue/")


def event_submission_id(row: dict[str, Any]) -> str:
    return artifact_value(row, "skillfab://submission/")


def event_commit(row: dict[str, Any]) -> str:
    return artifact_value(row, "commit:")


def artifact_value(row: dict[str, Any], prefix: str) -> str:
    for ref in row.get("artifact_refs") or []:
        text = str(ref)
        if text.startswith(prefix):
            return text[len(prefix) :]
    return ""


def event_round_index(row: dict[str, Any]) -> int:
    text = str(row.get("event_id") or "")
    if "-R" not in text:
        return 0
    try:
        return int(text.split("-R", 1)[1].split("-", 1)[0])
    except (IndexError, ValueError):
        return 0


def normalize_round_row(row: dict[str, Any]) -> dict[str, Any]:
    review = row.get("review") or {}
    return {
        "record_source": "rounds_jsonl",
        "issue_key": str(row.get("issue_key") or ""),
        "skill_id": str(row.get("skill_id") or ""),
        "target_os": row.get("target_os") or [],
        "skillfab_issue_id": str(row.get("skillfab_issue_id") or ""),
        "skillfab_submission_id": str(row.get("skillfab_submission_id") or ""),
        "round_index": int(row.get("round_index") or 0),
        "submission_commit": str(row.get("submission_commit") or ""),
        "review_verdict": str(review.get("verdict") or ""),
        "round_result": str(row.get("round_result") or ""),
        "review_agent_used": row.get("review_agent_used"),
        "dev_agent_model": str(row.get("dev_agent_model") or ""),
        "review_agent_model": str(row.get("review_agent_model") or ""),
        "api_provider": str(row.get("api_provider") or ""),
        "started_at": str(row.get("started_at") or ""),
        "ended_at": str(row.get("ended_at") or ""),
        "wall_time_seconds": row.get("wall_time_seconds"),
        "input_tokens": row.get("input_tokens"),
        "output_tokens": row.get("output_tokens"),
        "cache_creation_input_tokens": row.get("cache_creation_input_tokens"),
        "cache_read_input_tokens": row.get("cache_read_input_tokens"),
        "total_cost_usd": row.get("total_cost_usd"),
        "static_validation": row.get("static_validation") or {},
        "review_excerpt": review_excerpt(review),
        "pre_repair": row.get("pre_repair"),
        "post_repair": row.get("post_repair"),
        "task_reuse": row.get("task_reuse"),
    }


def event_fallback_rows(events: list[dict[str, Any]], existing_pairs: set[tuple[str, str, int]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in events:
        if row.get("event_type") not in {"dev_round_completed", "review_completed"}:
            continue
        issue_key = str(row.get("issue_key") or "")
        round_index = event_round_index(row)
        if not issue_key or round_index <= 0:
            continue
        groups[(issue_key, round_index)].append(row)

    rows = []
    for (issue_key, round_index), group in sorted(groups.items()):
        issue_id = next((event_issue_id(row) for row in group if event_issue_id(row)), "")
        if not issue_id or (issue_key, issue_id, round_index) in existing_pairs:
            continue
        dev_events = [row for row in group if row.get("event_type") == "dev_round_completed"]
        review_events = [row for row in group if row.get("event_type") == "review_completed"]
        primary = dev_events[-1] if dev_events else group[-1]
        review = review_events[-1] if review_events else {}
        rows.append(
            {
                "record_source": "events_jsonl_fallback",
                "issue_key": issue_key,
                "skill_id": str(primary.get("skill_id") or review.get("skill_id") or ""),
                "target_os": [],
                "skillfab_issue_id": issue_id,
                "skillfab_submission_id": event_submission_id(primary) or event_submission_id(review),
                "round_index": round_index,
                "submission_commit": event_commit(primary) or event_commit(review),
                "review_verdict": str(review.get("decision") or ""),
                "round_result": "not_evaluated",
                "review_agent_used": bool(review_events),
                "dev_agent_model": str(primary.get("model") or ""),
                "review_agent_model": str(review.get("model") or ""),
                "api_provider": "+".join(sorted({str(row.get("api_provider") or "") for row in group if row.get("api_provider")})),
                "started_at": min(str(row.get("started_at") or "") for row in group if row.get("started_at")),
                "ended_at": max(str(row.get("ended_at") or "") for row in group if row.get("ended_at")),
                "wall_time_seconds": sum_numeric(row.get("duration_seconds") for row in group),
                "input_tokens": sum_numeric(row.get("input_tokens") for row in group),
                "output_tokens": sum_numeric(row.get("output_tokens") for row in group),
                "cache_creation_input_tokens": sum_numeric(row.get("cache_creation_input_tokens") for row in group),
                "cache_read_input_tokens": sum_numeric(row.get("cache_read_input_tokens") for row in group),
                "total_cost_usd": sum_numeric(row.get("total_cost_usd") for row in group),
                "static_validation": {},
                "review_excerpt": review.get("summary") or primary.get("summary") or "",
                "pre_repair": None,
                "post_repair": None,
                "task_reuse": None,
            }
        )
    return rows


def db_delivery_fallback_rows(
    delivery_rows: list[dict[str, Any]],
    normalized_rows: list[dict[str, Any]],
    submissions: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    existing_commits = {
        (row.get("issue_key"), str(row.get("skillfab_issue_id") or ""), str(row.get("submission_commit") or ""))
        for row in normalized_rows
        if row.get("issue_key") and row.get("submission_commit")
    }
    rows = []
    for delivery in delivery_rows:
        issue_key = str(delivery.get("issue_key") or "")
        issue_id = str(delivery.get("skillfab_issue_id") or "")
        submission_id = str(delivery.get("skillfab_submission_id") or "")
        commit_sha = str(delivery.get("latest_commit_sha") or "")
        if not issue_key or not issue_id or not submission_id or not commit_sha:
            continue
        if (issue_key, issue_id, commit_sha) in existing_commits:
            continue
        submission = submissions.get(int(submission_id)) if submission_id.isdigit() else {}
        rows.append(
            {
                "record_source": "skillfab_db_delivery_fallback",
                "issue_key": issue_key,
                "skill_id": str(delivery.get("skill_id") or ""),
                "target_os": delivery.get("repair_os") or [],
                "skillfab_issue_id": issue_id,
                "skillfab_submission_id": submission_id,
                "round_index": int(delivery.get("repair_round_count") or 0),
                "submission_commit": commit_sha,
                "review_verdict": str(delivery.get("effective_review_result") or ""),
                "round_result": "not_evaluated",
                "review_agent_used": bool(delivery.get("effective_review_source")),
                "dev_agent_model": "",
                "review_agent_model": str(delivery.get("effective_review_source") or ""),
                "api_provider": "skillfab-db",
                "started_at": str(submission.get("updated_at") or submission.get("created_at") or ""),
                "ended_at": str(submission.get("updated_at") or submission.get("created_at") or ""),
                "wall_time_seconds": None,
                "input_tokens": None,
                "output_tokens": None,
                "cache_creation_input_tokens": None,
                "cache_read_input_tokens": None,
                "total_cost_usd": None,
                "static_validation": {},
                "review_excerpt": "Final delivered commit recovered from SkillFab DB because tracking rows did not contain this commit.",
                "pre_repair": None,
                "post_repair": None,
                "task_reuse": None,
            }
        )
    return rows


def sum_numeric(values: Any) -> int | float | None:
    total: int | float = 0
    seen = False
    for value in values:
        if isinstance(value, (int, float)):
            total += value
            seen = True
    return total if seen else None


def review_excerpt(review: dict[str, Any]) -> str:
    findings = review.get("findings") or []
    if isinstance(findings, list) and findings:
        return str(findings[0])[:700]
    if isinstance(findings, str):
        return findings[:700]
    for key in ("comment", "summary", "verdict"):
        if review.get(key):
            return str(review[key])[:700]
    return ""


def static_validation_status(static_validation: dict[str, Any]) -> str:
    if not static_validation:
        return ""
    values = [str(value) for value in static_validation.values() if value is not None]
    if any(value.startswith("fail") for value in values):
        return "fail"
    if any(value.startswith("unknown") or value == "not_run" for value in values):
        return "unknown"
    if any(value.startswith("pass") for value in values):
        return "pass"
    return ",".join(sorted(set(values)))[:120]


def package_ref(repo_id: Any, commit_sha: str) -> str:
    if not repo_id or not commit_sha:
        return ""
    return f"SkillFab/skill-dev-repos/{repo_id}.git@{commit_sha}"


def commit_available(repo_root: Path, repo_id: Any, commit_sha: str, skip: bool) -> bool | None:
    if skip or not repo_id or not commit_sha:
        return None
    repo_path = repo_root / f"{repo_id}.git"
    if not repo_path.exists():
        return False
    result = subprocess.run(
        ["git", f"--git-dir={repo_path}", "cat-file", "-e", f"{commit_sha}^{{commit}}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def build_round_records(
    delivery_path: Path,
    targets_path: Path,
    rounds_path: Path,
    events_path: Path,
    db_path: Path,
    repo_root: Path,
    skip_commit_check: bool = False,
) -> list[dict[str, Any]]:
    delivery_rows = load_jsonl(delivery_path)
    target_rows = load_jsonl(targets_path)
    round_rows = load_jsonl(rounds_path)
    event_rows = load_jsonl(events_path)
    delivery_by_key = {str(row.get("issue_key")): row for row in delivery_rows}
    target_by_key = {str(row.get("issue_key")): row for row in target_rows}
    submissions = read_submission_index(db_path)
    reviews = read_review_indexes(db_path)

    normalized = [normalize_round_row(row) for row in round_rows]
    existing_pairs = {
        (row["issue_key"], row["skillfab_issue_id"], row["round_index"])
        for row in normalized
        if row["issue_key"] and row["skillfab_issue_id"] and row["round_index"] > 0
    }
    normalized.extend(event_fallback_rows(event_rows, existing_pairs))
    normalized.extend(db_delivery_fallback_rows(delivery_rows, normalized, submissions))
    normalized.sort(key=round_sort_key)

    attempt_counts: dict[tuple[str, str, int], int] = defaultdict(int)
    records = []
    for item in normalized:
        issue_key = item["issue_key"]
        delivery = delivery_by_key.get(issue_key, {})
        target = target_by_key.get(issue_key, {})
        skill_id = item["skill_id"] or str(delivery.get("skill_id") or target.get("skill_id") or "")
        source, slug = split_skill_id(skill_id)
        issue_id = item["skillfab_issue_id"]
        submission_id = item["skillfab_submission_id"]
        submission = submissions.get(int(submission_id)) if submission_id.isdigit() else {}
        commit_sha = item["submission_commit"] or str(submission.get("latest_submission_commit") or "")
        db_review = match_db_review(issue_id, submission_id, commit_sha, item["round_index"], reviews)
        db_review_result = str((db_review or {}).get("review_result") or "")
        review_verdict = item["review_verdict"] or db_review_result
        attempt_key = (issue_key, issue_id, item["round_index"])
        attempt_counts[attempt_key] += 1
        round_attempt_index = attempt_counts[attempt_key]
        selected_issue_id = str(delivery.get("skillfab_issue_id") or "")
        selected_submission_id = str(delivery.get("skillfab_submission_id") or "")
        selected_round_count = delivery.get("repair_round_count")
        repo_id = submission.get("repo_id")
        record = {
            "schema_version": "skillfab_repair_round_record_v1",
            "round_record_id": f"{issue_key}-I{issue_id or 'NA'}-R{item['round_index']:02d}-A{round_attempt_index:02d}",
            "record_source": item["record_source"],
            "issue_key": issue_key,
            "skill_id": skill_id,
            "source": source or str(delivery.get("source") or target.get("source") or ""),
            "slug": slug or str(delivery.get("slug") or target.get("slug") or ""),
            "target_os": item["target_os"] or delivery.get("repair_os") or target.get("repair_os") or [],
            "affected_os": delivery.get("affected_os") or target.get("affected_os") or [],
            "skillfab_issue_id": int(issue_id) if issue_id.isdigit() else issue_id,
            "skillfab_submission_id": int(submission_id) if submission_id.isdigit() else submission_id,
            "skillfab_repo_id": repo_id,
            "skillfab_repo_name": submission.get("repo_name") or "",
            "round_index": item["round_index"],
            "round_attempt_index": round_attempt_index,
            "submission_commit": commit_sha,
            "candidate_package_ref": package_ref(repo_id, commit_sha),
            "candidate_package_available": commit_available(repo_root, repo_id, commit_sha, skip_commit_check),
            "is_selected_delivery_issue": bool(issue_id and issue_id == selected_issue_id),
            "is_selected_delivery_submission": bool(submission_id and submission_id == selected_submission_id),
            "is_final_delivery_package": False,
            "matches_delivery_commit": bool(commit_sha and commit_sha == str(delivery.get("latest_commit_sha") or "")),
            "is_terminal_round_for_selected_issue": bool(
                issue_id
                and issue_id == selected_issue_id
                and selected_round_count is not None
                and item["round_index"] == int(selected_round_count)
            ),
            "delivery_effective_review_result": str(delivery.get("effective_review_result") or ""),
            "delivery_effective_review_source": str(delivery.get("effective_review_source") or ""),
            "review_verdict": review_verdict,
            "round_result": item["round_result"],
            "review_agent_used": item["review_agent_used"],
            "dev_agent_model": item["dev_agent_model"],
            "review_agent_model": item["review_agent_model"],
            "api_provider": item["api_provider"],
            "started_at": item["started_at"],
            "ended_at": item["ended_at"],
            "wall_time_seconds": item["wall_time_seconds"],
            "input_tokens": item["input_tokens"],
            "output_tokens": item["output_tokens"],
            "cache_creation_input_tokens": item["cache_creation_input_tokens"],
            "cache_read_input_tokens": item["cache_read_input_tokens"],
            "total_cost_usd": item["total_cost_usd"],
            "static_validation_status": static_validation_status(item["static_validation"]),
            "static_validation": item["static_validation"],
            "db_review_result": db_review_result,
            "db_review_created_at": str((db_review or {}).get("created_at") or ""),
            "db_review_comment_id": (db_review or {}).get("id"),
            "review_excerpt": (item["review_excerpt"] or str((db_review or {}).get("comment") or "")[:700]),
            "pre_repair_outcome_summary": delivery.get("pre_repair_outcome_summary")
            or (target.get("pre_repair_baseline") or {}).get("outcome_summary", ""),
            "pre_repair_task_totals": delivery.get("pre_repair_task_totals")
            or (target.get("pre_repair_baseline") or {}).get("task_totals")
            or {},
            "post_repair": item["post_repair"],
            "task_reuse": item["task_reuse"],
        }
        records.append(sanitize(record))
    mark_final_delivery_packages(records, delivery_by_key)
    return records


def mark_final_delivery_packages(records: list[dict[str, Any]], delivery_by_key: dict[str, dict[str, Any]]) -> None:
    by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        row["is_final_delivery_package"] = False
        by_key[str(row.get("issue_key") or "")].append(row)
    for issue_key, delivery in delivery_by_key.items():
        latest_commit = str(delivery.get("latest_commit_sha") or "")
        if not latest_commit:
            continue
        candidates = [row for row in by_key.get(issue_key, []) if row.get("submission_commit") == latest_commit]
        if not candidates:
            continue
        selected_issue_id = str(delivery.get("skillfab_issue_id") or "")
        selected_submission_id = str(delivery.get("skillfab_submission_id") or "")
        best = sorted(
            candidates,
            key=lambda row: (
                str(row.get("skillfab_issue_id") or "") == selected_issue_id,
                str(row.get("skillfab_submission_id") or "") == selected_submission_id,
                row.get("review_verdict") == "approved",
                row.get("record_source") == "skillfab_db_delivery_fallback",
                int(row.get("round_index") or 0),
                int(row.get("round_attempt_index") or 0),
            ),
        )[-1]
        best["is_final_delivery_package"] = True


def round_sort_key(row: dict[str, Any]) -> tuple[str, int, int, str, str, str]:
    issue_id = row.get("skillfab_issue_id") or "0"
    issue_id_num = int(issue_id) if str(issue_id).isdigit() else 0
    return (
        str(row.get("issue_key") or ""),
        issue_id_num,
        int(row.get("round_index") or 0),
        timestamp_sort_value(row.get("started_at")),
        timestamp_sort_value(row.get("ended_at")),
        str(row.get("submission_commit") or ""),
    )


def timestamp_sort_value(value: Any) -> str:
    return str(value or "9999-99-99T99:99:99")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: csv_value(row.get(field)) for field in CSV_FIELDS})


def write_summary(path: Path, rows: list[dict[str, Any]], delivery_rows: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, Any]:
    issue_keys_with_records = {row["issue_key"] for row in rows}
    delivery_issue_keys = {row["issue_key"] for row in delivery_rows}
    by_key_pairs: dict[str, set[tuple[Any, Any]]] = defaultdict(set)
    issue_ids_by_key: dict[str, set[Any]] = defaultdict(set)
    for row in rows:
        by_key_pairs[row["issue_key"]].add((row.get("skillfab_issue_id"), row.get("round_index")))
        if row.get("skillfab_issue_id"):
            issue_ids_by_key[row["issue_key"]].add(row.get("skillfab_issue_id"))
    total_round_counts = Counter(str(len(by_key_pairs[row["issue_key"]])) for row in delivery_rows)
    final_package_records = [row for row in rows if row["is_final_delivery_package"]]
    summary = {
        "schema_version": "skillfab_repair_round_summary_v1",
        "generated_at": "2026-06-28",
        "unit": "one recorded repair round attempt or event fallback",
        "delivery_manifest": rel(args.delivery),
        "rounds_jsonl": rel(args.rounds),
        "events_jsonl": rel(args.events),
        "skillfab_db": rel(args.skillfab_db),
        "delivery_skill_count": len(delivery_rows),
        "round_record_count": len(rows),
        "skill_count_with_round_records": len(issue_keys_with_records),
        "delivery_skills_missing_round_records": sorted(delivery_issue_keys - issue_keys_with_records),
        "record_source_counts": dict(sorted(Counter(row["record_source"] for row in rows).items())),
        "review_verdict_counts": dict(sorted(Counter(row["review_verdict"] or "missing" for row in rows).items())),
        "round_result_counts": dict(sorted(Counter(row["round_result"] or "missing" for row in rows).items())),
        "round_index_counts": dict(sorted(Counter(str(row["round_index"]) for row in rows).items(), key=lambda item: int(item[0]))),
        "selected_delivery_issue_round_counts": dict(
            sorted(
                Counter(str(row.get("repair_round_count") or "missing") for row in delivery_rows).items(),
                key=lambda item: (item[0] == "missing", int(item[0]) if item[0].isdigit() else 999),
            )
        ),
        "total_skill_round_counts": dict(
            sorted(total_round_counts.items(), key=lambda item: (item[0] == "missing", int(item[0]) if item[0].isdigit() else 999))
        ),
        "skills_with_multiple_recorded_rounds": sum(1 for count in total_round_counts.elements() if int(count) >= 2),
        "skills_with_duplicate_or_followup_issues": sorted(key for key, issue_ids in issue_ids_by_key.items() if len(issue_ids) >= 2),
        "final_delivery_package_record_count": len(final_package_records),
        "final_delivery_package_missing_issue_keys": sorted(delivery_issue_keys - {row["issue_key"] for row in final_package_records}),
        "candidate_package_available_counts": dict(
            sorted(Counter(str(row["candidate_package_available"]) for row in rows).items())
        ),
        "cost_observed_record_count": sum(1 for row in rows if row.get("total_cost_usd") is not None),
        "cost_observed_total_usd": round(sum(float(row.get("total_cost_usd") or 0) for row in rows), 6),
        "token_observed_record_count": sum(1 for row in rows if row.get("input_tokens") is not None or row.get("output_tokens") is not None),
        "notes": [
            "Rows are metadata for repair candidates and review outcomes; repaired skill code was not executed.",
            "record_source=events_jsonl_fallback is used when a tracked event round is missing from rounds.jsonl.",
            "is_final_delivery_package identifies the commit currently delivered for cross-OS retesting.",
            "is_terminal_round_for_selected_issue identifies the maximum recorded round index for the selected SkillFab issue; it can differ from the final package when post-publish review certified an earlier commit.",
        ],
    }
    path.write_text(json.dumps(sanitize(summary), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def write_markdown(path: Path, rows: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    multi_round = Counter()
    final_rows_by_key = {}
    for row in rows:
        if row["is_final_delivery_package"]:
            final_rows_by_key[row["issue_key"]] = row
    by_key = defaultdict(list)
    for row in rows:
        by_key[row["issue_key"]].append(row)
    for issue_key, items in by_key.items():
        if len({(row["skillfab_issue_id"], row["round_index"]) for row in items}) >= 2:
            multi_round[issue_key] += 1

    lines = [
        "# SkillFab OS Repair Round Inventory 2026-06-28",
        "",
        "This file summarizes per-round SkillFab repair metadata for the 153 cross-OS repair targets.",
        "It does not contain post-repair dynamic execution results, and no repaired skill code was executed while generating it.",
        "",
        "## Summary",
        "",
        f"- Delivery skills: {summary['delivery_skill_count']}",
        f"- Round records: {summary['round_record_count']}",
        f"- Skills with round records: {summary['skill_count_with_round_records']}",
        f"- Missing round records: `{json.dumps(summary['delivery_skills_missing_round_records'], ensure_ascii=False)}`",
        f"- Record sources: `{json.dumps(summary['record_source_counts'], ensure_ascii=False, sort_keys=True)}`",
        f"- Review verdicts: `{json.dumps(summary['review_verdict_counts'], ensure_ascii=False, sort_keys=True)}`",
        f"- Selected delivery issue rounds: `{json.dumps(summary['selected_delivery_issue_round_counts'], ensure_ascii=False, sort_keys=True)}`",
        f"- Total skill rounds: `{json.dumps(summary['total_skill_round_counts'], ensure_ascii=False, sort_keys=True)}`",
        f"- Final delivery package records: {summary['final_delivery_package_record_count']}",
        f"- Candidate package availability: `{json.dumps(summary['candidate_package_available_counts'], ensure_ascii=False, sort_keys=True)}`",
        f"- Observed cost total: ${summary['cost_observed_total_usd']}",
        "",
        "## How To Use",
        "",
        "- Use rows with `is_final_delivery_package=true` to retest the delivered package.",
        "- Use earlier rows for the same `issue_key` to compare intermediate repair rounds.",
        "- Use `candidate_package_ref` to locate the bare SkillFab repo and commit.",
        "- `review_verdict` is the verdict for that specific row; `delivery_effective_review_result/source` is the final handoff gate for the delivered package.",
        "- Do not treat `not_evaluated` as a post-repair task result; it means dynamic re-evaluation has not been linked yet.",
        "",
        "## Skills With Multiple Recorded Rounds",
        "",
        "| issue_key | skill | recorded packages | final package ref | final gate | round verdict |",
        "|---|---|---:|---|---|---|",
    ]
    for issue_key in sorted(multi_round):
        items = by_key[issue_key]
        final = final_rows_by_key.get(issue_key, {})
        skill_id = items[0]["skill_id"]
        package_count = len({row["candidate_package_ref"] for row in items if row["candidate_package_ref"]})
        lines.append(
            f"| `{issue_key}` | `{skill_id}` | {package_count} | "
            f"`{final.get('candidate_package_ref') or ''}` | "
            f"`{final.get('delivery_effective_review_result') or ''}` ({final.get('delivery_effective_review_source') or ''}) | "
            f"`{final.get('review_verdict') or ''}` |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def rel(path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(REPO_ROOT))
    except ValueError:
        try:
            return str(resolved.relative_to(WORKSPACE_ROOT))
        except ValueError:
            return str(path)


def main() -> int:
    args = parse_args()
    rows = build_round_records(
        args.delivery,
        args.targets,
        args.rounds,
        args.events,
        args.skillfab_db,
        args.skillfab_repo_root,
        skip_commit_check=args.skip_commit_check,
    )
    args.out_prefix.parent.mkdir(parents=True, exist_ok=True)
    write_jsonl(args.out_prefix.with_suffix(".jsonl"), rows)
    write_csv(args.out_prefix.with_suffix(".csv"), rows)
    summary = write_summary(
        args.out_prefix.with_name(args.out_prefix.name + "-summary.json"),
        rows,
        load_jsonl(args.delivery),
        args,
    )
    write_markdown(args.out_prefix.with_suffix(".md"), rows, summary)
    print(json.dumps({"round_records": len(rows), "skills": summary["skill_count_with_round_records"]}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
