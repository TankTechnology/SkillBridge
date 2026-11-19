#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

source "$SCRIPT_DIR/bash-lib/config.sh"
source "$SCRIPT_DIR/bash-lib/infra.sh"
source "$SCRIPT_DIR/bash-lib/event.sh"
source "$SCRIPT_DIR/bash-lib/checkpoint.sh"

# ── Startup checks ──
setup_lock
check_disk_space

# ── Build task queue ──
echo "==> SkillComptest Batch Benchmark ($PLATFORM)"
echo "    DATA_STORE: $DATA_STORE"
echo "    Skills dir: $SKILLS_DIR"
echo ""

if [ ! -d "$SKILLS_DIR" ]; then
  echo "ERROR: Skills directory not found: $SKILLS_DIR" >&2
  echo "Run 'make fetch-data' first." >&2
  exit 1
fi

if [ "$ALLOW_WRITABLE_SKILL_SOURCE" != "1" ]; then
  probe="$SKILLS_DIR/.skillcomptest-write-probe.$$"
  if touch "$probe" 2>/dev/null; then
    rm -f "$probe"
    echo "ERROR: Skills directory is writable: $SKILLS_DIR" >&2
    echo "Batch execution must mount skill sources read-only to avoid source pollution." >&2
    echo "Use scripts/run-batch-detached.py or add a read-only /data/SkillsRepo submount." >&2
    exit 2
  fi
fi

echo "[$(date +%H:%M:%S)] Planning skills..."
mkdir -p "$QUEUE_DIR"

PLAN_ARGS=(
  --skills-dir "$SKILLS_DIR"
  --samples-dir "$SAMPLES_DIR"
  --exec-dir "$EXEC_DIR"
  --platform "$PLATFORM"
  --task-count "$TASK_COUNT"
  --task-design-model "$TASK_DESIGN_MODEL"
  --runner "$RUNNER"
  --claude-model "$SAMPLE_CLAUDE_MODEL"
  --source "$SOURCE"
  --skill "$SKILL"
  --max-skills "${MAX_SKILLS:-}"
  --skill-list "$SKILL_LIST"
  --queue-policy "$QUEUE_POLICY"
  --sample-refresh-policy "$SAMPLE_REFRESH_POLICY"
  --batch-id "$BATCH_ID"
  --queue-file "$NAMED_TASK_QUEUE_FILE"
  --plan-file "$BATCH_PLAN_FILE"
  --refresh-file "$SAMPLE_REFRESH_FILE"
  --legacy-queue-file "$TASK_QUEUE_FILE"
)
if [ "$ALLOW_RERUN_COMPLETE" = "1" ]; then
  PLAN_ARGS+=(--allow-rerun-complete)
fi
python3 "$REPO_ROOT/scripts/plan-batch.py" "${PLAN_ARGS[@]}"

TOTAL=$(wc -l < "$TASK_QUEUE_FILE" | tr -d ' ')
echo "[$(date +%H:%M:%S)] Skills queued: $TOTAL"
echo "[$(date +%H:%M:%S)] Batch plan: $BATCH_PLAN_FILE"
[ "$TOTAL" -eq 0 ] && { echo "No skills queued for this profile. Done."; exit 0; }

# ── Pipeline ──
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Pipeline: $PIPELINE_MODE | Phase1(SampleAgent) → Phase2(ExecAgent)"
echo "  S1: $S1_PARALLEL concurrent | S2: $S2_PARALLEL skills × $TASK_PARALLEL tasks | Max agents: $MAX_AGENT_PARALLEL"
echo "  Models: SampleAgent=$SAMPLE_CLAUDE_MODEL | ExecAgent=$EXEC_CLAUDE_MODEL"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

PIPELINE_START=$(date +%s)
setup_trap
log_event "pipeline_start" "{\"total_skills\": $TOTAL, \"platform\": \"$PLATFORM\", \"mode\": \"$PIPELINE_MODE\", \"s1_parallel\": $S1_PARALLEL, \"s2_parallel\": $S2_PARALLEL, \"task_parallel\": $TASK_PARALLEL, \"max_agent_parallel\": $MAX_AGENT_PARALLEL, \"task_count\": $TASK_COUNT, \"sample_model\": \"$SAMPLE_CLAUDE_MODEL\", \"exec_model\": \"$EXEC_CLAUDE_MODEL\"}"
update_progress 0 0

S1_EXIT=0
S2_EXIT=0

if [ "$PIPELINE_MODE" = "streaming" ]; then
  echo ""
  echo "━━━ Streaming Pipeline: SampleAgent feeds ExecAgent as each skill is ready ━━━"
  STREAM_RESULT_FILE="$QUEUE_DIR/$BATCH_ID.streaming.json"
  set +e
  python3 "$REPO_ROOT/scripts/streaming-pipeline.py" \
    --queue-file "$TASK_QUEUE_FILE" \
    --event-log "$EVENT_LOG" \
    --result-file "$STREAM_RESULT_FILE" \
    --s1-parallel "$S1_PARALLEL" \
    --s2-parallel "$S2_PARALLEL" \
    --task-parallel "$TASK_PARALLEL" \
    --max-agent-parallel "$MAX_AGENT_PARALLEL"
  STREAM_EXIT=$?
  set -e
  if [ -f "$STREAM_RESULT_FILE" ]; then
    S1_EXIT=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("s1_exit", 1))' "$STREAM_RESULT_FILE" 2>/dev/null || echo "$STREAM_EXIT")
    S2_EXIT=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("s2_exit", 1))' "$STREAM_RESULT_FILE" 2>/dev/null || echo 1)
  else
    S1_EXIT=$STREAM_EXIT
    S2_EXIT=1
  fi
