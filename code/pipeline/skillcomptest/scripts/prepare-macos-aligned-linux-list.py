#!/usr/bin/env python3
"""Prepare the canonical macOS-aligned Linux remainder list.

This script is read-only with respect to benchmark execution. It normalizes the
macOS v0.4.0 full release asset to ClawHub/SkillSh top5000 source-slug families,
then removes families that already have real Linux execution evidence.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import tarfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


SOURCE_FILES = {
    "ClawHub": "clawhub_top5000.csv",
    "SkillSh": "skillsh_top5000.csv",
}

MACOS_SOURCE_ALIASES = {
    "ClawHub": "ClawHub",
    "ClawhubTop1000": "ClawHub",
    "ClawhubSelect": "ClawHub",
    "ClawhubSelectScripts": "ClawHub",
    "SkillSh": "SkillSh",
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

    @property
    def family(self) -> tuple[str, str]:
        return (self.source, self.slug)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    macos_group = parser.add_mutually_exclusive_group(required=True)
    macos_group.add_argument(
        "--macos-release-tar",
        default="",
        help="macOS v0.4.0 release tar.gz containing data/benchmarks/macos/exec.",
    )
    macos_group.add_argument(
        "--macos-summary-paths",
        default="",
        help="Newline-delimited platform_summary.json member paths from the macOS release tar.",
    )
    parser.add_argument("--ranking-dir", required=True, help="Directory containing ClawHub/SkillSh top5000 CSVs.")
    parser.add_argument(
        "--skills-dir",
        action="append",
        default=[],
        help="Optional extracted skill source root used to resolve SKILL.md. Can be repeated.",
    )
    parser.add_argument("--dynamic-labels", default="", help="Linux dynamic skill labels JSONL.")
    parser.add_argument("--dynamic-observations", default="", help="Linux dynamic task observations JSONL.")
    parser.add_argument("--dynamic-runs-dir", default="", help="Directory of exported Linux run summary JSON files.")
    parser.add_argument(
        "--dynamic-report-dir",
        action="append",
        default=[],
        help="Directory of batch *.report.json files. Can be repeated.",
    )
    parser.add_argument("--dynamic-exec-dir", default="", help="Linux exec artifact directory.")
    parser.add_argument("--platform", default="linux", help="Dynamic-result platform used for skip records.")
    parser.add_argument("--scope", choices=("top5000", "top2000"), default="top5000")
    parser.add_argument("--out", required=True, help="Output concrete skill-list file for the remaining families.")
    parser.add_argument("--families-out", required=True, help="Output normalized Source/slug family list.")
    parser.add_argument("--metadata", required=True, help="Output machine-readable metadata JSON.")
    return parser.parse_args()


def read_ranked_rows(ranking_dir: Path) -> list[RankedRow]:
    rows: list[RankedRow] = []
    for source_order, (source, filename) in enumerate(SOURCE_FILES.items()):
        csv_path = ranking_dir / filename
        if not csv_path.is_file():
            continue
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
    rows.sort(key=lambda item: (item.rank, item.source_order))
    return rows


def normalize_macos_source(source: str) -> str | None:
    return MACOS_SOURCE_ALIASES.get(source)


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


def infer_semver_slug(name: str) -> str:
    match = re.match(r"^(.*?)-(?:v)?\d+(?:\.\d+){0,3}(?:[-._][A-Za-z0-9]+)?$", name)
    return match.group(1) if match else name


def build_rank_indexes(
    ranked_rows: list[RankedRow],
    scope: str,
) -> tuple[set[tuple[str, str]], dict[tuple[str, str], RankedRow], dict[str, list[str]]]:
    max_rank = 2000 if scope == "top2000" else 5000
    families: set[tuple[str, str]] = set()
    best_row_by_family: dict[tuple[str, str], RankedRow] = {}
    slugs_by_source: dict[str, set[str]] = {}
    for row in ranked_rows:
        slugs_by_source.setdefault(row.source, set()).add(row.slug)
        if row.rank > max_rank:
            continue
        families.add(row.family)
        best_row_by_family.setdefault(row.family, row)
    sorted_slugs = {
        source: sorted(slugs, key=lambda item: (-len(item), item))
        for source, slugs in slugs_by_source.items()
    }
    return families, best_row_by_family, sorted_slugs


def normalize_family(source: str, skill_name: str, slugs_by_source: dict[str, list[str]]) -> tuple[str, str] | None:
    normalized_source = normalize_macos_source(source)
    if normalized_source is None:
        return None
    slug = infer_slug_from_name(skill_name, slugs_by_source.get(normalized_source, []))
    if slug is None:
        slug = infer_semver_slug(skill_name)
    return (normalized_source, slug)


def parse_platform_summary_path(path: str) -> tuple[str, str] | None:
    parts = path.split("/")
    try:
        idx = parts.index("exec")
    except ValueError:
        return None
    if len(parts) <= idx + 2:
        return None
    source = parts[idx + 1]
    skill = parts[idx + 2]
    if source.startswith("._") or skill.startswith("._"):
        return None
    return (source, skill)


def iter_macos_summary_paths_from_tar(path: Path) -> list[str]:
    summary_paths: list[str] = []
    with tarfile.open(path, "r:gz") as archive:
        for member in archive:
            name = member.name
            if "/._" in name or name.rsplit("/", 1)[-1].startswith("._"):
                continue
            if name.endswith("/platform_summary.json") and "/macos/exec/" in name:
                summary_paths.append(name)
    return summary_paths


def read_macos_summary_paths(args: argparse.Namespace) -> list[str]:
    if args.macos_summary_paths:
        path = Path(args.macos_summary_paths)
        return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return iter_macos_summary_paths_from_tar(Path(args.macos_release_tar))


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


def collect_linux_skill_ids(args: argparse.Namespace) -> set[str]:
    paths = [Path(path) for path in (args.dynamic_labels, args.dynamic_observations) if path]
    dynamic_ids = read_dynamic_skill_ids(paths, args.platform)
    if args.dynamic_runs_dir:
        dynamic_ids.update(read_run_summary_skill_ids(Path(args.dynamic_runs_dir), args.platform))
    for report_dir in args.dynamic_report_dir:
        dynamic_ids.update(read_run_summary_skill_ids(Path(report_dir), args.platform))
    if args.dynamic_exec_dir:
        dynamic_ids.update(read_exec_artifact_skill_ids(Path(args.dynamic_exec_dir)))
    return dynamic_ids


def dynamic_family_keys(dynamic_skill_ids: set[str], slugs_by_source: dict[str, list[str]]) -> set[tuple[str, str]]:
    families: set[tuple[str, str]] = set()
    for skill_id in dynamic_skill_ids:
        try:
            source, name = skill_id.split("/", 1)
        except ValueError:
            continue
        family = normalize_family(source, name, slugs_by_source)
        if family and family[0] in SOURCE_FILES:
            families.add(family)
    return families


def resolve_skill_id(skills_dirs: list[Path], row: RankedRow) -> tuple[str, bool]:
    preferred = f"{row.source}/{row.preferred_name}"
    if not skills_dirs:
        return preferred, True
    candidates = [row.preferred_name]
    if row.slug not in candidates:
        candidates.append(row.slug)
    for skills_dir in skills_dirs:
        source_root = skills_dir / row.source
        for name in candidates:
            if (source_root / name / "SKILL.md").is_file():
                return f"{row.source}/{name}", True
    return preferred, False


def write_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    ranked_rows = read_ranked_rows(Path(args.ranking_dir))
    rank_families, best_row_by_family, slugs_by_source = build_rank_indexes(ranked_rows, args.scope)
    summary_paths = read_macos_summary_paths(args)

    macos_entries: set[tuple[str, str]] = set()
    macos_source_counts: Counter[str] = Counter()
    for path in summary_paths:
        parsed = parse_platform_summary_path(path)
        if parsed is None:
            continue
        macos_entries.add(parsed)
        macos_source_counts[parsed[0]] += 1

    macos_families = {
        family
        for source, skill in macos_entries
        for family in [normalize_family(source, skill, slugs_by_source)]
        if family and family in rank_families
    }
    dynamic_skill_ids = collect_linux_skill_ids(args)
    linux_families = dynamic_family_keys(dynamic_skill_ids, slugs_by_source)
    overlapped_families = macos_families & linux_families
    remaining_families = sorted(macos_families - linux_families)

    skills_dirs = [Path(path) for path in args.skills_dir if path]
    concrete_skill_ids: list[str] = []
    unresolved_skill_md: list[str] = []
    for family in remaining_families:
        row = best_row_by_family[family]
        skill_id, resolved = resolve_skill_id(skills_dirs, row)
        concrete_skill_ids.append(skill_id)
        if not resolved:
            unresolved_skill_md.append(skill_id)

    family_lines = [f"{source}/{slug}" for source, slug in remaining_families]
    write_lines(Path(args.out), concrete_skill_ids)
    write_lines(Path(args.families_out), family_lines)

    metadata = {
        "schema_version": "macos_aligned_linux_remainder_v1",
        "default_denominator": "macos_v0.4.0_full_asset_top5000_normalized_source_slug_family",
        "scope": args.scope,
        "macos_source_aliases": MACOS_SOURCE_ALIASES,
        "macos_platform_summary_paths": len(summary_paths),
        "macos_unique_source_skill_entries": len(macos_entries),
        "macos_source_counts": dict(sorted(macos_source_counts.items())),
        "rank_family_count": len(rank_families),
        "macos_aligned_family_count": len(macos_families),
        "linux_dynamic_skill_ids": len(dynamic_skill_ids),
        "linux_family_count": len(linux_families),
        "linux_overlap_family_count": len(overlapped_families),
        "remaining_family_count": len(remaining_families),
        "remaining_sources": dict(Counter(source for source, _ in remaining_families)),
        "concrete_skill_ids": len(concrete_skill_ids),
        "unresolved_skill_md_count": len(unresolved_skill_md),
        "unresolved_skill_md_first": unresolved_skill_md[:20],
        "first_families": family_lines[:10],
        "first_skill_ids": concrete_skill_ids[:10],
        "inputs": {
            "macos_release_tar": args.macos_release_tar,
            "macos_summary_paths": args.macos_summary_paths,
            "ranking_dir": args.ranking_dir,
            "skills_dir": args.skills_dir,
            "dynamic_labels": args.dynamic_labels,
            "dynamic_observations": args.dynamic_observations,
            "dynamic_runs_dir": args.dynamic_runs_dir,
            "dynamic_report_dir": args.dynamic_report_dir,
            "dynamic_exec_dir": args.dynamic_exec_dir,
        },
    }
    metadata_path = Path(args.metadata)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"macOS source/skill entries: {metadata['macos_unique_source_skill_entries']}")
    print(f"macOS-aligned families: {metadata['macos_aligned_family_count']}")
    print(f"Linux overlap families: {metadata['linux_overlap_family_count']}")
    print(f"Remaining families: {metadata['remaining_family_count']}")
    print(f"Output: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
