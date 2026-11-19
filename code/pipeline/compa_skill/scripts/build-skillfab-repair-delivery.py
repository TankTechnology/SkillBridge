#!/usr/bin/env python3
"""Build a clean SkillFab OS-repair delivery inventory.

This script does not run skill code. It joins the generated repair targets with
the local SkillFab database to produce a retest-ready manifest for collaborators.
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TARGETS = REPO_ROOT / "docs/repair-workflow/skillfab-repair-targets-20260628.jsonl"
DEFAULT_SKILLFAB_DB = REPO_ROOT.parent / "SkillFab/data/skillfab.db"
DEFAULT_OUT_PREFIX = REPO_ROOT / "docs/repair-workflow/skillfab-os-repair-delivery-20260628"
DEFAULT_POSTPUBLISH_REVIEWS = REPO_ROOT / "docs/repair-workflow/skillfab-postpublish-claude-review-20260628.jsonl"
DEFAULT_ROUNDS = REPO_ROOT / "docs/repair-workflow/skillfab-tracking/rounds.jsonl"
DEFAULT_EVENTS = REPO_ROOT / "docs/repair-workflow/skillfab-tracking/events.jsonl"

CSV_FIELDS = [
    "issue_key",
    "skill_id",
    "source",
    "slug",
    "repair_os",
    "affected_os",
    "delivery_status",
    "target_status",
    "skillfab_issue_id",
    "skillfab_submission_id",
    "submission_status",
    "latest_review_result",
    "effective_review_result",
    "effective_review_source",
    "postpublish_review_result",
    "postpublish_review_status",
    "postpublish_reviewed_at",
    "repair_round_count",
    "repair_round_source",
    "total_skill_round_count",
    "latest_commit_sha",
    "published_skill_id",
    "published_skill_name",
    "published_skill_status",
    "certification_level",
    "pre_repair_outcome_summary",
    "pre_repair_task_totals",
    "source_package_path",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    parser.add_argument("--skillfab-db", type=Path, default=DEFAULT_SKILLFAB_DB)
    parser.add_argument("--postpublish-reviews", type=Path, default=DEFAULT_POSTPUBLISH_REVIEWS)
    parser.add_argument("--rounds", type=Path, default=DEFAULT_ROUNDS)
    parser.add_argument("--events", type=Path, default=DEFAULT_EVENTS)
    parser.add_argument("--out-prefix", type=Path, default=DEFAULT_OUT_PREFIX)
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def read_publication_index(db_path: Path) -> dict[int, dict[str, Any]]:
    if not db_path.exists():
        return {}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT
              ss.id AS submission_id,
              ss.status AS submission_status,
              ss.skill_id AS published_skill_id,
              sk.name AS published_skill_name,
              sk.status AS published_skill_status,
              sk.certification_level AS certification_level
            FROM submissions ss
            LEFT JOIN skills sk ON sk.id = ss.skill_id
            """
        ).fetchall()
    finally:
        conn.close()
    return {int(row["submission_id"]): dict(row) for row in rows}


def read_postpublish_review_index(path: Path) -> dict[str, dict[str, Any]]:
    rows = [row for row in load_jsonl(path) if row.get("status") == "reviewed"]
    rows.sort(key=lambda row: str(row.get("reviewed_at") or ""))
    return {str(row.get("issue_key")): row for row in rows if row.get("issue_key")}


def event_issue_id(row: dict[str, Any]) -> str:
    for ref in row.get("artifact_refs") or []:
        text = str(ref)
        marker = "skillfab://issue/"
        if text.startswith(marker):
            return text[len(marker) :]
    return ""


def event_round_index(row: dict[str, Any]) -> int:
    text = str(row.get("event_id") or "")
    if "-R" not in text:
        return 0
    try:
        return int(text.split("-R", 1)[1].split("-", 1)[0])
    except (IndexError, ValueError):
        return 0


