# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: P1 proactive scanners — pure heuristics over okuro state.
# index:
#   class CandidateSignal
#   def scan_stale_progress
#   def scan_stuck_todo
#   def scan_aging_thought
#   def scan_sysinfo_disk
#   def scan_disk_smart
#   def scan_sysinfo_vram
#   def scan_session_failures
#   SCANNERS
# AGENT_HEADER_END -->
"""P1 proactive scanners — pure heuristics, no LLM.

Each scanner yields :class:`CandidateSignal` instances. The engine
deduplicates against open `signals` rows on ``source_ref`` before
inserting, so scanners can yield candidates freely without worrying
about repeat-fire on every cron tick.

Thresholds were picked to match existing okuro conventions:
- 14 days "stale" matches ``thoughts_aging`` flag-stuck threshold.
- 85% disk warn / 95% disk crit matches ``sysinfo_storage_status``.
- 90% VRAM threshold leaves headroom for background inference on the second GPU.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

log = logging.getLogger(__name__)


_STALE_PROGRESS_DAYS = 14
_STUCK_TODO_DAYS = 14
_AGING_THOUGHT_DAYS = 14
_DISK_WARN_PERCENT = 85
_DISK_CRIT_PERCENT = 95
_VRAM_WARN_PERCENT = 90
_SESSION_FAILURE_MIN = 3
_SESSION_FAILURE_RATIO = 2.0

_ACTIONABLE_THOUGHT_CATEGORIES: tuple[str, ...] = (
    "idea", "question", "decision", "todo",
)


@dataclass
class CandidateSignal:
    """A pre-dedupe candidate produced by a scanner."""

    source_ref: str
    severity: str
    summary: str
    evidence: dict[str, Any] = field(default_factory=dict)
    suggested_action: str | None = None


# ── stale_progress ───────────────────────────────────────────────────


def scan_stale_progress(db) -> Iterable[CandidateSignal]:
    """Yield one candidate per `progress` row idle > 14d (status != 'blocked').

    The same stale-progress signal stays open until the user promotes or
    discards it — once it's in the queue, scanners stop re-emitting.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=_STALE_PROGRESS_DAYS)
    ).strftime("%Y-%m-%d %H:%M:%S")

    rows = db.fetchall(
        """SELECT project, status, summary, updated_at FROM progress
           WHERE updated_at < ? AND status != 'blocked'
           ORDER BY updated_at ASC LIMIT 20""",
        (cutoff,),
    )

    for r in rows:
        project = r["project"]
        updated_at = r.get("updated_at")
        days_ago = _days_since(updated_at)
        status = r.get("status") or "unknown"
        summary = (r.get("summary") or "").strip()
        severity = "warn" if days_ago >= _STALE_PROGRESS_DAYS * 2 else "info"

        yield CandidateSignal(
            source_ref=f"stale-progress:{project}",
            severity=severity,
            summary=(
                f"{project}: no progress in {days_ago}d "
                f"(last status: {status})"
            ),
            evidence={
                "scanner": "stale_progress",
                "bucket": "stale",
                "source_kind": "project",
                "source_id": project,
                "source_age_days": days_ago,
                "source_created_at": updated_at,
                # Per-scanner detail kept for the expanded JSON dump.
                "project": project,
                "days_idle": days_ago,
                "last_status": status,
                "last_summary": summary[:240],
            },
            suggested_action=(
                f"Resume {project}: {summary[:80]}"
                if summary else
                f"Review {project} — idle {days_ago}d"
            ),
        )


# ── stuck_todo ──────────────────────────────────────────────────────


