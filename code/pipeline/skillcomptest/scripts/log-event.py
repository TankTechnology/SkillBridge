#!/usr/bin/env python3
"""Write a structured JSONL event to the pipeline event log.

Reads data from _LE_DATA env var to avoid bash brace-expansion issues.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

EVENT_LOG = Path(sys.argv[1])
EVENT_TYPE = sys.argv[2]

event = {
    "ts": time.time(),
    "iso": datetime.now(timezone.utc).isoformat(),
    "event": EVENT_TYPE,
}
batch_id = os.environ.get("BATCH_ID", "")
if batch_id:
    event["batch_id"] = batch_id

# Data passed via env var to avoid bash {} expansion
raw = os.environ.get("_LE_DATA", "{}")
try:
    event.update(json.loads(raw))
except (json.JSONDecodeError, TypeError):
    print(f"WARNING: failed to parse _LE_DATA env var: {raw[:100]}", file=sys.stderr)

EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)
with open(EVENT_LOG, "a") as f:
    f.write(json.dumps(event, ensure_ascii=False) + "\n")
