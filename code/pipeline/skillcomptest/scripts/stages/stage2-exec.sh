#!/usr/bin/env bash
# Phase 2 worker: run ExecAgent tasks for one skill.
# Invoked by xargs: bash stage2-exec.sh "source|name|skill_file"

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../bash-lib/config.sh"
source "$SCRIPT_DIR/../bash-lib/checkpoint.sh"
source "$SCRIPT_DIR/../bash-lib/retry.sh"
source "$SCRIPT_DIR/../bash-lib/event.sh"

line="$1"
IFS='|' read -r src name skill_file <<< "$line"
skill_dir="$(cd "$(dirname "$skill_file")" && pwd)"
SAMPLE_OUT="$SAMPLES_DIR/$src/$name/$TASK_DESIGN_MODEL"
EXEC_OUT="$EXEC_DIR/$src/$name/$TASK_DESIGN_MODEL/$TASK_DESIGN_MODEL"
TARGET_COUNT="${TASK_COUNT:-6}"

if _checkpoint_valid summary "$EXEC_OUT/platform_summary.json"; then
  echo "[$(date +%H:%M:%S)] SKIP:S2 $src/$name (already executed)"
  log_event "stage2_skip" "{\"skill\": \"$src/$name\"}"
  exit 0
fi

if [ ! -f "$SAMPLE_OUT/benchmark_manifest.json" ]; then
  echo "[$(date +%H:%M:%S)] SKIP:S2 $src/$name (no samples)"
  exit 0
fi

s2_start=$(date +%s)
echo "[$(date +%H:%M:%S)] START:S2 $src/$name ($TARGET_COUNT tasks, $TASK_PARALLEL concurrent)"
log_event "stage2_start" "{\"skill\": \"$src/$name\", \"target_tasks\": $TARGET_COUNT, \"model\": \"$EXEC_CLAUDE_MODEL\"}"
mkdir -p "$EXEC_OUT"

task_ids=()
if [ -f "$SAMPLE_OUT/benchmark_manifest.json" ]; then
  mapfile -t task_ids < <(python3 "$REPO_ROOT/scripts/pipeline-utils.py" task-ids "$SAMPLE_OUT/benchmark_manifest.json" 2>/dev/null)
