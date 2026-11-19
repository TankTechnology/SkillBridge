"""Batch queue planning for SkillCompTest.

This module only inspects existing artifacts and writes queue metadata. It must
not execute skill code.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
from typing import Iterable

from scripts.lib.io_utils import load_json


FINAL_STATUSES = {"pass", "fail", "blocked", "unsupported"}

QUEUE_STATES = {
    "untested": {"never_seen", "sample_ready"},
    "retry_incomplete": {"sample_incomplete", "exec_incomplete"},
    "all": {"never_seen", "sample_ready", "sample_incomplete", "exec_incomplete", "exec_complete"},
}


@dataclass(frozen=True)
class RunProfile:
    platform: str
    task_count: int
    task_design_model: str
    runner: str
    claude_model: str


@dataclass(frozen=True)
class SkillCandidate:
    source: str
    name: str
    skill_file: str

    @property
    def skill_id(self) -> str:
        return f"{self.source}/{self.name}"

    def queue_line(self) -> str:
        return f"{self.source}|{self.name}|{self.skill_file}"


@dataclass
class SkillPlanItem:
    source: str
    name: str
    skill_file: str
    state: str
    decision: str
    reason: str
    sample_tasks: int = 0
    exec_tasks: int = 0
    final_exec_tasks: int = 0
    refresh_sample: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def skill_id(self) -> str:
        return f"{self.source}/{self.name}" if self.source else self.name

    def queue_line(self) -> str:
        return f"{self.source}|{self.name}|{self.skill_file}"


@dataclass
class BatchPlanResult:
    profile: RunProfile
    queue_policy: str
    sample_refresh_policy: str
    allow_rerun_complete: bool
    items: list[SkillPlanItem]
    queued: list[SkillPlanItem]
    missing: list[SkillPlanItem]
    refresh_samples: list[SkillPlanItem]
    counts_by_state: dict[str, int]

    def to_json_dict(self, batch_id: str, candidate_source: str) -> dict:
        return {
            "batch_id": batch_id,
            "candidate_source": candidate_source,
            "profile": asdict(self.profile),
            "queue_policy": self.queue_policy,
            "sample_refresh_policy": self.sample_refresh_policy,
            "allow_rerun_complete": self.allow_rerun_complete,
            "candidate_count": len(self.items),
            "queued_count": len(self.queued),
            "missing_count": len(self.missing),
            "refresh_sample_count": len(self.refresh_samples),
            "counts_by_state": self.counts_by_state,
            "queued": [item.skill_id for item in self.queued],
            "missing": [item.skill_id for item in self.missing],
            "refresh_samples": [item.skill_id for item in self.refresh_samples],
            "items": [asdict(item) | {"skill_id": item.skill_id} for item in self.items],
        }


def load_skill_list(path: Path | str) -> list[str]:
    skill_list = Path(path)
    ids: list[str] = []
    for raw in skill_list.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        ids.append(line)
    return ids


def scan_skills(skills_dir: Path | str, source: str = "", skill: str = "") -> list[SkillCandidate]:
    root = Path(skills_dir)
    candidates: list[SkillCandidate] = []
    for skill_file in sorted(root.glob("*/*/SKILL.md")):
        name = skill_file.parent.name
        src = skill_file.parent.parent.name
        if source and src != source:
            continue
        if skill and name != skill:
            continue
        candidates.append(SkillCandidate(src, name, str(skill_file)))
    return candidates


def resolve_skill_ids(
    skills_dir: Path | str,
    skill_ids: Iterable[str],
) -> tuple[list[SkillCandidate], list[SkillPlanItem]]:
    root = Path(skills_dir)
    candidates: list[SkillCandidate] = []
    missing: list[SkillPlanItem] = []
    for skill_id in skill_ids:
        try:
            source, name = skill_id.split("/", 1)
        except ValueError:
            missing.append(
                SkillPlanItem("", skill_id, "", "missing_skill", "skip", "skill id must be Source/name")
            )
            continue
        skill_file = root / source / name / "SKILL.md"
        if skill_file.is_file():
            candidates.append(SkillCandidate(source, name, str(skill_file)))
        else:
            missing.append(
                SkillPlanItem(source, name, str(skill_file), "missing_skill", "skip", "skill not found")
            )
    return candidates, missing


def _sample_root(samples_dir: Path, candidate: SkillCandidate, profile: RunProfile) -> Path:
    return samples_dir / candidate.source / candidate.name / profile.task_design_model


def _exec_root(exec_dir: Path, candidate: SkillCandidate, profile: RunProfile) -> Path:
    return exec_dir / profile.platform / candidate.source / candidate.name / profile.task_design_model / profile.task_design_model


def _manifest_task_count(path: Path) -> tuple[int, bool]:
    data = load_json(path)
    if not isinstance(data, dict):
        return 0, False
    tasks = data.get("functional_tasks", data.get("tasks", []))
    if not isinstance(tasks, list):
        return 0, False
    return len(tasks), True


def _sample_task_dir_count(root: Path) -> int:
    tasks_dir = root / "tasks"
    if not tasks_dir.is_dir():
        return 0
    return sum(1 for child in tasks_dir.iterdir() if child.is_dir())


def _exec_status_counts(root: Path) -> tuple[int, int, list[str]]:
    tasks_dir = root / "tasks"
    if not tasks_dir.is_dir():
        return 0, 0, []
    total = 0
    final = 0
    errors: list[str] = []
    for metrics_path in sorted(tasks_dir.glob("T_*/task_metrics.json")):
        total += 1
        metrics = load_json(metrics_path)
        if not isinstance(metrics, dict):
            errors.append(f"malformed metrics: {metrics_path}")
            continue
        if metrics.get("status") in FINAL_STATUSES:
            final += 1
    return total, final, errors


def _summary_total(root: Path) -> tuple[int, bool]:
    data = load_json(root / "platform_summary.json")
    if not isinstance(data, dict):
        return 0, False
    total = sum(
        int(data.get(key, 0) or 0)
        for key in ("pass_count", "fail_count", "blocked_count", "unsupported_count")
    )
    return total, True


def classify_candidate(
    candidate: SkillCandidate,
    samples_dir: Path | str,
    exec_dir: Path | str,
    profile: RunProfile,
    sample_refresh_policy: str = "never",
) -> SkillPlanItem:
    samples_root = _sample_root(Path(samples_dir), candidate, profile)
    exec_root = _exec_root(Path(exec_dir), candidate, profile)

    manifest_count, manifest_valid = _manifest_task_count(samples_root / "benchmark_manifest.json")
    sample_dir_count = _sample_task_dir_count(samples_root)
    sample_tasks = min(manifest_count, sample_dir_count)
    sample_exists = samples_root.exists()
    sample_complete = manifest_valid and sample_tasks >= profile.task_count

    exec_total, final_exec_tasks, exec_errors = _exec_status_counts(exec_root)
    summary_total, summary_valid = _summary_total(exec_root)
    exec_complete = summary_valid and final_exec_tasks >= profile.task_count and summary_total >= profile.task_count

    refresh_sample = False
    errors = list(exec_errors)

    if exec_complete:
        state = "exec_complete"
        reason = "exec output has enough final task metrics for this profile"
    elif exec_total > 0 or summary_valid:
        state = "exec_incomplete"
        reason = "exec output exists but is incomplete for this profile"
    elif sample_complete:
        state = "sample_ready"
        reason = "sample output is ready and exec has not run"
        refresh_sample = sample_refresh_policy == "missing_exec" and sample_exists and exec_total == 0
    elif sample_exists:
        state = "sample_incomplete"
        reason = "sample output exists but is incomplete for this profile"
    else:
        state = "never_seen"
        reason = "no sample or exec output for this profile"

    return SkillPlanItem(
        source=candidate.source,
        name=candidate.name,
        skill_file=candidate.skill_file,
        state=state,
        decision="skip",
        reason=reason,
        sample_tasks=sample_tasks,
        exec_tasks=exec_total,
        final_exec_tasks=final_exec_tasks,
        refresh_sample=refresh_sample,
        errors=errors,
    )


def _should_queue(state: str, queue_policy: str, allow_rerun_complete: bool = False) -> bool:
    try:
        if state == "exec_complete" and queue_policy == "all" and not allow_rerun_complete:
            return False
        return state in QUEUE_STATES[queue_policy]
    except KeyError as exc:
        raise ValueError(f"unsupported queue policy: {queue_policy}") from exc


def plan_batch(
    skills_dir: Path | str,
    samples_dir: Path | str,
    exec_dir: Path | str,
    profile: RunProfile,
    queue_policy: str = "untested",
    source: str = "",
    skill: str = "",
    max_skills: int | None = None,
    skill_ids: Iterable[str] | None = None,
    sample_refresh_policy: str = "never",
    allow_rerun_complete: bool = False,
) -> BatchPlanResult:
    if queue_policy not in QUEUE_STATES:
        raise ValueError(f"unsupported queue policy: {queue_policy}")
    if sample_refresh_policy not in {"never", "missing_exec"}:
        raise ValueError(f"unsupported sample refresh policy: {sample_refresh_policy}")

    if skill_ids is None:
        candidates = scan_skills(skills_dir, source=source, skill=skill)
        missing: list[SkillPlanItem] = []
    else:
        candidates, missing = resolve_skill_ids(skills_dir, skill_ids)

    items = [
        classify_candidate(candidate, samples_dir, exec_dir, profile, sample_refresh_policy)
        for candidate in candidates
    ]
    for item in items:
        item.decision = "queue" if _should_queue(item.state, queue_policy, allow_rerun_complete) else "skip"
        if item.state == "exec_complete" and queue_policy == "all" and not allow_rerun_complete:
            item.reason = "exec output is complete; set ALLOW_RERUN_COMPLETE=1 to rerun completed skills"

    queued = [item for item in items if item.decision == "queue"]
    if max_skills is not None:
        queued = queued[:max_skills]
        queued_ids = {item.skill_id for item in queued}
        for item in items:
            if item.decision == "queue" and item.skill_id not in queued_ids:
                item.decision = "skip"
                item.reason = "skipped by MAX_SKILLS after policy filtering"

    refresh_samples = [item for item in queued if item.refresh_sample]
    counts_by_state: dict[str, int] = {}
    for item in items + missing:
        counts_by_state[item.state] = counts_by_state.get(item.state, 0) + 1

    return BatchPlanResult(
        profile=profile,
        queue_policy=queue_policy,
        sample_refresh_policy=sample_refresh_policy,
        allow_rerun_complete=allow_rerun_complete,
        items=items + missing,
        queued=queued,
        missing=missing,
        refresh_samples=refresh_samples,
        counts_by_state=counts_by_state,
    )


def write_batch_artifacts(
    result: BatchPlanResult,
    batch_id: str,
    candidate_source: str,
    queue_file: Path | str,
    plan_file: Path | str,
    refresh_file: Path | str,
) -> None:
    queue_path = Path(queue_file)
    plan_path = Path(plan_file)
    refresh_path = Path(refresh_file)
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    refresh_path.parent.mkdir(parents=True, exist_ok=True)

    queue_path.write_text("".join(f"{item.queue_line()}\n" for item in result.queued), encoding="utf-8")
    refresh_path.write_text("".join(f"{item.skill_id}\n" for item in result.refresh_samples), encoding="utf-8")
    plan_path.write_text(
        json.dumps(
            result.to_json_dict(batch_id=batch_id, candidate_source=candidate_source),
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
