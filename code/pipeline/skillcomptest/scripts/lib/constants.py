"""Canonical constants and classification logic shared across pipeline scripts."""

PASS_THRESHOLD_FULL = 8
PASS_THRESHOLD_PARTIAL = 5

CANONICAL_STATUSES = ("pass", "fail", "blocked", "unsupported")

FAILURE_CATEGORIES = (
    "missing_dependency",
    "missing_capability",
    "os_api_incompatible",
    "path_shell_assumption",
    "permission_model",
    "gui_or_browser_unavailable",
    "network_or_service",
    "encoding_locale",
    "packaging_or_install",
    "runtime_crash",
    "logic_error",
    "unknown",
)


def compute_verdict(pass_count, fail_count, blocked_count, unsupported_count, total):
    """Single source of truth for portability verdict classification."""
    if total == 0:
        return "partial_support"
    if pass_count >= PASS_THRESHOLD_FULL:
        return "full_support"
    if pass_count >= PASS_THRESHOLD_PARTIAL:
        return "partial_support"
    if blocked_count > total / 2:
        return "blocked"
    if unsupported_count > total / 2:
        return "unsupported"
    return "partial_support"
