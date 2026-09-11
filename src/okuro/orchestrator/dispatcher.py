# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Okuro Orchestrator Dispatcher — dispatches subtasks to CLI tools.
# index:
#   imports
#   def _build_subagent_env
#   def dispatch_position
#   def build_position_prompt
#   def build_role_prompt
#   def build_cli_command
#   def normalize_event
#   def execute_command
#   def truncate_output
#   def _get_role_knowledge
# AGENT_HEADER_END -->
"""Okuro Orchestrator Dispatcher — dispatches subtasks to CLI tools.

A6 (partial, Step 7 scope): subagents are instructed to call
`mcp__okuro__bootstrap()` as their first action. The hard-gate middleware
(Step 2) rejects every other tool until bootstrap has run, so subagent
sessions register in the sessions table as real sessions with real
tools_used — no more synthetic telemetry, no more "Tools: none" rows.

The full migration of `subprocess.Popen` into `okuro.bridge` is deferred.
`bridge.execute` uses a blocking `subprocess.run` and would lose the
line-by-line activity-file streaming that the web UI depends on. Extending
the bridge with a streaming mode is tracked as a follow-up (A6 full).
"""

import json as _json
import os
import re
import signal as _signal
import subprocess
import logging
import time
from datetime import datetime
from okuro.clock import utc_now_naive
from pathlib import Path
from typing import Any, Dict, Optional

from okuro.orchestrator.config import Config, resolve_role, get_cli_command_parts
from okuro.orchestrator.state import Subtask, Task, DAGNode, DAGGraph
from okuro.orchestrator.tools.injector import build_tools_section
from okuro.roles.skills import (
    list_role_metadata,
    pick_role_slice,
    render_metadata_block,
)
from okuro.sense.providers.template import _build_bootstrap_forms_block

logger = logging.getLogger("okuro.orchestrator.dispatcher")


# Deliverable extensions auto-rescued if found on /tmp paths in subagent
# output. Limited to user-facing output formats; binaries that a subagent
# might legitimately stage in /tmp during processing (intermediate
# tarballs, downloaded model weights, etc.) are intentionally excluded.
_DELIVERABLE_EXTS = (
    "html", "htm", "md", "markdown", "txt", "json", "csv", "yaml", "yml",
    "pdf", "svg", "png", "jpg", "jpeg", "webp", "gif",
    "mp3", "wav", "ogg", "m4a",
    "mp4", "webm", "mov",
    "pptx", "key",
)

