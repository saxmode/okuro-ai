# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: heartbeat and proactive checks for subtasks
# index: imports | class Sentinel
# AGENT_HEADER_END -->
"""Okuro Orchestrator Sentinel — heartbeat and proactive health checks."""

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("okuro.orchestrator.sentinel")


class Sentinel:
    def __init__(self, config):
        self.config = config
        self.last_check = 0
        self.check_interval = config.sentinel.check_interval
        self.enabled = config.sentinel.enabled
        self.consecutive_failures = 0
        # Wave-6 G19 — intervention threshold (minutes). When a subtask
        # exceeds this, sentinel emits a CRITICAL-severity warning and
        # writes a `sentinel_intervention_recommended` event to the
        # task's activity log. 0 disables (default behavior unchanged).
        self.intervene_after_minutes = float(
            getattr(config.sentinel, "intervene_after_minutes", 0) or 0
        )

    def should_check(self) -> bool:
        if not self.enabled:
            return False
        return time.time() - self.last_check >= self.check_interval

    def check(self, task, task_dir: Path) -> list[str]:
        self.last_check = time.time()
        suggestions = []
        task_label = task.title or task.description[:50]

        for phase in task.phases:
            for subtask in phase.subtasks:
                if subtask.status == "running" and subtask.started_at:
                    started = datetime.fromisoformat(subtask.started_at)
                    # subtask.started_at is written as datetime.utcnow()
                    # (naive UTC). Comparing against datetime.now() (naive
                    # local) added the timezone offset to every elapsed
                    # measurement — 120min false-positive alarm in CH
                    # (UTC+2 summer). Use UTC on both sides.
                    elapsed = (datetime.utcnow() - started).total_seconds()
                    if elapsed > 600:
                        desc = subtask.description[:60] if subtask.description else subtask.role
                        suggestions.append(
                            f"'{task_label}' — step {subtask.id} '{desc}' "
                            f"running {elapsed/60:.0f}min (role: {subtask.role}). May be stuck."
                        )
                    # Wave-6 G19 — escalate to intervention threshold.
                    if (self.intervene_after_minutes > 0
                            and elapsed > self.intervene_after_minutes * 60):
                        desc = subtask.description[:60] if subtask.description else subtask.role
                        suggestions.append(
                            f"INTERVENE: '{task_label}' — step {subtask.id} "
                            f"'{desc}' running {elapsed/60:.0f}min — exceeds "
                            f"intervene_after_minutes={self.intervene_after_minutes}. "
                            "Recommend kill + retry."
                        )
                        self._emit_intervention_event(
                            task_dir, subtask, elapsed_minutes=elapsed/60,
                        )

        if self.consecutive_failures >= 2:
            suggestions.append(
                f"'{task_label}' — {self.consecutive_failures} consecutive failures. "
                f"Decomposition or role assignment may need review."
            )

        log_path = task_dir / "log.jsonl"
        if log_path.exists():
            last_completion = self._get_last_completion_time(log_path)
            if last_completion:
                # event["ts"] is written as datetime.utcnow() (naive UTC).
                # Comparing against datetime.now() (naive local) added the
                # local offset to every stale measurement — false "pipeline
                # stalled" warnings on UTC+N machines (CEO's Mac at UTC+2).
                stale_minutes = (datetime.utcnow() - last_completion).total_seconds() / 60
                if stale_minutes > 15:
                    suggestions.append(
                        f"'{task_label}' — no subtask completed in {stale_minutes:.0f}min. "
                        f"Pipeline may be stalled."
                    )
        return suggestions

    def record_success(self):
        self.consecutive_failures = 0

    def record_failure(self):
        self.consecutive_failures += 1

    def log_observations(self, task_dir: Path, suggestions: list) -> int:
        """Append sentinel observations to the task's own activity log.

        These are operational telemetry — "step 4.1 has been running 125min,
        may be stuck" — addressed to whoever is watching the run, not to the
        user. They used to be written into `thoughts` via
        engine._capture_thought(category='observation', project='orchestrator').
        Measured 2026-07-15: that put ~200 rows (16.7% of the entire inbox)
        into the user's attention surface, every one of them shaped
        ``'Task label' — step 3.1 'truncated description…' running 121min
        (role: solution-architect). May be stuck.`` — nested truncation,
        internal ids, and zero meaning at a glance. 734 of 990 thoughts were
        dismissed. A heartbeat monitor's console log is not a thought.

        Same destination and dedup shape as _emit_intervention_event: the
        per-task .activity.jsonl, once per distinct observation per run (check()
        fires every loop iteration and would otherwise repeat the same line
        forever).

        Returns the number of newly-logged observations.
        """
        if not hasattr(self, "_observed"):
            self._observed: set = set()

        fresh = [s for s in suggestions if s not in self._observed]
        if not fresh:
            return 0

        log_path = task_dir / ".activity.jsonl"
        try:
            with open(log_path, "a") as f:
                for s in fresh:
                    f.write(json.dumps({
                        "ts": datetime.utcnow().isoformat(),
                        "type": "sentinel_observation",
                        "message": s,
                    }) + "\n")
        except OSError as exc:
            # Best-effort telemetry — never take the engine loop down for it.
            print(f"[SENTINEL] could not write activity log: {exc}")
            return 0

        self._observed.update(fresh)
        return len(fresh)

    def _emit_intervention_event(self, task_dir: Path, subtask,
                                   elapsed_minutes: float) -> None:
        """Wave-6 G19 — append a structured intervention event to the task log.

        Idempotency: only emits once per (subtask_id, run) by recording the
        subtask's started_at as the dedup key in an in-memory set. A retry
        resets started_at, so the next run can intervene again.

        Umbrella audit fix #9 — also writes a kill-request flag at
        ``{task_dir}/.kill-requests/{subtask_id}`` so the dispatcher's
        kill-watcher thread can SIGTERM the stuck subagent. Pre-fix the
        intervention only emitted an activity event that nothing in the
        live pipeline read — slow-but-alive subagents had no upstream
        teardown.
        """
        if not hasattr(self, "_intervened"):
            self._intervened: set = set()
        dedup_key = (subtask.id, subtask.started_at or "")
        if dedup_key in self._intervened:
            return
        self._intervened.add(dedup_key)

        log_path = task_dir / ".activity.jsonl"
        try:
            with open(log_path, "a") as f:
                f.write(json.dumps({
                    "ts": datetime.utcnow().isoformat(),
                    "type": "sentinel_intervention_recommended",
                    "subtask_id": subtask.id,
                    "role": getattr(subtask, "role", ""),
                    "elapsed_minutes": round(elapsed_minutes, 1),
                    "threshold_minutes": self.intervene_after_minutes,
                    "severity": "critical",
                }) + "\n")
        except OSError as exc:
            logger.warning("[sentinel] could not write intervention event: %s", exc)

        # Write kill-request flag so dispatcher's watcher can act.
        try:
            kill_dir = task_dir / ".kill-requests"
            kill_dir.mkdir(parents=True, exist_ok=True)
            flag = kill_dir / subtask.id
            flag.write_text(json.dumps({
                "requested_at": datetime.utcnow().isoformat(),
                "reason": (
                    f"sentinel intervene_after_minutes="
                    f"{self.intervene_after_minutes} exceeded "
                    f"({elapsed_minutes:.1f}min)"
                ),
                "elapsed_minutes": round(elapsed_minutes, 1),
            }))
        except OSError as exc:
            logger.warning("[sentinel] could not write kill-request: %s", exc)

    def _get_last_completion_time(self, log_path: Path) -> Optional[datetime]:
        last_completion = None
        try:
            with open(log_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                        if event.get("type") == "subtask_done":
                            last_completion = datetime.fromisoformat(event["ts"])
                    except (json.JSONDecodeError, KeyError):
                        continue
        except OSError as e:
            logger.warning(f"Failed to read log file {log_path}: {e}")
        return last_completion
