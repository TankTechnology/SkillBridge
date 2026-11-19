#!/usr/bin/env python3
"""Run SkillCompTest stages as a streaming two-stage pipeline."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class StageResult:
    skill_line: str
    skill_id: str
    stage: str
    status: str
    exit_code: int
    duration_s: int


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def log(message: str) -> None:
    print(f"[{ts()}] {message}", flush=True)


def load_queue(path: Path) -> list[str]:
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


def skill_id(skill_line: str) -> str:
    parts = skill_line.split("|")
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return skill_line


def write_event(event_log: Path, event_type: str, data: dict[str, Any]) -> None:
    event = {
        "ts": time.time(),
        "iso": utc_now(),
        "event": event_type,
    }
    batch_id = os.environ.get("BATCH_ID", "")
    if batch_id:
        event["batch_id"] = batch_id
    event.update(data)
    event_log.parent.mkdir(parents=True, exist_ok=True)
    with event_log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def run_stage(
    *,
    stage: str,
    script: Path,
    skill_line: str,
    repo_root: Path,
    agent_gate: threading.Semaphore,
) -> StageResult:
    sid = skill_id(skill_line)
    started = time.time()
    with agent_gate:
        rc = subprocess.run(["bash", str(script), skill_line], cwd=repo_root, env=os.environ.copy()).returncode
    duration = int(time.time() - started)
    status = "done" if rc == 0 else "failed"
    return StageResult(
        skill_line=skill_line,
        skill_id=sid,
        stage=stage,
        status=status,
        exit_code=rc,
        duration_s=duration,
    )


def s2_worker(
    *,
    worker_id: int,
    work_queue: "queue.Queue[str | None]",
    results: list[StageResult],
    results_lock: threading.Lock,
    stage2_script: Path,
    repo_root: Path,
    agent_gate: threading.Semaphore,
) -> None:
    while True:
        skill_line = work_queue.get()
        try:
            if skill_line is None:
                return
            result = run_stage(
                stage="S2",
                script=stage2_script,
                skill_line=skill_line,
                repo_root=repo_root,
                agent_gate=agent_gate,
            )
            with results_lock:
                results.append(result)
            log(f"STREAM:S2 worker={worker_id} {result.skill_id}: {result.status} ({result.duration_s}s)")
        finally:
            work_queue.task_done()


def run_streaming(args: argparse.Namespace) -> dict[str, Any]:
    queue_file = Path(args.queue_file)
    stage1_script = Path(args.stage1_script)
    stage2_script = Path(args.stage2_script)
    repo_root = Path(args.repo_root)
    event_log = Path(args.event_log)
    result_file = Path(args.result_file)

    skills = load_queue(queue_file)
    if not skills:
        raise SystemExit(f"ERROR: queue is empty: {queue_file}")

    s1_parallel = max(1, int(args.s1_parallel))
    s2_parallel = max(1, int(args.s2_parallel))
    max_agent_parallel = int(args.max_agent_parallel)
    if max_agent_parallel <= 0:
        max_agent_parallel = s1_parallel + s2_parallel
    max_agent_parallel = max(1, max_agent_parallel)

    log(
        "Streaming Pipeline: "
        f"{len(skills)} skills | S1={s1_parallel} S2={s2_parallel} "
        f"max_agents={max_agent_parallel}"
    )
    write_event(
        event_log,
        "phase1_start",
        {"parallelism": s1_parallel, "mode": "streaming"},
    )
    write_event(
        event_log,
        "phase2_start",
        {
            "skill_parallelism": s2_parallel,
            "task_parallelism": int(args.task_parallel),
            "mode": "streaming",
        },
    )

    pipeline_start = time.time()
    phase1_start = pipeline_start
    phase2_start = pipeline_start
    agent_gate = threading.Semaphore(max_agent_parallel)
    s2_queue: queue.Queue[str | None] = queue.Queue(maxsize=max(s2_parallel * 2, 1))
    s2_results: list[StageResult] = []
    s2_results_lock = threading.Lock()

    s2_threads = [
        threading.Thread(
            target=s2_worker,
            kwargs={
                "worker_id": idx + 1,
                "work_queue": s2_queue,
                "results": s2_results,
                "results_lock": s2_results_lock,
                "stage2_script": stage2_script,
                "repo_root": repo_root,
                "agent_gate": agent_gate,
            },
            daemon=False,
        )
        for idx in range(s2_parallel)
    ]
    for thread in s2_threads:
        thread.start()

    s1_results: list[StageResult] = []
    with ThreadPoolExecutor(max_workers=min(s1_parallel, len(skills))) as pool:
        futures = [
            pool.submit(
                run_stage,
                stage="S1",
                script=stage1_script,
                skill_line=skill_line,
                repo_root=repo_root,
                agent_gate=agent_gate,
            )
            for skill_line in skills
        ]
        completed = 0
        for future in as_completed(futures):
            result = future.result()
            s1_results.append(result)
            completed += 1
            log(
                f"STREAM:S1 [{completed}/{len(skills)}] "
                f"{result.skill_id}: {result.status} ({result.duration_s}s)"
            )
            if result.exit_code == 0:
                s2_queue.put(result.skill_line)
            else:
                log(f"STREAM:S2 skip {result.skill_id}: S1 failed")

    phase1_end = time.time()
    s1_exit = 0 if all(result.exit_code == 0 for result in s1_results) else 1
    write_event(
        event_log,
        "phase1_end",
        {"duration_s": int(phase1_end - phase1_start), "exit_code": s1_exit, "mode": "streaming"},
    )
    log(f"Streaming Phase 1 complete in {int(phase1_end - phase1_start)}s")

    for _ in s2_threads:
        s2_queue.put(None)
    s2_queue.join()
    for thread in s2_threads:
        thread.join()

    phase2_end = time.time()
    s2_exit = 0 if len(s2_results) == sum(1 for result in s1_results if result.exit_code == 0) and all(
        result.exit_code == 0 for result in s2_results
    ) else 1
    write_event(
        event_log,
        "phase2_end",
        {"duration_s": int(phase2_end - phase2_start), "exit_code": s2_exit, "mode": "streaming"},
    )
    log(f"Streaming Phase 2 complete in {int(phase2_end - phase2_start)}s")

    summary = {
        "schema_version": "skillcomptest_streaming_pipeline_v1",
        "generated_at": utc_now(),
        "mode": "streaming",
        "total_skills": len(skills),
        "s1_parallel": s1_parallel,
        "s2_parallel": s2_parallel,
        "task_parallel": int(args.task_parallel),
        "max_agent_parallel": max_agent_parallel,
        "duration_s": int(phase2_end - pipeline_start),
        "phase1_duration_s": int(phase1_end - phase1_start),
        "phase2_duration_s": int(phase2_end - phase2_start),
        "s1_exit": s1_exit,
        "s2_exit": s2_exit,
        "s1": [asdict(result) for result in sorted(s1_results, key=lambda item: item.skill_id)],
        "s2": [asdict(result) for result in sorted(s2_results, key=lambda item: item.skill_id)],
    }
    result_file.parent.mkdir(parents=True, exist_ok=True)
    result_file.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log(f"Streaming result: {result_file}")
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue-file", default=os.environ.get("TASK_QUEUE_FILE", ""))
    parser.add_argument("--stage1-script", default=str(REPO_ROOT / "scripts" / "stages" / "stage1-sample.sh"))
    parser.add_argument("--stage2-script", default=str(REPO_ROOT / "scripts" / "stages" / "stage2-exec.sh"))
    parser.add_argument("--repo-root", default=os.environ.get("REPO_ROOT", str(REPO_ROOT)))
    parser.add_argument("--event-log", default=os.environ.get("EVENT_LOG", ""))
    parser.add_argument("--result-file", default="")
    parser.add_argument("--s1-parallel", type=int, default=int(os.environ.get("S1_PARALLEL", "8")))
    parser.add_argument("--s2-parallel", type=int, default=int(os.environ.get("S2_PARALLEL", "8")))
    parser.add_argument("--task-parallel", type=int, default=int(os.environ.get("TASK_PARALLEL", "1")))
    parser.add_argument("--max-agent-parallel", type=int, default=int(os.environ.get("MAX_AGENT_PARALLEL", "0")))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.queue_file:
        raise SystemExit("ERROR: --queue-file or TASK_QUEUE_FILE is required")
    if not args.event_log:
        raise SystemExit("ERROR: --event-log or EVENT_LOG is required")
    if not args.result_file:
        batch_id = os.environ.get("BATCH_ID", "manual")
        queue_dir = Path(os.environ.get("QUEUE_DIR", str(Path(args.queue_file).parent)))
        args.result_file = str(queue_dir / f"{batch_id}.streaming.json")

    summary = run_streaming(args)
    return 0 if summary["s1_exit"] == 0 and summary["s2_exit"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
