# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Bootstrap packet assembler — a fixed CORE half plus a separately-fetched PROJECT half.
# index: imports | def _builders | def assemble | def assemble_project | def assemble_full
# AGENT_HEADER_END -->
"""Bootstrap packet assembler — a fixed CORE half plus a PROJECT half.

THE SPLIT. ``assemble()`` returns the CORE half only: session-invariant content
that is identical for every task on this machine. ``assemble_project(slug)``
returns the PROJECT half for a resolved slug. ``assemble_full()`` concatenates
both, for in-process consumers that have no second call to make.

WHY. Measured 2026-08-27: one packet carrying both halves came to 51,056 chars
for project okuro and 57,314 for the largest registered project, against a
50,000-char host result
envelope. Over that line the host discards the entire result and substitutes a
~2,000-char preview — so the more context okuro assembled, the less the agent
received. Each half fits alone with room to spare; only the sum broke. See
``contract.py`` for the section partition and the envelope constants.

WHAT WENT WITH IT. The token-budget ranking (``SECTION_PRIORITIES``,
``allocate_budget``) is deleted, not retuned. It existed to arbitrate scarcity;
after the split there is no scarcity to arbitrate. Section order is now the
declared order of ``contract.CORE_SECTIONS`` / ``contract.PROJECT_SECTIONS``.
"""

import json
import logging
import os as _os
from datetime import datetime, timezone

from .sections import (
    build_profile, build_behavioral, build_conventions, build_principles,
    build_principles_core, build_role_assignment, build_recipient,
    build_protocol,
    build_tools, build_tool_protocol, build_hardware,
    build_memory, build_memory_index, build_tunnels, build_project, build_thoughts, build_todos, build_reminders, build_distill_candidates, build_roles, build_codebase, build_people,
    build_during_work, build_progress, build_recent, build_design_profile, build_stack, build_session,
    build_brand, build_status_banner, _comm_prose,
)
from .intake import build_intake
from .resolver import resolve_project, resolve_role
from .budget import enforce_envelope
from .contract import (
    CORE_SECTIONS,
    PROJECT_SECTIONS,
    envelope_for,
    warn_threshold_for,
)
from ._safe import (
    _safe,
    clear_failed_sections,
    get_failed_sections,
    format_degraded_block,
)
from okuro.db.engine import okuro_home

log = logging.getLogger("okuro.sense.bootstrap.assembler")


def _authorship_from_config() -> dict:
    """Read the ``identity`` block from ``~/.okuro/config.yaml``.

    Honours ``$OKURO_HOME`` so a sandboxed run resolves its own config
    rather than the invoking user's. Returns ``{}`` for an absent,
    unreadable or malformed config: the banner degrades to naming nobody,
    which is the safe direction for a credit line.
    """
    import os
    from pathlib import Path

    home = os.environ.get("OKURO_HOME")
    base = Path(home) if home else okuro_home()
    path = base / "config.yaml"
    if not path.is_file():
        return {}
    import yaml

    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        return {}
    identity = data.get("identity")
    return identity if isinstance(identity, dict) else {}


def _get_rendered_body_ids() -> set[str]:
    """Read sections._RENDERED_BODY_IDS at call time (not closure time).

    Builders are invoked in a dict-iteration order; build_memory must run
    first and populate the cache before build_memory_index reads it.
    Lambdas capture the read at invocation time so the order matters.
    """
    from . import sections as _sections
    return set(_sections._RENDERED_BODY_IDS)


def _memory_index_limit() -> int:
    """M5: drop the duplicate pointer index by default (limit=0 short-
    circuits build_memory_index to empty). Legacy flag restores 30.
    Discovery surface lives in build_memory's pointer block; the
    additional index was duplicate coverage.
    """
    import os as _os
    if _os.environ.get("OKURO_LEGACY_BOOTSTRAP", "").lower() in (
        "1", "true", "yes"
    ):
        return 30
    return 0


