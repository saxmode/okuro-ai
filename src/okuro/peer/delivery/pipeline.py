# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: peer.delivery.pipeline — 5-stage delivery_send orchestration.
#   SourceDocument -> outline_for_recipient -> tokens_to_theme ->
#   channel.render -> deliveries.insert. Best-effort (HR-C3): failures
#   produce a delivery row with success=False rather than raising.
# index: imports | def send | def _resolve_brand_id
# AGENT_HEADER_END -->
"""Delivery pipeline orchestration.

Public entry point for Stream C: ``delivery.send(artifact_id, person_id,
channel, brand_id?)`` walks the 5-stage pipeline and persists a row in
the deliveries table. The MCP tool ``delivery_send`` (registered in
sense/mcp_tools.py) is a thin wrapper around this function.

The orchestrator's engine.py also calls this directly when
``task.audience`` is set, fanning out one send per Stream B artifact.

HR-C3: this function never raises into the caller. Every failure path
returns a {"success": False, "error": "..."} envelope and persists a
row with success=0 so we have an audit trail of failed deliveries.
"""

from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger(__name__)


_DEFAULT_CHANNEL = "markdown"


# Phrases a tailoring model produces when it is handed a pointer instead of
# content. Each was observed or is the same shape as one that was — this is a
# detector for a known failure mode, NOT a correctness proof, and it is meant
# to be appended to when a new phrasing shows up in the deliveries table.
_EMPTY_DELIVERY_MARKERS = (
    "content pending",
    "not yet provided",
    "no content was included",
    "content was not included",
    "content not provided",
    "[to be provided",
    "placeholder for the actual content",
)


def _self_declared_empty(body: str | None) -> str | None:
    """The first marker phrase found in a rendered body, or None."""
    text = (body or "").lower()
    for marker in _EMPTY_DELIVERY_MARKERS:
        if marker in text:
            return marker
    return None