def read_round_index(rounds_path: Path, events_path: Path) -> dict[str, Any]:
    selected_pairs: dict[str, set[int]] = {}
    by_key_pairs: dict[str, set[tuple[str, int]]] = {}
    for row in load_jsonl(rounds_path):
        issue_key = str(row.get("issue_key") or "")
        issue_id = str(row.get("skillfab_issue_id") or "")
        round_index = int(row.get("round_index") or 0)
        if not issue_key or not issue_id or round_index <= 0:
            continue
        selected_pairs.setdefault(issue_id, set()).add(round_index)
        by_key_pairs.setdefault(issue_key, set()).add((issue_id, round_index))

    event_pairs: dict[str, set[int]] = {}
    event_by_key_pairs: dict[str, set[tuple[str, int]]] = {}
    for row in load_jsonl(events_path):
        if row.get("event_type") not in {"dev_round_completed", "review_completed"}:
            continue
        issue_key = str(row.get("issue_key") or "")
        issue_id = event_issue_id(row)
        round_index = event_round_index(row)
        if not issue_key or not issue_id or round_index <= 0:
            continue
        event_pairs.setdefault(issue_id, set()).add(round_index)
        event_by_key_pairs.setdefault(issue_key, set()).add((issue_id, round_index))

    return {
        "rounds_by_issue_id": selected_pairs,
        "event_rounds_by_issue_id": event_pairs,
        "rounds_by_issue_key": by_key_pairs,
        "event_rounds_by_issue_key": event_by_key_pairs,
    }


def json_csv(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def delivery_status(row: dict[str, Any]) -> str:
    if row.get("target_status") == "completed":
        return "ready_for_cross_os_retest"
    if row.get("target_status") == "not_started_missing_source_package":
        return "blocked_missing_source_package"
    return f"not_ready_{row.get('target_status') or 'unknown'}"


def postpublish_decision(review: dict[str, Any] | None) -> str:
    if not review:
        return ""
    return str((review.get("review") or {}).get("decision") or "")


def postpublish_status(review: dict[str, Any] | None) -> str:
    if not review:
        return ""
    certification = review.get("certification") or {}
    if postpublish_decision(review) == "approved" and certification.get("certification_level") == "certified":
        return "approved_and_certified"
    if postpublish_decision(review):
        return f"reviewed_{postpublish_decision(review)}"
    return str(review.get("status") or "")


def effective_review(row: dict[str, Any], postpublish_review: dict[str, Any] | None, certification_level: str) -> tuple[str, str]:
    raw = str(row.get("latest_review_result") or "")
    if raw == "approved":
        return "approved", "submission_review"
    if certification_level == "certified" and postpublish_decision(postpublish_review) == "approved":
        return "approved", "postpublish_claude_review"
    if raw:
        return raw, "submission_review"
    if postpublish_decision(postpublish_review):
        return postpublish_decision(postpublish_review), "postpublish_claude_review"
    return "", ""


def selected_round_count(row: dict[str, Any], round_index: dict[str, Any]) -> tuple[int | None, str]:
    issue_id = str(row.get("skillfab_issue_id") or "")
    rounds = round_index["rounds_by_issue_id"].get(issue_id)
    if rounds:
        return max(rounds), "rounds_jsonl"
    event_rounds = round_index["event_rounds_by_issue_id"].get(issue_id)
    if event_rounds:
        return max(event_rounds), "events_jsonl"
    return None, "missing"


def total_skill_round_count(row: dict[str, Any], round_index: dict[str, Any]) -> int | None:
    issue_key = str(row.get("issue_key") or "")
    pairs = set(round_index["rounds_by_issue_key"].get(issue_key, set()))
    pairs.update(round_index["event_rounds_by_issue_key"].get(issue_key, set()))
    return len(pairs) if pairs else None


def build_delivery_row(
    row: dict[str, Any],
    publication: dict[str, Any] | None,
    postpublish_review: dict[str, Any] | None,
    round_index: dict[str, Any],
) -> dict[str, Any]:
    baseline = row.get("pre_repair_baseline") or {}
    certification_level = publication.get("certification_level") if publication else ""
    review_result, review_source = effective_review(row, postpublish_review, certification_level or "")
    round_count, round_source = selected_round_count(row, round_index)
    merged = {
        "schema_version": "skillfab_repair_delivery_v1",
        "issue_key": row.get("issue_key", ""),
        "skill_id": row.get("skill_id", ""),
        "source": row.get("source", ""),
        "slug": row.get("slug", ""),
        "repair_os": row.get("repair_os") or [],
        "affected_os": row.get("affected_os") or [],
        "delivery_status": delivery_status(row),
        "target_status": row.get("target_status", ""),
        "skillfab_issue_id": row.get("skillfab_issue_id"),
        "skillfab_submission_id": row.get("skillfab_submission_id"),
        "submission_status": row.get("submission_status", ""),
        "latest_review_result": row.get("latest_review_result", ""),
        "effective_review_result": review_result,
        "effective_review_source": review_source,
        "postpublish_review_result": postpublish_decision(postpublish_review),
        "postpublish_review_status": postpublish_status(postpublish_review),
        "postpublish_reviewed_at": postpublish_review.get("reviewed_at", "") if postpublish_review else "",
        "repair_round_count": round_count,
        "repair_round_source": round_source,
        "total_skill_round_count": total_skill_round_count(row, round_index),
        "latest_commit_sha": row.get("latest_commit_sha", ""),
        "published_skill_id": None,
        "published_skill_name": "",
        "published_skill_status": "",
        "certification_level": certification_level or "",
        "pre_repair_outcome_summary": baseline.get("outcome_summary", ""),
        "pre_repair_task_totals": baseline.get("task_totals") or {},
        "source_package_path": row.get("source_package_path", ""),
    }
    if publication:
        merged.update(
            {
                "submission_status": publication.get("submission_status") or merged["submission_status"],
                "published_skill_id": publication.get("published_skill_id"),
                "published_skill_name": publication.get("published_skill_name") or "",
                "published_skill_status": publication.get("published_skill_status") or "",
                "certification_level": publication.get("certification_level") or "",
            }
        )
    return merged


def build_delivery_rows(
    targets_path: Path,
    db_path: Path,
    postpublish_reviews_path: Path = DEFAULT_POSTPUBLISH_REVIEWS,
    rounds_path: Path = DEFAULT_ROUNDS,
    events_path: Path = DEFAULT_EVENTS,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    publication_index = read_publication_index(db_path)
    postpublish_index = read_postpublish_review_index(postpublish_reviews_path)
    round_index = read_round_index(rounds_path, events_path)
    rows = []
    for row in load_jsonl(targets_path):
        submission_id = row.get("skillfab_submission_id")
        publication = publication_index.get(int(submission_id)) if submission_id else None
        rows.append(build_delivery_row(row, publication, postpublish_index.get(str(row.get("issue_key"))), round_index))
    ready = [row for row in rows if row["delivery_status"] == "ready_for_cross_os_retest"]
    blocked = [row for row in rows if row["delivery_status"] != "ready_for_cross_os_retest"]
    return ready, blocked


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: json_csv(row.get(field)) for field in CSV_FIELDS})