def _builders(task_hint: str | None, project_slug: str | None,
              provider: str) -> dict:
    """Every section builder, keyed by section name.

    The keys of this dict MUST partition exactly into
    ``contract.CORE_SECTIONS`` + ``contract.PROJECT_SECTIONS`` — a builder in
    neither half would be unreachable, and a half naming a builder that does not
    exist would render a hole. ``tests/sense/test_bootstrap_split.py`` asserts
    the partition, so adding a builder without placing it fails loudly.
    """
    return {
        "behavioral": lambda: build_behavioral(),
        "profile": lambda: build_profile(),
        "conventions": lambda: build_conventions(),
        "people": lambda: build_people(),
        "recipient": lambda: build_recipient(task_hint),
        "role_assignment": lambda: build_role_assignment(task_hint),
        "principles_core": lambda: build_principles_core(),
        "principles": lambda: build_principles(),
        "protocol": lambda: build_protocol(),
        "tools": lambda: build_tools(provider=provider),
        "tool_protocol": lambda: build_tool_protocol(project_slug),
        "hardware": lambda: build_hardware(),
        # build_memory MUST run before build_memory_index — the index
        # reads sections._RENDERED_BODY_IDS to skip pointers whose bodies
        # already render in the memory section.
        "memory": lambda: build_memory(task_hint, project_slug),
        "memory_index": lambda: build_memory_index(
            task_hint, project_slug,
            limit=_memory_index_limit(),
            exclude_ids=_get_rendered_body_ids(),
        ),
        "tunnels": lambda: build_tunnels(project_slug),
        "project": lambda: build_project(project_slug),
        "intake": lambda: build_intake(task_hint, project_slug),
        "todos": lambda: build_todos(project_slug),
        "reminders": lambda: build_reminders(),
        # Renders nothing when the queue is empty — see the builder.
        "distill_candidates": lambda: build_distill_candidates(),
        "thoughts": lambda: build_thoughts(task_hint, project_slug),
        "roles": lambda: build_roles(),
        "codebase": lambda: build_codebase(),
        "during_work": lambda: build_during_work(),
        "progress": lambda: build_progress(project_slug),
        # Time-ordered, cross-project. Deliberately NOT gated on the task
        # hint: recency and relevance are different questions, and every
        # other memory surface here answers only the second one.
        "recent": lambda: build_recent(),
        "brand": lambda: build_brand(project_slug),
        "design_profile": lambda: build_design_profile(project_slug),
        "stack": lambda: build_stack(project_slug),
        "session": lambda: build_session(),
    }



def _run_builders(names, task_hint, project_slug, provider) -> dict:
    """Run the named builders, in `names` order, tolerating individual failures.

    Each builder is wrapped so one failure doesn't drop the remaining sections;
    the failure is recorded for the degraded-mode block instead of vanishing.
    Order is preserved because ``build_memory`` MUST run before
    ``build_memory_index`` (the index reads a cache the first one populates).
    """
    all_builders = _builders(task_hint, project_slug, provider)
    out: dict[str, str] = {}
    for name in names:
        builder = all_builders.get(name)
        if builder is None:
            continue
        result = _safe(f"assembler.builder.{name}", builder)
        if result is None:
            continue
        _, content, _ = result
        if content:
            out[name] = content
    return out