_TMP_PATH_RE = re.compile(
    r"(?P<path>/tmp/[A-Za-z0-9_./-]+\.(?:" + "|".join(_DELIVERABLE_EXTS) + r"))",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Per-role wall-clock budget (P1-2).
#
# qa-engineer, security-auditor, reviewer-class roles routinely sweep large
# deliverable surfaces and timed out under the previous flat 600 s default
# (audio task: qa-engineer SIGKILL at 600 s while validating AC1–AC10).
# Roles not listed inherit the legacy ladder (600 / 900 / 1200) keyed on
# retries. Add a role to _PER_ROLE_TIMEOUT_BASE when its evidence-driven
# typical runtime > 600 s; bump on a per-incident basis, not pre-emptively.
#
# Retry ladder still applies on top of the base: each retry adds 300 s, capped
# at +600 s, so a 1800 s qa-engineer becomes 2100 s / 2400 s on retries 1 / 2.
# ---------------------------------------------------------------------------

_PER_ROLE_TIMEOUT_BASE: dict[str, int] = {
    "qa-engineer": 1800,
    "security-auditor": 1200,
    "reviewer": 1200,
    "workforce-reviewer": 1200,
    "license-auditor": 1200,
    # task-20260527-233731 subtask 1.1 timed out twice at 600 s while
    # running real audio benchmarking (`jack_iodelay`, `dpkg -l`,
    # `pw-metadata` probes + reasoning). Bump to 1200 s base so retry 1
    # gets 1500 s and retry 2 gets 1800 s.
    "linux-audio-engineer": 1200,
    "audio-production-researcher": 1200,
}
_DEFAULT_TIMEOUT_BASE: int = 600
_RETRY_TIMEOUT_BUMP: int = 300
_RETRY_TIMEOUT_BUMP_CAP: int = 600


_TIMEOUT_OVERRIDE_SIDECAR = ".timeout-overrides.json"


def _read_timeout_override(task_dir: "Optional[Path]", subtask_id: str) -> Optional[int]:
    """Return a per-subtask timeout override (seconds) when one is staged
    in ``<task_dir>/.timeout-overrides.json``, else None.

    The sidecar is owned by the ``timeout_cap`` resolve endpoint:
    ``extend_and_retry`` writes ``{<subtask_id>: <seconds>}`` so the next
    dispatch picks a longer wall-clock without touching the closed
    ``task.yaml`` schema. The override is one-shot — callers (engine
    pre-dispatch / dispatcher post-dispatch) are responsible for
    deleting the entry once consumed; absence is the steady state.

    Best-effort: a missing / unreadable / malformed sidecar returns None
    silently so the legacy ladder remains the fallback.
    """
    if task_dir is None:
        return None
    try:
        path = task_dir / _TIMEOUT_OVERRIDE_SIDECAR
        if not path.exists():
            return None
        data = _json.loads(path.read_text(encoding="utf-8") or "{}")
        if not isinstance(data, dict):
            return None
        raw = data.get(subtask_id)
        if raw is None:
            return None
        val = int(raw)
        if val <= 0:
            return None
        return val
    except Exception:
        return None


def _consume_timeout_override(task_dir: "Optional[Path]", subtask_id: str) -> None:
    """One-shot drop of a sidecar override after it has informed a dispatch.

    Best-effort: a missing file / missing key / IO error is a no-op. The
    sidecar's whole point is to be ephemeral — the only correctness
    requirement is that ``_read_timeout_override`` does not return the
    same value twice for one user decision.
    """
    if task_dir is None or not subtask_id:
        return
    try:
        path = task_dir / _TIMEOUT_OVERRIDE_SIDECAR
        if not path.exists():
            return
        data = _json.loads(path.read_text(encoding="utf-8") or "{}")
        if not isinstance(data, dict) or subtask_id not in data:
            return
        del data[subtask_id]
        if data:
            path.write_text(_json.dumps(data), encoding="utf-8")
        else:
            try:
                path.unlink()
            except OSError:
                path.write_text("{}", encoding="utf-8")
    except Exception:
        # Sidecar maintenance must never break a dispatch.
        return


def _wall_clock_seconds(role: str, retries: int, *,
                        subtask_id: str = "",
                        task_dir: "Optional[Path]" = None) -> int:
    """Resolve the subprocess wall-clock for a subtask.

    Order of precedence:
      1. Per-subtask sidecar override (``.timeout-overrides.json``) when
         ``task_dir`` + ``subtask_id`` are supplied AND the file holds an
         entry for this subtask. One-shot; owned by the ``timeout_cap``
         resolve flow.
      2. Per-role base from _PER_ROLE_TIMEOUT_BASE.
      3. _DEFAULT_TIMEOUT_BASE.

    The retry bump (capped at _RETRY_TIMEOUT_BUMP_CAP) applies to (2)+(3)
    only; an explicit override is the literal wall-clock the user asked
    for. Idempotent and side-effect-free; callers do not need to
    special-case retries == 0.
    """
    if subtask_id and task_dir is not None:
        override = _read_timeout_override(task_dir, subtask_id)
        if override:
            return override
    base = _PER_ROLE_TIMEOUT_BASE.get(role or "", _DEFAULT_TIMEOUT_BASE)
    bump = min(max(int(retries or 0), 0) * _RETRY_TIMEOUT_BUMP, _RETRY_TIMEOUT_BUMP_CAP)
    return base + bump


def _rescue_tmp_deliverables(output: str, artifacts_dir: Path, *,
                             subtask_id: str) -> list[Path]:
    """Find /tmp/<filename>.<ext> paths in subagent output and copy each
    existing file into ``artifacts_dir``. Returns the list of rescued
    paths. Silently swallows IO errors — this is a safety net, not a
    correctness gate, and must not impact subagent result reporting.

    The convention (memory hash hint 068e80db) is that deliverables
    NEVER land in /tmp. This is the systemic guard for when a subagent
    ignores the role prompt's hard rule (see build_role_prompt).
    """
    if not output:
        return []
    import shutil
    seen: set[str] = set()
    rescued: list[Path] = []
    try:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("[%s] cannot create artifacts dir %s: %s",
                       subtask_id, artifacts_dir, exc)
        return []
    for m in _TMP_PATH_RE.finditer(output):
        src_str = m.group("path")
        if src_str in seen:
            continue
        seen.add(src_str)
        src = Path(src_str)
        if not src.is_file():
            continue
        # Skip files that have nothing to do with this run — heuristic:
        # rescue only if the file is newer than 24h to avoid copying
        # unrelated stale /tmp files referenced in passing.
        try:
            age_seconds = time.time() - src.stat().st_mtime
        except OSError:
            continue
        if age_seconds > 86_400:
            continue
        # Prefix with subtask_id so artifacts from different subtasks
        # don't collide in the flat artifacts/ folder.
        dest_name = f"{subtask_id}-{src.name}"
        dest = artifacts_dir / dest_name
        try:
            shutil.copy2(src, dest)
            rescued.append(dest)
            logger.warning(
                "[%s] rescued /tmp deliverable %s → %s (subagent ignored "
                "task-artifacts rule)", subtask_id, src, dest,
            )
        except OSError as exc:
            logger.warning("[%s] could not rescue %s: %s",
                           subtask_id, src, exc)
    return rescued


def _build_subagent_env(
    role: str,
    session_id: Optional[str],
    config: Config,
    *,
    task_id: str = "",
    subtask_id: str = "",
    dispatch_epoch: str = "",
) -> dict:
    """Build extra env vars for a subagent subprocess.

    Only telemetry tags. The previous scoped HOME override was a
    credentials-breaking hack that stripped the CLI credentials file,
    causing every subtask to fail with "Not logged in". Subagents now use
    the user's normal HOME so CLI auth, MCP config, and keyring all just
    work — which they must, because the subagent MUST call okuro bootstrap
    as its first action (the hard-gate middleware rejects any other tool
    before that).

    M4 — task_id + subtask_id propagate so the subagent's MCP server child
    can route role_body_fetched telemetry back to the originating task's
    event log. Each subagent spawns its own MCP child, so env vars are
    process-scoped and don't bleed across siblings.
    """
    extra_env: dict = {}
    if config.sense.enabled and config.sense.telemetry_tags:
        extra_env["OKURO_PROVIDER"] = f"orch-{role}"
        extra_env["OKURO_SESSION_TYPE"] = "subagent"
        if session_id:
            extra_env["OKURO_SESSION_ID"] = session_id
        if task_id:
            extra_env["OKURO_TASK_ID"] = task_id
        if subtask_id:
            extra_env["OKURO_SUBTASK_ID"] = subtask_id
        # P4.1 — the dispatch generation, so the subagent's own session row
        # records WHICH run it belongs to. Same ISO value artifact_write and
        # write_role_handover already carry (C12), not a parallel counter.
        if dispatch_epoch:
            extra_env["OKURO_DISPATCH_EPOCH"] = dispatch_epoch
        # M5+ assigned-role hard gate (mcp_middleware) — rejects every
        # tool call after bootstrap until roles_get(<role>) lifts the
        # gate. Closes the silent lazy-load bypass surfaced by the
        # tm-token-cost end-to-end test.
        if role:
            extra_env["OKURO_SUBTASK_ROLE"] = role
    # Unattended-auth: headless `claude` spawned by recurring crons (e.g.
    # role-refresh at 07:00, before any interactive session) cannot reach the
    # OAuth credentials file once its ~8h access token has expired overnight —
    # it 401s ("Invalid authentication credentials") and parks the task at
    # waiting_user. A long-lived CLAUDE_CODE_OAUTH_TOKEN (claude setup-token,
    # ~1yr, subscription-billed, no rotation) removes the expiry/rotation race
    # for ALL unattended subagent spawns. Injected via extra_env so it survives
    # the CLAUDE_*/ANTHROPIC_* env strip applied before the subprocess launch.
    if "CLAUDE_CODE_OAUTH_TOKEN" not in extra_env:
        try:
            from okuro.keyring import KeyringStorage

            tok = KeyringStorage().get_key("CLAUDE_CODE_OAUTH_TOKEN")
            if tok:
                extra_env["CLAUDE_CODE_OAUTH_TOKEN"] = tok
            else:
                logger.warning(
                    "CLAUDE_CODE_OAUTH_TOKEN not in keyring — unattended subagents "
                    "fall back to the OAuth credentials file (may 401 after token expiry)"
                )
        except Exception as e:  # keyring locked/unavailable — non-fatal
            logger.warning("keyring read for CLAUDE_CODE_OAUTH_TOKEN failed: %s", e)
    return extra_env


def dispatch_position(node: DAGNode, task: Task, config: Config,
                      session_id: str = None) -> Dict:
    """Dispatch a position node for deliberation."""
    result = {
        "success": False, "output": "", "duration": 0.0,
        "cli": "", "model": "", "error": "",
    }

    try:
        role_content = resolve_role(node.role, config)
    except (ValueError, FileNotFoundError) as e:
        result["error"] = f"Role resolution failed: {e}"
        logger.error(f"[{task.id}/{node.id}] {result['error']}")
        return result

    prompt = build_position_prompt(role_content, node, task, config, session_id=session_id)

    cli_name = config.cli_default
    # The node's own complexity IS its tier; intelligence=max only upgrades.
    from okuro.orchestrator.config import resolve_unit_tier

    tier = resolve_unit_tier(node, task)

    try:
        binary, flags, model = get_cli_command_parts(cli_name, tier, config)
        model_flag = config.cli_tools[cli_name].model_flag
    except Exception as e:
        result["error"] = f"Failed to get CLI command parts: {e}"
        return result

    result["cli"] = cli_name
    result["model"] = model

    cmd = build_cli_command(binary, flags, model_flag, model, prompt)
    cwd = config.tasks_dir / task.id
    timeout = config.execution.subtask_timeout

    extra_env = _build_subagent_env(
        node.role, session_id, config,
        task_id=task.id, subtask_id=node.id,
        # Taken at spawn, per dispatch. The previous commit added the
        # parameter and no caller passed it — which made OKURO_DISPATCH_EPOCH
        # unset in production and the guard downstream of it inert, the exact
        # failure class that commit documented.
        dispatch_epoch=utc_now_naive().isoformat(),
    )

    exec_result = execute_command(
        cmd, cwd, timeout,
        extra_env=extra_env,
        subtask_id=node.id,
        role=node.role,
        provider=cli_name,
    )
    result.update(exec_result)

    if result["output"]:
        result["output"] = truncate_output(result["output"], max_lines=500)

    return result


def _build_user_format_block() -> str:
    """Render the user's communication preferences as a prompt block.

    Subagent artifacts are user-facing — they land in the task-detail
    ArtifactsViewer in the web UI. They MUST follow the user's format
    preferences (lead with answer, tables/bullets, no preamble, etc.).
    Bootstrap loads the full profile, but role-prompt overrides like
    "use EXACTLY this structure" silently clobber it. This helper
    re-asserts the rules right next to the artifact-write directive
    so the model has them in active attention when rendering.

    Returns an empty string when profile is unavailable so callers can
    no-op gracefully on fresh installs.
    """
    try:
        from okuro.yu.profile import get_profile_raw

        profile = get_profile_raw() or {}
    except Exception:
        return ""

    if not isinstance(profile, dict) or not profile:
        return ""

    comm = profile.get("communication") if isinstance(profile.get("communication"), dict) else {}
    fmt = comm.get("format_preferences") if isinstance(comm.get("format_preferences"), dict) else {}

    def _texts(items, limit: int = 4) -> list[str]:
        if not isinstance(items, list):
            return []
        out: list[str] = []
        for item in items:
            if isinstance(item, str):
                t = item.strip()
            elif isinstance(item, dict):
                t = str(item.get("rule", "")).strip()
            else:
                t = ""
            if t and t not in out:
                out.append(t)
            if len(out) >= limit:
                break
        return out

    do_lines: list[str] = []
    do_lines.extend(_texts(comm.get("patterns")))
    do_lines.extend(t for t in _texts(fmt.get("preferred")) if t not in do_lines)

    avoid_lines: list[str] = []
    avoid_lines.extend(_texts(fmt.get("avoid")))
    avoid_lines.extend(t for t in _texts(comm.get("pet_peeves")) if t not in avoid_lines)

    if not do_lines and not avoid_lines:
        return ""

    # Form-compressed (M5 recovery): the RULES are re-asserted verbatim — that
    # is this helper's whole reason to exist — but as two inline runs instead
    # of a bullet per rule. Bootstrap's Behavioral Contract (Tier-1, never
    # truncated) already carries the same list in full; what is NOT anywhere
    # else, and is the load-bearing part, is the override sentence below.
    # Bulleting ~12 short phrases cost ~2.7x the bytes of joining them and
    # added no information.
    block = ["## User format preferences (the artifact lands in the user's UI)\n"]
    if do_lines:
        block.append(f"**Do:** {'; '.join(do_lines)}")
    if avoid_lines:
        block.append(f"**Avoid:** {'; '.join(avoid_lines)}")
    block.append(
        "\nThese override any prescriptive section template elsewhere in this "
        "prompt — headings are a scaffold, not a script.\n"
    )
    return "\n".join(block)


def _build_stack_block(task: Task) -> str:
    """Render the project's active stack profile as a prompt block.

    Pulls from okuro.stack.registry.active_profile_for(slug) using the
    project slug looked up from task.project_path. Without this, briefs
    are stack-blind: the subagent has to guess which language, framework,
    and package manager to use from filename hints alone, which works
    for trivial cases but fails for ambiguous or multi-stack repos.

    Returns an empty string when no project_path is set, no project row
    matches the path, no profile is bound to the project, or any lookup
    raises — the brief continues without the block (graceful degrade).
    """
    project_path = (getattr(task, "project_path", "") or "").strip()
    if not project_path:
        return ""
    try:
        from okuro.db import get_db
        row = get_db().fetchone(
            "SELECT id FROM projects WHERE path = ? AND active = 1",
            (project_path,),
        )
        if not row:
            return ""
        slug = row["id"]
        from okuro.stack.registry import active_profile_for
        profile = active_profile_for(slug)
    except Exception:
        return ""
    if not profile or not isinstance(profile, dict):
        return ""

    by_category = profile.get("by_category") or {}
    if not by_category:
        return ""

    label = profile.get("label") or profile.get("name") or ""
    lines: list[str] = ["## Project Stack (pinned for every subagent)\n"]
    if label:
        lines.append(f"**Profile:** {label}\n")
    # One line per category — keeps the block compact even on rich profiles.
    for category in sorted(by_category.keys()):
        entries = by_category[category] or []
        if not entries:
            continue
        rendered: list[str] = []
        for e in entries:
            name = e.get("name") or e.get("id") or ""
            if not name:
                continue
            version = e.get("version") or ""
            rendered.append(f"{name} {version}".strip())
        if rendered:
            lines.append(f"- **{category.replace('_', ' ').title()}:** {', '.join(rendered)}")

    lines.append(
        "\nUse this stack — do not introduce alternatives without a "
        "decision in `brief.decisions`. New deps must match the package "
        "manager above; tests must run via the test runner above.\n"
    )
    return "\n".join(lines)


def build_position_prompt(role_content: str, node: DAGNode, task: Task,
                          config: Config, session_id: str = None) -> str:
    """Build prompt for a position node."""
    parts = []

    parts.append("## Role Identity\n")
    parts.append(role_content)
    parts.append("")

    # Umbrella audit fix #4 — pre-fix, build_position_prompt skipped the
    # bootstrap directive, tool-protocol pointer, project_path, and
    # close-out tools. Deliberation positions bypassed ORCH-BRIEF-COMPLETE
    # entirely. Port the minimum-viable brief skeleton (positions are
    # lightweight, so we skip the full Stream A handover — positions are
    # not consumed by downstream subagents in the role-handover sense).
    parts.append("## Tool Protocol\n")
    parts.append(
        "Bootstrap (below) loads the canonical tool protocol into your "
        "session. For the durable reference, `read ~/.okuro/TOOL-PROTOCOL.md`."
    )
    parts.append("")

    forms_block = _build_bootstrap_forms_block()
    provider_tag = f"orch-pos-{node.role}"
    parts.append("## FIRST: Call the okuro bootstrap tool\n")
    parts.append(
        "Your very first tool call MUST be the okuro MCP server's "
        "`bootstrap` tool. Find it in your available tool list — its "
        "exact name varies by client. Common forms (try in order):\n"
    )
    parts.append(forms_block)
    parts.append("")
    parts.append(
        f"Call it with `task_hint='deliberation position: {node.role}'` and "
        f"`provider='{provider_tag}'`."
    )
    parts.append(
        "This registers your session, loads profile/memory/tool protocol, "
        "and enables telemetry. Before ending, call `session_report()` "
        "with tool feedback — non-negotiable.\n"
    )

    parts.append("## Your Assignment: Present a Position\n")
    # A continuation council carries the follow-up instructions on the node's
    # `description` (set by create_position_nodes(directive=...)). When present
    # the panel must deliberate THAT new work, with the original task as
    # background context — not re-litigate the completed original task.
    round_directive = (node.description or "").strip()
    if round_directive:
        parts.append(f"**New instructions under deliberation (this round):**\n{round_directive}\n")
        parts.append(f"**Original task (background context):** {task.description}")
    else:
        parts.append(f"**Task under deliberation:** {task.description}")
    parts.append(f"**Your role:** {node.role}")
    parts.append(f"**Position node:** {node.id} (round {node.round})\n")
    # Surface project context — positions reason about a target repo even
    # though they don't touch its files. Without this the panellist must
    # guess what codebase the task is about.
    task_project_path = getattr(task, "project_path", None)
    if task_project_path:
        parts.append(
            f"**Target project (read-only context):** `{task_project_path}`\n"
        )
    parts.append("You are part of a deliberation panel. Present your domain-specific perspective.\n")

    fmt_block = _build_user_format_block()
    if fmt_block:
        parts.append(fmt_block)

    parts.append("## Suggested Section Scaffold\n")
    parts.append(
        "Render the following sections, but write the CONTENT under each "
        "in the user's preferred shape (see User format preferences above). "
        "Section names are a scaffold for the deliberation, not a style override.\n"
    )
    parts.append("```markdown")
    parts.append(f'# Position: {{your role}} on "{task.description[:100]}"')
    parts.append("\n## CLAIM\nOne sentence recommendation.\n")
    parts.append("## REASONING\nDomain-specific evidence and logic.\n")
    parts.append("## RISKS\nWhat goes wrong if ignored.\n")
    parts.append("## CONSTRAINTS\nNon-negotiable requirements.\n")
    parts.append("## RECOMMENDATION\nConcrete next steps.\n```\n")

    if node.round > 1 and node.context_from:
        parts.append("## Previous Round Context\n")
        parts.append(f"This is round {node.round}. Previous discussion: node {node.context_from}")
        parts.append("Respond to what changed.\n")
        if task.graph:
            from okuro.orchestrator.deliberation import build_execution_brief
            prev_brief = build_execution_brief(task.graph, node.context_from)
            if prev_brief:
                parts.append(prev_brief)
                parts.append("")

    task_dir = config.tasks_dir / task.id

    parts.append("## Working Instructions\n")
    parts.append(f"- **Working directory:** `{task_dir}`")
    parts.append("- Be specific to YOUR domain. Be opinionated.")
    parts.append("- Keep it under 500 words.\n")

    parts.append("## HARD CONSTRAINT — Position-only, no deliverables\n")
    parts.append(
        "You are in DELIBERATION mode, not EXECUTION mode. Your job is to "
        "present a position — claim, reasoning, risks, recommendation — "
        "not to produce the artifact the task ultimately needs.\n"
    )
    parts.append(
        "**Do NOT:**\n"
        "- write files into the target project (no Write/Edit on project paths)\n"
        "- write HTML/CSS/JS/code/configs/diagrams as deliverables\n"
        "- run build/install/format/test commands that change project state\n"
        "- create branches, commit, or push\n"
        "- spawn subagents or follow-on tasks\n"
    )
    parts.append(
        "**Do:**\n"
        "- read project files for context (Read / cortex_search are fine)\n"
        "- write your position via the single `artifact_write` call below\n"
        "- cite specific files/lines/evidence inside the position body\n"
    )
    parts.append(
        "If your role's purpose is normally to BUILD (writer, deck-author, "
        "implementer), translate that purpose into a POSITION about HOW the "
        "build should happen — approach, structure, risks, trade-offs. The "
        "actual build runs in a later execution subtask after the panel "
        "resolves.\n"
    )

    # P0-6 — positions land as okuro brain artifacts (Stream B), not bare
    # `.md` files. The engine no longer auto-mirrors position output to
    # disk (it only does so when no brain row exists as a fallback), so
    # the subagent MUST call artifact_write for the deliberation panel
    # to surface the position in the UI.
    parts.append("## MANDATORY — Stream B (okuro brain artifact)\n")
    parts.append(
        "Call `artifact_write(...)` exactly once with your position. "
        "This is the canonical landing site — the deliberation panel "
        "reads brain artifacts, not disk files. Do NOT write a separate "
        "`.md` to artifacts/; the engine handles disk fallback when the "
        "brain write is missing.\n"
    )
    parts.append("```")
    parts.append("artifact_write(")
    parts.append('  kind="report",')
    parts.append(f'  task_id="{task.id}",')
    parts.append(f'  subtask_id="{node.id}",')
    parts.append(f'  title="Position: {node.role} on {task.description[:60]}",')
    parts.append('  summary="<one-line claim, indexed for semantic search>",')
    parts.append('  body="<full markdown body — CLAIM / REASONING / RISKS / CONSTRAINTS / RECOMMENDATION>",')
    parts.append('  media_type="text/markdown",')
    parts.append(")")
    parts.append("```")
    parts.append("")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Gate 2 §C11 — decomposer ↔ reviewer evidentiary contract.
#
# These helpers render the C11 invariants (evidentiary spec + gap-event
# escape hatch + structured retry context) into prompt text. They are
# called from `build_role_prompt` for the full subagent brief AND from
# the lighter `compose_brief()` public entry point that the C11
# correctness tests target.
# ---------------------------------------------------------------------------


def _render_evidentiary_contract(acceptance_criteria: list[str] | None) -> str:
    """Return the M3 reviewer evidentiary contract block.

    Always rendered — Gate 2 §C11 / G3 — regardless of whether the planner
    declared ACs. Without this block subagents ship work-product but omit
    the captured stdout the Critic prompt explicitly demands per AC, and
    the same FAIL fires every retry. See
    `docs/audit-2026-05-26/03-m3-review-loop.md` §1 + §8 (fix #1) and
    `docs/audit-2026-05-27/06-c11-dispatcher-walk.md` for the loops this
    block closes.

    Keywords the C11 correctness tests assert presence of:
    ``evidence``, ``stdout``, ``exit``, ``captured``, ``emit_task_event``,
    ``gap``, ``affects``.
    """
    has_acs = bool(acceptance_criteria)
    ac_clause = (
        "For EACH acceptance criterion above, your `artifact_write` body MUST "
        "contain a fenced block with the captured stdout / exit-code that "
        "proves the criterion is satisfied. "
        if has_acs
        else "Even without enumerated ACs, your `artifact_write` body MUST "
             "contain the captured stdout / exit-code for every runtime "
             "check you perform — the reviewer scans bodies for evidence. "
    )
    # Form-compressed (M5 recovery). Every rule below was in the previous
    # prose version; nothing was dropped. What went: the second identical
    # AC block in the shape example, and four restatements of "the reviewer
    # reads artifact bodies" that each re-earned their bytes at ~0 marginal
    # information. Keywords the C11 correctness tests assert are all retained.
    return (
        "**M3 reviewer evidentiary contract — read carefully:**\n"
        "The M3 pipeline (deterministic → critic → scorer) judges you on your "
        "okuro-brain artifact bodies — that is the ONLY evidence surface. "
        + ac_clause +
        "Files on disk, side-files in `artifacts/`, and `emit_task_event` "
        "bodies are INVISIBLE to the Critic unless quoted inside an artifact "
        "body.\n\n"
        "Write evidence to a SEPARATE `audience=\"agent\"` artifact with the "
        "SAME `subtask_id` as your user report (the reviewer concatenates "
        "every artifact across all audiences, so this satisfies the check "
        "while the user report stays clean):\n"
        "```\n"
        "artifact_write(\n"
        "    kind=\"report\", audience=\"agent\",\n"
        "    task_id=<this task>, subtask_id=<this subtask>,\n"
        "    title=\"<subtask> — AC evidence\",\n"
        "    summary=\"machine-verifiable acceptance-criteria evidence\",\n"
        "    body=<AC blocks, one per criterion, shaped as below>,\n"
        ")\n"
        "```\n"
        "Required shape per criterion — captured stdout, never a prose "
        "claim:\n"
        "```\n"
        "## AC1 — <criterion text>\n"
        "```bash\n"
        "$ <command that proves it>\n"
        "<captured stdout, capped 500 chars>\n"
        "<exit code>\n"
        "```\n"
        "```\n\n"
        "| Rule | Detail |\n"
        "|---|---|\n"
        "| Pre-check anchor | `## AC<n>` (or `### AC#<n>`) followed within "
        "~800 chars by a ```bash``` fence holding a `$ <cmd>` prompt OR two "
        "non-blank lines |\n"
        "| Unproven claim | FAILs as severity `infra_error` BEFORE the LLM "
        "critic runs |\n"
        "| `audience=\"user\"` report | plain-language outcomes only (\"✓ All "
        "8 components have a live order link\"); NEVER the literal `AC<n>` "
        "token in a heading, NEVER a bash/stdout block |\n"
        "| Why that matters | the pre-check anchors on the FIRST `## AC<n>` "
        "anywhere in the concatenated bodies — a bare `## AC1` in the user "
        "report with no fence under it FAILs the whole subtask |\n\n"
        "**Blocked by absent hardware, an unmade user decision, or a pending "
        "external dependency?** Emit this BEFORE `artifact_write` — "
        "gap-tracked ACs are 'partial-by-design'; without it the reviewer "
        "FAILs on the missing evidence:\n"
        "```python\n"
        "emit_task_event(\n"
        "    event_type='gap',\n"
        "    body={'summary': '<what is blocked>', 'affects': ['AC#1', 'AC#2']},\n"
        ")\n"
        "```"
    )


def _extract_ac_numbers_from_findings(
    findings: list[dict], error_text: str,
) -> list[int]:
    """Scan finding summaries + the prior error for AC# references.

    Picks up patterns like ``AC1``, ``AC #2``, ``AC#3``. Empty list when
    no AC# tags are found — callers should treat that as "all ACs
    implicated" rather than "none implicated".
    """
    nums: list[int] = []
    text_parts: list[str] = [error_text or ""]
    for f in findings or []:
        text_parts.append((f or {}).get("summary") or "")
        text_parts.append((f or {}).get("suggested_fix") or "")
        text_parts.append((f or {}).get("evidence") or "")
    blob = "\n".join(text_parts)
    for m in re.finditer(r"AC\s*#?\s*(\d+)", blob, re.IGNORECASE):
        try:
            nums.append(int(m.group(1)))
        except (TypeError, ValueError):
            continue
    seen: set[int] = set()
    dedup: list[int] = []
    for n in nums:
        if n not in seen:
            seen.add(n)
            dedup.append(n)
    return dedup


def _render_retry_context(
    subtask: Any,
    retry_context: dict | None,
    acceptance_criteria: list[str] | None,
) -> str:
    """Render the retry-only block: prior verdict summary + structured AC
    list + reinforcement of the gap-event escape hatch.

    Gate 2 §C11 / G2 — every retry brief explicitly names the gap-event
    escape hatch (``emit_task_event`` + ``gap`` + ``affects``) so the
    exemption logic at critic.py:73 is actionable, not buried 200 lines
    down in the M2 events block.

    Gate 2 §C11 / G7 — structures which ACs failed by parsing AC#
    references from the prior verdict / findings.
    """
    if not retry_context:
        return ""

    parts: list[str] = []
    parts.append("## RECOVERY — Previous Attempt Failed\n")
    rt = retry_context.get("retries")
    mx = retry_context.get("max_retries")
    if rt is not None or mx is not None:
        parts.append(f"This is retry #{rt if rt is not None else '?'} "
                     f"of {mx if mx is not None else '?'}.")
        parts.append("")

    findings = list(retry_context.get("findings") or [])
    implicated = list(retry_context.get("implicated_ac_numbers") or [])
    if not implicated:
        implicated = _extract_ac_numbers_from_findings(
            findings, getattr(subtask, "error", "") or "",
        )

    if acceptance_criteria:
        parts.append("### Previous FAIL — acceptance criteria status\n")
        for i, ac in enumerate(acceptance_criteria, 1):
            mark = "FAILED" if (not implicated or i in implicated) else "ok"
            parts.append(f"- [AC#{i}] [{mark}] {ac}")
        parts.append("")

    # The specific reviewer findings ALWAYS render (independent of the AC
    # block) — load-bearing first, with locus + the reviewer's suggested fix.
    # This is the surgical worklist: the agent must resolve EACH item by
    # patching the named artifact in place (artifact_write supersedes=...),
    # not regenerate. Without these explicit items the agent only saw
    # "AC failed" and could not target the actual defects (the root cause of
    # the same findings recurring on retry).
    if findings:
        # ROCK-SOLID v5 P6.5 — the redo worklist, split by what actually
        # decides the verdict.
        #
        # Measured on the run the latency dossier was built from
        # (task-20260730-233206, 38 review rounds, 171 findings parsed from
        # .activity.jsonl): 67% of findings were cosmetic, and across the
        # 14-day store only 4 FAILs in 164 came from cosmetic findings alone
        # (2.4%). So two thirds of the worklist was redo effort spent on
        # items that were never going to change the outcome.
        #
        # Pre-fix this rendered ONE flat list, load-bearing first, sliced at
        # 15 — which also meant a round with more than 15 load-bearing
        # findings silently dropped the overflow. That has a name in this
        # run's data: 14% of load-bearing findings were re-raises carrying
        # "STILL-OPEN". A finding dropped from the worklist comes straight
        # back as another round. This run peaked at 11, so the trap had not
        # bitten yet; the budget is now per-severity so it cannot.
        def _line(f: dict) -> list[str]:
            loc = f"{f.get('file', '?')}:{f.get('line', '?')}"
            out = [f"- {loc} — {str(f.get('summary', ''))[:260]}"]
            fix = f.get("suggested_fix")
            if fix:
                out.append(f"    → fix: {str(fix)[:260]}")
            return out

        load_bearing = [f or {} for f in findings
                        if (f or {}).get("severity") == "load_bearing"]
        cosmetic = [f or {} for f in findings
                    if (f or {}).get("severity") != "load_bearing"]

        if load_bearing:
            parts.append(
                "### MUST FIX — these decide the verdict\n"
                "Patch the named artifact in place and re-submit via "
                "`artifact_write(..., supersedes=<prior_id>)`. Do not "
                "regenerate from scratch; resolve each item below, then call "
                "`await_review`."
            )
            # No cap. A load-bearing finding you are not told about is a
            # finding you cannot fix, and it returns next round at the cost
            # of a full review cycle — far more expensive than the prompt
            # tokens this saves.
            for f in load_bearing:
                parts.extend(_line(f))
            parts.append("")

        if cosmetic:
            parts.append(
                "### Optional — improvements, NOT blockers\n"
                "These did not cause the FAIL and will not cause one. Fix "
                "them only where the change is local to work you are already "
                "touching above."
            )
            for f in cosmetic[:10]:
                parts.extend(_line(f))
            if len(cosmetic) > 10:
                # Stated rather than silently truncated: a list that stops
                # without saying so reads as the complete set.
                parts.append(f"- (+{len(cosmetic) - 10} more cosmetic findings "
                             "omitted — none of them blocks this subtask)")
            parts.append("")

        # The plan's explicit clause. "Do not regenerate from scratch" above
        # says what not to do to the FAILING artifact; this says what not to
        # touch at all, which is the larger waste on a multi-artifact subtask.
        parts.append(
            "### Do NOT redo unaffected work\n"
            "Anything not named above passed review. Leave it exactly as it "
            "is — do not re-run its research, do not rewrite it for "
            "consistency, do not re-verify it. Re-submitting unchanged work "
            "costs another full review round and resolves nothing."
        )
        parts.append("")

    parts.append(
        "### Retry protocol — evidentiary contract + gap-event escape\n"
        "The reviewer judges your `artifact_write` body content, NOT "
        "files under `/etc/`, NOT side-files in `artifacts/`, NOT prose "
        "claims. Every acceptance criterion needs a fenced `bash` block "
        "with the actual command + captured stdout inside your "
        "`artifact_write` body.\n\n"
        "If an AC is hardware-blocked, user-decision-blocked, or "
        "depends on an absent external dependency, emit BEFORE "
        "artifact_write:\n"
        "```python\n"
        "emit_task_event(\n"
        "    event_type='gap',\n"
        "    body={'summary': '<what is blocked>', 'affects': ['AC#1', 'AC#2']},\n"
        ")\n"
        "```\n"
        "The gap-event protocol exempts the named ACs from load-bearing "
        "scoring at the next reviewer pass (critic.py:73). Without it "
        "the reviewer FAILs on the missing evidence again.\n"
    )

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# WP8 — no-artifact corrective. A subagent that does the literal work but
# never calls ``artifact_write`` produces NO Stream-B deliverable; the
# reviewer/finalizer has nothing to judge → FAIL → retry. On the cold
# re-dispatch the generic content-retry framing does not name the gap, so
# the same LLM skips ``artifact_write`` again. This corrective is distinct
# from a content FAIL: a content FAIL says "fix X in your artifact"; a
# no-artifact FAIL says "you produced NOTHING reviewable — call
# artifact_write before anything else".
# ---------------------------------------------------------------------------

# Sentinel substrings that the no-artifact failure paths stamp into the
# subtask error. Keep in sync with:
#   dispatcher_streaming.py — "subagent never wrote an artifact"
#   engine.py _finalize Check 1 — "produced no artifact (no Stream B"
_NO_ARTIFACT_ERROR_MARKERS = (
    "never wrote an artifact",
    "produced no artifact",
    "no stream b",
)


def is_no_artifact_error(error_text: str | None) -> bool:
    """True when ``error_text`` indicates the prior attempt produced no
    Stream-B artifact (as opposed to a content FAIL on an artifact that
    DID exist). Case-insensitive substring match against the canonical
    no-artifact failure markers stamped by the dispatcher/engine."""
    if not error_text:
        return False
    low = error_text.lower()
    return any(m in low for m in _NO_ARTIFACT_ERROR_MARKERS)


def _render_no_artifact_corrective(subtask: Any, task: Any) -> str:
    """Top-of-prompt BLOCKER for a re-dispatch whose prior attempt wrote no
    Stream-B artifact. Leads the brief so a monotropic LLM cannot miss it:
    the deliverable does not exist until ``artifact_write`` is called."""
    sid = getattr(subtask, "id", "?")
    tid = getattr(task, "id", "?")
    role = getattr(subtask, "role", "?")
    desc = (getattr(task, "description", "") or "")[:60]
    return (
        "# ⛔ BLOCKER — your previous turn produced NO okuro-brain artifact\n\n"
        "The deliverable DOES NOT EXIST until you call "
        "`artifact_write(kind='report', ...)`. Your last attempt did the "
        "work but never wrote a Stream-B artifact, so the reviewer had "
        "NOTHING to judge and the task failed. This is NOT a content "
        "problem — there is no content to fix. You produced nothing "
        "reviewable.\n\n"
        "**You MUST call `artifact_write` NOW, before doing anything else**, "
        "with this exact shape:\n"
        "```python\n"
        "artifact_write(\n"
        '    kind="report",\n'
        f'    task_id="{tid}",\n'
        f'    subtask_id="{sid}",\n'
        f'    title="{role}: {desc}",\n'
        '    summary="<one-line claim, indexed for semantic search>",\n'
        '    body="<full markdown — per-AC fenced bash blocks with captured stdout/exit>",\n'
        '    media_type="text/markdown",\n'
        ")\n"
        "```\n"
        "Then immediately call `await_review(...)` per the session-loop "
        "protocol below. Do the actual work as before, but the "
        "`artifact_write` call is the load-bearing step — without it the "
        "task fails again identically. Treat this as the single highest "
        "priority of this turn.\n"
    )


def compose_brief(
    *,
    task: Any,
    subtask: Any,
    retry_context: dict | None = None,
) -> str:
    """Public C11 brief composition entry point.

    Gate 2 §C11 — single canonical composer for the structural pieces of
    a subagent brief the reviewer judges against:

    | Block | Always rendered? |
    |---|---|
    | Acceptance criteria checklist | yes (or "none declared" fallback) |
    | M3 reviewer evidentiary contract | yes (G3 unconditional) |
    | Gap-event escape hatch | yes |
    | Retry context (RECOVERY block) | only when ``retry_context`` provided |

    Decoupled from `build_role_prompt`'s heavyweight context (role
    identity, tool protocol, working instructions, …) so:

    1. Tests can compose a brief from a minimal `SimpleNamespace` task /
       subtask without standing up a `Config` object.
    2. The C11 invariant — "the brief tells the agent everything the
       reviewer will judge against" — is enforceable at one site.

    `build_role_prompt` calls into the same helpers
    (`_render_evidentiary_contract`, `_render_retry_context`) so the
    full brief and the composer never drift apart.
    """
    acs = list(getattr(subtask, "acceptance_criteria", None) or [])
    parts: list[str] = []

    if acs:
        parts.append("**Acceptance criteria (deliverable MUST satisfy all):**")
        for x in acs:
            parts.append(f"- [ ] {x}")
        parts.append("")
    else:
        parts.append(
            "**Acceptance criteria:** none declared by the planner. Your "
            "`artifact_write` body is still the canonical deliverable; "
            "the reviewer judges its content."
        )
        parts.append("")

    parts.append(_render_evidentiary_contract(acs))
    parts.append("")

    retry_block = _render_retry_context(subtask, retry_context, acs)
    if retry_block:
        parts.append(retry_block)

    return "\n".join(parts)


def build_role_prompt(role_content: str, subtask: Subtask, task: Task,
                      config: Config, session_id: str = None,
                      effective_project_path: "Optional[Path]" = None) -> str:
    """Assemble the full prompt sent to the CLI agent.

    Wave-5b G18: when ``effective_project_path`` is set (engine created a
    git worktree for this parallel subagent), the prompt teaches the
    subagent to operate on that path instead of ``task.project_path`` so
    commits land on an isolated branch + index.

    WP8: when this is a cold re-dispatch whose prior attempt produced no
    Stream-B artifact (``subtask.error`` matches the no-artifact markers),
    a BLOCKER corrective LEADS the prompt — distinct from a content-FAIL
    retry — so the LLM cannot skip ``artifact_write`` a second time.
    """
    prompt_parts = []

    # WP8 — no-artifact corrective (must lead). Detected purely from the
    # prior-attempt error the engine stamped onto the subtask; a content
    # FAIL (artifact existed but failed review) does NOT match the markers
    # and so falls through to the normal brief below.
    if is_no_artifact_error(getattr(subtask, "error", "")):
        prompt_parts.append(_render_no_artifact_corrective(subtask, task))
        prompt_parts.append("")

    # Review-ledger surgical retry (Phase A): when the blocked_review "Fix"
    # path stamped structured critic findings onto this subtask, LEAD the brief
    # with a RECOVERY worklist so the agent patches EXACTLY the flagged
    # elements in place (and supersedes the prior artifact) instead of blindly
    # regenerating — the root cause of the same findings recurring on retry.
    # blocked_review "Decide" — authoritative user adjudications LEAD the
    # recovery section (above findings). The agent must APPLY each decision and
    # must not re-open it; this is what stops the churn when the reviewer
    # escalated a constraint only the user can call.
    _decisions = list(getattr(subtask, "decision_guidance", None) or [])
    if _decisions:
        dparts: list[str] = ["## AUTHORITATIVE USER DECISIONS — apply exactly, do NOT re-litigate\n"]
        for i, d in enumerate(_decisions, 1):
            dec = (d or {}).get("decision", "") if isinstance(d, dict) else str(d)
            rat = (d or {}).get("rationale", "") if isinstance(d, dict) else ""
            dparts.append(f"{i}. **Decision:** {dec}")
            if rat:
                dparts.append(f"   **Rationale:** {rat}")
        dparts.append(
            "\nThese are user rulings on previously-blocked findings. Reflect "
            "them consistently across the deliverable; treat any prior text that "
            "contradicts them as the defect to fix."
        )
        prompt_parts.append("\n".join(dparts))
        prompt_parts.append("")

    _review_findings = list(getattr(subtask, "review_findings", None) or [])
    if _review_findings:
        _retry_block = _render_retry_context(
            subtask,
            {
                "findings": _review_findings,
                "retries": getattr(subtask, "retries", 0),
            },
            list(getattr(subtask, "acceptance_criteria", None) or []),
        )
        if _retry_block:
            prompt_parts.append(_retry_block)
            prompt_parts.append("")

    # 1. Role identity
    #
    # M4 (Agent Skills): default path injects only the metadata cards for
    # the top-K most relevant roles (deterministic tag overlap → semantic
    # fallback). The subagent calls `roles_get(<role_id>)` lazily to load
    # the full body when it adopts. Legacy path (env var OR task.legacy_roles)
    # re-inlines the full lean_prompt as before M4. Telemetry counts the
    # bytes injected at each path so we can measure the per-spawn delta.
    _legacy_role_injection = (
        os.environ.get("OKURO_LEGACY_ROLE_INJECTION", "").lower() in ("1", "true", "yes")
        or bool(getattr(task, "legacy_roles", False))
    )
    metadata_bytes_injected = 0
    body_bytes_injected = 0
    skill_card_ids: list[str] = []
    if _legacy_role_injection:
        prompt_parts.append("## Role Identity\n")
        prompt_parts.append(role_content)
        prompt_parts.append("")
        body_bytes_injected = len(role_content.encode("utf-8"))
    else:
        try:
            all_cards = list_role_metadata()
        except Exception as exc:
            logger.warning("M4 slice-picker unavailable (%s); falling back "
                           "to full role injection for this spawn", exc)
            prompt_parts.append("## Role Identity\n")
            prompt_parts.append(role_content)
            prompt_parts.append("")
            body_bytes_injected = len(role_content.encode("utf-8"))
        else:
            cards = pick_role_slice(
                task_description=subtask.description,
                assigned_role=subtask.role,
                top_k=5,
                cards=all_cards,
            )
            skill_card_ids = [c["id"] for c in cards]
            prompt_parts.append("## Role Identity\n")
            prompt_parts.append(
                f"**Your assigned role:** `{subtask.role}` — call "
                f"`roles_get('{subtask.role}')` BEFORE acting on this "
                f"assignment to load its full operating directive. The "
                f"metadata cards below are progressive-disclosure pointers; "
                f"only the assigned role's body is binding."
            )
            prompt_parts.append("")
            prompt_parts.append("### Relevant skills (metadata-only, lazy bodies)")
            for card in cards:
                row = render_metadata_block(card)
                prompt_parts.append(row)
                metadata_bytes_injected += len(row.encode("utf-8")) + 1
            prompt_parts.append("")
            prompt_parts.append(
                "Adopt the assigned role first. Consult another role only "
                "when its domain is on the critical path for this subtask — "
                "then call `roles_get('<id>')` to load its body. The full "
                "catalog is reachable via `roles_list()`; do not request "
                "bodies you will not use."
            )
            prompt_parts.append("")

    # 2. Tool protocol pointer.
    #
    # Umbrella audit fix #3 — pre-fix, this block read
    # `knowledge/_shared/protocol-core.md` which okuro never generates,
    # so the entire block was a silent no-op. Bootstrap (mandatory FIRST
    # tool call, see section 3.6) loads the canonical tool protocol into
    # the session; the inline Mandatory Tool Rules table (section 3.8)
    # is the at-glance reminder. The pointer below is the fallback for
    # callers that skip bootstrap (e.g. position prompts via different
    # code path) and matches the CLAUDE.md convention:
    # "Include `read ~/.okuro/TOOL-PROTOCOL.md` in every subagent prompt
    # so the subagent honours the same protocol."
    #
    # M5 recovery: the routing rules were emitted TWICE — this pointer, then a
    # four-row markdown table 60 lines later ("MANDATORY — Tool Rules") — while
    # bootstrap's own "Tool Protocol — MANDATORY ROUTING" section carries the
    # same four rules a third time, in more detail. Three copies of one rule
    # set is not emphasis; it is ~700 bytes on every spawn. Merged here into
    # one at-a-glance line; bootstrap remains the authority.
    prompt_parts.append("## Tool Protocol\n")
    prompt_parts.append(
        "Bootstrap (below) loads the canonical protocol; "
        "`read ~/.okuro/TOOL-PROTOCOL.md` is the durable reference. "
        "At a glance — find code: `cortex_search`/`cortex_route` (never "
        "Grep/Glob); read: `cortex_read_header` then `cortex_read_section` "
        "(never full-file `Read`); system state: `sysinfo_*` (never "
        "`nvidia-smi`/`docker ps`); secrets: `keyring_get` (never .env). "
        "Before finishing: `write_memory(topic, content)` if you discovered "
        "anything non-obvious."
    )
    prompt_parts.append("")

    # 3. Role knowledge
    db_knowledge = _get_role_knowledge(subtask.role, subtask.description, config)
    if db_knowledge:
        prompt_parts.append("## Role Knowledge\n")
        prompt_parts.append(db_knowledge)
        prompt_parts.append("")
    else:
        knowledge_path = config.knowledge_dir / "roles" / subtask.role / "knowledge.md"
        if knowledge_path.exists():
            try:
                with open(knowledge_path, "r") as f:
                    prompt_parts.append("## Role Knowledge\n")
                    prompt_parts.append(f.read())
                    prompt_parts.append("")
            except Exception as e:
                logger.warning(f"Failed to read role knowledge: {e}")

    # 3.5 Available tools
    tools_section = build_tools_section(subtask.role, config.tools_registry_path)
    if tools_section:
        prompt_parts.append("## Available Tools\n")
        prompt_parts.append(tools_section)
        prompt_parts.append("")

    # 3.6 Session start directive
    # Subagents MUST bootstrap themselves — the hard-gate middleware rejects
    # every other tool until they do, so this is load-bearing. Pre-injecting
    # context was a previous (broken) approach: it marked a FAKE bootstrap in
    # telemetry while the subagent never registered a real session. Every
    # "Tools: none" subagent row in session_history came from that path.
    #
    # Provider-agnostic discovery (audit Sprint 2C, finding #14): the okuro
    # bootstrap tool is surfaced under different names by different CLIs —
    # `mcp__okuro__bootstrap` (Claude Code), `mcp_okuro_bootstrap` (Gemini),
    # `bootstrap` (Codex). Hardcoding the Claude form leaked Claude-only
    # assumptions to subagents dispatched on codex/gemini. We render the
    # full discovery list from the same source providers/template.py uses.
    provider_tag = f"orch-{subtask.role}"
    forms_block = _build_bootstrap_forms_block()
    prompt_parts.append("## FIRST: Call the okuro bootstrap tool")
    prompt_parts.append(
        "Your very first tool call MUST be the okuro MCP server's "
        "`bootstrap` tool. Find it in your available tool list — its "
        "exact name varies by client. Common forms (try in order):\n"
    )
    prompt_parts.append(forms_block)
    prompt_parts.append("")
    prompt_parts.append(
        f"Call it with `task_hint='{subtask.description[:140]}'` and "
        f"`provider='{provider_tag}'`."
    )
    prompt_parts.append(
        "This registers your session, loads profile/memory/tool protocol, "
        "and enables telemetry. The middleware rejects every other tool "
        "until bootstrap runs. Before ending, call `session_report()` with "
        "tool feedback — non-negotiable.\n"
    )

    # 3.8 Tool reminder — REMOVED (M5 recovery). This table was the second of
    # three copies of the same routing rules; it is now folded into the
    # "## Tool Protocol" block above. No rule was dropped.

    # 4. Assignment
    prompt_parts.append("## Your Assignment\n")
    prompt_parts.append(f"**Task:** {task.description}")
    prompt_parts.append(f"**Subtask:** {subtask.id} - {subtask.description}")
    prompt_parts.append(f"**Task ID:** {task.id}\n")

    # 4.0 — M1 Locked Decisions (ADRs). When the user resolved a decision
    # gate earlier in this task, every downstream subagent inherits the
    # chosen direction verbatim. Skip silently when there are no ADRs so
    # legacy auto-execute prompts stay byte-identical.
    adrs = list(getattr(task, "adrs", []) or [])
    if adrs:
        prompt_parts.append("## Locked Decisions (ADRs)\n")
        prompt_parts.append(
            "The user resolved the following decision gate(s) for this "
            "task. Treat each as load-bearing — do NOT re-litigate; "
            "implement against the selected option.\n"
        )
        for adr in adrs:
            gate_id = adr.get("gate_id", "?")
            phase_id = adr.get("phase_id", "?")
            prompt = adr.get("prompt", "")
            selected_label = adr.get("selected_label", "")
            selected_id = adr.get("selected_option_id", "")
            rationale = adr.get("selected_rationale", "")
            prompt_parts.append(
                f"- **{gate_id}** (phase {phase_id}) — {prompt}"
            )
            prompt_parts.append(
                f"  → SELECTED: `{selected_id}` — {selected_label}"
            )
            if rationale:
                prompt_parts.append(f"    rationale: {rationale}")
        prompt_parts.append("")

    # 4.0b — M2 Decision Trace (compressor) + Active Decisions (event log).
    # Sits BELOW ADRs because ADRs are user-locked and binding; the
    # decision-trace is agent-derived context. When the compressor has
    # run, its latest trace is the primary upstream-context block. When
    # it hasn't (auto-execute tasks, new tasks), fall through to active
    # decisions extracted from the event log. Refs-only handovers below
    # (block 6) stay as backup detail-on-demand for both paths.
    try:
        from okuro.sense.task_events import latest_compression, active_decisions
        from okuro.sense.artifacts import artifact_get
        _comp = latest_compression(task_id=task.id)
        _active = active_decisions(task_id=task.id)
    except Exception:
        _comp = None
        _active = []
    if _comp:
        cb = _comp.get("body") or {}
        aid = cb.get("artifact_id") or ""
        seq_lo = cb.get("covers_seq_from", "?")
        seq_hi = cb.get("covers_seq_to", "?")
        trace_md = ""
        if aid:
            try:
                art = artifact_get(aid, include_body=True)
                if art:
                    trace_md = (art.get("body") or "").strip()
            except Exception:
                trace_md = ""
        if trace_md:
            prompt_parts.append("## Decision Trace (compressor)\n")
            prompt_parts.append(
                f"*Compressor distilled events seq {seq_lo}–{seq_hi} into "
                f"the structured trace below. Read this BEFORE the "
                f"per-dependency handovers — it is the cross-subtask "
                f"context the next serial step inherits. Refs-only "
                f"handovers (below) remain as detail-on-demand.*\n"
            )
            prompt_parts.append(trace_md)
            prompt_parts.append("")
    elif _active:
        # Active decisions block — surfaces task_events without compression.
        prompt_parts.append("## Active Decisions (event log)\n")
        prompt_parts.append(
            "Unsuperseded decisions emitted by prior subtasks in this "
            "task. Treat as the running ADR set the compressor would "
            "lock if it ran now.\n"
        )
        prompt_parts.append("| Topic | Choice | Rationale |")
        prompt_parts.append("|-------|--------|-----------|")
        for d in _active[:15]:
            body = d.get("body") or d
            topic = (body.get("topic") or "?").replace("|", "\\|")
            choice = (body.get("choice") or "?").replace("|", "\\|")
            rationale = (body.get("rationale") or "").replace("|", "\\|")
            prompt_parts.append(f"| {topic} | {choice} | {rationale} |")
        prompt_parts.append("")

    # Wave-4 G8 — typed contract metadata. All optional; planner fills when
    # intent is concrete enough. Without this, subagents rely on prose
    # `description` alone and downstream re-derives contracts from
    # interpretation. Render each non-empty field as its own block so it's
    # impossible to miss in the middle of a long prompt. Use getattr with
    # defaults so SimpleNamespace test fixtures and pre-G8 Subtask pickles
    # still build cleanly.
    _inputs = getattr(subtask, "inputs", []) or []
    _outputs = getattr(subtask, "outputs", []) or []
    _accept = getattr(subtask, "acceptance_criteria", []) or []
    _targets = getattr(subtask, "target_paths", []) or []
    _contract_id = getattr(subtask, "contract_id", "") or ""
    if _inputs:
        prompt_parts.append("**Inputs (consume):**")
        for x in _inputs:
            prompt_parts.append(f"- {x}")
        prompt_parts.append("")
    if _outputs:
        prompt_parts.append("**Outputs (must produce):**")
        for x in _outputs:
            prompt_parts.append(f"- {x}")
        prompt_parts.append("")
    # Gate 2 §C11 / G3 — AC checklist + evidentiary contract render
    # UNCONDITIONALLY. Previously this entire block was gated on `_accept`
    # being non-empty; if the decomposer omitted ACs the subagent never
    # learned the reviewer's spec OR the gap-event escape hatch.
    if _accept:
        prompt_parts.append("**Acceptance criteria (deliverable MUST satisfy all):**")
        for x in _accept:
            prompt_parts.append(f"- [ ] {x}")
        prompt_parts.append("")
    else:
        prompt_parts.append(
            "**Acceptance criteria:** none declared by the planner. Your "
            "`artifact_write` body is still the canonical deliverable; "
            "the reviewer judges its content."
        )
        prompt_parts.append("")
    prompt_parts.append(_render_evidentiary_contract(_accept))
    prompt_parts.append("")
    if _targets:
        prompt_parts.append("**Target paths (create or modify):**")
        for x in _targets:
            prompt_parts.append(f"- `{x}`")
        prompt_parts.append("")
    if _contract_id:
        prompt_parts.append(
            f"**Realizes contract:** `{_contract_id}` — "
            "fetch via `artifact_get(contract_id)` before writing code."
        )
        prompt_parts.append("")

    # 4.5 Session-loop retry protocol.
    #
    # Every task runs through ``dispatch_subtask_streaming``: the
    # subagent runs inside a long-lived session and MUST call
    # ``await_review`` immediately after every ``artifact_write``.
    # Skipping this deadlocks the engine: the reviewer publishes a
    # verdict the subagent never collects, the wall-clock cap fires,
    # the session is torn down, and the task ends ``blocked_review``
    # from the user's POV. This protocol drives patch-in-place
    # convergence on FAIL verdicts via the live verdict — the subagent
    # never re-spawns cold, so there is no separate recovery prompt.
    prompt_parts.append("## Session-loop retry protocol — CONVERGENT EDITING\n")
    # L4 — pre-write self-check. 55.9% of round-1 review verdicts are FAIL and
    # a review round costs a mean 162s of wall clock (review_loop_stats), while
    # a meaningful share of round-1 findings are mechanically detectable from
    # the deliverable text alone. This step converts those from a full round
    # into a tool call. Placed BEFORE the await_review protocol because it is
    # the step that precedes the write.
    prompt_parts.append(
        "BEFORE your first `artifact_write`, run the reviewer's own "
        "deterministic checks against your draft:\n"
    )
    prompt_parts.append("```python")
    prompt_parts.append(
        f"mcp__okuro__precheck_deliverable(task_id=\"{task.id}\", "
        f"subtask_id=\"{subtask.id}\", body=<your draft body>, "
        "kind=<the kind you will pass to artifact_write>)"
    )
    prompt_parts.append("```")
    prompt_parts.append("")
    prompt_parts.append(
        "Fix everything it reports, THEN write. These are the same checks the "
        "reviewer runs, so a finding here is a FAIL there — one costs a tool "
        "call, the other costs a full review round. Read `checks_skipped`: "
        "those could not run pre-write, so `ok: true` means \"nothing "
        "mechanical found\", NOT \"the reviewer will pass this\".\n"
    )
    prompt_parts.append(
        "You are running inside a long-lived session. After EVERY "
        "`artifact_write` you MUST immediately call:\n"
    )
    prompt_parts.append("```python")
    prompt_parts.append(
        f"mcp__okuro__await_review(subtask_id=\"{subtask.id}\", "
        "artifact_id=\"<id-just-written>\")"
    )
    prompt_parts.append("```")
    prompt_parts.append("")
    prompt_parts.append(
        "Do NOT exit. Do NOT call `session_report` yet. Wait for the verdict.\n"
    )
    prompt_parts.append("The tool will return one of:\n")
    prompt_parts.append(
        "- `{verdict: \"PASS\", ...}` — your work was accepted. Now call "
        "`write_role_handover` (Stream A) + `session_report` and exit."
    )
    prompt_parts.append(
        "- `{verdict: \"FAIL\", findings: [...], implicated_acs: [...]}` — "
        "apply each finding to the artifact body you just wrote. Patch in "
        "place, preserve structure the reviewer accepted, then call:\n"
        "  ```python\n"
        "  artifact_write(supersedes=\"<prior_id>\", body=<patched body>, ...)\n"
        "  ```\n"
        "  Immediately call `await_review` again with the NEW artifact_id. Loop."
    )
    prompt_parts.append(
        "- `{verdict: \"CAP\", ...}` — retries cap hit. Capture state via "
        "`write_role_handover` + `session_report` and exit; engine will "
        "surface `blocked_review` to the user."
    )
    prompt_parts.append(
        "- `{verdict: \"NEEDS_USER\", ...}` — surface a clear summary, call "
        "`session_report`, exit. Engine will route to user."
    )
    prompt_parts.append(
        "- `{status: \"still_reviewing\", elapsed_s: N}` — NORMAL, not a "
        "failure; the reviewer runs on the same slow CLI you do and can take "
        "SEVERAL MINUTES. Immediately re-call `await_review` with the SAME "
        "args + `keep_alive_interval_s: 60`, writing nothing in between. Keep "
        "polling — never treat a repeat as blocked, never close the session "
        "before a real PASS/FAIL/CAP/NEEDS_USER verdict."
    )
    prompt_parts.append(
        "- `{status: \"timeout\"}` — only after the FULL 30-minute window. "
        "Re-call `await_review` once more; if it times out AGAIN, then "
        "escalate via `write_role_handover` (outcome=\"blocked\") + "
        "`session_report`. A single `still_reviewing` is NOT a timeout."
    )
    prompt_parts.append("")
    prompt_parts.append(
        "**Critical:** NEVER finish (`session_report`) immediately after "
        "`artifact_write` without first awaiting a verdict. The engine "
        "deadlocks if you skip `await_review`.\n"
    )

    # 5. Working instructions
    task_dir = config.tasks_dir / task.id
    artifacts_dir = task_dir / "artifacts"

    if subtask.artifact_name:
        artifact_title = f"{subtask.id} — {subtask.artifact_name}"
    else:
        artifact_title = f"{subtask.id} — {subtask.role}"

    # Subagent reports (Stream B) are user-facing and land in
    # ArtifactsViewer. Inject the user's format preferences right next
    # to the artifact_write directive so the rules stay anchored at
    # draft time. HR7 — fmt_block governs Stream B only, never Stream A.
    fmt_block = _build_user_format_block()
    if fmt_block:
        prompt_parts.append(fmt_block)

    # Wave-3 G11 — per-task conventions block. Pinned verbatim from
    # task.conventions (auto-loaded by create_task from
    # {project_path}/CONVENTIONS.md). Without this, folder layout, naming,
    # and error envelope rules drift across the 30-subagent build.
    conv = getattr(task, "conventions", "")
    if isinstance(conv, str) and conv.strip():
        prompt_parts.append("## Project Conventions (pinned for every subagent)\n")
        prompt_parts.append(conv.strip())
        prompt_parts.append(
            "\nFollow these verbatim. Deviation MUST be justified in your "
            "role-handover brief.decisions; otherwise reviewers/cascade "
            "will reject the work.\n"
        )

    # Stack-aware brief — surface the project's active stack profile so
    # the subagent uses the right language, framework, and package
    # manager instead of guessing from filename hints.
    stack_block = _build_stack_block(task)
    if stack_block:
        prompt_parts.append(stack_block)

    prompt_parts.append("## Working Instructions\n")
    prompt_parts.append(f"- **Working directory:** `{task_dir}`")
    # File-output destination — HARD RULE. Subagents historically wrote
    # generated HTML/PDF/audio into /tmp because nothing in the prompt
    # told them not to. The post-flight scanner rescues stragglers, but
    # the cure is to refuse to land them outside the task tree in the
    # first place.
    prompt_parts.append(
        f"- **Deliverable files (HTML, PDF, slides, images, audio, video, "
        f"markdown, JSON, etc.) SHOULD land in:** `{artifacts_dir}`"
    )
    prompt_parts.append(
        "  - This is what the okuro \"See result\" preview, the artifact "
        "index, and the user UI all walk by default."
    )
    prompt_parts.append(
        "  - **Exception:** if the task description explicitly names a "
        "different output path (e.g. `/tmp/foo/`, a specific project "
        "subdirectory), honor the task description. The engine will "
        "auto-mirror any /tmp/* deliverables back into `artifacts/` "
        "post-flight so they remain visible to the UI."
    )
    prompt_parts.append(
        "  - For intermediate working files, use a subdirectory inside "
        "the working directory (e.g. `./work/`) and only promote the "
        "final deliverable."
    )
    # Wave-5b G18 — when the engine created a per-subagent git worktree
    # for this dispatch, point the subagent at it explicitly. Operating
    # against the original project_path while a sibling holds the index
    # races on git commits / checkouts. The worktree has its own branch
    # (okuro/<task>/<sub>) so commits land in isolation.
    if effective_project_path is not None:
        prompt_parts.append(
            f"- **PROJECT WORKTREE (use this path for ALL code + git ops):** "
            f"`{effective_project_path}`"
        )
        prompt_parts.append(
            "  - This is YOUR isolated git worktree on a dedicated branch. "
            "DO NOT touch the original `task.project_path` — sibling "
            "subagents are operating on it concurrently. Race = lost work."
        )
        prompt_parts.append(
            "  - All `git add` / `git commit` / `git checkout` / file edits "
            "MUST happen inside this worktree. Cleanup is handled by the "
            "orchestrator after your subtask completes."
        )
    else:
        # Umbrella audit fix #2 — ORCH-BRIEF-COMPLETE requires every brief
        # to state the target repo absolute path. Pre-fix, the non-worktree
        # path (serial OR non-git project) rendered ONLY the artifacts
        # directory and a vague "Code lands in the project repos" line —
        # the subagent never received task.project_path. Now always
        # render it when the task has one.
        task_project_path = getattr(task, "project_path", None)
        if task_project_path:
            prompt_parts.append(
                f"- **PROJECT REPO (use this path for ALL code + git ops):** "
                f"`{task_project_path}`"
            )
            prompt_parts.append(
                "  - Commit frequently. Stage only deliverables. "
                "Never `git add -A`."
            )
        else:
            prompt_parts.append(
                f"- **Code lands in the project repos** (not in "
                f"`{artifacts_dir}`). Commit frequently."
            )
    prompt_parts.append("- Follow Protocol steps in order")
    prompt_parts.append("- Do NOT modify `task.yaml` status — orchestrator manages state")
    prompt_parts.append("- Git: NEVER `git add -A`. Stage only deliverables.\n")

    # 5.5 Closeout — Two Streams (HR5 — both required for outcome=success)
    prompt_parts.append("## Closeout — Two Streams\n")
    prompt_parts.append(
        "Before you call `session_report`, produce BOTH of the following. "
        "They serve different consumers and must NEVER share content.\n"
    )
    prompt_parts.append("### Stream A — agent-to-agent role-handover (mandatory)\n")
    prompt_parts.append(
        "Call `write_role_handover(...)` exactly once. Refs only — never "
        "inline source. The next subagent reads brief.summary + decisions, "
        "then resolves cortex_refs via `cortex_read_section(path, start, end)`."
    )
    prompt_parts.append("")
    prompt_parts.append("```")
    prompt_parts.append("write_role_handover(")
    prompt_parts.append(f'  subtask_id="{subtask.id}",')
    prompt_parts.append(f'  from_role="{subtask.role}",')
    prompt_parts.append(f'  task_id="{task.id}",')
    prompt_parts.append("  brief={")
    prompt_parts.append('    "summary": "<=600 chars, 1-3 sentences. What I did.",')
    prompt_parts.append('    "decisions": [{"decision": "...", "rationale": "..."}],')
    prompt_parts.append('    "outcome": "success" | "partial" | "failed",')
    prompt_parts.append('    "open_questions": ["..."],')
    prompt_parts.append('    "next_role_hint": "<role-id or null>",')
    # The long inline comments that used to hang off project_path /
    # produced_files were re-stated almost verbatim in the Hard rules block
    # 20 lines below — the same guidance arriving twice inside ONE section.
    # Kept once, in Hard rules, where the validator consequences live.
    prompt_parts.append('    "project_path": "/abs/path/to/realized/project",  # see Hard rules')
    prompt_parts.append('    "produced_files": ["/abs/path.ext"],  # code in the repo, not just artifacts/')
    prompt_parts.append('    "contracts": [  # OPTIONAL — typed contracts you defined/extended')
    prompt_parts.append('      {"name": "POST /auth/register",')
    prompt_parts.append('       "kind": "api",  # api|schema|type|envelope|event|config')
    prompt_parts.append('       "schema": "```ts\\nrequest: {...}\\nresponse: {...}\\n```",')
    prompt_parts.append('       "notes": "<=400 chars, plain text, NO fenced code"}')
    prompt_parts.append('    ],')
    prompt_parts.append("  },")
    prompt_parts.append("  cortex_refs=[")
    prompt_parts.append('    {"path": "<abs or registered-root-relative>",')
    prompt_parts.append('     "start_line": <int>, "end_line": <int>,')
    prompt_parts.append('     "purpose": "<=160 chars",')
    prompt_parts.append('     "anchor": "<AGENT_HEADER section id or null>"},')
    prompt_parts.append("  ],")
    prompt_parts.append("  kg_edges=[")
    prompt_parts.append('    {"subject": "...", "predicate": "...", "object": "...",')
    prompt_parts.append('     "confidence": 1.0},')
    prompt_parts.append("  ],")
    prompt_parts.append(")")
    prompt_parts.append("```")
    prompt_parts.append("")
    prompt_parts.append("Hard rules — the validator rejects writes that violate these:\n")
    prompt_parts.append("- NEVER inline source code in any brief field. Always emit a `cortex_ref` (path + start_line + end_line + purpose).")
    prompt_parts.append("- Every file you wrote or non-trivially modified MUST have at least one `cortex_ref` pointing at it.")
    prompt_parts.append("- `summary` ≤ 600 chars. If you need more, you're handing off prose, not facts — add another decision row or a cortex_ref.")
    prompt_parts.append("- `outcome=\"failed\"` → at least one `open_question` describing the recovery path.")
    prompt_parts.append("- If you wrote any code into a project directory (created or edited files outside the orchestrator's task dir), set `brief['project_path']` to that project root. Without it, the preview \"See result\" button cannot launch your work and falls back to artifacts.\n")
    prompt_parts.append("### Stream B — user-facing report (mandatory)\n")
    prompt_parts.append(
        "Call `artifact_write(...)` once with `audience=\"user\"` (the "
        "default) — the ONLY artifact the okuro web UI shows by default. "
        "Shape it per the User format preferences above and the "
        "`audience=\"user\"` row of the evidentiary contract table.\n"
    )
    prompt_parts.append("```")
    prompt_parts.append("artifact_write(")
    prompt_parts.append('  kind="report",')
    prompt_parts.append(f'  task_id="{task.id}",')
    prompt_parts.append(f'  subtask_id="{subtask.id}",')
    prompt_parts.append(f'  title="{artifact_title}",')
    prompt_parts.append('  summary="<one-line summary indexed for semantic search>",')
    prompt_parts.append('  body="<full markdown body — your deliverable>",')
    prompt_parts.append('  media_type="text/markdown",')
    prompt_parts.append(")")
    prompt_parts.append("```")
    prompt_parts.append("")
    prompt_parts.append(
        "The streams are independent and MUST NOT duplicate. Stream A is "
        "the structured refs-only handover for the next agent; the "
        "`audience=\"agent\"` evidence artifact holds machine-verifiable "
        "AC proof for the reviewer; Stream B is the polished, human "
        "readable document for the user. Stream A + Stream B are both "
        "mandatory for `outcome=success` (either missing → subtask "
        "`partial`); the agent evidence artifact is mandatory whenever "
        "the subtask has acceptance criteria or runtime checks.\n"
    )

    # Phase B — numbers ledger. LLMs are unreliable at multi-step arithmetic;
    # hand-written totals drift and the LLM critic loops trying to catch them.
    # If the deliverable states ANY computed number (total, sum, subtotal,
    # cross-referenced figure, financial/TCO tally), the producer must emit ONE
    # ```numbers ledger so the reviewer recomputes it DETERMINISTICALLY (a
    # mismatch FAILs in <50ms with 0 tokens and short-circuits the LLM critic).
    # Self-gating: a deliverable with no computed numbers simply omits the
    # ledger and the reviewer check passes. This is the option (c) injection
    # that feeds the deterministic numeric_consistency gate.
    prompt_parts.append("### Numbers ledger (mandatory IF your deliverable has computed totals)\n")
    prompt_parts.append(
        "Never hand-write a total. Put each number ONCE as a named fact, then "
        "assert every total/subtotal/cross-reference — the reviewer recomputes "
        "the assertions and FAILs hard on any arithmetic drift. Include this "
        "fenced block in your Stream-B body:\n"
    )
    prompt_parts.append("```numbers")
    prompt_parts.append("# facts: name = <number, or arithmetic over earlier names>")
    prompt_parts.append("revenue_y1 = 1200")
    prompt_parts.append("revenue_y2 = 1800")
    prompt_parts.append("total_revenue = 3000")
    prompt_parts.append("# checks: assert <lhs> == <rhs>  (recomputed deterministically)")
    prompt_parts.append("assert total_revenue == revenue_y1 + revenue_y2")
    prompt_parts.append("# derived values (CAGR, margin, %) MUST be asserted too —")
    prompt_parts.append("# wrap rounding in round() so the check is exact:")
    prompt_parts.append("cagr_pct = 16.5")
    prompt_parts.append("assert cagr_pct == round((revenue_y2/revenue_y1)**(1/1)*100 - 100, 1)")
    prompt_parts.append("# or allow a small absolute tolerance with a leading ~tol:")
    prompt_parts.append("assert ~1 total_revenue == revenue_y1 + revenue_y2")
    prompt_parts.append("```")
    prompt_parts.append(
        "Rules: one ledger per deliverable; thousands separators (1,200) are "
        "fine; the only call allowed is round(x[, ndigits]) — no other code. "
        "Every HEADLINE number you state in prose (totals, CAGR, margin, %, "
        "growth) must be asserted against INDEPENDENT facts (not restated as a "
        "trivially-true equality) — an unasserted or self-referential number is "
        "treated as unverified.\n"
    )

    # 5.6 — M2 producer events. Stream A + B capture the deliverable
    # surface; M2 captures the load-bearing reasoning that should outlive
    # the subtask. Without this directive the cross-subtask decision log
    # stays empty and the next subagent's prompt loses the
    # Decision-Trace / Active-Decisions block, regressing to descriptions
    # alone. The finalize-time detector emits a synthetic `gap` event
    # when this directive is ignored, so the silence is itself surfaced
    # in the M2 log instead of going invisible.
    prompt_parts.append("## MANDATORY — M2 Producer Events\n")
    prompt_parts.append(
        "Before you call `session_report`, emit at least ONE typed event "
        "via `emit_task_event(...)` that captures a load-bearing fact "
        "from this subtask. Pick the kind that fits:\n"
    )
    prompt_parts.append(
        "- `decision` — you chose X over Y for a load-bearing reason "
        "(topic, choice, rationale, alternatives).\n"
        "- `contract` — you defined or extended a typed contract another "
        "subtask will consume (name, kind, schema_body, notes).\n"
        "- `gap` — you found something missing that blocks downstream "
        "work (summary, affects).\n"
        "- `open_question` — you flagged something the user must answer "
        "(question, blocking).\n"
        "- `supersedes` — you replaced a prior decision (target_event_id, "
        "reason, new_choice).\n"
    )
    prompt_parts.append("```")
    prompt_parts.append("emit_task_event(")
    prompt_parts.append(f'  task_id="{task.id}",')
    prompt_parts.append(f'  subtask_id="{subtask.id}",')
    prompt_parts.append(f'  from_role="{subtask.role}",')
    prompt_parts.append('  event_type="decision",  # or contract | gap | open_question | supersedes')
    prompt_parts.append('  body={')
    prompt_parts.append('    "topic": "<=120 chars",')
    prompt_parts.append('    "choice": "<=400 chars",')
    prompt_parts.append('    "rationale": "<=800 chars",')
    prompt_parts.append('  },')
    prompt_parts.append(")")
    prompt_parts.append("```")
    prompt_parts.append("")
    prompt_parts.append(
        "Why: M2 is the append-only cross-subtask decision log that the "
        "compressor reads and the next serial subagent inherits as "
        "context. Silent finalization breaks that chain — the next "
        "subagent has no way to know what reasoning produced the work "
        "it depends on. Emit early in the subtask, not as an "
        "afterthought; bodies are validated, conflicts with active ADRs "
        "are rejected at append time.\n"
    )

    # 6. Previous subtask context — handover-injected (HR1, HR9: NO fallback).
    # Loads role-handover rows for the producer subtasks; renders brief + KG
    # hint + cortex-ref table. Subagents NEVER read other subagents' .md.
    if subtask.dependencies:
        prompt_parts.append("## Previous Subtask Context\n")
        prompt_parts.append(
            "Each upstream subtask emitted a role-handover (Stream A — "
            "agent-optimized, refs only). Read the briefs below; resolve "
            "refs via `cortex_read_section(path, start_line, end_line)` "
            "when you need source. Do NOT request inlined code from prior "
            "subagents — it doesn't exist on this channel by design."
        )

        # Wave-6 G12 — token-budget cap. Assembly subtasks routinely depend
        # on 8+ fan-out subtasks; with no cap each handover injects
        # decisions + cortex_refs + contracts and the prompt overflows
        # before the subagent reads its assignment. Hard-cap dependencies
        # rendered to the most recent N (newest first); the rest are
        # listed by id with a "fetch via list_role_handovers if needed"
        # sentinel so the consumer can opt in to the long tail.
        _DEPS_HARD_CAP = 6
        deps_to_render = list(subtask.dependencies)
        deps_overflow: list[str] = []
        if len(deps_to_render) > _DEPS_HARD_CAP:
            deps_overflow = deps_to_render[:-_DEPS_HARD_CAP]
            deps_to_render = deps_to_render[-_DEPS_HARD_CAP:]
        if deps_overflow:
            prompt_parts.append(
                f"\n*Dependencies truncated to {_DEPS_HARD_CAP} most-recent "
                f"of {len(subtask.dependencies)} total. Hidden: "
                f"{', '.join(deps_overflow)}. Fetch via "
                f"`list_role_handovers(task_id=\"{task.id}\", limit=200)` "
                "if you need the long tail.*\n"
            )

        prompt_parts.append("")

        try:
            from okuro.sense.role_handover import read_role_handover as _read_handover
        except Exception:  # pragma: no cover — should never miss
            logger.error("role_handover storage unavailable; cannot inject handover briefs")
            _read_handover = None

        # Upstream subtasks whose review the HUMAN settled. Downstream must
        # neither re-litigate the flagged finding nor treat it as a blocker —
        # without this the consumer reads a degraded input, files it as an
        # open question, and the reviewer flags the same thing one layer down.
        _dep_states = {
            st.id: st
            for ph in task.phases for st in ph.subtasks
        }

        for dep_id in deps_to_render:
            prompt_parts.append(f"### Subtask {dep_id}")
            _dep = _dep_states.get(dep_id)
            _overridden = getattr(_dep, "review_state", "") == "overridden"
            if _overridden:
                _shipped = getattr(_dep, "status", "") == "done"
                prompt_parts.append(
                    "\n> **The user overruled the quality review on this "
                    "dependency.** That decision is final and authoritative.\n>\n"
                    "> " + (
                        "Its deliverable stands as-is — use it. "
                        if _shipped else
                        "It produced no deliverable and will not be retried. "
                        "Proceed with the inputs you do have. "
                    ) +
                    "Do NOT re-open the flagged issue, do NOT ask for a retry, "
                    "and do NOT record it as an `open_question`. If it limits "
                    "what you can deliver, say so plainly in your own output "
                    "and continue.\n"
                )
            ho = _read_handover(subtask_id=dep_id, task_id=task.id) if _read_handover else None
            if not ho:
                if _overridden:
                    # Already explained above — a retry demand here would
                    # directly contradict the user's decision.
                    prompt_parts.append("")
                    continue
                # HR9: NO degraded fallback. State the absence and let the
                # consumer raise it as an open question rather than silently
                # paper over it with a 40-line excerpt.
                prompt_parts.append(
                    f"\n*No role-handover row found for subtask `{dep_id}`. "
                    "If this is a real dependency, the upstream subtask "
                    "must be retried so it produces one — surface this as "
                    "an `open_question` in your own handover.*\n"
                )
                continue

            brief = ho.get("brief") or {}
            outcome = brief.get("outcome", "?")
            from_role = ho.get("from_role") or "?"
            prompt_parts.append(
                f"*Handover from `{from_role}` — outcome: **{outcome}** "
                f"(handover_id: `{ho.get('id', '?')[:12]}`)*"
            )
            prompt_parts.append("")
            summary = (brief.get("summary") or "").strip()
            if summary:
                prompt_parts.append(f"**Summary:** {summary}")
                prompt_parts.append("")

            decisions = brief.get("decisions") or []
            if decisions:
                prompt_parts.append("**Decisions:**")
                prompt_parts.append("")
                prompt_parts.append("| Decision | Rationale |")
                prompt_parts.append("|----------|-----------|")
                for d in decisions:
                    if isinstance(d, dict):
                        dec = (d.get("decision") or "").replace("|", "\\|")
                        rat = (d.get("rationale") or "").replace("|", "\\|")
                        prompt_parts.append(f"| {dec} | {rat} |")
                prompt_parts.append("")

            open_qs = brief.get("open_questions") or []
            if open_qs:
                prompt_parts.append("**Open questions:**")
                for q in open_qs:
                    if isinstance(q, str):
                        prompt_parts.append(f"- {q}")
                prompt_parts.append("")

            # Wave-4 G9 — typed contracts. Inlined verbatim because they
            # ARE the surface downstream consumes (API endpoints, DB
            # schemas, message envelopes). The 600-char summary cap was
            # too small to carry these without lossy paraphrase; a
            # cortex_ref alone forces an extra round-trip per consumer.
            contracts = brief.get("contracts") or []
            if contracts:
                prompt_parts.append("**Contracts produced by upstream:**")
                prompt_parts.append("")
                for c in contracts:
                    if not isinstance(c, dict):
                        continue
                    name = c.get("name", "?")
                    kind = c.get("kind", "?")
                    schema_body = c.get("schema", "")
                    notes = c.get("notes", "")
                    prompt_parts.append(f"#### `{name}` ({kind})")
                    prompt_parts.append("")
                    prompt_parts.append(schema_body.rstrip())
                    if notes:
                        prompt_parts.append("")
                        prompt_parts.append(f"_Notes:_ {notes}")
                    prompt_parts.append("")
                prompt_parts.append(
                    "Treat these as binding. If you must diverge, justify "
                    "in your own brief.decisions and emit a superseding "
                    "contract in your handover.\n"
                )

            # Wave-3 G7 — file-provenance ledger. Files this subtask
            # materially changed in the project repo (not just under
            # tasks/{id}/artifacts/). Without this, downstream subagents
            # cannot discover real code that landed outside artifacts/
            # and re-implement it from scratch — the dominant drift mode
            # for actual webapp builds.
            produced_files = brief.get("produced_files") or []
            if produced_files:
                prompt_parts.append("**Files produced by upstream:**")
                prompt_parts.append("")
                for p in produced_files:
                    if isinstance(p, str) and p.strip():
                        prompt_parts.append(f"- `{p.strip()}`")
                prompt_parts.append(
                    "\nRead these BEFORE writing new code in the same area "
                    "— extend, don't re-implement.\n"
                )

            refs = ho.get("cortex_refs") or []
            if refs:
                # Wave-6 G12 — cap cortex_refs per handover. Decisions
                # are NEVER dropped (those are the binding signal); only
                # refs are pruned because the consumer can fetch the
                # full set via the handover row directly.
                _REFS_PER_HANDOVER_CAP = 8
                refs_overflow = max(0, len(refs) - _REFS_PER_HANDOVER_CAP)
                refs_render = refs[:_REFS_PER_HANDOVER_CAP]

                prompt_parts.append("**Cortex refs (resolve on demand):**")
                prompt_parts.append("")
                prompt_parts.append("| # | Path | Lines | Purpose |")
                prompt_parts.append("|---|------|-------|---------|")
                for i, ref in enumerate(refs_render, start=1):
                    if not isinstance(ref, dict):
                        continue
                    path = (ref.get("path") or "").replace("|", "\\|")
                    s = ref.get("start_line", "?")
                    e = ref.get("end_line", "?")
                    purpose = (ref.get("purpose") or "").replace("|", "\\|")
                    prompt_parts.append(f"| {i} | `{path}` | {s}-{e} | {purpose} |")
                if refs_overflow:
                    prompt_parts.append(
                        f"| … | _+{refs_overflow} more refs_ | _truncated_ | "
                        "_call `read_role_handover(subtask_id=...)` for the full set_ |"
                    )
                prompt_parts.append("")

            hint = brief.get("next_role_hint")
            if hint:
                prompt_parts.append(f"**Next-role hint from upstream:** `{hint}`")
                prompt_parts.append("")

            prompt_parts.append(
                f"**Lineage:** call `kg_query(entity=\"{dep_id}\", direction=\"both\")` "
                "for triples this subtask asserted (decisions, ownership, succession)."
            )
            prompt_parts.append("")

    # 6.5 Wave-4 G10 — cross-task open-question aggregation. Per-dep injection
    # only reaches direct descendants; siblings on parallel branches inherit
    # wrong assumptions silently. Aggregate the union of all open_questions
    # across this task's handovers (excluding the direct-dep set already
    # rendered above) into an "Active open questions across task" block so
    # every subagent sees the full unresolved set before acting.
    try:
        from okuro.sense.role_handover import list_role_handovers as _list_handovers
        all_hos = _list_handovers(task_id=task.id, limit=200)
    except Exception:
        all_hos = []

    direct_dep_ids = set(subtask.dependencies or [])
    cross_open_qs: list[tuple[str, str]] = []  # (subtask_id, question)
    seen_questions: set[str] = set()
    for ho in all_hos:
        ho_subtask = ho.get("subtask_id") or ""
        if ho_subtask == subtask.id:
            continue  # don't echo this subtask's own prior handover (retries)
        if ho_subtask in direct_dep_ids:
            continue  # already rendered in the per-dep block above
        brief = ho.get("brief") or {}
        for q in (brief.get("open_questions") or []):
            if not isinstance(q, str):
                continue
            q_clean = q.strip()
            if not q_clean or q_clean in seen_questions:
                continue
            seen_questions.add(q_clean)
            cross_open_qs.append((ho_subtask, q_clean))

    if cross_open_qs:
        prompt_parts.append("## Active Open Questions Across Task\n")
        prompt_parts.append(
            "Open questions from sibling subtasks (NOT direct dependencies). "
            "If any apply to your scope, surface them in your own brief.open_questions "
            "with whatever resolution or hand-off you can offer — silent "
            "inheritance of wrong assumptions across parallel branches is a "
            "primary failure mode."
        )
        prompt_parts.append("")
        prompt_parts.append("| From subtask | Question |")
        prompt_parts.append("|--------------|----------|")
        for sid, q in cross_open_qs[:15]:
            q_safe = q.replace("|", "\\|")
            prompt_parts.append(f"| `{sid}` | {q_safe} |")
        prompt_parts.append("")

    # 7. Reusable capabilities
    try:
        from okuro.orchestrator.capabilities.registry import CapabilityRegistry
        cap_path = config.orchestrator_root / "capabilities.yaml"
        registry = CapabilityRegistry(cap_path)
        if registry.count > 0:
            relevant = registry.format_relevant(subtask.description, max_caps=5)
            if relevant:
                prompt_parts.append(relevant)
    except Exception:
        pass

    # M4 telemetry — emit the role_slice event AFTER the prompt is fully
    # assembled (so we capture the actual injection that hit the wire).
    # Best-effort: failure to write telemetry must never block dispatch.
    try:
        from okuro.sense.task_events import append_event
        append_event(
            task_id=task.id,
            subtask_id=subtask.id,
            from_role="dispatcher",
            event_type="role_slice",
            body={
                "subtask_role": subtask.role,
                "card_ids": skill_card_ids,
                "metadata_bytes": metadata_bytes_injected,
                "body_bytes": body_bytes_injected,
                "legacy_mode": _legacy_role_injection,
            },
            created_by="dispatcher.build_role_prompt",
        )
    except Exception as exc:
        logger.debug("role_slice telemetry skipped: %s", exc)

    return "\n".join(prompt_parts)


def build_cli_command(binary: str, flags: list[str], model_flag: str,
                      model: str, prompt: str) -> list[str]:
    cmd = [binary]
    cmd.extend(flags)
    if model_flag and model:
        cmd.append(model_flag)
        cmd.append(model)
    cmd.append(prompt)
    return cmd


def _normalize_event_claude(event: dict) -> dict | None:
    """Parse a Claude `--output-format stream-json` event.

    Claude streams structured `assistant` / `tool_use` / `thinking` /
    `result` blocks. We project them onto the activity-stream schema
    consumed by the web UI and the activity-watcher tail.
    """
    ts = datetime.utcnow().isoformat()
    etype = event.get("type")

    if etype == "system" and event.get("subtype") == "init":
        return {"ts": ts, "type": "session_start", "tools_count": len(event.get("tools", []))}

    if etype == "assistant":
        for block in event.get("message", {}).get("content", []):
            btype = block.get("type")
            if btype == "tool_use":
                inp = block.get("input", {})
                # Fallback chain — order matters. file_path wins for
                # Write/Read/Edit so their preview stays the path even
                # when their payload also has a `content` key. Okuro
                # tools (log_progress/bootstrap/capture_thought/
                # write_memory/set_reminder) land in the tail keys.
                preview = (
                    inp.get("file_path")
                    or inp.get("command")
                    or inp.get("pattern")
                    or inp.get("query")
                    or inp.get("summary")
                    or inp.get("task_hint")
                    or inp.get("content")
                    or inp.get("what")
                    or inp.get("topic")
                    or ""
                )
                return {"ts": ts, "type": "tool_use", "name": block.get("name", "?"), "preview": preview}
            elif btype == "thinking":
                return {"ts": ts, "type": "thinking", "preview": block.get("thinking", "")}
            elif btype == "text":
                text = block.get("text", "")
                if text.strip():
                    return {"ts": ts, "type": "text", "text": text}
        return None

    if etype == "result":
        # M5+ — token usage incl. cache columns, so effective billable input
        # can be separated from the cached prefix.
        #
        # The spawn_usage EMITTER this comment used to point at lived in the
        # legacy `dispatch_subtask` and was deleted with it on 2026-05-30,
        # leaving the comment asserting a consumer that no longer existed. The
        # emitter now lives in dispatcher_streaming (`_emit_spawn_usage`),
        # which is the path subagents actually run on; this normalization
        # remains correct for the legacy path's own activity rows.
        usage = event.get("usage") or {}
        return {
            "ts": ts, "type": "result",
            "success": event.get("subtype") == "success",
            "duration_ms": event.get("duration_ms", 0),
            "cost_usd": event.get("total_cost_usd", 0),
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
        }

    return None


def _normalize_event_codex(line: str) -> dict | None:
    """Parse one line of Codex CLI output.

    Codex emits human-readable progress lines, not stream-json. We wrap
    each non-empty line as a `progress` event so the activity stream is
    not silently empty for codex subagents (audit finding #11b). If a
    line happens to be structured JSON (some codex versions ship that),
    parse it best-effort and tag with the original type — never drop.
    """
    text = line.strip()
    if not text:
        return None
    ts = datetime.utcnow().isoformat()
    if text.startswith("{") and text.endswith("}"):
        try:
            data = _json.loads(text)
            if isinstance(data, dict):
                return {
                    "ts": ts,
                    "type": "progress",
                    "subtype": data.get("type", "json"),
                    "line": text,
                    "data": data,
                }
        except (ValueError, _json.JSONDecodeError):
            pass
    return {"ts": ts, "type": "progress", "line": text}


def _normalize_event_gemini(line: str) -> dict | None:
    """Parse one line of Gemini CLI output.

    Gemini also emits plain text (`--yolo` mode). Same best-effort
    progress-line wrapping as codex.
    """
    text = line.strip()
    if not text:
        return None
    ts = datetime.utcnow().isoformat()
    if text.startswith("{") and text.endswith("}"):
        try:
            data = _json.loads(text)
            if isinstance(data, dict):
                return {
                    "ts": ts,
                    "type": "progress",
                    "subtype": data.get("type", "json"),
                    "line": text,
                    "data": data,
                }
        except (ValueError, _json.JSONDecodeError):
            pass
    return {"ts": ts, "type": "progress", "line": text}


def _normalize_event_passthrough(line: str) -> dict | None:
    """Last-resort wrapper for unknown providers.

    Never drop output silently — wrap as `raw`. Keeps the activity
    stream populated even when a brand-new CLI is wired up before its
    parser exists. The downstream UI can render `raw` as a code line.
    """
    text = line.strip()
    if not text:
        return None
    ts = datetime.utcnow().isoformat()
    return {"ts": ts, "type": "raw", "line": text}


def normalize_event(line, provider: str | None = None) -> dict | None:
    """Normalize one line of CLI output to the activity-stream schema.

    Provider-aware dispatcher (audit Sprint 2C, finding #11b):
      * ``claude``  — expects structured stream-json events. ``line``
        may be a pre-parsed dict (legacy callers) or a JSON string.
      * ``codex``   — plain-text progress lines, with opportunistic
        JSON detection.
      * ``gemini``  — same as codex.
      * unknown     — passthrough wrapper as ``{"type": "raw"}``.

    Returns ``None`` for empty lines so the activity file isn't padded
    with blank entries.
    """
    if provider in (None, "claude", "claude-code"):
        # Legacy entry: callers used to parse the JSON themselves and
        # pass us a dict. Preserve that contract for backwards compat.
        if isinstance(line, dict):
            return _normalize_event_claude(line)
        text = line.strip() if isinstance(line, str) else ""
        if not text:
            return None
        try:
            event = _json.loads(text)
        except (ValueError, _json.JSONDecodeError):
            # If a Claude run somehow emits a non-JSON line (stderr leak,
            # warning text), don't drop it — fall back to passthrough.
            return _normalize_event_passthrough(text)
        if not isinstance(event, dict):
            return None
        return _normalize_event_claude(event)

    text = line if isinstance(line, str) else _json.dumps(line)
    if provider == "codex":
        return _normalize_event_codex(text)
    if provider == "gemini":
        return _normalize_event_gemini(text)
    return _normalize_event_passthrough(text)


def execute_command(cmd: list[str], cwd: Path, timeout: int,
                    extra_env: Dict = None,
                    subtask_id: str | None = None,
                    role: str | None = None,
                    provider: str | None = None) -> Dict:
    """Execute command via subprocess with line-by-line streaming.

    subtask_id + role are stamped onto every .activity.jsonl line so
    parallel subagents in the same task can be demultiplexed downstream.

    ``provider`` selects the activity-stream parser
    (claude / codex / gemini / unknown→passthrough). Without it, codex
    and gemini runs left ``.activity.jsonl`` empty because their plain
    text lines failed claude's stream-json parse.
    """
    result = {"success": False, "output": "", "duration": 0.0, "error": ""}

    def _tag(entry: dict) -> dict:
        if subtask_id is not None:
            entry.setdefault("subtask_id", subtask_id)
        if role is not None:
            entry.setdefault("role", role)
        return entry

    # Defensive: ensure all cmd elements are strings (debug: tuple error traced to here)
    cmd = [str(c) if not isinstance(c, str) else c for c in cmd]

    cmd_display = cmd.copy()
    if len(cmd_display[-1]) > 100:
        cmd_display[-1] = cmd_display[-1][:100] + "..."
    logger.info(f"Executing: {' '.join(cmd_display)}")

    start_time = time.time()
    env = {
        k: v
        for k, v in os.environ.items()
        if k != "CLAUDECODE"
        and not k.startswith("CLAUDE_")
        and not k.startswith("ANTHROPIC_")
    }
    if extra_env:
        env.update(extra_env)

    activity_file = cwd / ".activity.jsonl"

    try:
        af = open(activity_file, "a")
    except OSError:
        af = None

    try:
        if af:
            cmd_name = str(cmd[0]).split("/")[-1] if cmd else "unknown"
            af.write(_json.dumps(_tag({"ts": datetime.utcnow().isoformat(), "type": "subtask_start", "cmd": cmd_name})) + "\n")
            af.flush()

        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=cwd, env=env, start_new_session=True,
        )

        final_output = ""
        all_stdout = []

        # Wave-5 G15 — concurrent stderr drain. The pre-fix loop drained
        # stderr only AFTER stdout EOF; if a subagent wrote >64KB to
        # stderr the kernel pipe buffer filled, the child blocked on
        # write, and the parent never reached the post-loop stderr.read()
        # until the 600s timeout fired. Drain in a daemon thread so both
        # pipes can fill independently. Thread auto-exits on stderr EOF.
        import threading as _threading
        stderr_chunks: list[str] = []

        def _drain_stderr():
            try:
                if proc.stderr is None:
                    return
                for chunk in iter(proc.stderr.readline, ""):
                    if not chunk:
                        break
                    stderr_chunks.append(chunk)
            except Exception:
                pass

        stderr_thread = _threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        # Umbrella audit fix #9 — kill-request watcher. Sentinel writes a
        # flag at {task_dir}/.kill-requests/{subtask_id} when intervention
        # threshold is exceeded. Without this watcher, the intervention
        # event was emitted but no live pipeline component acted on it —
        # slow-but-alive subagents had no upstream teardown. Daemon thread
        # polls every 2s and SIGTERMs (then SIGKILLs) the process group.
        kill_requested = {"flag": False, "reason": ""}
        if subtask_id is not None:
            # cwd is the task dir for orchestrator dispatches.
            kill_flag_path = cwd / ".kill-requests" / subtask_id

            def _watch_kill_request():
                try:
                    while proc.poll() is None:
                        if kill_flag_path.exists():
                            try:
                                kill_requested["reason"] = kill_flag_path.read_text()
                            except OSError:
                                kill_requested["reason"] = "(unreadable flag)"
                            kill_requested["flag"] = True
                            try:
                                os.killpg(proc.pid, _signal.SIGTERM)
                            except (OSError, AttributeError):
                                # AttributeError: os.killpg / signal.SIG* are
                                # POSIX-only and absent on Windows.
                                try:
                                    proc.terminate()
                                except Exception:
                                    pass
                            # Give the process a 5s grace period before SIGKILL.
                            for _ in range(50):
                                if proc.poll() is not None:
                                    return
                                time.sleep(0.1)
                            try:
                                os.killpg(proc.pid, _signal.SIGKILL)
                            except (OSError, AttributeError):
                                try:
                                    proc.kill()
                                except Exception:
                                    pass
                            return
                        time.sleep(2.0)
                except Exception:
                    pass

            kill_watcher = _threading.Thread(
                target=_watch_kill_request, daemon=True,
            )
            kill_watcher.start()
        else:
            kill_watcher = None

        while True:
            elapsed = time.time() - start_time
            if elapsed >= timeout:
                try:
                    os.killpg(proc.pid, _signal.SIGKILL)
                except (OSError, AttributeError):
                    proc.kill()
                proc.wait(timeout=10)
                if af:
                    try:
                        af.write(_json.dumps(_tag({"ts": datetime.utcnow().isoformat(), "type": "error", "message": f"Timed out after {timeout}s"})) + "\n")
                        af.flush()
                    except Exception:
                        pass
                # Wave-5 G15 — let the stderr drain thread flush any
                # pending kernel-buffer content so the timeout error
                # surface includes whatever the subagent shouted before
                # being killed (often the most diagnostic line).
                try:
                    stderr_thread.join(timeout=2)
                except Exception:
                    pass
                stderr_at_kill = "".join(stderr_chunks)
                result["duration"] = time.time() - start_time
                result["error"] = (
                    f"Timed out after {timeout}s — killed (pid={proc.pid})"
                    + (f"; stderr tail: {stderr_at_kill[-500:]}" if stderr_at_kill.strip() else "")
                )
                result["output"] = final_output if final_output else "".join(all_stdout)
                return result

            line = proc.stdout.readline()
            if not line and proc.poll() is not None:
                break
            if not line:
                continue

            all_stdout.append(line)

            # Provider-aware activity normalization. For claude we still
            # try to parse `result` events to capture final_output; for
            # codex/gemini the plain text lines are wrapped as `progress`
            # so .activity.jsonl is never empty (audit Sprint 2C #11b).
            try:
                activity_event = normalize_event(line, provider=provider)
                if activity_event and af:
                    af.write(_json.dumps(_tag(activity_event)) + "\n")
                    af.flush()
            except Exception:
                pass

            if provider in (None, "claude", "claude-code"):
                try:
                    event = _json.loads(line.strip())
                    if isinstance(event, dict) and event.get("type") == "result":
                        final_output = event.get("result", "")
                except (ValueError, _json.JSONDecodeError):
                    pass

        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, _signal.SIGKILL)
            except (OSError, AttributeError):
                proc.kill()
            proc.wait(timeout=5)

        # Wave-5 G15 — wait for the stderr drain thread to finish, then
        # collect everything it captured. join with a small timeout so a
        # stuck thread can't hang the dispatcher loop.
        try:
            stderr_thread.join(timeout=5)
        except Exception:
            pass
        stderr = "".join(stderr_chunks)
        result["duration"] = time.time() - start_time

        if proc.returncode == 0:
            result["success"] = True
            result["output"] = final_output if final_output else "".join(all_stdout)
        else:
            result["output"] = stderr if stderr.strip() else "".join(all_stdout)
            # Detect signal-induced termination. Popen surfaces signals as
            # negative returncodes; the shell convention is 128+signum, so
            # both forms reach us depending on how the child exited.
            _SIGNAL_EXITS = {-15: "SIGTERM", 143: "SIGTERM",
                             -9: "SIGKILL", 137: "SIGKILL",
                             -2: "SIGINT", 130: "SIGINT"}
            killed_by = _SIGNAL_EXITS.get(proc.returncode)
            if kill_requested["flag"]:
                # Umbrella audit fix #9 — surface the kill-request reason
                # so handle_failure → cascade-skip can route precisely.
                result["error"] = (
                    f"Killed by sentinel intervention "
                    f"(returncode={proc.returncode}). "
                    f"{kill_requested['reason']}"
                )
                result["killed_by_signal"] = killed_by or ""
            elif killed_by:
                # External kill (not us). Tag for retry path + dashboard.
                result["error"] = (
                    f"Killed by {killed_by} (returncode={proc.returncode}) — "
                    f"external signal, not a dispatcher timeout. Possible "
                    f"causes: parent process-group teardown, OOM, or "
                    f"nested-CLI process collision."
                )
                result["killed_by_signal"] = killed_by
                if af:
                    try:
                        af.write(_json.dumps(_tag({
                            "ts": datetime.utcnow().isoformat(),
                            "type": "subtask_killed",
                            "signal": killed_by,
                            "returncode": proc.returncode,
                        })) + "\n")
                        af.flush()
                    except Exception:
                        pass
            else:
                result["error"] = f"Exited with code {proc.returncode}"

    except FileNotFoundError:
        result["duration"] = time.time() - start_time
        result["error"] = f"Binary not found: {cmd[0]}"
    except Exception as e:
        result["duration"] = time.time() - start_time
        result["error"] = f"Execution failed: {e}"
    finally:
        if af:
            af.close()

    return result


