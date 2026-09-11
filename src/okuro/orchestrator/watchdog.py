# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: crash detection via heartbeat staleness
# index: imports | def _is_process_alive | def check_task | def scan_all_tasks | def main
# AGENT_HEADER_END -->
"""
Okuro Orchestrator Watchdog — Crash Detection via Heartbeat

Scans task directories for active tasks whose orchestrator has died.
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from okuro.orchestrator.yamlfast import FastLoader, yload
from okuro.db.engine import okuro_home

HEARTBEAT_STALE_SECONDS = 120  # 2 minutes

# libyaml-backed loader — identical SafeLoader semantics, ~13x faster on this
# corpus. check_task() runs once per task directory from scan_all_tasks(), so
# the parse cost here is O(number of task dirs) and that number only grows.
# One definition for the package lives in orchestrator/yamlfast.py; the local
# names stay so existing call sites and the equivalence test read unchanged.
_FastLoader = FastLoader
_yload = yload


def _is_process_alive(pid: int) -> bool:
    """Cross-platform check if a process is alive."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def check_task(task_dir: Path, verbose: bool = False) -> bool:
    """Check a single task directory for a crashed orchestrator.

    Returns True if a crash was detected and handled.
    """
    task_yaml = task_dir / "task.yaml"
    if not task_yaml.exists():
        return False

    try:
        with open(task_yaml) as f:
            task_data = _yload(f)
    except Exception:
        return False

    if not task_data:
        return False

    status = task_data.get("status", "")
    task_id = task_data.get("id", task_dir.name)

    if status not in ("active", "planning"):
        if verbose:
            print(f"  [{task_id}] status={status} — skipping")
        return False

    pid_path = task_dir / ".orchestrator.pid"
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            if _is_process_alive(pid):
                if verbose:
                    print(f"  [{task_id}] orchestrator alive (PID {pid})")
                return False
        except (ValueError, OSError):
            pass

    heartbeat_path = task_dir / ".heartbeat"
    if heartbeat_path.exists():
        try:
            mtime = heartbeat_path.stat().st_mtime
            age = time.time() - mtime
            if age < HEARTBEAT_STALE_SECONDS:
                if verbose:
                    print(f"  [{task_id}] heartbeat fresh ({age:.0f}s ago)")
                return False
        except OSError:
            pass
    else:
        if not pid_path.exists():
            if verbose:
                print(f"  [{task_id}] no heartbeat or PID file — legacy task, skipping")
            return False

    print(f"  [CRASH DETECTED] {task_id} — orchestrator is dead")

    try:
        from okuro.orchestrator.state import atomic_dump_yaml, couple_task_dict
        task_data["status"] = "failed"
        # P4.10 — the C8 coupling, applied to a raw dict. A crashed task
        # written straight to yaml kept any `awaiting` it had, so it came back
        # as failed-with-a-gate and the UI offered a decision on a dead run.
        atomic_dump_yaml(
            task_yaml, couple_task_dict(task_data), default_flow_style=False,
        )
    except Exception as e:
        print(f"    WARNING: Failed to update task.yaml: {e}")

    plan_yaml = task_dir / "plan.yaml"
    if plan_yaml.exists():
        try:
            with open(plan_yaml) as f:
                plan_data = _yload(f)
            reset_count = 0
            if plan_data and "phases" in plan_data:
                for phase in plan_data["phases"]:
                    for st in phase.get("subtasks", []):
                        if st.get("status") in ("running", "waiting_approval"):
                            st["status"] = "pending"
                            st["error"] = ""
                            reset_count += 1
            if reset_count:
                from okuro.orchestrator.state import atomic_dump_yaml
                atomic_dump_yaml(plan_yaml, plan_data, default_flow_style=False, sort_keys=False)
                print(f"    Reset {reset_count} orphaned subtask(s) to pending")
        except Exception as e:
            print(f"    WARNING: Failed to reset subtasks in plan.yaml: {e}")

    log_path = task_dir / "log.jsonl"
    try:
        event = {
            "type": "orchestrator_crashed_detected",
            "detected_by": "watchdog",
            "ts": datetime.utcnow().isoformat(),
        }
        with open(log_path, "a") as f:
            f.write(json.dumps(event) + "\n")
    except Exception as e:
        print(f"    WARNING: Failed to write log: {e}")

    for stale_path in (heartbeat_path, pid_path):
        try:
            if stale_path.exists():
                stale_path.unlink()
        except OSError:
            pass

    return True


def scan_all_tasks(tasks_dir: Path, verbose: bool = False) -> int:
    """Scan all task directories. Returns count of crashes detected."""
    if not tasks_dir.exists():
        if verbose:
            print(f"No tasks directory at {tasks_dir}")
        return 0

    crashes = 0
    task_dirs = sorted(
        (d for d in tasks_dir.iterdir() if d.is_dir() and d.name.startswith("task-")),
        reverse=True,
    )
    if verbose:
        print(f"Scanning {len(task_dirs)} task(s) in {tasks_dir}")

    for task_dir in task_dirs:
        if check_task(task_dir, verbose=verbose):
            crashes += 1
    return crashes


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Okuro Orchestrator Watchdog")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--tasks-dir", type=Path, default=None,
                        help="Override tasks directory")
    args = parser.parse_args()

    if args.tasks_dir:
        tasks_dir = args.tasks_dir
    else:
        try:
            from okuro.orchestrator.config import load_config
            config = load_config()
            tasks_dir = config.tasks_dir
        except Exception:
            tasks_dir = okuro_home() / "orchestrator" / "tasks"

    print(f"[watchdog] {datetime.utcnow().isoformat()} — scanning tasks")
    crashes = scan_all_tasks(tasks_dir, verbose=args.verbose)

    if crashes:
        print(f"[watchdog] {crashes} crashed orchestrator(s) detected and marked failed")
    else:
        print("[watchdog] All clear — no crashed orchestrators")

    return 1 if crashes else 0


if __name__ == "__main__":
    sys.exit(main())
