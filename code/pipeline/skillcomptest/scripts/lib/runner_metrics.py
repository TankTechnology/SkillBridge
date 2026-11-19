"""Helpers for extracting runner usage and cost from CLI logs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)

ESTIMATE_KEYS = (
    "estimated_prompt_tokens",
    "estimated_log_tokens",
    "prompt_chars",
    "log_chars",
    "json_payload_count",
    "duration_s",
)


def load_json_payloads(path: Path) -> list[Any]:
    if not path.is_file() or path.stat().st_size == 0:
        return []
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return []
    try:
        return [json.loads(text)]
    except json.JSONDecodeError:
        payloads = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payloads.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return payloads


def _walk(value: Any):
    yield value
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "usage" and isinstance(child, dict):
                continue
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _number(value: Any):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def extract_usage(payloads: list[Any]) -> dict[str, Any]:
    usage = {
        "input_tokens": None,
        "output_tokens": None,
        "cache_creation_input_tokens": None,
        "cache_read_input_tokens": None,
        "total_cost_usd": None,
    }
    token_totals = {key: 0 for key in TOKEN_KEYS}
    token_seen = {key: False for key in TOKEN_KEYS}
    total_cost = 0.0
    cost_seen = False

    for payload in payloads:
        for item in _walk(payload):
            if not isinstance(item, dict):
                continue
            cost = _number(item.get("total_cost_usd"))
            if cost is not None:
                total_cost += float(cost)
                cost_seen = True
            nested_usage = item.get("usage")
            if isinstance(nested_usage, dict):
                for key in TOKEN_KEYS:
                    value = _number(nested_usage.get(key))
                    if value is not None:
                        token_totals[key] += int(value)
                        token_seen[key] = True
            for key in TOKEN_KEYS:
                value = _number(item.get(key))
                if value is not None:
                    token_totals[key] += int(value)
                    token_seen[key] = True

    for key in TOKEN_KEYS:
        if token_seen[key]:
            usage[key] = token_totals[key]
    if cost_seen:
        usage["total_cost_usd"] = round(total_cost, 8)
    return usage


def summarize_metric_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    summary = {
        "input_tokens": None,
        "output_tokens": None,
        "cache_creation_input_tokens": None,
        "cache_read_input_tokens": None,
        "total_cost_usd": None,
        "estimated_prompt_tokens": 0,
        "estimated_log_tokens": 0,
        "prompt_chars": 0,
        "log_chars": 0,
        "json_payload_count": 0,
        "duration_s": 0,
        "metric_file_count": 0,
    }
    token_totals = {key: 0 for key in TOKEN_KEYS}
    token_seen = {key: False for key in TOKEN_KEYS}
    total_cost = 0.0
    cost_seen = False

    for record in records:
        if not isinstance(record, dict):
            continue
        summary["metric_file_count"] += 1
        cost = _number(record.get("total_cost_usd"))
        if cost is not None:
            total_cost += float(cost)
            cost_seen = True
        for key in TOKEN_KEYS:
            value = _number(record.get(key))
            if value is not None:
                token_totals[key] += int(value)
                token_seen[key] = True
        for key in ESTIMATE_KEYS:
            value = _number(record.get(key))
            if value is not None:
                summary[key] += int(value)

    for key in TOKEN_KEYS:
        if token_seen[key]:
            summary[key] = token_totals[key]
    if cost_seen:
        summary["total_cost_usd"] = round(total_cost, 8)
    return summary