def scan_stuck_todo(db) -> Iterable[CandidateSignal]:
    """Yield one candidate per open priority-1/2 todo older than 14d."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=_STUCK_TODO_DAYS)
    ).strftime("%Y-%m-%d %H:%M:%S")

    # Cap at 5 per scan — same items-per-level axiom that gates
    # aging_thought. Without the cap, a 47-row backlog (observed
    # 2026-05-19 against the live DB) floods the queue.
    rows = db.fetchall(
        """SELECT id, title, priority, project, created_at FROM todos
           WHERE status = 'open' AND priority <= 2 AND created_at < ?
           ORDER BY priority ASC, created_at ASC LIMIT 5""",
        (cutoff,),
    )

    for r in rows:
        todo_id = str(r["id"])
        created_at = r.get("created_at")
        days_ago = _days_since(created_at)
        title = (r.get("title") or "").strip()
        project = r.get("project")

        yield CandidateSignal(
            source_ref=f"stuck-todo:{todo_id}",
            severity="warn",
            summary=(
                f"{project + ': ' if project else ''}"
                f"P{r['priority']} todo stuck {days_ago}d — {title[:80]}"
            ),
            evidence={
                "scanner": "stuck_todo",
                "bucket": "stale",
                "source_kind": "todo",
                "source_id": todo_id,
                "source_age_days": days_ago,
                "source_created_at": created_at,
                # Per-scanner detail kept for the expanded JSON dump.
                "todo_id": todo_id,
                "priority": r["priority"],
                "project": project,
                "title": title[:240],
                "days_old": days_ago,
            },
            suggested_action=f"Triage stuck todo: {title[:80]}",
        )


# ── aging_thought ───────────────────────────────────────────────────


def scan_aging_thought(db) -> Iterable[CandidateSignal]:
    """Yield candidates for surfaced-but-unengaged actionable thoughts > 14d.

    Mirrors the reminder-suggestion engine's actionable-category filter
    (note/observation excluded — 0% historical conversion) AND the
    `thoughts_aging` last_surfaced gate: only thoughts okuro has
    actually shown the user are eligible. Caps at 5 per scan to keep
    the daily signal volume below the user's items-per-level axiom.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=_AGING_THOUGHT_DAYS)
    ).strftime("%Y-%m-%d %H:%M:%S")

    placeholders = ",".join("?" * len(_ACTIONABLE_THOUGHT_CATEGORIES))
    rows = db.fetchall(
        f"""SELECT id, content, project, created_at FROM thoughts
           WHERE status = 'open'
             AND created_at < ?
             AND last_surfaced IS NOT NULL
             AND json_extract(metadata, '$.category') IN ({placeholders})
           ORDER BY created_at ASC LIMIT 5""",
        (cutoff, *_ACTIONABLE_THOUGHT_CATEGORIES),
    )

    for r in rows:
        thought_id = str(r["id"])
        created_at = r.get("created_at")
        days_ago = _days_since(created_at)
        raw = (r.get("content") or "").strip()
        first_line = _first_actionable_line(raw)
        project = r.get("project")

        yield CandidateSignal(
            source_ref=f"aging-thought:{thought_id}",
            severity="info",
            summary=(
                f"{project + ': ' if project else ''}"
                f"Thought idle {days_ago}d — {first_line[:80]}"
            ),
            evidence={
                "scanner": "aging_thought",
                "bucket": "stale",
                "source_kind": "thought",
                "source_id": thought_id,
                "source_age_days": days_ago,
                "source_created_at": created_at,
                # Per-scanner detail kept for the expanded JSON dump.
                "thought_id": thought_id,
                "project": project,
                "days_old": days_ago,
                "snippet": raw[:240],
            },
            suggested_action=first_line[:80],
        )


# ── sysinfo_disk ────────────────────────────────────────────────────


def scan_sysinfo_disk(_db) -> Iterable[CandidateSignal]:
    """Yield one candidate per mount over 85% used.

    Source: ``okuro.system.storage.get_storage_status``. Failures
    (no psutil, permission denied) yield nothing — the scanner is
    best-effort.
    """
    try:
        from okuro.system.storage import get_storage_status
        status = get_storage_status(alert_threshold=_DISK_WARN_PERCENT)
    except Exception as exc:
        log.debug("disk scanner unavailable: %s", exc)
        return
    for mount in status.get("mounts", []):
        pct = mount.get("usage_percent", 0)
        if pct < _DISK_WARN_PERCENT:
            continue

        severity = "crit" if pct >= _DISK_CRIT_PERCENT else "warn"
        path = mount.get("path", "?")
        free_gb = mount.get("free_gb", 0.0)

        yield CandidateSignal(
            source_ref=f"disk:{path}",
            severity=severity,
            summary=f"Storage {path} at {pct}% — {free_gb:.1f} GB free",
            evidence={
                "scanner": "sysinfo_disk",
                "bucket": "metric",
                "source_kind": "metric",
                "source_id": path,
                # Snapshot metric — no source_age_days. metric_value
                # carries the current reading instead.
                "metric_value": pct,
                "metric_unit": "%",
                "mount": path,
                "usage_percent": pct,
                "free_gb": free_gb,
                "device": mount.get("device"),
            },
            suggested_action=f"Free space on {path}",
        )


