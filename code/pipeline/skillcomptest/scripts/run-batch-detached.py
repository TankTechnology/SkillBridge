#!/usr/bin/env python3
"""Start a SkillCompTest batch in the background and track its launcher log."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE = "ClaudeCode_M2.7_linuxquick6_20260615"
DEFAULT_TIMEOUT_PER_TASK = 150
DEFAULT_MAX_RETRIES = 2


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def container_skill_list(skill_list: str, data_store: Path) -> str:
    if skill_list.startswith("/data/"):
        return skill_list
    path = Path(skill_list)
    if not path.is_absolute():
        path = (REPO_ROOT / path).resolve()
    queue_dir = (data_store / "queues").resolve()
    try:
        rel = path.resolve().relative_to(queue_dir)
    except ValueError as exc:
        raise SystemExit(f"ERROR: skill list must be under {queue_dir}: {path}") from exc
    return f"/data/queues/{rel.as_posix()}"


def read_only_skill_repo_volumes(data_store: Path, skills_root: str = "") -> list[str]:
    """Return docker volume specs that make fetched skill sources immutable."""
    if skills_root:
        host_path = Path(skills_root)
        if not host_path.is_absolute():
            host_path = (REPO_ROOT / host_path).resolve()
        return [f"{host_path.resolve()}:/data/SkillsRepo:ro"]

    volumes: list[str] = []
    for dirname in ("SkillsRepo", "skills-repo"):
        host_path = (data_store / dirname).resolve()
        if host_path.is_dir():
            volumes.append(f"{host_path}:/data/{dirname}:ro")
    return volumes


def build_docker_command(args: argparse.Namespace) -> list[str]:
    data_store = Path(args.data_store)
    skill_list = container_skill_list(args.skill_list, data_store)
    envs = {
        "BATCH_ID": args.run_id,
        "SKILL_LIST": skill_list,
        "S1_PARALLEL": str(args.s1),
        "S2_PARALLEL": str(args.s2),
        "TASK_PARALLEL": str(args.task_parallel),
        "PIPELINE_MODE": args.pipeline_mode,
        "MAX_AGENT_PARALLEL": str(args.max_agent_parallel),
        "TIMEOUT_PER_TASK": str(args.timeout_per_task),
        "MAX_RETRIES": str(args.max_retries),
        "TASK_COUNT": str(args.task_count),
        "TASK_DESIGN_MODEL": args.task_design_model,
        "QUEUE_POLICY": args.queue_policy,
        "SAMPLE_REFRESH_POLICY": args.sample_refresh_policy,
        "ALLOW_RERUN_COMPLETE": "1" if args.allow_rerun_complete else "0",
        "PLATFORM": args.platform,
        "RUNNER": args.runner,
    }
    if args.claude_model:
        envs["CLAUDE_MODEL"] = args.claude_model
    if args.sample_model:
        envs["SAMPLE_CLAUDE_MODEL"] = args.sample_model
    if args.exec_model:
        envs["EXEC_CLAUDE_MODEL"] = args.exec_model

    cmd = ["docker", "compose", "run", "--rm"]
    for key, value in envs.items():
        cmd.extend(["-e", f"{key}={value}"])
    for volume in read_only_skill_repo_volumes(data_store, args.skills_root):
        cmd.extend(["-v", volume])
    cmd.extend(["skillcomptest", "batch"])
    return cmd


def build_watch(args: argparse.Namespace, command: list[str], log_path: Path, watch_path: Path) -> dict[str, Any]:
    return {
        "schema_version": "skillcomptest_watch_v1",
        "run_id": args.run_id,
        "repo_root": str(REPO_ROOT),
        "data_store": str(Path(args.data_store)),
        "skills_root": str(Path(args.skills_root).resolve()) if args.skills_root else "",
        "log_path": str(log_path),
        "watch_path": str(watch_path),
        "started_at": utc_now(),
        "pid": None,
        "command": command,
        "read_only_skill_repo_volumes": read_only_skill_repo_volumes(Path(args.data_store), args.skills_root),
        "status_command": f"python3 scripts/batch-status.py --run-id {args.run_id}",
        "export_command": f"python3 scripts/export-run-summary.py --run-id {args.run_id}",
        "config": {
            "s1": args.s1,
            "s2": args.s2,
            "task_parallel": args.task_parallel,
            "pipeline_mode": args.pipeline_mode,
            "max_agent_parallel": args.max_agent_parallel,
            "timeout_per_task": args.timeout_per_task,
            "max_retries": args.max_retries,
            "approx_max_task_wall_s": args.timeout_per_task * args.max_retries,
            "timeout_policy": "TIMEOUT_PER_TASK applies per runner attempt; baseline uses MAX_RETRIES=2 for about 300s total task budget.",
            "task_count": args.task_count,
            "task_design_model": args.task_design_model,
            "queue_policy": args.queue_policy,
            "sample_refresh_policy": args.sample_refresh_policy,
            "allow_rerun_complete": args.allow_rerun_complete,
            "platform": args.platform,
            "runner": args.runner,
        },
    }


def supervise(watch_path: Path) -> int:
    watch = load_json(watch_path)
    command = watch.get("command")
    if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
        print(f"ERROR: invalid watch command in {watch_path}", file=sys.stderr)
        return 2

    run_id = str(watch.get("run_id", ""))
    print(f"[{utc_now()}] starting batch: {run_id}", flush=True)
    started = time.time()
    docker_env = os.environ.copy()
    docker_env["DATA_STORE_HOST"] = str(watch.get("data_store", ""))
    docker_rc = subprocess.run(command, cwd=REPO_ROOT, env=docker_env).returncode
    finished = time.time()
    watch.update(
        {
            "docker_returncode": docker_rc,
            "finished_at": utc_now(),
            "duration_s": int(finished - started),
        }
    )
    write_json(watch_path, watch)
    print(f"[{utc_now()}] docker finished: rc={docker_rc}", flush=True)

    report_path = Path(str(watch.get("data_store", REPO_ROOT / ".cache" / "skillcomptest"))) / "queues" / f"{run_id}.report.json"
    export_cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "export-run-summary.py"),
        "--run-id",
        run_id,
        "--data-store",
        str(watch.get("data_store", REPO_ROOT / ".cache" / "skillcomptest")),
    ]
    if report_path.is_file():
        export_rc = subprocess.run(export_cmd, cwd=REPO_ROOT).returncode
    else:
        print(f"[{utc_now()}] report not found, skip export: {report_path}", flush=True)
        export_rc = None
    watch = load_json(watch_path)
    watch["export_returncode"] = export_rc
    write_json(watch_path, watch)
    print(f"[{utc_now()}] export finished: rc={export_rc}", flush=True)
    return docker_rc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=False)
    parser.add_argument("--skill-list")
    parser.add_argument("--data-store", default=os.environ.get("DATA_STORE", str(REPO_ROOT / ".cache" / "skillcomptest")))
    parser.add_argument("--skills-root", default="", help="Optional skill source root to mount read-only as /data/SkillsRepo.")
    parser.add_argument("--s1", type=int, default=8)
    parser.add_argument("--s2", type=int, default=8)
    parser.add_argument("--task-parallel", type=int, default=1)
    parser.add_argument("--pipeline-mode", choices=("streaming", "barrier"), default="streaming")
    parser.add_argument("--max-agent-parallel", type=int, default=24)
    parser.add_argument("--timeout-per-task", type=int, default=DEFAULT_TIMEOUT_PER_TASK)
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    parser.add_argument("--task-count", type=int, default=6)
    parser.add_argument("--task-design-model", default=DEFAULT_PROFILE)
    parser.add_argument("--queue-policy", default="untested")
    parser.add_argument("--sample-refresh-policy", default="never")
    parser.add_argument("--allow-rerun-complete", action="store_true")
    parser.add_argument("--platform", default="linux")
    parser.add_argument("--runner", default="claude")
    parser.add_argument("--claude-model", default="")
    parser.add_argument("--sample-model", default="")
    parser.add_argument("--exec-model", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--supervise", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--watch-path", help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.supervise:
        if not args.watch_path:
            raise SystemExit("ERROR: --watch-path is required with --supervise")
        return supervise(Path(args.watch_path))

    if not args.run_id:
        raise SystemExit("ERROR: --run-id is required")
    if not args.skill_list:
        raise SystemExit("ERROR: --skill-list is required")

    data_store = Path(args.data_store)
    watcher_dir = data_store / "watchers"
    log_path = watcher_dir / f"{args.run_id}.log"
    watch_path = watcher_dir / f"{args.run_id}.watch.json"
    command = build_docker_command(args)
    watch = build_watch(args, command, log_path, watch_path)

    if args.dry_run:
        print(json.dumps(watch, indent=2, ensure_ascii=False))
        return 0

    watcher_dir.mkdir(parents=True, exist_ok=True)
    write_json(watch_path, watch)
    log_file = open(log_path, "ab", buffering=0)
    supervisor_cmd = [sys.executable, str(Path(__file__).resolve()), "--supervise", "--watch-path", str(watch_path)]
    process = subprocess.Popen(
        supervisor_cmd,
        cwd=REPO_ROOT,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    watch["pid"] = process.pid
    watch["supervisor_command"] = supervisor_cmd
    write_json(watch_path, watch)

    print(f"Started run: {args.run_id}")
    print(f"Watcher PID: {process.pid}")
    print(f"Log: {log_path}")
    print(f"Status: python3 scripts/batch-status.py --run-id {args.run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
