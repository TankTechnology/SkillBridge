#!/usr/bin/env python3
"""Plan a SkillCompTest batch queue without executing skills."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.lib.batch_queue import RunProfile, load_skill_list, plan_batch, write_batch_artifacts


def _positive_int_or_none(value: str) -> int | None:
    if value == "":
        return None
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skills-dir", required=True)
    parser.add_argument("--samples-dir", required=True)
    parser.add_argument("--exec-dir", required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--task-count", type=int, required=True)
    parser.add_argument("--task-design-model", required=True)
    parser.add_argument("--runner", required=True)
    parser.add_argument("--claude-model", required=True)
    parser.add_argument("--source", default="")
    parser.add_argument("--skill", default="")
    parser.add_argument("--max-skills", type=_positive_int_or_none, default=None)
    parser.add_argument("--skill-list", default="")
    parser.add_argument("--queue-policy", default="untested")
    parser.add_argument("--sample-refresh-policy", default="never")
    parser.add_argument("--allow-rerun-complete", action="store_true")
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--queue-file", required=True)
    parser.add_argument("--plan-file", required=True)
    parser.add_argument("--refresh-file", required=True)
    parser.add_argument("--legacy-queue-file", required=True)
    parser.add_argument("--queued-skill-list-file", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    skill_ids = None
    candidate_source = "repository"
    if args.skill_list:
        skill_list_path = Path(args.skill_list)
        if not skill_list_path.is_file():
            print(f"ERROR: SKILL_LIST not found: {skill_list_path}", file=sys.stderr)
            return 2
        skill_ids = load_skill_list(skill_list_path)
        candidate_source = str(skill_list_path)

    profile = RunProfile(
        platform=args.platform,
        task_count=args.task_count,
        task_design_model=args.task_design_model,
        runner=args.runner,
        claude_model=args.claude_model,
    )
    result = plan_batch(
        skills_dir=Path(args.skills_dir),
        samples_dir=Path(args.samples_dir),
        exec_dir=Path(args.exec_dir).parent,
        profile=profile,
        queue_policy=args.queue_policy,
        source=args.source,
        skill=args.skill,
        max_skills=args.max_skills,
        skill_ids=skill_ids,
        sample_refresh_policy=args.sample_refresh_policy,
        allow_rerun_complete=args.allow_rerun_complete,
    )
    write_batch_artifacts(
        result,
        batch_id=args.batch_id,
        candidate_source=candidate_source,
        queue_file=args.queue_file,
        plan_file=args.plan_file,
        refresh_file=args.refresh_file,
    )
    Path(args.legacy_queue_file).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(args.queue_file, args.legacy_queue_file)
    if args.queued_skill_list_file:
        queued_skill_list = Path(args.queued_skill_list_file)
        queued_skill_list.parent.mkdir(parents=True, exist_ok=True)
        queued_skill_list.write_text("".join(f"{item.skill_id}\n" for item in result.queued), encoding="utf-8")

    print(f"Batch ID: {args.batch_id}")
    print(f"Candidate source: {candidate_source}")
    print(f"Candidates: {len(result.items)}")
    print(f"Queued: {len(result.queued)}")
    print(f"Missing: {len(result.missing)}")
    print(f"Refresh samples: {len(result.refresh_samples)}")
    print(f"Allow rerun complete: {result.allow_rerun_complete}")
    print(f"Plan: {args.plan_file}")
    print(f"Queue: {args.queue_file}")
    if args.queued_skill_list_file:
        print(f"Queued skill list: {args.queued_skill_list_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