def _mint_session(provider: str, task_hint: str | None,
                  project_slug: str | None) -> str | None:
    """Write the telemetry marker and create the session row.

    Runs BEFORE the builders. build_memory (and other sections) surface
    memories/thoughts into surface_log, whose session_id is read from
    session_state — so if the id isn't set yet, every one of those surfacings
    logs session_id=NULL. That was 90% of surface_log (Fable audit G6), which
    starved the memory-utility loop that joins surface_log against session
    compliance (it saw ~11% of surfacings).

    CORE HALF ONLY. ``assemble_project`` must never call this: a second marker
    with a new id would trip ``_rotate_for_new_agent`` and wipe the compliance
    state of the session that just bootstrapped.
    """
    def _write_session():
        from okuro.telemetry.logger import write_bootstrap_marker
        sid = write_bootstrap_marker(provider=provider, task_hint=task_hint or "")

        from okuro.sense.telemetry import create_session
        from okuro.sense.work_identity import resolve_work_identity

        # P4.1 — stamp the session with the work it was dispatched to do.
        #
        # Resolved at the TRANSPORT boundary, never from this process's
        # environment. Under HTTP MCP this code runs inside the one shared
        # daemon, whose environment carries no subagent's task id — reading it
        # here is what filed every dispatched session as "unknown work" and
        # left the lease and reap queries with nothing to match.
        #
        # pid/host come from the same identity for the same reason: the reap
        # signals the pid on this row, and os.getpid() here is the daemon.
        #
        # An interactive human session resolves no identity and keeps NULL,
        # which is correct: it was not dispatched to anything.
        _wi = resolve_work_identity()

        create_session(
            session_id=sid,
            provider=provider,
            task_hint=task_hint or "",
            project=project_slug,
            task_id=(_wi.task_id or None) if _wi else None,
            subtask_id=_wi.subtask_id if _wi else None,
            dispatch_epoch=_wi.dispatch_epoch if _wi else None,
            pid=_wi.agent_pid if _wi else None,
            host=_wi.agent_host if _wi else None,
        )
        # Bind it into session_state NOW so surfacings during the builder loop
        # below carry it. (mark_bootstrapped, called by the MCP caller after
        # assemble() returns, sets the same field — this just gets it in place
        # before the surfacings, not after.)
        try:
            from okuro.sense.session_state import get_session_state
            get_session_state()["session_id"] = sid
        except Exception:
            pass
        return sid

    return _safe("assembler.telemetry_marker", _write_session)


def _build_banner(provider: str) -> str | None:
    """Proof-of-boot greeting — CORE half only.

    Inserted right after the generated header so a human or LLM can verify in
    one sweep that the profile loaded and what was bound into this session.
    """
    from okuro.yu.profile import get_profile_raw
    profile = get_profile_raw() or {}
    identity = profile.get("identity", {}) or {}
    user_name = identity.get("name") or identity.get("handle") or None
    profession = identity.get("profession")

    # Version from the installed package (falls back to empty if absent).
    # Strip a trailing ".0" patch — "3.0.0" displays as "3.0", but
    # "3.0.1" stays full. Matches how the user refers to major versions.
    # Inner try is intentional: a missing __version__ should still let
    # the banner render with an empty version string. Recorded via
    # _safe so the failure is observable without breaking the banner.
    version = _safe(
        "assembler.banner.version",
        lambda: (
            lambda v: v.rsplit(".", 1)[0]
            if v.endswith(".0") and v.count(".") == 2
            else v
        )(getattr(__import__("okuro"), "__version__", "") or ""),
        default="",
    )

    # Authorship is DATA. config `identity.author` wins so a
    # redistributed build can name its real author; otherwise the
    # profile name stands in, and with neither the banner names
    # nobody rather than shipping one machine's owner as a literal.
    authorship = _safe(
        "assembler.banner.authorship", _authorship_from_config, default={}
    ) or {}
    author = authorship.get("author") or identity.get("name") or None
    author_contact = authorship.get("author_contact") or None

    banner = build_status_banner(
        user=user_name,
        profession=profession,
        comm_prose=_comm_prose(profile),
        provider=provider,
        version=version,
        author=author,
        author_contact=author_contact,
    )
    # Wrap the banner in a first-response directive so the agent quotes
    # it verbatim at the top of its opening reply. The banner itself
    # lives in the raw packet; Claude Code / Cursor / Codex UIs collapse
    # tool output payloads, so without this directive the user never
    # sees the banner — only the agent's synthesized summary.
    return (
        "## Bootstrap greeting (quote verbatim as the FIRST BLOCK of your "
        "first reply in this session, preserving blank lines, then continue normally)\n\n"
        f"{banner}"
    )


