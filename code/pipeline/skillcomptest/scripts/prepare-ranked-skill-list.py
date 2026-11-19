#!/usr/bin/env python3
"""Prepare a rank-ordered skill list from release top-N CSVs.

This script only reads metadata and SKILL.md presence. It must not execute skill
code. The output is intended for scripts/plan-batch.py --skill-list, which does
the profile-specific sample/exec skip check before any expensive run starts.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path


SOURCE_FILES = {
    "ClawHub": "clawhub_top5000.csv",
    "SkillSh": "skillsh_top5000.csv",
}


@dataclass(frozen=True)
class RankedRow:
    source: str
    source_order: int
    rank: int
    slug: str
    version: str

    @property
    def preferred_name(self) -> str:
        return f"{self.slug}-{self.version}" if self.version else self.slug


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ranking-dir", required=True, help="Directory containing clawhub_top5000.csv, etc.")
    parser.add_argument("--skills-dir", required=True, help="Extracted top-5000 skill package root.")
    parser.add_argument("--dynamic-labels", default="", help="Linux dynamic skill labels JSONL to skip.")
    parser.add_argument("--dynamic-observations", default="", help="Linux dynamic task observations JSONL to skip.")
    parser.add_argument("--dynamic-runs-dir", default="", help="Directory of exported Linux run summary JSON files to skip.")
    parser.add_argument(
        "--dynamic-report-dir",
        action="append",
        default=[],
        help="Directory of batch *.report.json files to skip. Can be repeated.",
    )
    parser.add_argument(
        "--dynamic-exec-dir",
        default="",
        help="Linux exec artifact directory containing Source/skill/profile/profile/platform_summary.json files.",
    )
    parser.add_argument("--platform", default="linux", help="Dynamic-result platform used for skip records.")
    parser.add_argument("--sources", default="ClawHub,SkillSh", help="Comma-separated source names.")
    parser.add_argument(
        "--skip-mode",
        choices=("exact", "slug-family"),
        default="exact",
        help="Skip exact package IDs only, or skip prior versions that share a source/slug family.",
    )
    parser.add_argument("--limit", type=int, default=0, help="Maximum output skill IDs after skips. 0 means no limit.")
    parser.add_argument("--out", required=True, help="Output Source/name skill-list file.")
    parser.add_argument("--metadata", required=True, help="Output JSON metadata file.")
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_sources(raw: str) -> list[str]:
    sources = [part.strip() for part in raw.split(",") if part.strip()]
    unknown = [source for source in sources if source not in SOURCE_FILES]
    if unknown:
        raise ValueError(f"unsupported sources: {', '.join(unknown)}")
    return sources


def read_ranked_rows(ranking_dir: Path, sources: list[str]) -> tuple[list[RankedRow], dict[str, dict]]:
    rows: list[RankedRow] = []
    source_meta: dict[str, dict] = {}
    for source_order, source in enumerate(sources):
        csv_path = ranking_dir / SOURCE_FILES[source]
        source_meta[source] = {
            "ranking_csv": str(csv_path),
            "ranking_sha256": "",
            "ranked_rows": 0,
            "matched_skill_md": 0,
            "matched_slug_version_directory": 0,
            "matched_slug_directory": 0,
            "missing_no_skill_md": 0,
            "skipped_existing_dynamic": 0,
            "skipped_existing_dynamic_exact": 0,
            "skipped_existing_dynamic_slug_family": 0,
            "skipped_duplicate": 0,
            "written": 0,
        }
        if not csv_path.is_file():
            source_meta[source]["missing_ranking_csv"] = True
            continue
        source_meta[source]["ranking_sha256"] = sha256_file(csv_path)
        with csv_path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                slug = (row.get("slug") or "").strip()
                version = (row.get("version") or "").strip()
                rank_raw = (row.get("rank") or "").strip()
                if not slug or not rank_raw:
                    continue
                try:
                    rank = int(rank_raw)
                except ValueError:
                    continue
                rows.append(RankedRow(source, source_order, rank, slug, version))
                source_meta[source]["ranked_rows"] += 1
    rows.sort(key=lambda row: (row.rank, row.source_order))
    return rows, source_meta


def read_dynamic_skill_ids(paths: list[Path], platform: str) -> set[str]:
    seen: set[str] = set()
    for path in paths:
        if not path or not path.is_file():
            continue
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("platform") != platform:
                    continue
                skill_id = record.get("skill_id")
                if isinstance(skill_id, str) and "/" in skill_id:
                    seen.add(skill_id)
    return seen


def read_run_summary_skill_ids(runs_dir: Path, platform: str) -> set[str]:
    seen: set[str] = set()
    if not runs_dir.is_dir():
        return seen
    for path in sorted(runs_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get("platform") not in (None, platform):
            continue
        results = data.get("results", [])
        if not isinstance(results, list):
            continue
        for result in results:
            if not isinstance(result, dict):
                continue
            skill_id = result.get("skill_id")
            if isinstance(skill_id, str) and "/" in skill_id:
                seen.add(skill_id)
                continue
            source = result.get("source")
            skill = result.get("skill")
            if isinstance(source, str) and isinstance(skill, str) and source and skill:
                seen.add(f"{source}/{skill}")
    return seen


def read_exec_artifact_skill_ids(exec_dir: Path) -> set[str]:
    seen: set[str] = set()
    if not exec_dir.is_dir():
        return seen
    for summary_path in exec_dir.glob("*/*/*/*/platform_summary.json"):
        try:
            rel = summary_path.relative_to(exec_dir)
        except ValueError:
            continue
        parts = rel.parts
        if len(parts) >= 5:
            seen.add(f"{parts[0]}/{parts[1]}")
    return seen


def resolve_skill_id(skills_dir: Path, row: RankedRow) -> tuple[str, str] | None:
    source_root = skills_dir / row.source
    candidates = []
    if row.preferred_name:
        candidates.append(row.preferred_name)
    if row.slug and row.slug not in candidates:
        candidates.append(row.slug)

    for name in candidates:
        try:
            has_skill = (source_root / name / "SKILL.md").is_file()
        except OSError:
            has_skill = False
        if has_skill:
            match_kind = "matched_slug_directory" if name == row.slug else "matched_slug_version_directory"
            return f"{row.source}/{name}", match_kind
    return None


def build_slug_index(ranked_rows: list[RankedRow]) -> tuple[dict[str, str], dict[str, list[str]]]:
    exact_slug_by_id: dict[str, str] = {}
    slugs_by_source: dict[str, set[str]] = {}
    for row in ranked_rows:
        slugs_by_source.setdefault(row.source, set()).add(row.slug)
        exact_slug_by_id[f"{row.source}/{row.preferred_name}"] = row.slug
        exact_slug_by_id[f"{row.source}/{row.slug}"] = row.slug
    sorted_slugs = {
        source: sorted(slugs, key=lambda item: (-len(item), item))
        for source, slugs in slugs_by_source.items()
    }
    return exact_slug_by_id, sorted_slugs


def infer_slug_from_name(name: str, source_slugs: list[str]) -> str | None:
    for slug in source_slugs:
        if name == slug:
            return slug
        prefix = f"{slug}-"
        if name.startswith(prefix):
            suffix = name[len(prefix) :]
            if suffix and suffix[0].isdigit():
                return slug
    return None


def build_dynamic_slug_keys(
    dynamic_skill_ids: set[str],
    exact_slug_by_id: dict[str, str],
    slugs_by_source: dict[str, list[str]],
) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for skill_id in dynamic_skill_ids:
        try:
            source, name = skill_id.split("/", 1)
        except ValueError:
            continue
        slug = exact_slug_by_id.get(skill_id)
        if slug is None:
            slug = infer_slug_from_name(name, slugs_by_source.get(source, []))
        if slug:
            keys.add((source, slug))
    return keys


def build_ranked_skill_list(
    ranking_dir: Path,
    skills_dir: Path,
    sources: list[str],
    dynamic_skill_ids: set[str],
    limit: int,
    skip_mode: str = "exact",
) -> tuple[list[str], dict]:
    ranked_rows, source_counts = read_ranked_rows(ranking_dir, sources)
    exact_slug_by_id, slugs_by_source = build_slug_index(ranked_rows)
    dynamic_slug_keys = (
        build_dynamic_slug_keys(dynamic_skill_ids, exact_slug_by_id, slugs_by_source)
        if skip_mode == "slug-family"
        else set()
    )
    written: list[str] = []
    emitted: set[str] = set()
    totals = {
        "ranked_rows": len(ranked_rows),
        "matched_skill_md": 0,
        "missing_no_skill_md": 0,
        "skipped_existing_dynamic": 0,
        "skipped_existing_dynamic_exact": 0,
        "skipped_existing_dynamic_slug_family": 0,
        "skipped_duplicate": 0,
        "written_count": 0,
    }

    for row in ranked_rows:
        resolved = resolve_skill_id(skills_dir, row)
        source_meta = source_counts[row.source]
        if resolved is None:
            totals["missing_no_skill_md"] += 1
            source_meta["missing_no_skill_md"] += 1
            continue

        skill_id, match_kind = resolved
        exact_skip = skill_id in dynamic_skill_ids
        slug_family_skip = (
            skip_mode == "slug-family"
            and not exact_skip
            and (row.source, row.slug) in dynamic_slug_keys
        )
        totals["matched_skill_md"] += 1
        source_meta["matched_skill_md"] += 1
        source_meta[match_kind] += 1

        if skill_id in emitted:
            totals["skipped_duplicate"] += 1
            source_meta["skipped_duplicate"] += 1
            continue
        if exact_skip or slug_family_skip:
            totals["skipped_existing_dynamic"] += 1
            if exact_skip:
                totals["skipped_existing_dynamic_exact"] += 1
                source_meta["skipped_existing_dynamic_exact"] = source_meta.get("skipped_existing_dynamic_exact", 0) + 1
            else:
                totals["skipped_existing_dynamic_slug_family"] += 1
                source_meta["skipped_existing_dynamic_slug_family"] = (
                    source_meta.get("skipped_existing_dynamic_slug_family", 0) + 1
                )
            source_meta["skipped_existing_dynamic"] += 1
            continue

        written.append(skill_id)
        emitted.add(skill_id)
        source_meta["written"] += 1
        if limit and len(written) >= limit:
            break

    totals["written_count"] = len(written)
    return written, {
        "schema_version": "ranked_skill_list_v1",
        "sources": sources,
        "skip_mode": skip_mode,
        "limit": limit,
        "dynamic_skill_ids": len(dynamic_skill_ids),
        "dynamic_slug_keys": len(dynamic_slug_keys),
        "source_counts": source_counts,
        **totals,
        "first_skill_ids": written[:10],
        "last_skill_ids": written[-10:],
    }


def main() -> int:
    args = parse_args()
    if args.limit < 0:
        print("ERROR: --limit must be non-negative", file=sys.stderr)
        return 2
    try:
        sources = parse_sources(args.sources)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    dynamic_paths = [Path(path) for path in (args.dynamic_labels, args.dynamic_observations) if path]
    dynamic_skill_ids = read_dynamic_skill_ids(dynamic_paths, args.platform)
    if args.dynamic_runs_dir:
        dynamic_skill_ids.update(read_run_summary_skill_ids(Path(args.dynamic_runs_dir), args.platform))
    for report_dir in args.dynamic_report_dir:
        dynamic_skill_ids.update(read_run_summary_skill_ids(Path(report_dir), args.platform))
    if args.dynamic_exec_dir:
        dynamic_skill_ids.update(read_exec_artifact_skill_ids(Path(args.dynamic_exec_dir)))
    written, metadata = build_ranked_skill_list(
        ranking_dir=Path(args.ranking_dir),
        skills_dir=Path(args.skills_dir),
        sources=sources,
        dynamic_skill_ids=dynamic_skill_ids,
        limit=args.limit,
        skip_mode=args.skip_mode,
    )
    metadata["platform"] = args.platform

    out_path = Path(args.out)
    metadata_path = Path(args.metadata)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("".join(f"{skill_id}\n" for skill_id in written), encoding="utf-8")
    metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Ranked rows: {metadata['ranked_rows']}")
    print(f"Dynamic skip ids: {metadata['dynamic_skill_ids']}")
    print(f"Matched SKILL.md: {metadata['matched_skill_md']}")
    print(f"Skipped existing dynamic: {metadata['skipped_existing_dynamic']}")
    print(f"Missing/no SKILL.md: {metadata['missing_no_skill_md']}")
    print(f"Written: {metadata['written_count']}")
    print(f"Output: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
