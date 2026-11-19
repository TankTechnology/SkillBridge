#!/usr/bin/env python3
"""Pipeline orchestrator: replaces bash+xargs with reliable Python concurrency."""

import os, sys, json, time, subprocess, shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime, timezone

REPO_ROOT = Path(os.environ.get("REPO_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
DATA_STORE = Path(os.environ.get("DATA_STORE", REPO_ROOT / ".cache" / "skillcomptest"))
STAGE1_SCRIPT = REPO_ROOT / "scripts" / "stages" / "stage1-sample.sh"
STAGE2_SCRIPT = REPO_ROOT / "scripts" / "stages" / "stage2-exec.sh"

def ts():
    return datetime.now().strftime("%H:%M:%S")

def log(msg: str):
    print(f"[{ts()}] {msg}", flush=True)

def load_queue():
    queue_file = DATA_STORE / ".batch-task-queue.txt"
    if not queue_file.exists():
        log(f"ERROR: queue file not found: {queue_file}")
        return []
    with open(queue_file) as f:
        return [line.strip() for line in f if line.strip() and "|" in line]

def run_stage1(skill_line: str) -> dict:
    """Run SampleAgent for one skill. Returns result dict."""
    parts = skill_line.split("|")
    name = f"{parts[0]}/{parts[1]}"
    started = time.time()
    log_file = DATA_STORE / "samples" / parts[0] / parts[1] / f"phase1_{int(started)}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        with open(log_file, "w") as lf:
            result = subprocess.run(
                ["bash", str(STAGE1_SCRIPT), skill_line],
                capture_output=True, text=True, timeout=int(os.environ.get("TIMEOUT_PER_STAGE", 600)),
                cwd=REPO_ROOT, env={**os.environ, "DATA_STORE": str(DATA_STORE)}
            )
            lf.write(result.stdout)
            lf.write(result.stderr)

        elapsed = time.time() - started
        return {"skill": name, "status": "done" if result.returncode == 0 else "failed",
                "duration_s": round(elapsed), "exit_code": result.returncode,
                "log": str(log_file)}
    except subprocess.TimeoutExpired:
        return {"skill": name, "status": "timeout", "duration_s": round(time.time()-started), "log": str(log_file)}
    except Exception as e:
        return {"skill": name, "status": "error", "duration_s": round(time.time()-started), "error": str(e)}

def run_stage2(skill_line: str) -> dict:
    """Run ExecAgent for one skill. Returns result dict."""
    parts = skill_line.split("|")
    name = f"{parts[0]}/{parts[1]}"
    started = time.time()

    try:
        result = subprocess.run(
            ["bash", str(STAGE2_SCRIPT), skill_line],
            capture_output=True, text=True, timeout=int(os.environ.get("TIMEOUT_PER_TASK", 300)) * 5,
            cwd=REPO_ROOT, env={**os.environ, "DATA_STORE": str(DATA_STORE)}
        )
        elapsed = time.time() - started
        return {"skill": name, "status": "done" if result.returncode == 0 else "failed",
                "duration_s": round(elapsed), "exit_code": result.returncode}
    except subprocess.TimeoutExpired:
        return {"skill": name, "status": "timeout", "duration_s": round(time.time()-started)}
    except Exception as e:
        return {"skill": name, "status": "error", "duration_s": round(time.time()-started), "error": str(e)}

def run_phase(phase: int, skills: list, parallel: int):
    """Run a phase on multiple skills concurrently."""
    fn = run_stage1 if phase == 1 else run_stage2
    label = f"Phase {phase}"

    log(f"{label}: {len(skills)} skills, {parallel} concurrent")
    started = time.time()
    results = []
    completed = 0

    with ThreadPoolExecutor(max_workers=parallel) as pool:
        futures = {pool.submit(fn, s): s for s in skills}
        for f in as_completed(futures):
            r = f.result()
            results.append(r)
            completed += 1
            pct = completed / len(skills) * 100
            log(f"{label} [{completed}/{len(skills)} {pct:.0f}%] {r['skill']}: {r['status']} ({r['duration_s']}s)")

    total = time.time() - started
    success = sum(1 for r in results if r['status'] == 'done')
    failed = sum(1 for r in results if r['status'] != 'done')
    log(f"{label} complete: {success}/{len(skills)} done, {failed} failed, {total:.0f}s")

    # Save results
    report_file = DATA_STORE / f"phase{phase}_report.json"
    with open(report_file, "w") as f:
        json.dump({"started": datetime.now(timezone.utc).isoformat(), "duration_s": round(total),
                   "total": len(skills), "success": success, "failed": failed, "results": results}, f, indent=2)
    log(f"Report: {report_file}")
    return results

def main():
    phase = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    parallel = int(os.environ.get(f"S{phase}_PARALLEL", os.environ.get("S1_PARALLEL", 3)))

    if phase not in (1, 2):
        log("Usage: orchestrator.py 1|2")
        sys.exit(1)

    skills = load_queue()
    if not skills:
        log("No skills in queue.")
        sys.exit(1)

    run_phase(phase, skills, parallel)

if __name__ == "__main__":
    main()