fi
if [ ${#task_ids[@]} -eq 0 ] && [ -d "$SAMPLE_OUT/tasks" ]; then
  mapfile -t task_ids < <(find "$SAMPLE_OUT/tasks" -mindepth 1 -maxdepth 1 -type d -name 'T_*' -printf '%f\n' 2>/dev/null | sort)
fi
if [ ${#task_ids[@]} -eq 0 ]; then
  echo "[$(date +%H:%M:%S)] FAIL:S2 $src/$name (no tasks found)"
  exit 1
fi

run_exec_task() {
  local tid="$1"
  local task_out="$EXEC_OUT/tasks/$tid"
  mkdir -p "$task_out/results"

  if _checkpoint_valid task-metrics "$task_out/task_metrics.json"; then
    echo "[$(date +%H:%M:%S)] SKIP:TASK $src/$name/$tid"
    log_event "task_skip" "{\"skill\": \"$src/$name\", \"task_id\": \"$tid\"}"
    return 0
  fi

  echo "[$(date +%H:%M:%S)] START:TASK $src/$name/$tid"
  log_event "task_start" "{\"skill\": \"$src/$name\", \"task_id\": \"$tid\"}"

  local t0=$(date +%s)
  python3 "$REPO_ROOT/scripts/pipeline-utils.py" timestamp "$task_out/start_timestamp.json" "$t0" "$(date -Iseconds 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%S%z)" 2>/dev/null

  local pf=$(mktemp)
  sed \
    -e "s|\$PLATFORM|$PLATFORM|g" \
    -e "s|\$SKILL_ID|$src/$name|g" \
    -e "s|\$SKILL_FILE|$skill_file|g" \
    -e "s|\$SKILL_DIR|$skill_dir|g" \
    -e "s|\$SAMPLE_OUT|$SAMPLE_OUT|g" \
    -e "s|\$EXEC_OUT|$EXEC_OUT|g" \
    -e "s|\$TASK_ID|$tid|g" \
    -e "s|\$TASK_DIR|tasks/$tid|g" \
    -e "s|\$REPO_ROOT|$REPO_ROOT|g" \
    "$REPO_ROOT/scripts/exec-task-prompt.txt" > "$pf"

  local log_file="$task_out/execution.log"
  export _MOCK_TASK_OUT="$task_out"
  set +e
  CLAUDE_MODEL="$EXEC_CLAUDE_MODEL" retry_runner \
    "TASK:$src/$name/$tid" \
    "$TIMEOUT_PER_TASK" \
    "$log_file" \
    "$pf" \
    "$task_out/runner_metrics.json"
  local rc=$?
  set -e
  rm -f "$pf"

  local t1=$(date +%s)
  local dur=$((t1 - t0))

  python3 "$REPO_ROOT/scripts/pipeline-utils.py" timestamp "$task_out/end_timestamp.json" "$t1" "$(date -Iseconds 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%S%z)" 2>/dev/null
  python3 "$REPO_ROOT/scripts/pipeline-utils.py" bash-timing "$task_out/bash_timing.json" "$tid" "$t0" "$t1" "$dur" "$rc"

  if [ $rc -eq 124 ]; then
    echo "[$(date +%H:%M:%S)] TIMEOUT:TASK $src/$name/$tid (${dur}s)"
    log_event "task_timeout" "{\"skill\": \"$src/$name\", \"task_id\": \"$tid\", \"duration_s\": $dur}"
    python3 "$REPO_ROOT/scripts/pipeline-utils.py" runner-failure-metrics \
      "$task_out/task_metrics.json" "$tid" "$PLATFORM" "ExecAgent" "$src/$name" \
      "runner_timeout" "$TIMEOUT_PER_TASK" "$EXEC_CLAUDE_MODEL" "$rc" "$dur"
    return 0
  fi

  if [ -f "$task_out/task_metrics.json" ]; then
    echo "[$(date +%H:%M:%S)] DONE:TASK $src/$name/$tid (${dur}s)"
    log_event "task_done" "{\"skill\": \"$src/$name\", \"task_id\": \"$tid\", \"duration_s\": $dur}"
    return 0
  else
    echo "[$(date +%H:%M:%S)] FAIL:TASK $src/$name/$tid (${dur}s, no task_metrics.json)"
    log_event "task_fail" "{\"skill\": \"$src/$name\", \"task_id\": \"$tid\", \"duration_s\": $dur}"
    python3 "$REPO_ROOT/scripts/pipeline-utils.py" runner-failure-metrics \
      "$task_out/task_metrics.json" "$tid" "$PLATFORM" "ExecAgent" "$src/$name" \
      "runner_missing_metrics" "$TIMEOUT_PER_TASK" "$EXEC_CLAUDE_MODEL" "$rc" "$dur"
    return 0
  fi
}

export -f run_exec_task ts retry_runner log_event _checkpoint_valid
export src name skill_file skill_dir SAMPLE_OUT EXEC_OUT PLATFORM TASK_PARALLEL TIMEOUT_PER_TASK CLAUDE_MODEL SAMPLE_CLAUDE_MODEL EXEC_CLAUDE_MODEL RUNNER KIMI_OPTS KIMI_THINKING MAX_RETRIES RETRY_BACKOFF_BASE MOCK_CLAUDE EVENT_LOG REPO_ROOT DATA_STORE
export CLAUDE_OUTPUT_FORMAT CLAUDE_NO_SESSION_PERSISTENCE
export OPENROUTER_API_KEY ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN ANTHROPIC_API_KEY CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC CLAUDE_SETTING_SOURCES

task_queue=$(mktemp)
for tid in "${task_ids[@]}"; do
  echo "$tid" >> "$task_queue"
done

set +e
cat "$task_queue" | xargs -P "$TASK_PARALLEL" -I {} bash -c 'run_exec_task "$@"' _ {}
set -e
rm -f "$task_queue"

python3 "$REPO_ROOT/scripts/aggregate-tasks.py" "$EXEC_OUT" "$SAMPLE_OUT" "$PLATFORM" "$src/$name"
agg_exit=$?

s2_end=$(date +%s)
s2_dur=$((s2_end - s2_start))
python3 "$REPO_ROOT/scripts/pipeline-utils.py" timing-update "$EXEC_OUT/timing.json" stage2 "$s2_start" "$s2_end" "$s2_dur"

if [ $agg_exit -eq 0 ] && [ -f "$EXEC_OUT/platform_summary.json" ]; then
  echo "[$(date +%H:%M:%S)] DONE:S2 $src/$name (${s2_dur}s, ${#task_ids[@]} tasks)"
  log_event "stage2_done" "{\"skill\": \"$src/$name\", \"duration_s\": $s2_dur, \"tasks_executed\": ${#task_ids[@]}}"
else
  echo "[$(date +%H:%M:%S)] FAIL:S2 $src/$name (${s2_dur}s, aggregation failed)"
  log_event "stage2_fail" "{\"skill\": \"$src/$name\", \"duration_s\": $s2_dur}"
  exit 1
fi
