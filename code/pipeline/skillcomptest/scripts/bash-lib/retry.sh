#!/usr/bin/env bash
# retry_runner: call the configured LLM runner ($RUNNER) with exponential backoff.
# Default: claude. Set RUNNER=kimi to use Kimi Code.
# Call: retry_runner LABEL TIMEOUT LOGFILE PROMPT_FILE [METRICS_FILE]

ts() { date +%H:%M:%S; }

retry_runner() {
  local label="$1" timeout_val="$2" log_file="$3" prompt_file="${4:-}" metrics_file="${5:-}"
  local had_errexit=0
  case "$-" in
    *e*) had_errexit=1 ;;
  esac

  if [ "${MOCK_CLAUDE:-}" = "1" ]; then
    echo "[MOCK] $label" > "$log_file"
    case "$label" in
      S1:*)
        local mock_sample="${_MOCK_SAMPLE_OUT:-}"
        [ -n "$mock_sample" ] && mkdir -p "$mock_sample/tasks"/T_{01..10} && \
          echo '{"functional_tasks":[{"id":"T_01"},{"id":"T_02"},{"id":"T_03"},{"id":"T_04"},{"id":"T_05"},{"id":"T_06"},{"id":"T_07"},{"id":"T_08"},{"id":"T_09"},{"id":"T_10"}]}' > "$mock_sample/benchmark_manifest.json"
        ;;
      TASK:*)
        local mock_task="${_MOCK_TASK_OUT:-}"
        [ -n "$mock_task" ] && mkdir -p "$mock_task/results" && \
          echo "{\"task_id\":\"mock\",\"platform\":\"$PLATFORM\",\"runner\":\"$RUNNER\",\"status\":\"pass\",\"failure_category\":\"\",\"summary\":\"mock task\"}" > "$mock_task/task_metrics.json"
        ;;
    esac
    return 0
  fi

  [ -n "$metrics_file" ] && rm -f "$metrics_file"

  local attempt=1 rc=0
  while [ $attempt -le "$MAX_RETRIES" ]; do
    local attempt_start attempt_end attempt_duration
    attempt_start=$(date +%s)
    set +e
    case "${RUNNER:-claude}" in
      kimi)
        timeout --kill-after=30 "$timeout_val" kimi -p "$(cat "$prompt_file")" --print --yolo \
          --add-dir "$REPO_ROOT" --add-dir "$DATA_STORE" \
          ${KIMI_OPTS:-} \
          > "$log_file" 2>&1
        rc=$?
        ;;
      *)
        local claude_args=(-p --model "$CLAUDE_MODEL" --dangerously-skip-permissions)
        claude_args+=(--tools default --allowedTools Bash,Read,Write,Edit,Glob,Grep,LS)
        if [ -n "${CLAUDE_SETTING_SOURCES:-}" ]; then
          claude_args+=(--setting-sources "$CLAUDE_SETTING_SOURCES")
        fi
        if [ -n "${CLAUDE_OUTPUT_FORMAT:-}" ]; then
          claude_args+=(--output-format "$CLAUDE_OUTPUT_FORMAT")
        fi
        if [ "${CLAUDE_NO_SESSION_PERSISTENCE:-}" = "1" ]; then
          claude_args+=(--no-session-persistence)
        fi
        timeout --kill-after=30 "$timeout_val" claude "${claude_args[@]}" "$(cat "$prompt_file")" > "$log_file" 2>&1
        rc=$?
        ;;
    esac
    if [ "$had_errexit" -eq 1 ]; then
      set -e
    else
      set +e
    fi
    attempt_end=$(date +%s)
    attempt_duration=$((attempt_end - attempt_start))
    if [ -n "$metrics_file" ]; then
      python3 "$REPO_ROOT/scripts/record-runner-metrics.py" \
        --log-file "$log_file" \
        --output "$metrics_file" \
        --runner "${RUNNER:-claude}" \
        --model "$CLAUDE_MODEL" \
        --label "$label" \
        --attempt "$attempt" \
        --duration-s "$attempt_duration" \
        --exit-code "$rc" \
        --prompt-file "$prompt_file" >/dev/null 2>&1 || true
    fi
    [ $rc -eq 0 ] && return 0
    if [ $rc -eq 124 ] || [ $rc -gt 128 ]; then
      if [ $attempt -lt "$MAX_RETRIES" ]; then
        local wait=$((RETRY_BACKOFF_BASE ** attempt))
        echo "[$(ts)] RETRY:$label attempt $attempt/$MAX_RETRIES (wait ${wait}s, rc=$rc)" >&2
        sleep "$wait"
      fi
    else
      return $rc
    fi
    ((attempt++))
  done
  return $rc
}