def _off_budget_alerts() -> list[str]:
    """Warnings that ride ABOVE the envelope: stale code, then security.

    Off-budget by design — a warning that gets compressed to nothing isn't a
    warning. Their chars are still COUNTED against the envelope (as overhead)
    so they cannot push a half over the host limit unnoticed.
    """
    alerts: list[str] = []

    # Stale-code alert — this session's okuro code and tool descriptions are
    # frozen at process start (Python imports once; MCP tool descriptions are
    # sent at handshake), so a long-lived session silently drifts from the
    # repo. The drift is invisible from inside the session it damages, which
    # is why it ran for days undetected.
    def _stale_code_warning():
        from okuro.system.code_version import (
            detect_code_drift,
            format_drift_warning,
        )
        drift = detect_code_drift()
        if drift is not None:
            return format_drift_warning(drift)
        return None

    # Security alerts — impossible to miss, but below the "quote verbatim"
    # greeting so the banner UX stays intact. _safe keeps the failure
    # observable while ensuring a check failure never breaks bootstrap.
    def _security_warning():
        from okuro.system.security import (
            detect_okuro_home_in_git_tree,
            format_git_tree_warning,
        )
        risk = detect_okuro_home_in_git_tree()
        if risk is not None:
            return format_git_tree_warning(risk)
        return None

    for label, fn in (
        ("assembler.security_alerts", _security_warning),
        ("assembler.stale_code", _stale_code_warning),
    ):
        out = _safe(label, fn)
        if out:
            alerts.append(out)
    return alerts


def _render_half(header: str, prelude: list[str], rendered: dict[str, str],
                 order, provider: str = "unknown", half: str = "half") -> str:
    """Join one half into a packet, asserting its own size.

    ``prelude`` is everything off-budget that sits between the header and the
    sections (banner, alerts, degraded block). Its chars count toward the
    envelope as overhead: a half that fits only because its warnings were not
    measured is a half that gets discarded by the host.

    The envelope is the PROVIDER's, not a global constant — see
    ``contract.envelope_for``. Two thresholds, and only one of them touches the
    packet: at the envelope, content is trimmed and the trim is announced
    in-packet; at ``warn_threshold_for`` nothing is trimmed and nothing is
    added, the pressure is logged. A warning printed into the packet would
    spend the very headroom it is warning about, and it addresses the
    maintainer, not the agent reading the briefing.
    """
    envelope = envelope_for(provider)
    overhead = len(header) + 2 + sum(len(p) + 2 for p in prelude)
    measured = overhead + sum(
        len(rendered[n]) + 2 for n in order if rendered.get(n)
    )
    rendered, overflow = enforce_envelope(
        rendered, tuple(order), envelope, overhead_chars=overhead
    )

    warn_at = warn_threshold_for(provider)
    if not overflow and measured >= warn_at:
        # The half still fits. It is the TREND that is the finding: nothing
        # observed headroom shrinking before, so the first signal was content
        # already gone. Named sizes, so the log line is actionable on its own.
        log.warning(
            "bootstrap: %s half is %d chars — past the %d-char warn line for "
            "provider %r (envelope %d). Nothing was trimmed; the halves grow "
            "with the brain and the lever is a smaller section set.",
            half, measured, warn_at, provider, envelope,
        )

    parts = [header]
    if overflow:
        # Above everything but the header: the agent must see that the packet
        # it is reading is incomplete before it reads any of it.
        parts.append(overflow)
        log.error(
            "bootstrap: %s half exceeded the %d-char envelope (provider %r) "
            "and was trimmed",
            half, envelope, provider,
        )
    parts.extend(prelude)
    for name in order:
        content = rendered.get(name)
        if content and content.strip():
            parts.append(content)
    return "\n\n".join(parts)


def _degraded_block() -> str | None:
    """Degraded-mode summary — which sections failed to build, if any."""
    failures = get_failed_sections()
    return format_degraded_block(failures) if failures else None


