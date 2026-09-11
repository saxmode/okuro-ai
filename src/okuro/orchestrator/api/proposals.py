# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Proposals API — CRUD endpoints for okuro signal proposals/opportunities.
# index:
#   imports
#   class ProposalResponse
#   class ProposalEdit
#   class ProposalAcknowledge
#   class ProposalSnooze
#   class ProposalStats
#   class BandDefinition
#   def _read_state_file
#   def _read_overrides
#   def _write_overrides
#   def _compute_band
#   def _apply_overrides
#   def _is_snoozed
#   def _enrich_opportunity
#   def _get_dismissal_store
#   def _create_task_from_proposal
# AGENT_HEADER_END -->
"""Proposals API — CRUD endpoints for okuro signal proposals/opportunities.

Reads the state file written by the signal daemon, applies user overrides,
and exposes proposals for a web UI to fully control signal triage.
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

logger = logging.getLogger("okuro.orchestrator.api.proposals")

# ── Constants ────────────────────────────────────────────────────────────────

STATE_FILE = os.path.expanduser("~/.cache/okuro-signal/opportunities.json")
# OKURO_ROOT resolved at import time from environment or parent directory
OKURO_ROOT = Path(os.environ.get("OKURO_ROOT", Path(__file__).parent.parent.resolve()))
OVERRIDES_FILE = OKURO_ROOT / "signals" / "overrides.json"

BAND_DEFINITIONS = {
    "urgent":  {"min": 0.75, "max": 1.0,  "color": "#ef4444", "label": "ACT TODAY"},
    "active":  {"min": 0.50, "max": 0.74, "color": "#f59e0b", "label": "THIS SESSION"},
    "parked":  {"min": 0.25, "max": 0.49, "color": "#6b7280", "label": "ON RADAR"},
    "sinking": {"min": 0.10, "max": 0.24, "color": "#374151", "label": "DECAYING"},
    "dead":    {"min": 0.0,  "max": 0.09, "color": "#1f2937", "label": "BELOW THRESHOLD"},
}

# ── Pydantic Models ──────────────────────────────────────────────────────────


class ProposalResponse(BaseModel):
    signal_id: str
    channel: str
    signal_type: str = ""
    content: str                              # signal content (truncated to 500 chars)
    timestamp: str                            # ISO
    score: float
    score_override: Optional[float] = None
    band: str
    category: str
    effort: str
    question: str
    suggested_action: str
    suggested_role: Optional[str] = None
    reason: str
    base_priority: float = 0.0
    urgency_multiplier: float = 1.0
    freshness: float = 1.0
    engagement: float = 1.0
    noise_penalty: float = 0.0
    duplicate_of: Optional[str] = None
    snoozed_until: Optional[str] = None
    metadata: dict = Field(default_factory=dict)


class ProposalEdit(BaseModel):
    question: Optional[str] = None
    suggested_action: Optional[str] = None
    category: Optional[str] = None
    effort: Optional[str] = None
    suggested_role: Optional[str] = None
    score_override: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class ProposalAcknowledge(BaseModel):
    create_task: bool = False


class ProposalSnooze(BaseModel):
    hours: float = Field(default=4.0, gt=0, le=168, description="Hours to snooze (max 7 days)")


class ProposalStats(BaseModel):
    total: int
    by_band: Dict[str, int]
    by_category: Dict[str, int]
    dismissed_count: int
    snoozed_count: int


class BandDefinition(BaseModel):
    min: float
    max: float
    color: str
    label: str


# ── File I/O ─────────────────────────────────────────────────────────────────


def _read_state_file() -> dict:
    """Read the signal daemon's state file. Returns empty structure on error."""
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        logger.warning("Cannot read state file %s: %s", STATE_FILE, exc)
        return {"updated_at": None, "opportunities": [], "dismissed_ids": []}


