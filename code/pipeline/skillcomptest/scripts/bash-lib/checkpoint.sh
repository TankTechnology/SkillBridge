#!/usr/bin/env bash
# Checkpoint validation helpers. Source after config.sh.

_checkpoint_valid() {
  local type="$1" path="$2" target="${3:-0}"
  [ ! -f "$path" ] && return 1
  python3 "$REPO_ROOT/scripts/pipeline-utils.py" checkpoint "$type" "$path" ${target:+"$target"} 2>/dev/null
}

update_progress() {
  local skills_done="${1:-0}"
  local tasks_done="${2:-0}"
  python3 "$REPO_ROOT/scripts/pipeline-utils.py" progress \
    "$PROGRESS_FILE" "$skills_done" "${TOTAL:-0}" "$tasks_done" \
    "$((${TOTAL:-0} * TASK_COUNT))" "$(($(date +%s) - PIPELINE_START))" 2>/dev/null || true
}
