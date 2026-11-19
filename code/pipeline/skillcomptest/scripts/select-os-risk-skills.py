#!/usr/bin/env python3
"""Select skills likely to expose fixable OS compatibility issues."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.lib.batch_queue import RunProfile
from scripts.lib.os_risk_selector import select_os_risk_skills, write_selection_artifacts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skills-dir", required=True)
    parser.add_argument("--samples-dir", required=True)
    parser.add_argument("--exec-dir", required=True, help="Exec root, e.g. .cache/skillcomptest/exec")
    parser.add_argument("--platform", default="linux")
    parser.add_argument("--task-count", type=int, default=6)
    parser.add_argument("--task-design-model", required=True)
    parser.add_argument("--runner", default="claude")
    parser.add_argument("--claude-model", default="deepseek-v4-flash")
    parser.add_argument("--source", default="")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--include-complete", action="store_true")
    parser.add_argument(
        "--exclude-auth-heavy",
        action="store_true",
        help="Skip API/account/login-heavy skills for faster OS-focused batches.",
    )
    parser.add_argument(
        "--include-guide-only",
        action="store_true",
        help="Also include skills that only have guide text OS signals and no local executable surface.",
    )
    parser.add_argument("--output-list", required=True)
    parser.add_argument("--output-metadata", required=True)
    parser.add_argument("--shard-dir", default="")
    parser.add_argument("--shard-size", type=int, default=20)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    profile = RunProfile(
        platform=args.platform,
        task_count=args.task_count,
        task_design_model=args.task_design_model,
        runner=args.runner,
        claude_model=args.claude_model,
    )
    selected = select_os_risk_skills(
        skills_dir=Path(args.skills_dir),
        samples_dir=Path(args.samples_dir),
        exec_dir=Path(args.exec_dir),
        profile=profile,
        limit=args.limit,
        source=args.source,
        include_complete=args.include_complete,
        exclude_auth_heavy=args.exclude_auth_heavy,
        require_executable_surface=not args.include_guide_only,
    )
    write_selection_artifacts(
        selected=selected,
        list_path=Path(args.output_list),
        metadata_path=Path(args.output_metadata),
        shard_dir=Path(args.shard_dir) if args.shard_dir else None,
        shard_size=args.shard_size,
    )
    print(f"Selected: {len(selected)}")
    if selected:
        print(f"Top score: {selected[0].score} {selected[0].skill_id} {','.join(selected[0].reasons)}")
        print(f"Tail score: {selected[-1].score} {selected[-1].skill_id} {','.join(selected[-1].reasons)}")
    print(f"List: {args.output_list}")
    print(f"Metadata: {args.output_metadata}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