def _read_overrides() -> dict:
    """Read user overrides. Returns empty dict if file missing."""
    try:
        with open(OVERRIDES_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_overrides(overrides: dict) -> None:
    """Atomic write of overrides file (write .tmp, rename)."""
    OVERRIDES_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(OVERRIDES_FILE.parent), suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(overrides, f, indent=2, default=str)
        os.rename(tmp_path, str(OVERRIDES_FILE))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ── Helpers ──────────────────────────────────────────────────────────────────


def _compute_band(score: float) -> str:
    """Map a 0-1 score to a band name."""
    if score >= 0.75:
        return "urgent"
    elif score >= 0.50:
        return "active"
    elif score >= 0.25:
        return "parked"
    elif score >= 0.10:
        return "sinking"
    return "dead"


def _apply_overrides(opp: dict, overrides: dict) -> dict:
    """Apply user overrides on top of a raw opportunity dict. Returns enriched copy."""
    result = dict(opp)
    ov = overrides.get(opp["signal_id"], {})
    if not ov:
        return result

    # Apply field-level overrides
    for field in ("question", "suggested_action", "category", "effort", "suggested_role"):
        if field in ov:
            result[field] = ov[field]

    # Score override replaces computed score for display/sorting
    if "score_override" in ov and ov["score_override"] is not None:
        result["score_override"] = ov["score_override"]
        result["score"] = ov["score_override"]

    # Snooze
    if "snoozed_until" in ov:
        result["snoozed_until"] = ov["snoozed_until"]

    return result


def _is_snoozed(opp: dict) -> bool:
    """Check if an opportunity is currently snoozed."""
    snoozed_until = opp.get("snoozed_until")
    if not snoozed_until:
        return False
    try:
        snooze_dt = datetime.fromisoformat(snoozed_until)
        return datetime.now(timezone.utc) < snooze_dt
    except (ValueError, TypeError):
        return False


def _enrich_opportunity(opp: dict, overrides: dict) -> ProposalResponse:
    """Convert a raw state-file opportunity + overrides into a ProposalResponse."""
    opp = _apply_overrides(opp, overrides)

    # Extract scoring transparency fields from metadata
    meta = opp.get("metadata", {})
    signal_type = meta.get("signal_type", opp.get("signal_type", ""))
    score = opp.get("score", 0.0)

    return ProposalResponse(
        signal_id=opp["signal_id"],
        channel=opp.get("channel", ""),
        signal_type=signal_type,
        content=(opp.get("content", "") or "")[:500],
        timestamp=opp.get("timestamp", ""),
        score=score,
        score_override=opp.get("score_override"),
        band=opp.get("band", "") or _compute_band(score),
        category=opp.get("category", ""),
        effort=opp.get("effort", ""),
        question=opp.get("question", ""),
        suggested_action=opp.get("suggested_action", ""),
        suggested_role=opp.get("suggested_role") or meta.get("suggested_role"),
        reason=opp.get("reason", ""),
        base_priority=opp.get("base_priority", meta.get("base_priority", 0.0)),
        urgency_multiplier=opp.get("urgency_multiplier", meta.get("urgency_multiplier", 1.0)),
        freshness=opp.get("freshness", meta.get("freshness", 1.0)),
        engagement=opp.get("engagement", meta.get("engagement", 1.0)),
        noise_penalty=opp.get("noise_penalty", meta.get("noise_penalty", 0.0)),
        duplicate_of=opp.get("duplicate_of", meta.get("duplicate_of")),
        snoozed_until=opp.get("snoozed_until"),
        metadata=meta,
    )


def _get_dismissal_store():
    """Lazy-import and instantiate the SignalDismissalStore."""
    from okuro.orchestrator.signals.db import SignalDismissalStore
    return SignalDismissalStore()


# ── Router ───────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/proposals", tags=["proposals"])


@router.get("", response_model=List[ProposalResponse])
async def get_proposals(
    band: Optional[str] = Query(default=None, description="Filter by band: urgent/active/parked/sinking/dead"),
    limit: int = Query(default=50, ge=1, le=500),
    sort: str = Query(default="score", description="Sort by: score, age, band"),
):
    """List all active proposals with full detail."""
    state = _read_state_file()
    overrides = _read_overrides()
    dismissed_ids = set(state.get("dismissed_ids", []))

    proposals = []
    for opp in state.get("opportunities", []):
        if opp.get("dismissed") or opp["signal_id"] in dismissed_ids:
            continue
        enriched = _enrich_opportunity(opp, overrides)
        # Skip currently-snoozed proposals from the main list
        if _is_snoozed(enriched.model_dump()):
            continue
        proposals.append(enriched)

    # Filter by band
    if band:
        band_lower = band.lower()
        if band_lower not in BAND_DEFINITIONS:
            raise HTTPException(400, f"Unknown band: {band}. Valid: {', '.join(BAND_DEFINITIONS)}")
        proposals = [p for p in proposals if p.band == band_lower]

    # Sort
    if sort == "age":
        proposals.sort(key=lambda p: p.timestamp or "", reverse=True)
    elif sort == "band":
        band_order = {"urgent": 0, "active": 1, "parked": 2, "sinking": 3, "dead": 4}
        proposals.sort(key=lambda p: (band_order.get(p.band, 5), -p.score))
    else:
        # Default: score descending
        proposals.sort(key=lambda p: p.score, reverse=True)

    return proposals[:limit]


@router.get("/stats", response_model=ProposalStats)
async def get_proposal_stats():
    """Aggregate stats across all proposals."""
    state = _read_state_file()
    overrides = _read_overrides()
    dismissed_ids = set(state.get("dismissed_ids", []))

    by_band: Dict[str, int] = {b: 0 for b in BAND_DEFINITIONS}
    by_category: Dict[str, int] = {}
    snoozed_count = 0
    total_active = 0

    for opp in state.get("opportunities", []):
        if opp.get("dismissed") or opp["signal_id"] in dismissed_ids:
            continue
        enriched = _enrich_opportunity(opp, overrides)

        if _is_snoozed(enriched.model_dump()):
            snoozed_count += 1
            continue

        total_active += 1
        by_band[enriched.band] = by_band.get(enriched.band, 0) + 1
        by_category[enriched.category] = by_category.get(enriched.category, 0) + 1

    return ProposalStats(
        total=total_active,
        by_band=by_band,
        by_category=by_category,
        dismissed_count=len(dismissed_ids),
        snoozed_count=snoozed_count,
    )


@router.get("/bands", response_model=Dict[str, BandDefinition])
async def get_proposal_bands():
    """Return band definitions for UI rendering."""
    return {k: BandDefinition(**v) for k, v in BAND_DEFINITIONS.items()}


@router.get("/{signal_id:path}", response_model=ProposalResponse)
async def get_proposal_detail(signal_id: str):
    """Get a single proposal with full detail including scoring breakdown."""
    state = _read_state_file()
    overrides = _read_overrides()

    for opp in state.get("opportunities", []):
        if opp["signal_id"] == signal_id:
            return _enrich_opportunity(opp, overrides)

    raise HTTPException(404, f"Proposal not found: {signal_id}")


@router.patch("/{signal_id:path}", response_model=ProposalResponse)
async def edit_proposal(signal_id: str, edit: ProposalEdit):
    """Edit a proposal's user-facing fields. Persisted to overrides.json."""
    # Verify the signal exists
    state = _read_state_file()
    found = None
    for opp in state.get("opportunities", []):
        if opp["signal_id"] == signal_id:
            found = opp
            break
    if not found:
        raise HTTPException(404, f"Proposal not found: {signal_id}")

    # Read current overrides, merge changes
    overrides = _read_overrides()
    current = overrides.get(signal_id, {})

    edits = edit.model_dump(exclude_none=True)
    if not edits:
        raise HTTPException(400, "No fields to update")

    current.update(edits)
    overrides[signal_id] = current
    _write_overrides(overrides)

    logger.info("Proposal %s edited: %s", signal_id, list(edits.keys()))
    return _enrich_opportunity(found, overrides)


@router.post("/{signal_id:path}/dismiss")
async def dismiss_proposal(signal_id: str):
    """Dismiss a proposal. Stored in the signal dismissal DB."""
    # Verify signal exists
    state = _read_state_file()
    found = any(o["signal_id"] == signal_id for o in state.get("opportunities", []))
    if not found:
        raise HTTPException(404, f"Proposal not found: {signal_id}")

    try:
        store = _get_dismissal_store()
        store.persist(signal_id, reason="api_dismiss")
    except Exception as exc:
        logger.error("Failed to persist dismissal for %s: %s", signal_id, exc)
        raise HTTPException(500, f"Failed to persist dismissal: {exc}")

    logger.info("Proposal %s dismissed via API", signal_id)
    return {"status": "dismissed", "signal_id": signal_id}


@router.post("/{signal_id:path}/acknowledge")
async def acknowledge_proposal(signal_id: str, body: Optional[ProposalAcknowledge] = None):
    """Acknowledge a proposal ('I'll handle this'). Optionally creates an okuro task."""
    # Verify signal exists
    state = _read_state_file()
    found = None
    for opp in state.get("opportunities", []):
        if opp["signal_id"] == signal_id:
            found = opp
            break
    if not found:
        raise HTTPException(404, f"Proposal not found: {signal_id}")

    # Dismiss (acknowledge = dismiss + optional task)
    try:
        store = _get_dismissal_store()
        store.persist(signal_id, reason="acknowledged")
    except Exception as exc:
        logger.warning("Failed to persist acknowledgment for %s: %s", signal_id, exc)

    result = {"status": "acknowledged", "signal_id": signal_id, "task_created": False}

    # Optionally spawn an okuro task from the proposal
    if body and body.create_task:
        try:
            task_id = _create_task_from_proposal(found)
            result["task_created"] = True
            result["task_id"] = task_id
            logger.info("Proposal %s acknowledged + task %s created", signal_id, task_id)
        except Exception as exc:
            logger.error("Task creation from proposal %s failed: %s", signal_id, exc)
            result["task_error"] = str(exc)
    else:
        logger.info("Proposal %s acknowledged (no task)", signal_id)

    return result


@router.post("/{signal_id:path}/snooze")
async def snooze_proposal(signal_id: str, body: Optional[ProposalSnooze] = None):
    """Snooze a proposal for N hours."""
    # Verify signal exists
    state = _read_state_file()
    found = any(o["signal_id"] == signal_id for o in state.get("opportunities", []))
    if not found:
        raise HTTPException(404, f"Proposal not found: {signal_id}")

    hours = body.hours if body else 4.0
    from datetime import timedelta
    snooze_until = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()

    overrides = _read_overrides()
    current = overrides.get(signal_id, {})
    current["snoozed_until"] = snooze_until
    overrides[signal_id] = current
    _write_overrides(overrides)

    logger.info("Proposal %s snoozed until %s", signal_id, snooze_until)
    return {"status": "snoozed", "signal_id": signal_id, "snoozed_until": snooze_until}


# ── Task creation helper ─────────────────────────────────────────────────────


def _create_task_from_proposal(opp: dict) -> str:
    """Create an okuro task from a proposal. Returns task_id."""
    from datetime import datetime as _dt

    task_id = f"task-{_dt.now().strftime('%Y%m%d-%H%M%S')}"
    tasks_dir = OKURO_ROOT / "tasks"
    task_dir = tasks_dir / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    description = opp.get("suggested_action", opp.get("question", ""))
    source_signal = opp.get("signal_id", "")

    task_yaml = {
        "id": task_id,
        "description": description,
        "status": "pending",
        "created_at": _dt.now(timezone.utc).isoformat(),
        "source": f"proposal:{source_signal}",
        "metadata": {
            "origin": "proposals-api",
            "signal_id": source_signal,
            "signal_channel": opp.get("channel", ""),
            "signal_score": opp.get("score", 0),
            "original_question": opp.get("question", ""),
        },
    }

    from okuro.orchestrator.state import atomic_dump_yaml
    atomic_dump_yaml(task_dir / "task.yaml", task_yaml, default_flow_style=False, sort_keys=False)

    logger.info("Created task %s from proposal %s", task_id, source_signal)
    return task_id
