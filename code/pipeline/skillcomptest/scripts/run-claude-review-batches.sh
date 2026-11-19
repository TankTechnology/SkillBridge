#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  scripts/run-claude-review-batches.sh RUN_DIR [MAX_PARALLEL]

Runs Claude Code over prompt-batch-*.md files in RUN_DIR. Each prompt is sent
via stdin to avoid command-line length limits. Existing non-empty result files
are skipped.
USAGE
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ] || [ $# -lt 1 ]; then
  usage
  exit 0
fi

RUN_DIR="$1"
MAX_PARALLEL="${2:-6}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -f "$REPO_ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$REPO_ROOT/.env"
  set +a
fi

: "${CLAUDE_MODEL:?CLAUDE_MODEL must be set in .env or environment}"

run_one() {
  local prompt_file="$1"
  local stem batch_id out_file err_file
  stem="$(basename "$prompt_file" .md)"
  batch_id="${stem#prompt-batch-}"
  out_file="$RUN_DIR/result-batch-${batch_id}.json"
  err_file="$RUN_DIR/stderr-batch-${batch_id}.log"

  if [ -s "$out_file" ]; then
    echo "SKIP $batch_id"
    return 0
  fi

  local args=(-p --model "$CLAUDE_MODEL" --dangerously-skip-permissions --output-format json --no-session-persistence --max-budget-usd "${CLAUDE_MAX_BUDGET_USD:-8}" --disallowedTools "Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch")
  if [ -n "${CLAUDE_SETTING_SOURCES:-}" ]; then
    args+=(--setting-sources "$CLAUDE_SETTING_SOURCES")
  fi

  echo "START $batch_id"
  timeout --kill-after=30 "${CLAUDE_REVIEW_TIMEOUT_S:-900}" claude "${args[@]}" < "$prompt_file" > "$out_file" 2> "$err_file"
  echo "DONE $batch_id"
}

export RUN_DIR CLAUDE_MODEL CLAUDE_SETTING_SOURCES CLAUDE_MAX_BUDGET_USD CLAUDE_REVIEW_TIMEOUT_S
export -f run_one

find "$RUN_DIR" -maxdepth 1 -name 'prompt-batch-*.md' -print0 |
  sort -z |
  xargs -0 -n 1 -P "$MAX_PARALLEL" bash -lc 'run_one "$0"'
