#!/usr/bin/env python3
"""Build a ClawHub skill-version inventory and reusable queue lists."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SKILLS_DIR = REPO_ROOT / ".cache" / "skillcomptest" / "SkillsRepo"
DEFAULT_DOCS_DIR = REPO_ROOT / "docs" / "linux-testing" / "inventory"
DEFAULT_QUEUE_DIR = REPO_ROOT / ".cache" / "skillcomptest" / "queues"


@dataclass(frozen=True)
class SkillVersion:
    source: str
    slug: str
    version: str
    dirname: str
    skill_id: str
    skill_file: str
    published_at: int | None
    owner_id: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def version_key(version: str) -> tuple[tuple[int, int | str], ...]:
    """Return a deterministic semver-ish key without extra dependencies."""
    parts: list[tuple[int, int | str]] = []
    for token in re.findall(r"\d+|[A-Za-z]+", version):
        if token.isdigit():
            parts.append((0, int(token)))
        else:
            parts.append((1, token.lower()))
    return tuple(parts)


def entry_sort_key(entry: SkillVersion) -> tuple[Any, ...]:
    return (entry.slug, version_key(entry.version), entry.dirname)


def latest_sort_key(entry: SkillVersion) -> tuple[Any, ...]:
    published = entry.published_at if entry.published_at is not None else -1
    return (published, version_key(entry.version), entry.dirname)


def load_meta(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def find_skill_file(skill_dir: Path) -> Path:
    for name in ("SKILL.md", "skill.md"):
        path = skill_dir / name
        if path.is_file():
            return path
    return skill_dir / "SKILL.md"


def scan_source(skills_dir: Path, source: str) -> tuple[list[SkillVersion], list[str]]:
    source_dir = skills_dir / source
    if not source_dir.is_dir():
        raise FileNotFoundError(f"source directory not found: {source_dir}")

    entries: list[SkillVersion] = []
    missing_meta: list[str] = []
    for skill_dir in sorted(path for path in source_dir.iterdir() if path.is_dir()):
        meta = load_meta(skill_dir / "_meta.json")
        skill_file = find_skill_file(skill_dir)
        if meta is None:
            missing_meta.append(skill_dir.name)
            continue
        slug = str(meta.get("slug") or skill_dir.name)
        version = str(meta.get("version") or "")
        owner_id = str(meta.get("ownerId") or "")
        published_raw = meta.get("publishedAt")
        published_at = int(published_raw) if isinstance(published_raw, (int, float)) else None
        entries.append(
            SkillVersion(
                source=source,
                slug=slug,
                version=version,
                dirname=skill_dir.name,
                skill_id=f"{source}/{skill_dir.name}",
                skill_file=display_path(skill_file),
                published_at=published_at,
                owner_id=owner_id,
            )
        )
    return entries, missing_meta


def group_by_slug(entries: list[SkillVersion]) -> dict[str, list[SkillVersion]]:
    grouped: dict[str, list[SkillVersion]] = {}
    for entry in entries:
        grouped.setdefault(entry.slug, []).append(entry)
    return grouped


def select_latest_by_slug(grouped: dict[str, list[SkillVersion]]) -> list[SkillVersion]:
    return sorted((max(items, key=latest_sort_key) for items in grouped.values()), key=lambda item: item.slug)


def distinct_version_groups(grouped: dict[str, list[SkillVersion]]) -> dict[str, list[SkillVersion]]:
    return {
        slug: sorted(items, key=entry_sort_key)
        for slug, items in grouped.items()
        if len({item.version for item in items}) > 1
    }


def same_version_duplicate_groups(grouped: dict[str, list[SkillVersion]]) -> dict[str, list[SkillVersion]]:
    return {
        slug: sorted(items, key=entry_sort_key)
        for slug, items in grouped.items()
        if len(items) > 1 and len({item.version for item in items}) == 1
    }


def write_list(path: Path, entries: list[SkillVersion], header: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(entry.skill_id for entry in entries)
    path.write_text(f"# {header}\n{body}\n", encoding="utf-8")


def display_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(REPO_ROOT))
    except ValueError:
        return str(resolved)


def compact_entry(entry: SkillVersion) -> dict[str, Any]:
    return asdict(entry)


def render_markdown(
    inventory: dict[str, Any],
    multi_version: dict[str, list[SkillVersion]],
    same_version_duplicates: dict[str, list[SkillVersion]],
) -> str:
    summary = inventory["summary"]
    outputs = inventory["outputs"]

    lines = [
        "# ClawHub Version Inventory",
        "",
        f"Generated at: `{inventory['generated_at']}`",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Source directories | {summary['total_dirs']} |",
        f"| Valid metadata entries | {summary['valid_meta_entries']} |",
        f"| Missing or bad metadata directories | {summary['missing_meta_dirs']} |",
        f"| Unique skill slugs | {summary['unique_skill_slugs']} |",
        f"| Slugs with distinct versions | {summary['slugs_with_distinct_versions']} |",
        f"| Entries in distinct-version slugs | {summary['entries_in_distinct_version_slugs']} |",
        f"| Slugs with same-version duplicate dirs | {summary['slugs_with_same_version_duplicate_dirs']} |",
        "",
        "## Queue Lists",
        "",
        "| Purpose | Path | Count |",
        "| --- | --- | ---: |",
        f"| Package-level benchmark, keep every published version | `{outputs['all_versions_list']}` | {summary['valid_meta_entries']} |",
        f"| Coverage benchmark, one latest directory per slug | `{outputs['latest_per_skill_list']}` | {summary['latest_per_skill_entries']} |",
        f"| Version-regression analysis, only slugs with distinct versions | `{outputs['multiversion_regression_list']}` | {summary['entries_in_distinct_version_slugs']} |",
        "",
        "## Policy",
        "",
        "- Treat `source/dirname` as the unique benchmark package identity.",
        "- Treat `_meta.json.slug` as the logical skill family.",
        "- Use the latest-per-skill list when estimating current ClawHub Linux coverage.",
        "- Use the all-versions list when the experiment cares about package-level history.",
        "- Use the multiversion-regression list when looking for version regressions such as missing files between releases.",
        "- Agents may read other versions for context, but must not copy files across versions into the tested skill source.",
        "",
        "## Distinct-Version Skill Families",
        "",
        "| Slug | Latest selected | Versions |",
        "| --- | --- | --- |",
    ]

    for slug, items in sorted(multi_version.items()):
        latest = max(items, key=latest_sort_key)
        versions = ", ".join(f"{item.version} (`{item.dirname}`)" for item in items)
        lines.append(f"| `{slug}` | `{latest.skill_id}` | {versions} |")

    lines.extend(["", "## Same-Version Duplicate Directories", ""])
    if same_version_duplicates:
        lines.extend(["| Slug | Version | Directories |", "| --- | --- | --- |"])
        for slug, items in sorted(same_version_duplicates.items()):
            dirs = ", ".join(f"`{item.dirname}`" for item in items)
            lines.append(f"| `{slug}` | `{items[0].version}` | {dirs} |")
    else:
        lines.append("None.")

    lines.extend(["", "## Missing Or Bad Metadata", ""])
    if inventory["missing_meta_dirs"]:
        for dirname in inventory["missing_meta_dirs"]:
            lines.append(f"- `{dirname}`")
    else:
        lines.append("None.")

    return "\n".join(lines) + "\n"


def build_inventory(
    skills_dir: Path,
    source: str,
    docs_dir: Path,
    queue_dir: Path,
) -> dict[str, Any]:
    entries, missing_meta = scan_source(skills_dir, source)
    grouped = group_by_slug(entries)
    latest_entries = select_latest_by_slug(grouped)
    multi_version = distinct_version_groups(grouped)
    same_version_duplicates = same_version_duplicate_groups(grouped)
    multiversion_entries = [
        entry
        for _slug, items in sorted(multi_version.items())
        for entry in sorted(items, key=entry_sort_key)
    ]

    all_versions_list = queue_dir / f"{source.lower()}-all-versions.list"
    latest_per_skill_list = queue_dir / f"{source.lower()}-latest-per-skill.list"
    multiversion_regression_list = queue_dir / f"{source.lower()}-multiversion-regression.list"
    write_list(all_versions_list, sorted(entries, key=entry_sort_key), f"{source} all valid versioned package entries")
    write_list(latest_per_skill_list, latest_entries, f"{source} latest package per logical skill slug")
    write_list(
        multiversion_regression_list,
        multiversion_entries,
        f"{source} package entries whose logical skill slug has distinct versions",
    )

    summary = {
        "total_dirs": len([path for path in (skills_dir / source).iterdir() if path.is_dir()]),
        "valid_meta_entries": len(entries),
        "missing_meta_dirs": len(missing_meta),
        "unique_skill_slugs": len(grouped),
        "latest_per_skill_entries": len(latest_entries),
        "slugs_with_distinct_versions": len(multi_version),
        "entries_in_distinct_version_slugs": len(multiversion_entries),
        "slugs_with_same_version_duplicate_dirs": len(same_version_duplicates),
    }
    outputs = {
        "all_versions_list": display_path(all_versions_list),
        "latest_per_skill_list": display_path(latest_per_skill_list),
        "multiversion_regression_list": display_path(multiversion_regression_list),
        "json": display_path(docs_dir / f"{source.lower()}-version-inventory.json"),
        "markdown": display_path(docs_dir / f"{source.lower()}-version-inventory.md"),
    }
    inventory: dict[str, Any] = {
        "schema_version": "clawhub_version_inventory_v1",
        "generated_at": utc_now(),
        "source": source,
        "skills_dir": display_path(skills_dir),
        "summary": summary,
        "outputs": outputs,
        "missing_meta_dirs": missing_meta,
        "same_version_duplicates": [
            {"slug": slug, "entries": [compact_entry(entry) for entry in items]}
            for slug, items in sorted(same_version_duplicates.items())
        ],
        "distinct_version_skill_families": [
            {
                "slug": slug,
                "latest_skill_id": max(items, key=latest_sort_key).skill_id,
                "entries": [compact_entry(entry) for entry in items],
            }
            for slug, items in sorted(multi_version.items())
        ],
        "latest_per_skill": [compact_entry(entry) for entry in latest_entries],
    }

    docs_dir.mkdir(parents=True, exist_ok=True)
    json_path = docs_dir / f"{source.lower()}-version-inventory.json"
    json_path.write_text(json.dumps(inventory, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md_path = docs_dir / f"{source.lower()}-version-inventory.md"
    md_path.write_text(render_markdown(inventory, multi_version, same_version_duplicates), encoding="utf-8")
    return inventory


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skills-dir", default=str(DEFAULT_SKILLS_DIR))
    parser.add_argument("--source", default="ClawHub")
    parser.add_argument("--docs-dir", default=str(DEFAULT_DOCS_DIR))
    parser.add_argument("--queue-dir", default=str(DEFAULT_QUEUE_DIR))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        inventory = build_inventory(
            skills_dir=Path(args.skills_dir),
            source=args.source,
            docs_dir=Path(args.docs_dir),
            queue_dir=Path(args.queue_dir),
        )
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    summary = inventory["summary"]
    print(f"Source: {inventory['source']}")
    print(f"Valid entries: {summary['valid_meta_entries']}")
    print(f"Unique slugs: {summary['unique_skill_slugs']}")
    print(f"Distinct-version slugs: {summary['slugs_with_distinct_versions']}")
    print(f"Latest-per-skill list: {inventory['outputs']['latest_per_skill_list']}")
    print(f"All-versions list: {inventory['outputs']['all_versions_list']}")
    print(f"Multiversion-regression list: {inventory['outputs']['multiversion_regression_list']}")
    print(f"Inventory: {inventory['outputs']['markdown']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