# ── sysinfo_vram ────────────────────────────────────────────────────


def scan_sysinfo_vram(_db) -> Iterable[CandidateSignal]:
    """Yield one candidate per GPU whose VRAM utilization > 90%.

    Source: ``okuro.system.gpu.get_gpu_status``. Skips silently when
    nvidia-smi is missing (Mac, CI). P1 reads instantaneous values;
    sustained-window detection is deferred to P2.
    """
    try:
        from okuro.system.gpu import get_gpu_status
        status = get_gpu_status()
    except Exception as exc:
        log.debug("vram scanner unavailable: %s", exc)
        return
    if status.get("error"):
        log.debug("vram scanner: %s", status["error"])
        return

    for gpu in status.get("gpus", []):
        vram = gpu.get("vram", {}) or {}
        util = vram.get("utilization_percent", 0)
        if util < _VRAM_WARN_PERCENT:
            continue

        gpu_id = gpu.get("id", 0)
        name = gpu.get("name", f"GPU{gpu_id}")

        yield CandidateSignal(
            source_ref=f"vram:gpu{gpu_id}",
            severity="info",
            summary=(
                f"{name} VRAM at {util:.0f}% — "
                f"{vram.get('used_mb', 0)}/{vram.get('total_mb', 0)} MB"
            ),
            evidence={
                "scanner": "sysinfo_vram",
                "bucket": "metric",
                "source_kind": "metric",
                "source_id": f"gpu{gpu_id}",
                "metric_value": util,
                "metric_unit": "%",
                "gpu_id": gpu_id,
                "gpu_name": name,
                "vram_percent": util,
                "used_mb": vram.get("used_mb"),
                "total_mb": vram.get("total_mb"),
            },
            suggested_action=f"Free VRAM on {name}",
        )


# ── session_failures ────────────────────────────────────────────────


def scan_session_failures(db) -> Iterable[CandidateSignal]:
    """Yield a candidate when last 24h sessions errored ≥2x the 14d baseline.

    Uses the `sessions` table (020 + 021). A failure is end_reason in
    {error, timeout, crash}. Min absolute floor (`_SESSION_FAILURE_MIN`)
    avoids firing on every tiny spike.
    """
    try:
        row_recent = db.fetchone(
            """SELECT COUNT(*) AS c FROM sessions
               WHERE ended_at >= datetime('now', '-24 hours')
                 AND end_reason IN ('error','timeout','crash')""",
        )
        recent = int(row_recent["c"]) if row_recent else 0

        row_baseline = db.fetchone(
            """SELECT COUNT(*) AS c FROM sessions
               WHERE ended_at >= datetime('now', '-14 days')
                 AND ended_at < datetime('now', '-24 hours')
                 AND end_reason IN ('error','timeout','crash')""",
        )
        baseline_total = int(row_baseline["c"]) if row_baseline else 0
    except Exception as exc:
        log.debug("session-failures scanner skipped: %s", exc)
        return

    baseline_per_day = baseline_total / 13.0 if baseline_total else 0.0

    if recent < _SESSION_FAILURE_MIN:
        return
    if baseline_per_day > 0 and recent < baseline_per_day * _SESSION_FAILURE_RATIO:
        return

    # Use a 24h-bucketed source_ref so today's spike stays one signal —
    # tomorrow can fire a fresh one if the spike persists.
    bucket = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    yield CandidateSignal(
        source_ref=f"session-failures:{bucket}",
        severity="warn",
        summary=(
            f"{recent} session failures in last 24h "
            f"(14d baseline: {baseline_per_day:.1f}/day)"
        ),
        evidence={
            "scanner": "session_failures",
            "bucket": "metric",
            "source_kind": "session_bucket",
            "source_id": bucket,
            "metric_value": recent,
            "metric_unit": "failures/24h",
            "recent_24h": recent,
            "baseline_per_day": round(baseline_per_day, 2),
            "date_bucket": bucket,
        },
        suggested_action="Investigate recent session errors",
    )


# ── helpers ─────────────────────────────────────────────────────────


def _days_since(ts: str | None) -> int:
    if not ts:
        return 0
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).days
    except Exception:
        return 0


