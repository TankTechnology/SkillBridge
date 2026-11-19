#!/usr/bin/env bash
# JSONL event logging helper.

log_event() {
  local event_type="$1"
  export _LE_DATA="${2:-{\}}"
  python3 "$REPO_ROOT/scripts/log-event.py" "$EVENT_LOG" "$event_type" 2>>"$DATA_STORE/event-errors.log" || true
}