def assemble(budget: int | None = None,
             sections_filter: list[str] | None = None,
             task_hint: str = None, include_digest: bool = False,
             provider: str = "unknown") -> tuple[str, str | None, str | None]:
    """Assemble the CORE bootstrap half.

    THIS NO LONGER RETURNS THE WHOLE PACKET. It returns the session-invariant
    core — behavioral contract, profile, conventions, principles, protocol,
    tools, hardware, roles, session footer. Everything that varies with the
    resolved project (memory, todos, reminders, progress, people, thoughts,
    role assignment, brand/design/stack, codebase) now comes from
    :func:`assemble_project`, which the MCP layer holds the session's tools
    hostage for until it is called. See ``contract.py`` for why.

    Args:
        budget: ACCEPTED AND IGNORED. Retained so existing callers keep
            working. The core is a fixed set of sections that fits its envelope
            with ~25,000 chars to spare, so there is nothing to budget and
            nothing to rank; passing a small number no longer buys a smaller
            packet. Measured 2026-08-27: no production caller passed one.
        sections_filter: List of sections to include, or None for the whole
            core. A name outside the core is ignored here — fetch it via
            :func:`assemble_project`.
        task_hint: Optional task description. Still used to RESOLVE the project
            (the returned slug is what the project half is fetched for) and by
            ``build_recipient``; it does not change which core sections render.
        include_digest: Accepted for signature compatibility; the thoughts
            section it applied to lives in the project half.
        provider: Agent provider name (for telemetry and the banner)
    Returns:
        (markdown_packet, project_slug, session_id) — slug and session_id
        are None if resolution/creation failed. The session_id is the one
        written to the telemetry marker and persisted via `create_session`;
        callers use it to pass into `mark_bootstrapped(session_id=...)` so
        later `session_report` calls can resolve the right session without
        reading the marker (B1 fix).
    """
    # Reset the per-bootstrap failure list so consecutive `assemble()` calls
    # don't accumulate cross-run failures (each packet must reflect ONLY the
    # current run's degradation).
    clear_failed_sections()

    # Resolve project from task hint — the core carries no project CONTENT, but
    # it must still report WHICH project the second call should fetch.
    project_slug = resolve_project(task_hint) if task_hint else None

    order = [s for s in CORE_SECTIONS
             if not sections_filter
             or sections_filter == ["all"]
             or s in sections_filter]

    session_id = _mint_session(provider, task_hint, project_slug)

    rendered = _run_builders(order, task_hint, project_slug, provider)

    header = (
        "# Agent Context — Okuro · CORE\n"
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}"
    )
    if project_slug:
        header += _safe(
            "assembler.project_role_lookup",
            lambda: _project_role_line(project_slug, task_hint),
            default=f"\nProject: {project_slug}",
        )

    prelude = []
    banner = _safe("assembler.banner", lambda: _build_banner(provider))
    if banner:
        prelude.append(banner)
    prelude.extend(_off_budget_alerts())
    degraded = _degraded_block()
    if degraded:
        prelude.append(degraded)

    packet = _render_half(header, prelude, rendered, order, provider, half="core")
    _emit_size_telemetry(packet, rendered, provider, half="core")
    return packet, project_slug, session_id


def assemble_project(project_slug: str | None,
                     task_hint: str | None = None,
                     provider: str = "unknown",
                     include_digest: bool = False,
                     sections_filter: list[str] | None = None) -> tuple[str, str | None]:
    """Assemble the PROJECT half for an already-resolved slug.

    Deliberately does NOT mint a session or write a telemetry marker: this is
    the second half of ONE bootstrap, not a second bootstrap. A second marker
    with a new id would trip ``session_state._rotate_for_new_agent`` and wipe
    the compliance flags of the session that just booted.

    A None/empty slug is legitimate — the hint resolved to no project — and
    yields the project-independent sections only (recent, reminders, distill
    candidates, roles assignment for the hint). It is never an error.

    Returns:
        (markdown_packet, project_slug)
    """
    clear_failed_sections()

    order = [s for s in PROJECT_SECTIONS
             if not sections_filter
             or sections_filter == ["all"]
             or s in sections_filter]

    rendered = _run_builders(order, task_hint, project_slug, provider)

    header = (
        "# Agent Context — Okuro · PROJECT\n"
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}"
    )
    if project_slug:
        header += _safe(
            "assembler.project_role_lookup",
            lambda: _project_role_line(project_slug, task_hint),
            default=f"\nProject: {project_slug}",
        )
    else:
        header += (
            "\nProject: none — the task hint resolved to no registered project. "
            "This half carries only what does not depend on one."
        )

    prelude = []
    degraded = _degraded_block()
    if degraded:
        prelude.append(degraded)

    packet = _render_half(header, prelude, rendered, order, provider, half="project")
    _emit_size_telemetry(packet, rendered, provider, half="project")
    return packet, project_slug


