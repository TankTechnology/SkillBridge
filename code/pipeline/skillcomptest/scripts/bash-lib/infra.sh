#!/usr/bin/env bash
# Infrastructure: lock, disk check, graceful shutdown trap.

setup_lock() {
  exec {LOCK_FD}>"$LOCK_FILE"
  if ! flock -n "$LOCK_FD"; then
    echo "ERROR: Another batch-bench.sh is already running (lock: $LOCK_FILE)" >&2
    exit 1
  fi
}

check_disk_space() {
  local free_kb
  free_kb=$(df -k --output=avail "$DATA_STORE" 2>/dev/null | tail -1 || echo "")
  if [ -n "$free_kb" ] && [ "$free_kb" -lt $((MIN_FREE_MB * 1024)) ]; then
    echo "ERROR: Low disk space: ${free_kb}KB available (< ${MIN_FREE_MB}MB)." >&2
    exit 1
  fi
}

setup_trap() {
  _cleanup() {
    echo "" >&2
    echo "[$(date +%H:%M:%S)] Shutdown signal received, cleaning up..." >&2
    jobs -p | xargs -r kill 2>/dev/null || true
    update_progress
    exit 130
  }
  trap _cleanup SIGINT SIGTERM
}
