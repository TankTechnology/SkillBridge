"""TypedDict schemas for pipeline data structures.

Used for documentation and mypy type checking. Not enforced at runtime.
"""

from typing import List, TypedDict


class TaskResult(TypedDict):
    task_id: str
    status: str
    failure_category: str
    duration_s: float
    summary: str


class PlatformSummary(TypedDict):
    skill: str
    platform: str
    pass_count: int
    fail_count: int
    blocked_count: int
    unsupported_count: int
    platform_success_rate: float
    portability_verdict: str
    failure_categories_observed: List[str]
    portability_blocker_types: List[str]
    environment_summary: dict
    generated_by: str
    generated_at: str


class BatchReportEntry(TypedDict):
    source: str
    skill: str
    status: str
    tasks_generated: int
    tasks_pass: int
    tasks_fail: int
    tasks_blocked: int
    tasks_unsupported: int
    success_rate: float
    portability_verdict: str
    exec_duration_s: float
    est_tokens: int
    log_chars: int
