#!/usr/bin/env python3
"""Wait for one detached run to finish, then launch another command.

This is a small orchestration helper for SkillCompTest experiments where the
batch runner uses a global lock and two batches cannot run at the same time.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time


REPO_ROOT = Path(__file__).resolve().parent.parent


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def log_line(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{utc_now()}] {message}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wait-watch", required=True, type=Path)
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    if not args.command:
        raise SystemExit("ERROR: command is required")
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise SystemExit("ERROR: command is required")

    log_line(args.log, f"waiting for {args.wait_watch}")
    while True:
        watch = load_json(args.wait_watch)
        if watch.get("finished_at"):
            log_line(args.log, "waited run finished; launching command")
            break
        pid = watch.get("pid")
        log_line(args.log, f"waited run still active; pid={pid}; sleep={args.poll_seconds}s")
        time.sleep(args.poll_seconds)

    proc = subprocess.run(command, cwd=REPO_ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if proc.stdout:
        with args.log.open("a", encoding="utf-8") as handle:
            handle.write(proc.stdout)
    log_line(args.log, f"launch command exited rc={proc.returncode}")
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