def _first_actionable_line(raw: str) -> str:
    """Return the first non-empty, non-heading line of a thought body."""
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("##") or line == "---":
            continue
        if line.startswith("- [x]"):
            continue
        if line.startswith("- [ ] "):
            line = line[6:]
        elif line.startswith("- "):
            line = line[2:]
        if len(line) > 10:
            return line
    return raw[:80]


# ── disk_smart ───────────────────────────────────────────────────────


def scan_disk_smart(_db) -> Iterable[CandidateSignal]:
    """Yield one candidate per drive whose SMART health is warn/fail.

    Source: ``okuro.system.smart.get_disk_health`` (SATA + NVMe).
    Emits one signal per unhealthy device (worst finding as the
    plain-language summary); ``evidence`` carries every fired rule plus
    the raw normalized attributes — the why-drilldown. ``source_ref`` is
    keyed on the drive serial so the verify-on-fix re-check
    (``smart.recheck_disk_health``) can reconcile the exact device.
    Best-effort: missing backend / privilege yields nothing.
    """
    try:
        from okuro.system.smart import get_disk_health
        report = get_disk_health()
    except Exception as exc:
        log.debug("disk-smart scanner unavailable: %s", exc)
        return

    for dev in report.get("devices", []):
        if dev.get("health") not in ("warn", "fail"):
            continue
        findings = dev.get("findings") or []
        if not findings:
            continue
        headline = findings[0]
        ref = dev.get("serial") or dev.get("device")
        severity = "crit" if dev["health"] == "fail" else "warn"

        yield CandidateSignal(
            source_ref=f"smart:{ref}",
            severity=severity,
            summary=headline["message"],
            evidence={
                "scanner": "disk_smart",
                "bucket": "hardware",
                "source_kind": "device",
                "source_id": ref,
                "device": dev.get("device"),
                "model": dev.get("model"),
                "serial": dev.get("serial"),
                "protocol": dev.get("type"),
                "smart_passed": dev.get("smart_passed"),
                "health": dev["health"],
                "rule": headline["id"],
                "attribute": headline.get("attribute"),
                "value": headline.get("value"),
                "findings": findings,
                "attributes": dev.get("attributes", {}),
                "read_via": dev.get("read_via"),
            },
            suggested_action=headline["message"],
        )


# Ordered: cheapest/most-reliable first so the per-scan budget is spent
# on the highest-signal scanners before the noisier ones.
SCANNERS: dict[str, Any] = {
    "stale_progress": scan_stale_progress,
    "stuck_todo": scan_stuck_todo,
    "aging_thought": scan_aging_thought,
    "sysinfo_disk": scan_sysinfo_disk,
    "disk_smart": scan_disk_smart,
    "sysinfo_vram": scan_sysinfo_vram,
    "session_failures": scan_session_failures,
}

# Scanners whose yield is the COMPLETE current truth: they enumerate every
# candidate that exists (every mount, every GPU) with no LIMIT, so a condition
# they do NOT yield is a condition that no longer holds. The engine retracts
# their open signals accordingly — see engine._retract_resolved.
#
# Why this needs a whitelist rather than applying to every scanner: the others
# are SAMPLED, not exhaustive. stale_progress caps at LIMIT 20, stuck_todo and
# aging_thought at LIMIT 5. A candidate missing from their yield may simply be
# past the cap, not resolved — auto-retracting those would silently close real
# alerts the user never saw.
#
# session_failures is deliberately excluded too, despite yielding at most one:
# its source_ref is date-bucketed, so yesterday's spike can never be re-emitted
# and would be retracted every night on a technicality rather than because the
# situation changed.
#
# The cost of getting this wrong is asymmetric — a stale alert is visible and
# dismissable, a silently-retracted real alert is neither. Only add a scanner
# here if it enumerates its whole domain unconditionally.
EXHAUSTIVE_SCANNERS: frozenset[str] = frozenset({"sysinfo_disk", "sysinfo_vram"})


__all__ = [
    "CandidateSignal",
    "SCANNERS",
    "EXHAUSTIVE_SCANNERS",
    "scan_stale_progress",
    "scan_stuck_todo",
    "scan_aging_thought",
    "scan_sysinfo_disk",
    "scan_disk_smart",
    "scan_sysinfo_vram",
    "scan_session_failures",
]
