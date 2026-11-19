#!/usr/bin/env bash
# Phase 1 worker: run SampleAgent for one skill.
# Invoked by xargs: bash stage1-sample.sh "source|name|skill_file"

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../bash-lib/config.sh"
source "$SCRIPT_DIR/../bash-lib/checkpoint.sh"
source "$SCRIPT_DIR/../bash-lib/retry.sh"
source "$SCRIPT_DIR/../bash-lib/event.sh"

line="$1"
IFS='|' read -r src name skill_file <<< "$line"
SAMPLE_OUT="$SAMPLES_DIR/$src/$name/$TASK_DESIGN_MODEL"
TARGET_COUNT="${TASK_COUNT:-6}"

should_refresh_sample() {
  [ "${SAMPLE_REFRESH_POLICY:-never}" = "missing_exec" ] || return 1
  [ -f "${SAMPLE_REFRESH_FILE:-}" ] || return 1
  grep -Fxq "$src/$name" "$SAMPLE_REFRESH_FILE"
}

if should_refresh_sample && [ -d "$SAMPLE_OUT" ]; then
  archive="${SAMPLE_OUT}.refresh-${BATCH_ID:-manual}"
  if [ -e "$archive" ]; then
    archive="${archive}-$(date +%s)"
  fi
  echo "[$(date +%H:%M:%S)] REFRESH:S1 $src/$name (archive existing sample output)"
  mv "$SAMPLE_OUT" "$archive"
  log_event "stage1_refresh_sample" "{\"skill\": \"$src/$name\", \"archive\": \"$archive\"}"
fi

if [ -d "$SAMPLE_OUT/tasks" ] && _checkpoint_valid manifest "$SAMPLE_OUT/benchmark_manifest.json" "$TARGET_COUNT"; then
  actual=$(find "$SAMPLE_OUT/tasks" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l)
  if [ "$actual" -ge "$TARGET_COUNT" ]; then
    echo "[$(date +%H:%M:%S)] SKIP:S1 $src/$name (already has $actual tasks)"
    log_event "stage1_skip" "{\"skill\": \"$src/$name\", \"existing_tasks\": $actual}"
    exit 0
  fi
  echo "[$(date +%H:%M:%S)] START:S1 $src/$name (retry: has $actual, target $TARGET_COUNT)"
else
  echo "[$(date +%H:%M:%S)] START:S1 $src/$name"
fi

t0=$(date +%s)
mkdir -p "$SAMPLE_OUT"
pf=$(mktemp)
sed -e "s|\$SKILL_ID|$src/$name|g" -e "s|\$SKILL_FILE|$skill_file|g" \
    -e "s|\$SAMPLE_OUT|$SAMPLE_OUT|g" -e "s|\$REPO_ROOT|$REPO_ROOT|g" \
    -e "s|\$TASK_COUNT|$TARGET_COUNT|g" \
    "$REPO_ROOT/scripts/sample-agent-prompt.txt" > "$pf"

log_event "stage1_start" "{\"skill\": \"$src/$name\", \"model\": \"$SAMPLE_CLAUDE_MODEL\"}"
export _MOCK_SAMPLE_OUT="$SAMPLE_OUT"
CLAUDE_MODEL="$SAMPLE_CLAUDE_MODEL" retry_runner \
  "S1:$src/$name" \
  "$TIMEOUT_PER_STAGE" \
  "$SAMPLE_OUT/sample_agent.log" \
  "$pf" \
  "$SAMPLE_OUT/runner_metrics.json"
rc=$?; rm -f "$pf"; t1=$(date +%s)
actual=$(find "$SAMPLE_OUT/tasks" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l)

if [ $rc -eq 124 ]; then
  echo "[$(date +%H:%M:%S)] TIMEOUT:S1 $src/$name ($((t1-t0))s)"
  log_event "stage1_timeout" "{\"skill\": \"$src/$name\", \"duration_s\": $((t1-t0))}"
  exit 1
elif [ "$actual" -ge "$TARGET_COUNT" ] && [ -f "$SAMPLE_OUT/benchmark_manifest.json" ]; then
  echo "[$(date +%H:%M:%S)] DONE:S1 $src/$name ($((t1-t0))s, $actual tasks)"
  log_event "stage1_done" "{\"skill\": \"$src/$name\", \"duration_s\": $((t1-t0)), \"tasks_generated\": $actual, \"model\": \"$SAMPLE_CLAUDE_MODEL\"}"
else
  echo "[$(date +%H:%M:%S)] FAIL:S1 $src/$name ($((t1-t0))s, got $actual tasks)"
  log_event "stage1_fail" "{\"skill\": \"$src/$name\", \"duration_s\": $((t1-t0)), \"actual_tasks\": $actual}"
  exit 1
fi
