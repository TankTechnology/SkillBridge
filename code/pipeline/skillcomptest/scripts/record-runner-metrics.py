#!/usr/bin/env python3
"""Write structured metrics for one LLM runner invocation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.lib.runner_metrics import extract_usage, load_json_payloads, summarize_metric_records


def read_existing_attempts(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict) and isinstance(data.get("attempts"), list):
        return [item for item in data["attempts"] if isinstance(item, dict)]
    if isinstance(data, dict) and data.get("schema_version") == "runner_metrics_v1":
        return [data]
    return []


def merge_attempts(current: dict, existing_attempts: list[dict]) -> dict:
    attempts = [item for item in existing_attempts if item.get("attempt") != current["attempt"]]
    attempts.append(current)
    attempts.sort(key=lambda item: item.get("attempt", 0))
    summary = summarize_metric_records(attempts)
    last = attempts[-1]
    merged = {
        "schema_version": "runner_metrics_v1",
        "label": last["label"],
        "runner": last["runner"],
        "model": last["model"],
        "attempt": last["attempt"],
        "attempt_count": len(attempts),
        "exit_code": last["exit_code"],
        "log_file": last["log_file"],
        **summary,
        "attempts": attempts,
    }
    return merged


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-file", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--runner", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--attempt", type=int, required=True)
    parser.add_argument("--duration-s", type=int, required=True)
    parser.add_argument("--exit-code", type=int, required=True)
    parser.add_argument("--prompt-file", default="")
    args = parser.parse_args()

    log_path = Path(args.log_file)
    prompt_path = Path(args.prompt_file) if args.prompt_file else None
    payloads = load_json_payloads(log_path)
    usage = extract_usage(payloads)
    log_chars = log_path.stat().st_size if log_path.is_file() else 0
    prompt_chars = prompt_path.stat().st_size if prompt_path and prompt_path.is_file() else 0

    metrics = {
        "schema_version": "runner_metrics_v1",
        "label": args.label,
        "runner": args.runner,
        "model": args.model,
        "attempt": args.attempt,
        "duration_s": args.duration_s,
        "exit_code": args.exit_code,
        "log_file": str(log_path),
        "log_chars": log_chars,
        "prompt_chars": prompt_chars,
        "estimated_log_tokens": log_chars // 4,
        "estimated_prompt_tokens": prompt_chars // 4,
        "json_payload_count": len(payloads),
        **usage,
    }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    merged = merge_attempts(metrics, read_existing_attempts(output))
    output.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
