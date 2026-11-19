#!/usr/bin/env python3
"""Validate the raw-data release for parseability and release hygiene."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any


PATTERNS = {
    "openai_token": re.compile(r"(?<![A-Za-z0-9_])sk-[A-Za-z0-9_-]{12,}"),
    "github_token": re.compile(r"(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    "google_key": re.compile(r"AIza[0-9A-Za-z_-]{20,}"),
    "aws_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "bearer_token": re.compile(r"Bearer\s+[A-Za-z0-9._-]{20,}"),
    "private_key": re.compile(r"-----BEGIN (?:RSA|OPENSSH|EC|DSA|PGP) PRIVATE KEY-----"),
    "auth_url": re.compile(r"https?://[^\s/@]+:[^\s/@]+@"),
    "email": re.compile(r"[A-Za-z0-9_.%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "posix_user_path": re.compile(r"/(?:home|Users|root)/[A-Za-z0-9_.-]+/"),
    "windows_user_path": re.compile(r"(?i)[A-Z]:[\\/]Users[\\/][^\\/\s\"']+"),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("data/raw"))
    parser.add_argument("--out-dir", type=Path, default=Path("audit"))
    return parser.parse_args()


def scan_file(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    suffix = path.suffix.lower()
    rows = None
    if suffix == ".json":
        json.loads(text)
    elif suffix == ".jsonl":
        rows = 0
        for line in text.splitlines():
            if line.strip():
                json.loads(line)
                rows += 1
    elif suffix == ".csv":
        rows = sum(1 for _ in csv.DictReader(text.splitlines()))
    hits = []
    for name, pattern in PATTERNS.items():
        match = pattern.search(text)
        if match:
            hits.append({"pattern": name, "excerpt": match.group(0)[:120]})
    return {
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "rows": rows,
        "hygiene_hits": hits,
    }


def main() -> int:
    args = parse_args()
    if not args.root.exists():
        raise SystemExit(f"raw data root does not exist: {args.root}")
    files = sorted(path for path in args.root.rglob("*") if path.is_file())
    report: dict[str, Any] = {"root": str(args.root), "files": {}, "errors": []}
    for path in files:
        relative = str(path.relative_to(args.root))
        try:
            report["files"][relative] = scan_file(path)
        except Exception as exc:
            report["errors"].append({"path": relative, "error": str(exc)})
    hits = [
        {"path": path, **hit}
        for path, details in report["files"].items()
        for hit in details["hygiene_hits"]
    ]
    report["hygiene_hits"] = hits
    report["file_count"] = len(files)
    report["total_bytes"] = sum(item["bytes"] for item in report["files"].values())
    report["status"] = "fail" if report["errors"] or hits else "pass"

    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "raw-release-audit.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    lines = [
        "# Raw Release Audit",
        "",
        f"- Status: **{report['status']}**",
        f"- Files: {report['file_count']}",
        f"- Bytes: {report['total_bytes']}",
        f"- Parse errors: {len(report['errors'])}",
        f"- Hygiene hits: {len(hits)}",
        "",
        "The audit checks JSON/JSONL/CSV parseability and scans raw records for",
        "credential-like values, authenticated URLs, email addresses, and local",
        "user paths. It does not claim anonymity against external linkage.",
        "",
    ]
    if report["errors"]:
        lines.extend(["## Parse errors", ""])
        lines.extend(f"- `{item['path']}`: {item['error']}" for item in report["errors"])
        lines.append("")
    if hits:
        lines.extend(["## Hygiene hits", ""])
        lines.extend(f"- `{item['path']}`: {item['pattern']}" for item in hits)
        lines.append("")
    (args.out_dir / "raw-release-audit.md").write_text("\n".join(lines), encoding="utf-8")

    print(json.dumps({"status": report["status"], "files": report["file_count"], "hygiene_hits": len(hits)}, sort_keys=True))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