def send(
    artifact_id: str,
    *,
    person_id: str | None = None,
    channel: str = _DEFAULT_CHANNEL,
    brand_id: str | None = None,
    title: str | None = None,
    context: str | None = None,
    created_by: str | None = None,
    extra_documents: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Run the delivery pipeline for one (artifact, recipient, channel).

    Returns:
        {
            "success": bool,
            "delivery_id": str | None,
            "channel": str,
            "person_id": str | None,
            "duration_ms": int,
            "error": str | None,
            "media_type": str | None,
        }

    ``extra_documents`` is ``[(label, text), ...]`` appended to the source
    body before the outline stage. It exists because the delivery pipeline
    reads artifact BODIES only, while the M3 reviewer and the preview
    proposer BOTH also read the files a subtask declared — measured
    2026-08-01, and it made Stream C the one consumer that could hand a
    recipient a summary of a document they do not have.

    The caller supplies the content, never a path. This module serves any
    artifact from any source and must not learn how to resolve an
    orchestrator subtask's declared outputs; that knowledge lives in the
    orchestrator, which is why the parameter is generic text.
    """
    start = time.monotonic()

    # Validate inputs early — these branches never persist a row.
    if not artifact_id or not artifact_id.strip():
        return _err(channel, person_id, "artifact_id is required",
                    duration_ms=int((time.monotonic() - start) * 1000))

    from okuro.peer.delivery import channels as channel_registry
    if channel not in channel_registry.list_channels():
        return _err(channel, person_id,
                    f"unknown channel {channel!r}; available: "
                    f"{channel_registry.list_channels()}",
                    duration_ms=int((time.monotonic() - start) * 1000))

    # Stage 1 — SourceDocument
    from okuro.peer.delivery.source import from_artifact_id
    source = from_artifact_id(artifact_id, extra_documents=extra_documents)
    if source is None:
        return _persist_failure(
            artifact_id=artifact_id, channel=channel, person_id=person_id,
            brand_id=brand_id, title=title, error="artifact not found",
            duration_ms=int((time.monotonic() - start) * 1000),
            created_by=created_by,
        )

    # Stage 2 — Outline (slider-driven)
    cognitive = _cognitive_for(person_id)
    from okuro.peer.delivery.outline import outline_for_recipient
    outline = outline_for_recipient(
        source,
        cognitive,
        translate=bool(person_id),
        person_id=person_id,
        context=context or source.summary or None,
    )

    # Stage 3 — ThemeBundle
    resolved_brand_id = _resolve_brand_id(brand_id, source)
    from okuro.peer.delivery.theme import tokens_to_theme
    theme = tokens_to_theme(resolved_brand_id, channel)

    # Stage 4 — Channel render
    renderer = channel_registry.get(channel)
    if renderer is None:
        # Should never hit (we validated above) but be defensive.
        return _persist_failure(
            artifact_id=artifact_id, channel=channel, person_id=person_id,
            brand_id=resolved_brand_id, title=title or source.title,
            error=f"channel {channel!r} not registered",
            duration_ms=int((time.monotonic() - start) * 1000),
            outline=outline.to_json(), theme=theme.to_json(),
            created_by=created_by,
        )

    try:
        output = renderer(outline, theme)
    except Exception as exc:
        log.exception("channel renderer %s raised", channel)
        return _persist_failure(
            artifact_id=artifact_id, channel=channel, person_id=person_id,
            brand_id=resolved_brand_id, title=title or source.title,
            error=f"renderer raised: {exc}",
            duration_ms=int((time.monotonic() - start) * 1000),
            outline=outline.to_json(), theme=theme.to_json(),
            created_by=created_by,
        )

    duration_ms = int((time.monotonic() - start) * 1000)

    # Stage 4b — a delivery that announces its own emptiness is not a success.
    #
    # Observed live 2026-08-01: a delivery to a named recipient rendered
    # "## Scenario 1 *[Content pending — not yet provided by sender]*" for all
    # three sections, and was recorded with success=1. HR-C3 guarantees this
    # module never RAISES; that is not the same as never being wrong, and the
    # deliveries table is the audit log — a lie in it is worse than a gap.
    #
    # The pipeline itself was faithful. The source artifact body was a POINTER
    # ("Deliverable: report.html (self-contained, inline CSS)") rather than the
    # content, so the tailoring model correctly reported that the content was
    # not there. Fixing THAT belongs upstream, at what a subagent puts in an
    # artifact body. This only stops the wrong answer being filed as a right
    # one.
    #
    # Deliberately a marker list, not a length or similarity heuristic: the
    # failure is a model politely narrating an absence, and it produces MORE
    # text, not less. A short delivery can be perfectly good.
    _empty_marker = _self_declared_empty(output.body)
    if _empty_marker and output.success:
        log.warning("delivery for %s rendered a self-declaring-empty body (%r)",
                    artifact_id, _empty_marker)
        output.success = False
        output.error = (
            f"rendered body declares its own content missing ({_empty_marker!r}) — "
            "the source artifact body likely points at a deliverable instead of "
            "containing it"
        )

    # Stage 5 — persist
    from okuro.peer.delivery.store import delivery_write
    delivery_id = delivery_write(
        artifact_id=artifact_id,
        person_id=person_id,
        channel=channel,
        brand_id=resolved_brand_id,
        title=title or source.title,
        outline=outline.to_json(),
        theme=theme.to_json(),
        body=output.body,
        body_blob=output.body_blob,
        body_path=output.body_path,
        media_type=output.media_type,
        duration_ms=output.duration_ms or duration_ms,
        cost_usd=output.cost_usd,
        provider=output.provider,
        model=output.model,
        success=output.success,
        error=output.error,
        created_by=created_by,
    )
    if isinstance(delivery_id, str) and delivery_id.startswith("REJECTED"):
        return _err(channel, person_id, delivery_id,
                    duration_ms=duration_ms)

    return {
        "success": output.success,
        "delivery_id": delivery_id,
        "channel": channel,
        "person_id": person_id,
        "brand_id": resolved_brand_id,
        "duration_ms": output.duration_ms or duration_ms,
        "error": output.error,
        "media_type": output.media_type,
    }


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _cognitive_for(person_id: str | None) -> dict[str, Any]:
    """Read the firewalled cognitive profile for a person, or empty."""
    if not person_id:
        return {}
    try:
        from okuro.peer.cognitive_profile import cognitive_profile_for_llm
        return cognitive_profile_for_llm(person_id) or {}
    except Exception as exc:
        log.debug("cognitive profile read failed for %s: %s", person_id, exc)
        return {}


def _resolve_brand_id(brand_id: str | None, source) -> str | None:
    """Use explicit brand_id when given; else fall back to project-default
    via stack_brand_resolve when the artifact has a project. Returns None
    if nothing resolves (theme falls back to neutral defaults).
    """
    if brand_id:
        return brand_id
    try:
        # Some installs route project->brand via stack; this is best-effort.
        from okuro.db import get_db
        # SourceDocument already exposes the project the artifact belongs to
        # via the underlying artifact row, but we don't surface it on
        # SourceDocument today. Quick lookup:
        db = get_db()
        row = db.fetchone(
            "SELECT project FROM artifacts WHERE id = ?",
            (source.artifact_id,),
        )
        project = row.get("project") if row else None
        if not project:
            return None
        bind = db.fetchone(
            "SELECT brand_id FROM stack_project_brand WHERE project_slug = ?",
            (project,),
        )
        return bind["brand_id"] if bind else None
    except Exception:
        return None


def _err(channel: str, person_id: str | None, msg: str,
         *, duration_ms: int) -> dict[str, Any]:
    return {
        "success": False,
        "delivery_id": None,
        "channel": channel,
        "person_id": person_id,
        "duration_ms": duration_ms,
        "error": msg,
        "media_type": None,
    }


def _persist_failure(
    *,
    artifact_id: str,
    channel: str,
    person_id: str | None,
    brand_id: str | None,
    title: str | None,
    error: str,
    duration_ms: int,
    outline: dict | None = None,
    theme: dict | None = None,
    created_by: str | None,
) -> dict[str, Any]:
    """Persist a row even when the pipeline failed mid-flight (audit trail).

    Returns the same envelope shape as `send`. If even the persist fails
    (DB hiccup, etc.) we degrade to the in-memory error envelope.
    """
    try:
        from okuro.peer.delivery.store import delivery_write
        delivery_id = delivery_write(
            artifact_id=artifact_id,
            person_id=person_id,
            channel=channel,
            brand_id=brand_id,
            title=title,
            outline=outline or {},
            theme=theme or {},
            body=None,
            body_blob=None,
            body_path=None,
            media_type=None,
            duration_ms=duration_ms,
            cost_usd=None,
            provider=None,
            model=None,
            success=False,
            error=error,
            created_by=created_by,
        )
    except Exception as exc:
        log.warning("failure-persist itself failed: %s", exc)
        delivery_id = None
    if isinstance(delivery_id, str) and delivery_id.startswith("REJECTED"):
        delivery_id = None
    return {
        "success": False,
        "delivery_id": delivery_id,
        "channel": channel,
        "person_id": person_id,
        "brand_id": brand_id,
        "duration_ms": duration_ms,
        "error": error,
        "media_type": None,
    }