def assemble_full(budget: int | None = None,
                  sections_filter: list[str] | None = None,
                  task_hint: str = None, include_digest: bool = False,
                  provider: str = "unknown") -> tuple[str, str | None, str | None]:
    """Both halves, concatenated — for IN-PROCESS consumers only.

    NOT FOR THE MCP TOOL SURFACE. The whole point of the split is that both
    halves together exceed the 50,000-char host result envelope; returning this
    from a tool re-creates the bug. It exists for callers that inject the packet
    directly into a prompt (``orchestrator/api/chat.py``), where there is no
    tool-result limit and no second call to enforce — those callers would
    otherwise SILENTLY lose the project half.

    Returns the same triple as :func:`assemble`.
    """
    core, project_slug, session_id = assemble(
        budget=budget, sections_filter=sections_filter, task_hint=task_hint,
        include_digest=include_digest, provider=provider,
    )
    project, _ = assemble_project(
        project_slug, task_hint=task_hint, provider=provider,
        include_digest=include_digest, sections_filter=sections_filter,
    )
    return f"{core}\n\n{project}", project_slug, session_id


def _project_role_line(project_slug: str, task_hint: str | None) -> str:
    from okuro.db import get_db
    db = get_db()
    row = db.fetchone(
        "SELECT roles FROM projects WHERE id = ?",
        (project_slug,),
    )
    roles_raw = row["roles"] if row else None
    roles = json.loads(roles_raw) if isinstance(roles_raw, str) else (roles_raw or [])
    role = resolve_role(task_hint, roles) if roles else None
    if role:
        return f"\nProject: {project_slug} | Role: {role}"
    return f"\nProject: {project_slug}"


def _emit_size_telemetry(packet: str, sections: dict, provider: str,
                         half: str) -> None:
    """M5 telemetry — emit bootstrap_sizes to the originating task's event log.

    Fires only when the caller is a dispatched subagent (work identity resolves
    a task + subtask). Work identity comes from the RESOLVER, never os.environ:
    the live MCP transport is HTTP to ONE shared daemon whose environment
    belongs to no subagent, so an env read here is False on every real call.
    Best-effort — telemetry failure must never block packet emission.
    """
    from okuro.sense.work_identity import resolve_work_identity as _rwi

    _wi = _rwi()
    _task_id = (_wi.task_id if _wi else "") or ""
    _subtask_id = (_wi.subtask_id if _wi else "") or ""
    if not (_task_id and _subtask_id):
        return
    try:
        from okuro.sense.task_events import append_event
        _legacy = _os.environ.get("OKURO_LEGACY_BOOTSTRAP", "").lower() in (
            "1", "true", "yes"
        )
        append_event(
            task_id=_task_id,
            subtask_id=_subtask_id,
            from_role="bootstrap",
            event_type="bootstrap_sizes",
            body={
                "half": half,
                "total_bytes": len(packet.encode("utf-8")),
                "total_chars": len(packet),
                "memory_bytes": len(sections.get("memory", "").encode("utf-8")),
                "memory_index_bytes": len(
                    sections.get("memory_index", "").encode("utf-8")
                ),
                "tools_bytes": len(sections.get("tools", "").encode("utf-8")),
                "tunnels_bytes": len(sections.get("tunnels", "").encode("utf-8")),
                "legacy_mode": _legacy,
                "provider": provider,
            },
            created_by="bootstrap.assembler",
        )
    except Exception:
        pass
