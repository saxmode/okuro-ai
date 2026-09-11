# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Tier matrix for okuro MCP tools — defaults + YAML override loader.
# index:
#   imports
#   TIER vocabulary
#   DEFAULT POLICY map
#   DEFAULT_TIER_RULES
#   _SHELL_SAFE_BASH_ALLOWLIST
#   def classify_tool
#   def default_policy_for_tier
#   def load_tier_overrides
#   def merge_tier_rules
#   def resolve_tool_policy
# AGENT_HEADER_END -->
"""Tier matrix for okuro MCP tools.

Contract: every gated tool call resolves to exactly one tier and one
default policy. The tier matrix lives in two layers:

  A. **Built-in defaults** declared in :data:`DEFAULT_TIER_RULES`. These are
     pattern-based — a tool is matched by exact-name or prefix so we don't
     have to enumerate the 64-tool registry exhaustively.
  B. **User overrides** at ``~/.okuro/inline/tool_tiers.yaml``. The file is
     optional; when present it merges on top of the defaults.

Tiers (canonical for wave 3a — sourced from
``docs/research/cli-streaming-contract.md`` §4.1):

  read           — auto-approved; cortex_*, get_*, list_*, search_*,
                   sysinfo_*, read_memory, todo_get, …
  write          — ask-once-per-session; write_memory, todo_update,
                   todo_done, todo_add, log_progress, capture_thought,
                   set_reminder.
  shell-safe     — auto-approved; reserved for the subset of Bash that
                   only invokes the explicit allowlist.
  shell-write    — ask-every-call; the catch-all for any other Bash use.
  secrets        — deny by default; keyring_get is the only built-in
                   member.
  destructive    — deny by default; keyring_delete, artifact_delete,
                   delivery_delete, anything matching the destructive
                   pattern list.

Default policy per tier (matches the contract):

  auto           — execute immediately, audit as 'auto'.
  ask-once       — first call this session prompts; cached approvals let
                   subsequent calls auto-approve.
  ask-every      — every call prompts.
  deny           — refuse; the dispatcher returns an 'approval_denied'
                   error and never executes the handler.

This module is intentionally a pure data + lookup layer. The async
approval-wait machinery lives in :mod:`okuro.sense.approval_gate` —
keeping the two split means tests for the matrix can run without an
event loop.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Optional
from okuro.db.engine import okuro_home

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

Tier = Literal["read", "write", "shell-safe", "shell-write", "secrets", "destructive"]
Policy = Literal["auto", "ask-once", "ask-every", "deny"]

TIERS: tuple[Tier, ...] = (
    "read",
    "write",
    "shell-safe",
    "shell-write",
    "secrets",
    "destructive",
)

DEFAULT_POLICY: dict[Tier, Policy] = {
    "read": "auto",
    "write": "ask-once",
    "shell-safe": "auto",
    "shell-write": "ask-every",
    "secrets": "deny",
    "destructive": "deny",
}


# ---------------------------------------------------------------------------
# Built-in classification rules
# ---------------------------------------------------------------------------
#
# Each entry maps a tool name OR a name-prefix (ending in '*') to a tier.
# Exact-name wins over prefix; the first matching prefix wins for prefixes.
# Tools that don't match any rule fall back to ``read`` (the safest tier
# under the contract — auto-approved read-only operations).

DEFAULT_TIER_RULES: dict[str, Tier] = {
    # ----- read tier — codebase + memory + sysinfo, all auto -----
    "cortex_*": "read",
    "read_memory": "read",
    "read_role_handover": "read",
    "list_role_handovers": "read",
    "await_review": "read",  # long-poll wait; no DB write from the await side
    "sysinfo_*": "read",
    "todo_get": "read",
    "todo_list": "read",
    "get_progress": "read",
    "get_project": "read",
    "get_profile": "read",
    "get_principles": "read",
    "list_projects": "read",
    "list_reminders": "read",
    "search_thoughts": "read",
    "session_history": "read",
    "session_score": "read",
    "session_list": "read",
    "session_trace": "read",
    "session_report": "read",  # auditing your own session is read-shaped
    "daily_digest": "read",
    "tool_performance": "read",
    "compliance_scorecard": "read",
    "bridge_providers": "read",
    "bridge_status": "read",
    "bridge_stream_event": "read",  # polling reads, never executes a tool
    "canon_list_*": "read",
    "canon_get_*": "read",
    "canon_validate": "read",
    "roles_list": "read",
    "roles_get": "read",
    "roles_match": "read",
    "roles_domains": "read",
    "roles_knowledge": "read",
    "artifact_get": "read",
    "artifact_list": "read",
    "artifact_search": "read",
    "delivery_get": "read",
    "delivery_list": "read",
    "person_get": "read",
    "person_list": "read",
    "person_match": "read",
    "person_lens": "read",
    # Semantic search over audiences — same shape as person_match. The
    # unmatched fallback is already "read", but an explicit rule is what the
    # audit reads; implicit agreement is indistinguishable from an oversight.
    "target_group_match": "read",
    "principle_set_get": "read",
    "principle_set_list": "read",
    "stack_*": "read",  # all current stack_* are read-shaped query tools
    "trace_*": "read",
    "keyring_list": "read",  # returns key names only (no values per docstring)
    "mcp_health": "read",
    "bootstrap": "read",
    "brain_advise": "read",
    "list_tool_invocations": "read",
    "memory_audit": "read",
    "memory_stale": "read",
    "memory_utility": "read",
    "kg_query": "read",
    "kg_resolve": "read",
    "kg_stats": "read",
    "kg_timeline": "read",
    "tunnel_find": "read",
    "tunnel_concepts": "read",
    "tunnel_link": "read",
    "tunnel_bridge": "read",
    "transcript_search": "read",
    "transcript_sweep": "read",
    "transcript_adapters": "read",
    "behavioral_rules": "read",
    "role_diary_read": "read",
    "role_diary_search": "read",
    "role_diary_stats": "read",
    "preview_status": "read",
    "preview_logs": "read",
    # Enumeration over one project slug — never writes. The unmatched
    # fallback is already "read", but an implicit pass is indistinguishable
    # from a forgotten classification when the matrix is audited.
    "project_inventory": "read",
    # Assembles from existing tables; never writes. Same reasoning as
    # project_inventory — an implicit pass through the "read" fallback is
    # indistinguishable from a forgotten classification when the matrix is
    # audited.
    "project_status": "read",
    # ----- write tier — ask-once-per-session, audited -----
    # project_envelope defaults to dry_run=True, but the tier describes what a
    # tool CAN do, not what its defaults do — it rewrites the project column on
    # arbitrary rows once dry_run=False. Classified by capability. Not
    # destructive: it never deletes, never touches content, and every apply
    # writes an undo file consumed by project_envelope_undo.
    "project_envelope": "write",
    "project_envelope_undo": "write",
    # Writes the declared phase plan. `replace=True` deletes phases omitted
    # from the list, but only within one project's own plan and only rows this
    # tool created — no content, no cross-project reach. Write, not destructive.
    "project_phases_set": "write",
    "write_memory": "write",
    "log_progress": "write",
    "capture_thought": "write",
    "update_thought": "write",
    "set_reminder": "write",
    "snooze_reminder": "write",
    "dismiss_reminder": "write",
    "acknowledge_reminder": "write",
    "accept_suggestion": "write",
    "reject_suggestion": "write",
    "todo_add": "write",
    "todo_update": "write",
    "todo_done": "write",
    "update_profile": "write",
    "artifact_write": "write",
    "artifact_supersede": "write",
    "write_role_handover": "write",
    "delivery_send": "write",
    "person_add": "write",
    "person_update": "write",
    "person_update_sliders": "write",
    "person_translate": "write",
    "principle_set_upsert": "write",
    "role_diary_write": "write",
    "roles_learn": "write",
    "run_maintenance": "write",
    "keyring_set": "write",  # storing a new secret is a normal write
    "bridge_stream_start": "write",  # spawning a CLI is a state change
    "bridge_stream_input": "write",
    "bridge_stream_cancel": "write",
    "bridge_stream_approve": "write",
    "bridge_invoke": "write",
    "preview_start": "write",
    "preview_stop": "write",
    "kg_add": "write",
    "kg_assert": "write",
    "orchestrator_create": "write",
    "orchestrator_continue": "write",
    "orchestrator_approve": "write",
    "orchestrator_status": "read",  # status-only read
    # ----- secrets tier — deny unless explicitly overridden -----
    "keyring_get": "secrets",
    # ----- destructive tier — deny by default -----
    "keyring_delete": "destructive",
    "artifact_delete": "destructive",
    "delivery_delete": "destructive",
    "principle_set_delete": "destructive",
    "kg_invalidate": "destructive",
}


# Bash-tool sub-classification: when the dispatched tool is a generic
# Bash/shell wrapper, look at the command string to decide between
# 'shell-safe' (read-only inspection) and 'shell-write' (any other use).
# Mirrors the contract's "subset of bash that hits only ls/ps/cat/df/free/
# journalctl/uptime/uname/which/id" description.
_SHELL_SAFE_BASH_ALLOWLIST: frozenset[str] = frozenset(
    {
        "ls",
        "ps",
        "cat",
        "df",
        "free",
        "journalctl",
        "uptime",
        "uname",
        "which",
        "id",
    }
)


# Destructive-pattern regex. Applied to (tool_name + " " + serialised args)
# as a final safety net so a freshly-added tool whose name pattern doesn't
# match anything else can't slip past the gate on a literal "rm -rf" arg.
_DESTRUCTIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\brm\s+-rf\b", re.IGNORECASE),
    re.compile(r"\bDROP\s+TABLE\b", re.IGNORECASE),
    re.compile(r"\bTRUNCATE\b", re.IGNORECASE),
    re.compile(r"\bDELETE\s+FROM\b\s+[^W]", re.IGNORECASE),  # DELETE w/o WHERE
)


# ---------------------------------------------------------------------------
# Built-in classifier
# ---------------------------------------------------------------------------


def _match_rule(tool_name: str, rules: Mapping[str, Tier]) -> Optional[Tier]:
    """Return the tier from ``rules`` that best matches ``tool_name``.

    Match order:
      1. Exact name match.
      2. Longest prefix match (rule ends in '*').
      3. None if nothing matches.
    """
    if tool_name in rules:
        return rules[tool_name]
    best: tuple[int, Optional[Tier]] = (0, None)
    for pattern, tier in rules.items():
        if not pattern.endswith("*"):
            continue
        prefix = pattern[:-1]
        if tool_name.startswith(prefix) and len(prefix) > best[0]:
            best = (len(prefix), tier)
    return best[1]


def _sniff_bash(args: Mapping[str, Any]) -> Tier:
    """Sub-classify a bash tool call by its command string."""
    cmd = ""
    for key in ("command", "cmd", "shell", "script"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            cmd = value.strip()
            break
    if not cmd:
        return "shell-write"
    first_token = cmd.split()[0]
    # Strip absolute paths so /usr/bin/ls still classifies as safe.
    binary = first_token.rsplit("/", 1)[-1]
    if binary in _SHELL_SAFE_BASH_ALLOWLIST:
        return "shell-safe"
    return "shell-write"


def _destructive_args(tool_name: str, args: Mapping[str, Any]) -> bool:
    """Return True iff serialised args trip a destructive regex."""
    try:
        import json

        haystack = tool_name + " " + json.dumps(args, default=str)
    except Exception:
        haystack = tool_name + " " + str(args)
    return any(pat.search(haystack) for pat in _DESTRUCTIVE_PATTERNS)


def classify_tool(
    tool_name: str,
    args: Optional[Mapping[str, Any]] = None,
    *,
    overrides: Optional[Mapping[str, Tier]] = None,
) -> Tier:
    """Classify ``tool_name`` into one of :data:`TIERS`.

    The order of precedence is:

      1. ``overrides`` (from a loaded YAML) — exact-name then longest prefix.
      2. :data:`DEFAULT_TIER_RULES` — same matching shape.
      3. Bash / shell sub-classification (when ``tool_name`` looks like a
         generic shell tool).
      4. Destructive-pattern regex on the serialised args.
      5. Fallback to ``read`` (the safest tier; auto-approved).
    """
    args = args or {}
    if overrides:
        tier = _match_rule(tool_name, overrides)
        if tier is not None:
            return tier

    tier = _match_rule(tool_name, DEFAULT_TIER_RULES)

    # Generic bash / shell wrappers carry a "command" argument — refine
    # the tier based on the first token of the command. Only kick in when
    # the rule lookup left us at ``write`` or returned None (so an
    # explicit override beats this heuristic).
    lowered = tool_name.lower()
    if lowered in {"bash", "shell", "run_shell", "exec_command"} or lowered.endswith(
        "_bash"
    ):
        bash_tier = _sniff_bash(args)
        # Bash defaults to shell-write; only relax to shell-safe when the
        # command is in the allowlist.
        return bash_tier

    if _destructive_args(tool_name, args):
        return "destructive"

    return tier or "read"


def default_policy_for_tier(tier: Tier) -> Policy:
    """Return the default approval policy for ``tier``."""
    return DEFAULT_POLICY.get(tier, "deny")


# ---------------------------------------------------------------------------
# YAML override loader
# ---------------------------------------------------------------------------


def _override_path() -> Path:
    """Return the canonical override path; honours ``$OKURO_HOME``."""
    home = os.environ.get("OKURO_HOME")
    base = Path(home) if home else okuro_home()
    return base / "inline" / "tool_tiers.yaml"


_VALID_TIERS = frozenset(TIERS)
_VALID_POLICIES = frozenset(("auto", "ask-once", "ask-every", "deny"))


def load_tier_overrides(
    path: Optional[Path] = None,
) -> tuple[dict[str, Tier], dict[Tier, Policy]]:
    """Load + validate the YAML override.

    Schema:

    ```yaml
    rules:
      <tool_name_or_prefix>: <tier>
    policies:
      <tier>: <policy>   # override the default policy for a tier
    ```

    Returns ``({}, {})`` if the file is absent / unreadable / invalid;
    invalid entries are dropped with a warning, valid ones are merged.
    """
    target = path or _override_path()
    if not target.is_file():
        return {}, {}

    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        logger.warning(
            "tool_tiers: pyyaml unavailable — skipping overrides at %s", target
        )
        return {}, {}

    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("tool_tiers: failed to parse %s: %s", target, exc)
        return {}, {}

    if not isinstance(raw, dict):
        logger.warning("tool_tiers: %s root must be a mapping", target)
        return {}, {}

    rules_out: dict[str, Tier] = {}
    rules = raw.get("rules") or {}
    if isinstance(rules, dict):
        for key, value in rules.items():
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            if value not in _VALID_TIERS:
                logger.warning(
                    "tool_tiers: rule %r=%r drops (unknown tier)", key, value
                )
                continue
            rules_out[key] = value  # type: ignore[assignment]

    policies_out: dict[Tier, Policy] = {}
    policies = raw.get("policies") or {}
    if isinstance(policies, dict):
        for key, value in policies.items():
            if (
                not isinstance(key, str)
                or not isinstance(value, str)
                or key not in _VALID_TIERS
                or value not in _VALID_POLICIES
            ):
                logger.warning(
                    "tool_tiers: policy %r=%r drops (invalid)", key, value
                )
                continue
            policies_out[key] = value  # type: ignore[assignment, index]

    return rules_out, policies_out


def merge_tier_rules(
    overrides: Mapping[str, Tier],
) -> dict[str, Tier]:
    """Return the effective rules table: defaults overlaid with ``overrides``.

    Overrides win on key collision — this is the documented merge order.
    """
    merged = dict(DEFAULT_TIER_RULES)
    merged.update(overrides)
    return merged


def resolve_tool_policy(
    tool_name: str,
    args: Optional[Mapping[str, Any]] = None,
    *,
    rule_overrides: Optional[Mapping[str, Tier]] = None,
    policy_overrides: Optional[Mapping[Tier, Policy]] = None,
) -> tuple[Tier, Policy]:
    """One-call helper: classify + look up policy in one go.

    Tests use this directly; the approval gate uses it via
    :func:`okuro.sense.approval_gate.classify_for_session`, which also
    folds in session-level ``approval_overrides`` from the
    ``sessions_inline`` row.
    """
    tier = classify_tool(tool_name, args, overrides=rule_overrides)
    if policy_overrides and tier in policy_overrides:
        return tier, policy_overrides[tier]
    return tier, default_policy_for_tier(tier)


__all__ = [
    "DEFAULT_POLICY",
    "DEFAULT_TIER_RULES",
    "Policy",
    "Tier",
    "TIERS",
    "classify_tool",
    "default_policy_for_tier",
    "load_tier_overrides",
    "merge_tier_rules",
    "resolve_tool_policy",
]