def truncate_output(output: str, max_lines: int = 500) -> str:
    lines = output.split("\n")
    if len(lines) <= max_lines:
        return output
    head_lines = 150
    tail_lines = max_lines - head_lines
    dropped = len(lines) - head_lines - tail_lines
    result = lines[:head_lines]
    result.append("")
    result.append(f"[... truncated {dropped} lines ...]")
    result.append("")
    result.extend(lines[-tail_lines:])
    return "\n".join(result)


# ── Brain integration (direct imports) ─────────────────────────────────────


def _get_role_knowledge(role: str, task_description: str, config) -> str | None:
    """Query role_knowledge (persistent, per-role store) for the subtask.

    Reads from okuro.roles.knowledge.read_knowledge so learnings written
    via roles_learn flow back into the next invocation's prompt. Each
    entry is formatted with its type tag and an optional source-URL
    footer so the role can trace provenance.
    """
    if config and not config.sense.enabled:
        return None
    try:
        from okuro.roles.knowledge import read_knowledge
        entries = read_knowledge(
            role_id=role, task_hint=task_description, limit=5
        )
        if not entries:
            return None
        parts = []
        for e in entries:
            header = f"[{e['type']}] {e['content']}"
            if e.get("source_url"):
                header += f"\n  ↳ {e['source_url']}"
            parts.append(header)
        return "\n\n".join(parts)
    except Exception as e:
        logger.warning(f"Role knowledge query failed for {role}: {e}")
    return None
