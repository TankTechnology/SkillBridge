#!/usr/bin/env bash
set -euo pipefail

DATA_STORE="${DATA_STORE:-/data}"
export DATA_STORE

echo "==> SkillComptest Docker Runner"
echo "    DATA_STORE: $DATA_STORE"

# Copy auth files from read-only mounts to writable home (always overwrite)
[ -f /tmp/host-claude.json ] && cp -f /tmp/host-claude.json ~/.claude.json || true
[ -d /tmp/host-claude ] && cp -rf /tmp/host-claude ~/.claude || true

OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-}"
if [ -n "$OPENROUTER_API_KEY" ]; then
  export OPENROUTER_API_KEY
  ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-https://openrouter.ai/api}"
  ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL%/}"
  export ANTHROPIC_BASE_URL
  export ANTHROPIC_AUTH_TOKEN="${ANTHROPIC_AUTH_TOKEN:-$OPENROUTER_API_KEY}"
  export ANTHROPIC_API_KEY=""
  export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="${CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC:-1}"
  export CLAUDE_SETTING_SOURCES="${CLAUDE_SETTING_SOURCES:-project,local}"
  echo "==> Claude Code configured for OpenRouter"
  echo "    ANTHROPIC_BASE_URL: $ANTHROPIC_BASE_URL"
  echo "    CLAUDE_SETTING_SOURCES: $CLAUDE_SETTING_SOURCES"
elif [ -n "${ANTHROPIC_BASE_URL:-}" ] || [ -n "${ANTHROPIC_AUTH_TOKEN:-}" ]; then
  ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-}"
  ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL%/}"
  export ANTHROPIC_BASE_URL
  if [ -n "${ANTHROPIC_AUTH_TOKEN:-}" ]; then
    export ANTHROPIC_AUTH_TOKEN
    export ANTHROPIC_API_KEY=""
  fi
  export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="${CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC:-1}"
  export CLAUDE_SETTING_SOURCES="${CLAUDE_SETTING_SOURCES:-project,local}"
  echo "==> Claude Code custom Anthropic-compatible endpoint configured"
  echo "    ANTHROPIC_BASE_URL: ${ANTHROPIC_BASE_URL:-default}"
  echo "    CLAUDE_SETTING_SOURCES: $CLAUDE_SETTING_SOURCES"
fi

# Generate Kimi Code config from env vars
KIMI_API_KEY="${KIMI_API_KEY:-}"
KIMI_THINKING="${KIMI_THINKING:-on}"
if [ -n "$KIMI_API_KEY" ]; then
  mkdir -p ~/.kimi
  THINKING_VAL="true"
  [ "$KIMI_THINKING" = "off" ] && THINKING_VAL="false"
  cat > ~/.kimi/config.toml << EOF
default_model = "kimi-for-coding"
default_thinking = $THINKING_VAL
default_yolo = true

[providers.kimi-for-coding]
type = "kimi"
base_url = "https://api.kimi.com/coding/v1"
api_key = "$KIMI_API_KEY"

[models.kimi-for-coding]
provider = "kimi-for-coding"
model = "kimi-for-coding"
max_context_size = 262144
EOF
  echo "==> Kimi Code configured (thinking=$KIMI_THINKING)"
  export KIMI_OPTS=""
else
  echo "==> KIMI_API_KEY not set, Kimi Code unavailable"
fi

# Start Xvfb for headless browser automation
XVFB_PID=
if command -v Xvfb >/dev/null 2>&1; then
  echo "==> Starting Xvfb on :99..."
  Xvfb :99 -screen 0 1280x720x24 -ac +extension GLX +render &
  XVFB_PID=$!
  sleep 0.5
  echo "    Xvfb PID: $XVFB_PID"
fi

cleanup() {
  echo "==> Shutting down..."
  [ -n "$XVFB_PID" ] && kill "$XVFB_PID" 2>/dev/null || true
}
trap cleanup EXIT

need_data() {
  [ ! -d "$DATA_STORE/samples" ] || [ ! -d "$DATA_STORE/SkillsRepo" ]
}

fetch_if_needed() {
  if need_data; then
    if [ -z "${GH_TOKEN:-}" ]; then
      echo "ERROR: Data not found and GH_TOKEN is not set." >&2
      echo "Run first: docker run -e GH_TOKEN=ghp_... -v skillcomptest_data:/data skillcomptest fetch" >&2
      exit 1
    fi
    export GH_TOKEN
    echo "==> Data not found, fetching first..."
    make fetch-data
  fi
}

case "${1:-bench}" in
  fetch)
    if [ -z "${GH_TOKEN:-}" ]; then
      echo "ERROR: GH_TOKEN is not set." >&2
      echo "Pass it via: docker run -e GH_TOKEN=ghp_... skillcomptest fetch" >&2
      exit 1
    fi
    export GH_TOKEN
    echo "==> Fetching data release..."
    make fetch-data
    echo "==> Fetch complete. Data ready at $DATA_STORE"
    ;;
  bench)
    fetch_if_needed
    echo "==> Running benchmark validation..."
    cd /app/TskPkgs/anthropic-skill-portability-453
    node scripts/validate-pack.js
    echo "==> Benchmark complete."
    ;;
  batch)
    fetch_if_needed
    echo "==> Running batch compatibility benchmark..."
    chmod +x /app/scripts/batch-bench.sh
    export DATA_STORE PLATFORM SKILL SOURCE MAX_SKILLS MAX_PARALLEL TIMEOUT_PER_STAGE
    export S1_PARALLEL S2_PARALLEL TASK_PARALLEL TIMEOUT_PER_TASK
    export PIPELINE_MODE MAX_AGENT_PARALLEL
    export SAMPLE_CLAUDE_MODEL EXEC_CLAUDE_MODEL CLAUDE_OUTPUT_FORMAT CLAUDE_NO_SESSION_PERSISTENCE
    export QUEUE_POLICY SKILL_LIST BATCH_ID SAMPLE_REFRESH_POLICY ALLOW_RERUN_COMPLETE
    export PLATFORM="${PLATFORM:-linux}"
    exec bash /app/scripts/batch-bench.sh
    ;;
  shell)
    echo "==> Starting shell..."
    exec /bin/bash
    ;;
  *)
    echo "==> Running: $*"
    exec "$@"
    ;;
esac