elif [ "$PIPELINE_MODE" = "barrier" ]; then
  # Phase 1: SampleAgent
  echo ""
  echo "━━━ Phase 1: SampleAgent ($S1_PARALLEL concurrent) ━━━"
  PHASE1_START=$(date +%s)
  log_event "phase1_start" "{\"parallelism\": $S1_PARALLEL, \"mode\": \"barrier\"}"

  set +e
  cat "$TASK_QUEUE_FILE" | xargs -P "$S1_PARALLEL" -I {} bash "$SCRIPT_DIR/stages/stage1-sample.sh" {}
  S1_EXIT=$?
  set -e

  PHASE1_END=$(date +%s)
  PHASE1_DUR=$((PHASE1_END - PHASE1_START))
  echo "[$(date +%H:%M:%S)] Phase 1 complete in ${PHASE1_DUR}s ($((PHASE1_DUR / 60))m)"
  log_event "phase1_end" "{\"duration_s\": $PHASE1_DUR, \"exit_code\": $S1_EXIT, \"mode\": \"barrier\"}"

  # Phase 2: ExecAgent
  echo ""
  echo "━━━ Phase 2: ExecAgent ($S2_PARALLEL skills, $TASK_PARALLEL tasks/skill) ━━━"
  PHASE2_START=$(date +%s)
  log_event "phase2_start" "{\"skill_parallelism\": $S2_PARALLEL, \"task_parallelism\": $TASK_PARALLEL, \"mode\": \"barrier\"}"

  set +e
  cat "$TASK_QUEUE_FILE" | xargs -P "$S2_PARALLEL" -I {} bash "$SCRIPT_DIR/stages/stage2-exec.sh" {}
  S2_EXIT=$?
  set -e

  PHASE2_END=$(date +%s)
  PHASE2_DUR=$((PHASE2_END - PHASE2_START))
  echo "[$(date +%H:%M:%S)] Phase 2 complete in ${PHASE2_DUR}s ($((PHASE2_DUR / 60))m)"
  log_event "phase2_end" "{\"duration_s\": $PHASE2_DUR, \"exit_code\": $S2_EXIT, \"mode\": \"barrier\"}"
else
  echo "ERROR: Unsupported PIPELINE_MODE=$PIPELINE_MODE (expected streaming or barrier)" >&2
  exit 2
fi

PIPELINE_END=$(date +%s)
PIPELINE_DURATION=$((PIPELINE_END - PIPELINE_START))
PIPELINE_EXIT=$(( S1_EXIT + S2_EXIT ))

if [ $PIPELINE_EXIT -ne 0 ]; then
  echo "[$(date +%H:%M:%S)] WARNING: Pipeline had failures (S1 exit=$S1_EXIT S2 exit=$S2_EXIT)" >&2
fi
echo "[$(date +%H:%M:%S)] Pipeline complete in ${PIPELINE_DURATION}s ($((PIPELINE_DURATION / 60))m)"
log_event "pipeline_end" "{\"duration_s\": $PIPELINE_DURATION, \"s1_exit\": $S1_EXIT, \"s2_exit\": $S2_EXIT, \"mode\": \"$PIPELINE_MODE\"}"
update_progress "$TOTAL" "$TOTAL"

# ── Generate report ──
echo ""
echo "━━━ Generating compatibility report ━━━"
START_TIME=$(date -d "@$PIPELINE_START" -Iseconds 2>/dev/null || date -Iseconds)
END_TIME=$(date -d "@$PIPELINE_END" -Iseconds 2>/dev/null || date -Iseconds)

python3 "$REPO_ROOT/scripts/generate-report.py" \
    "$TASK_QUEUE_FILE" "$EXEC_DIR" "$SAMPLES_DIR" "$TASK_DESIGN_MODEL" \
    "$NAMED_BATCH_REPORT" "$PLATFORM" "$START_TIME" "$END_TIME" "$PIPELINE_DURATION" "$TASK_COUNT"
cp "$NAMED_BATCH_REPORT" "$LATEST_BATCH_REPORT"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Batch complete."
echo "  Pipeline: ${PIPELINE_DURATION}s ($((PIPELINE_DURATION / 60))m)"
echo "  Report:  $NAMED_BATCH_REPORT"
echo "  Latest:  $LATEST_BATCH_REPORT"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

[ $PIPELINE_EXIT -eq 0 ] && exit 0
[ $PIPELINE_EXIT -eq 124 ] && exit 124
exit 1