def write_markdown(path: Path, ready: list[dict[str, Any]], blocked: list[dict[str, Any]]) -> None:
    by_os = Counter(os_name for row in ready for os_name in row["repair_os"])
    by_source = Counter(row["source"] for row in ready)
    by_round = Counter(str(row["repair_round_count"] or "missing") for row in ready)
    by_total_round = Counter(str(row["total_skill_round_count"] or "missing") for row in ready)
    by_effective_review = Counter(row["effective_review_result"] or "missing" for row in ready)
    by_effective_source = Counter(row["effective_review_source"] or "missing" for row in ready)
    by_certification = Counter(row["certification_level"] or "missing" for row in ready)
    lines = [
        "# SkillFab OS Repair Delivery 2026-06-28",
        "",
        "This inventory is generated from the canonical SkillFab repair targets.",
        "It records repaired/published skills for cross-OS retesting. No skill code was executed while generating this file.",
        "",
        "Definitions:",
        "- `effective_review_result` is the final trusted review gate. It uses the selected submission review when available; otherwise it uses the post-publish Claude static review.",
        "- `repair_round_count` counts the rounds for the selected SkillFab issue that produced the delivered skill.",
        "- `total_skill_round_count` counts all recorded rounds for the same `issue_key`, including superseded duplicate/follow-up issues.",
        "",
        "## Summary",
        "",
        f"- Ready for cross-OS retest: {len(ready)}",
        f"- Blocked / not deliverable: {len(blocked)}",
        f"- Effective review result: `{json.dumps(dict(sorted(by_effective_review.items())), ensure_ascii=False, sort_keys=True)}`",
        f"- Effective review source: `{json.dumps(dict(sorted(by_effective_source.items())), ensure_ascii=False, sort_keys=True)}`",
        f"- Certification level: `{json.dumps(dict(sorted(by_certification.items())), ensure_ascii=False, sort_keys=True)}`",
        f"- By source: `{json.dumps(dict(sorted(by_source.items())), ensure_ascii=False, sort_keys=True)}`",
        f"- By repair OS: `{json.dumps(dict(sorted(by_os.items())), ensure_ascii=False, sort_keys=True)}`",
        f"- By selected repair rounds: `{json.dumps(dict(sorted(by_round.items())), ensure_ascii=False, sort_keys=True)}`",
        f"- By total skill rounds: `{json.dumps(dict(sorted(by_total_round.items())), ensure_ascii=False, sort_keys=True)}`",
        "",
        "## Ready Skills",
        "",
        "| # | Skill | Repair OS | Rounds | Effective Review | SkillFab Issue | Submission | Published Skill | Pre-Repair Outcome |",
        "|---|---|---|---:|---|---:|---:|---|---|",
    ]
    for index, row in enumerate(ready, 1):
        published = row["published_skill_name"] or row["published_skill_id"] or ""
        lines.append(
            f"| {index} | `{row['skill_id']}` | {', '.join(row['repair_os'])} | "
            f"{row.get('repair_round_count') or ''} | `{row.get('effective_review_result') or ''}` ({row.get('effective_review_source') or ''}) | "
            f"{row.get('skillfab_issue_id') or ''} | {row.get('skillfab_submission_id') or ''} | "
            f"`{published}` | {row.get('pre_repair_outcome_summary') or ''} |"
        )
    if blocked:
        lines.extend(["", "## Blocked", "", "| Skill | Reason |", "|---|---|"])
        for row in blocked:
            lines.append(f"| `{row['skill_id']}` | `{row['delivery_status']}` |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_summary(path: Path, ready: list[dict[str, Any]], blocked: list[dict[str, Any]], args: argparse.Namespace) -> None:
    round_counts = Counter(str(row["repair_round_count"] or "missing") for row in ready)
    total_round_counts = Counter(str(row["total_skill_round_count"] or "missing") for row in ready)
    summary = {
        "schema_version": "skillfab_repair_delivery_summary_v1",
        "targets": str(args.targets),
        "skillfab_db": str(args.skillfab_db),
        "ready_count": len(ready),
        "blocked_count": len(blocked),
        "ready_by_certification_level": dict(sorted(Counter(row["certification_level"] for row in ready).items())),
        "ready_by_effective_review_result": dict(sorted(Counter(row["effective_review_result"] or "missing" for row in ready).items())),
        "ready_by_effective_review_source": dict(sorted(Counter(row["effective_review_source"] or "missing" for row in ready).items())),
        "ready_by_source": dict(sorted(Counter(row["source"] for row in ready).items())),
        "ready_by_repair_os": dict(sorted(Counter(os_name for row in ready for os_name in row["repair_os"]).items())),
        "selected_issue_round_counts": dict(sorted(round_counts.items(), key=lambda item: (item[0] == "missing", int(item[0]) if item[0].isdigit() else 999))),
        "total_skill_round_counts": dict(sorted(total_round_counts.items(), key=lambda item: (item[0] == "missing", int(item[0]) if item[0].isdigit() else 999))),
        "blocked": [{"issue_key": row["issue_key"], "skill_id": row["skill_id"], "delivery_status": row["delivery_status"]} for row in blocked],
    }
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    ready, blocked = build_delivery_rows(args.targets, args.skillfab_db, args.postpublish_reviews, args.rounds, args.events)
    args.out_prefix.parent.mkdir(parents=True, exist_ok=True)
    write_jsonl(args.out_prefix.with_suffix(".jsonl"), ready + blocked)
    write_csv(args.out_prefix.with_suffix(".csv"), ready + blocked)
    write_markdown(args.out_prefix.with_suffix(".md"), ready, blocked)
    write_summary(args.out_prefix.with_name(args.out_prefix.name + "-summary.json"), ready, blocked, args)
    print(json.dumps({"ready": len(ready), "blocked": len(blocked)}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
