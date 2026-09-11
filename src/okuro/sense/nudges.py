# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: The advisory half of the middleware — the per-turn rule prefix and
#   the one-nudge-per-call priority chain. Nothing here can refuse a call.
# index:
#   imports
#   def _build_compliance_nudges
#   def _get_behavioral_pool
#   def _per_turn_rule_prefix
#   def _nudge_bootstrap_missing
#   def _nudge_session_end
#   def _nudge_behavioral
#   def _nudge_cortex
#   def _build_one_nudge
# AGENT_HEADER_END -->
"""Nudges and the per-turn rule prefix.

EXTRACTED FROM mcp_middleware.py, 2026-09-09, and the split is along the line
that actually matters in that module: GATES REFUSE, NUDGES DO NOT. Everything
here rides along with a tool result the caller already earned; nothing here can
stop a call. Keeping the two in one file made a 1582-line module whose name
described only half of it.

Worth knowing before changing anything here, because it is the measured reason
the gates exist in the first place: a message returned inside a tool result
produces no detectable behaviour change in this corpus (cortex nudge n=80,
p=0.215; regression discontinuity on the session-end reminder shows no step at
the cutoff). So this file is the CHEAP channel, not the reliable one. Anything
that must actually happen belongs on a gate in mcp_middleware.py, not on a
nudge here.

Names keep their leading underscore and are re-exported from
`okuro.sense.mcp_middleware`, where existing callers import them from.
"""

from okuro.sense.guard_log import guard_failed as _guard_failed
from okuro.sense.session_state import get_session_state

# ---------------------------------------------------------------------------
# Provider-level compliance nudges (used inside bootstrap packet only)
# ---------------------------------------------------------------------------

def _build_compliance_nudges(provider: str) -> str:
    try:
        from okuro.sense.telemetry import get_provider_compliance
        pc = get_provider_compliance(provider)
    except Exception as exc:
        _guard_failed("provider_compliance", exc)
        return ""

    if not pc or pc["total_sessions"] < 3:
        return ""

    avg = pc["avg_normalized"]
    if avg >= 0.7:
        return f"- **Provider compliance:** good ({avg})\n"

    _NUDGE_DEFS = {
        "cortex": (50, "cortex_rate", "Use `cortex_search()`/`cortex_route()` instead of Grep/Glob"),
        "memory": (50, "memory_rate", "Call `write_memory()` when you discover non-obvious patterns"),
        "report": (50, "report_rate", "Call `session_report()` before ending \u2014 most-missed step"),
        "progress": (50, "progress_rate", "Call `log_progress()` at meaningful milestones"),
        "bootstrap": (80, "bootstrap_rate", "Always call `bootstrap()` first in every session"),
    }

    nudges = []
    for name, (threshold, key, instruction) in _NUDGE_DEFS.items():
        rate = pc.get(key, 100)
        if rate < threshold:
            nudges.append((name, rate, instruction))

    nudges.sort(key=lambda x: x[1])
    get_session_state()["compliance_gaps"] = [n[0] for n in nudges]

    if not nudges:
        return f"- **Provider compliance:** {avg} (no major gaps)\n"

    if avg >= 0.4:
        lines = [f"- **Provider compliance:** needs improvement ({avg}). Focus this session:"]
    else:
        lines = [f"- **Provider compliance:** poor ({avg}). **Fix these this session:**"]

    for name, rate, instruction in nudges[:3]:
        lines.append(f"  - **{name}** ({rate:.0f}%) \u2014 {instruction}")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Behavioral pool — small, rotates per nudge, sourced from user profile
# ---------------------------------------------------------------------------

_BEHAVIORAL_THRESHOLD = 8    # start re-asserting after 8 tool calls
_BEHAVIORAL_INTERVAL = 6     # then every 6 calls
_BEHAVIORAL_SLICE_SIZE = 3   # how many rules per injection

_BEHAVIORAL_FALLBACK_POOL = [
    "Format: tables/bullets beat prose. Lead with the answer.",
    "Autonomy: fix errors without asking. Interrupt only for decisions.",
    "Options: max 3, each with pros/cons + a recommendation.",
    "Short, focused blocks. No walls of uniform text.",
    "Don't restate the question. Don't add filler. No 'Great question'.",
    "Anger signals (caps/!) = research MORE, slow down, root cause.",
    "Swearing = energy, not anger. Match it, keep working.",
    "When user infodumps, engage the full content. Don't summarize back.",
]


def _get_behavioral_pool() -> list[str]:
    cached = get_session_state().get("_behavioral_pool")
    if cached is not None:
        return cached

    rules: list[str] = []
    try:
        from okuro.yu.profile import get_profile_raw
        profile = get_profile_raw() or {}

        comm = profile.get("communication", {})
        patterns = comm.get("patterns", [])[:5]
        rules.extend(patterns)

        ip = comm.get("interruption_policy") or profile.get("boundaries", {}).get("interruption_policy")
        if ip:
            rules.append(f"Interruption: {ip}")

        ap = comm.get("anger_protocol", {})
        if isinstance(ap, dict) and ap.get("response"):
            rules.append(f"On anger signals: {ap['response']}")

        hates = profile.get("work_style", {}).get("hates", [])
        if hates:
            rules.append(f"User hates: {', '.join(hates[:5])}. Never produce these.")

        fp = profile.get("communication", {}).get("format_preferences", {})
        pref = fp.get("preferred", [])
        if pref:
            rules.append(f"Preferred format: {', '.join(pref[:4])}")
    except Exception as exc:
        _guard_failed("behavioral_pool_profile", exc)

    if not rules:
        rules = list(_BEHAVIORAL_FALLBACK_POOL)

    get_session_state()["_behavioral_pool"] = rules
    return rules


# ---------------------------------------------------------------------------
# Per-turn rule prefix — fires UNCONDITIONALLY on every non-bootstrap call
# ---------------------------------------------------------------------------
#
# Why this exists: the previous design only fired behavioral rules every 6
# tool calls after turn 8. By the time the agent saw a re-check, drift was
# already locked in. The injection at session start (CLAUDE.md / equivalents)
# is reliable for FORMAT (markdown shape, JSON schema) but the model can
# still drift on STYLE (preamble, filler, "you should", restating the
# question, switching topics mid-response). Those are semantic constraints
# regex-based Stop hooks can't catch.
#
# Cheaper and more reliable than post-generation regex blocking: keep the
# rules near the top of every tool result the agent reads. ~30-60 tokens
# per call, no retry cost (the Stop hook pays 2x tokens on every block).
#
# Provider-agnostic by design: this module is the MCP server's middleware,
# so whichever CLI (claude / codex / gemini / cursor) calls okuro's tools
# gets the same rule prefix on the way back.

_RULE_PREFIX_TAG = "[okuro·rules]"

# Hard cap on rule prefix size. The point is to be compact — if profile
# pulls more than this, we trim. Optimised for "rules stay in agent's
# active attention" not "rules are exhaustive". Verbatim profile rules
# are still in the bootstrap packet for the full picture.
_RULE_PREFIX_MAX_CHARS = 280

# Throttle: emit the prefix on every call for the first N non-bootstrap
# tool calls (anchors the rules in fresh-session attention) and then
# suppress on routine calls. Once the cap is reached, the prefix only
# rides ALONG with an episodic nudge (session-end / cortex / bootstrap-
# missing) — so re-anchoring happens when the agent is already being
# corrected, not on every read_memory call. Avoids the clutter the user
# explicitly hates while keeping the early-session signal.
_RULE_PREFIX_ANCHOR_TURNS = 3


def _per_turn_rule_prefix() -> str | None:
    """Render a compact, profile-driven rules line for this call.

    Layout: ``[okuro·rules] rule1 · rule2 · rule3 · …`` on a single line.
    Rules drawn from (in priority order):
      1. ``communication.patterns`` — the user's lead behavioral rules
      2. ``communication.format_preferences.preferred`` — what to emit
      3. ``communication.pet_peeves`` (rule field) — what to avoid

    Profile fields are mixed-shape (str | {rule, rationale, evidence}).
    We strip to the bare rule text and lowercase for compactness.

    Returns None when:
      - profile is empty / unreadable (graceful no-op on fresh installs)
      - rendered prefix would exceed `_RULE_PREFIX_MAX_CHARS` after trim
        (defensive — shouldn't happen with the hard cap)
    """
    cached = get_session_state().get("_per_turn_rule_prefix")
    if cached is not None:
        return cached or None  # empty string cached → no prefix this session

    rules: list[str] = []
    try:
        from okuro.yu.profile import get_profile_raw

        profile = get_profile_raw() or {}
    except Exception as exc:
        _guard_failed("rule_prefix_profile", exc)
        get_session_state()["_per_turn_rule_prefix"] = ""
        return None

    if not isinstance(profile, dict) or not profile:
        get_session_state()["_per_turn_rule_prefix"] = ""
        return None

    def _flatten(items) -> list[str]:
        out: list[str] = []
        if not isinstance(items, list):
            return out
        for item in items:
            if isinstance(item, str):
                text = item.strip()
            elif isinstance(item, dict):
                text = str(item.get("rule", "")).strip()
            else:
                text = ""
            if text:
                # Squash to one line; the prefix is single-line by design.
                out.append(text.split("\n", 1)[0])
        return out

    comm = profile.get("communication") if isinstance(profile.get("communication"), dict) else {}
    fmt = comm.get("format_preferences") if isinstance(comm.get("format_preferences"), dict) else {}

    # Top patterns first (highest behavioral signal — usually "lead with answer").
    rules.extend(_flatten(comm.get("patterns"))[:3])

    preferred = _flatten(fmt.get("preferred"))[:2]
    if preferred:
        rules.append("prefer " + "/".join(p.split()[0] for p in preferred))

    # Pet peeves render as "no <peeve>" — cheap, signal-dense.
    peeves = _flatten(comm.get("pet_peeves"))[:2]
    for p in peeves:
        # Strip leading "no " / "don't " redundancy; keep it short.
        cleaned = p
        for prefix in ("no ", "don't ", "avoid "):
            if cleaned.lower().startswith(prefix):
                cleaned = cleaned[len(prefix):]
                break
        rules.append(f"no {cleaned}")

    if not rules:
        get_session_state()["_per_turn_rule_prefix"] = ""
        return None

    line = " · ".join(rules)
    if len(line) > _RULE_PREFIX_MAX_CHARS:
        # Trim from the end (lowest-priority signals fall off first).
        line = line[: _RULE_PREFIX_MAX_CHARS - 1].rsplit(" · ", 1)[0] + " …"

    rendered = f"{_RULE_PREFIX_TAG} {line}"
    get_session_state()["_per_turn_rule_prefix"] = rendered
    return rendered


# ---------------------------------------------------------------------------
# One-nudge-per-call priority queue
# ---------------------------------------------------------------------------

_SESSION_END_THRESHOLD = 10      # start session-end nudge after N calls
# After the threshold, the session-end nudge fires on EVERY tool call. This
# is deliberate: tm-launcher had it fire every 8 calls and agents routinely
# ended sessions in the gap. Per the A8 principle, enforcement must be
# un-skippable. A single short nudge on every late-session tool result is
# cheaper than a missed session_report().
_CORTEX_NUDGE_THRESHOLD = 5


def _nudge_bootstrap_missing() -> str | None:
    """Fires if the session has not bootstrapped and tool_calls >= 2."""
    if get_session_state()["bootstrapped"]:
        return None
    if get_session_state()["tool_calls"] < 2:
        return None
    return (
        "\u26a0 bootstrap() was not called. Call "
        "mcp__okuro__bootstrap(task_hint='...', provider='...') to load your "
        "full context (profile, memory, project, session tracking)."
    )


def _nudge_session_end() -> str | None:
    """Fires on EVERY tool call after threshold until session_report is called.

    The mandatory close-out footer (artifact_write / write_memory /
    log_progress / session_report) is the directive channel — unbudgeted,
    per-tool-call. Budget compression can no longer truncate it (the old
    build_session() pipeline is gone).

    Two things this message has to get right, both learned from sessions
    that finished their work and lost it anyway:

    * `artifact_write` is NAMED. The list used to hold only the three
      brain-writes, so an agent that had produced a document-length
      deliverable satisfied everything it was told was mandatory and
      returned without shipping it — the mandate ranked telemetry above the
      deliverable. Conditional, like the other lines: a session that already
      shipped is never told to ship twice.
    * The ORDER is stated. "Before ending you MUST call <tools>" reads, to
      an agent whose final TEXT is its return value, as an instruction to
      end on a tool call — which hands its caller nothing. Naming the
      close-out without naming where it sits relative to the reply is how a
      complete piece of work arrives as silence.
    """
    if get_session_state().get("session_reported"):
        return None
    n = get_session_state()["tool_calls"]
    if n < _SESSION_END_THRESHOLD:
        return None
    missing = []
    if not get_session_state().get("artifact_written"):
        missing.append(
            "`artifact_write()` if you produced a document-length deliverable "
            "\u2014 that IS the deliverable, not a nice-to-have"
        )
    if not get_session_state().get("memory_written"):
        missing.append("`write_memory()` if you discovered anything non-obvious")
    if not get_session_state().get("progress_logged"):
        missing.append("`log_progress()` for substantive work")
    missing.append("`session_report()` \u2014 ALWAYS, rate the tools you used")
    body = "\n".join(f"  {i+1}. {m}" for i, m in enumerate(missing))
    return (
        f"Session-end reminder (turn {n}): before ending, you MUST call:\n{body}\n"
        "  Then FINISH WITH YOUR OWN REPLY, IN PLAIN TEXT. These calls come "
        "BEFORE that reply, never after it \u2014 whoever is waiting on you (a "
        "user, or the agent that spawned you) receives your final message, "
        "not your last tool result."
    )


def _nudge_behavioral() -> str | None:
    """DEPRECATED \u2014 superseded by `_per_turn_rule_prefix` (always-on, compact).

    Left as a no-op stub instead of hard-removed because external test
    suites and downstream consumers may still reference the symbol. The
    behavioral re-check now ships on every non-bootstrap call via the
    prefix path; firing both would double-budget the same rules.
    """
    return None


def _nudge_cortex() -> str | None:
    """Fires once if cortex usage is a compliance gap and agent hasn't used it."""
    if get_session_state().get("cortex_nudged"):
        return None
    if "cortex" not in get_session_state().get("compliance_gaps", []):
        return None
    if get_session_state().get("cortex_used"):
        return None
    if get_session_state()["tool_calls"] < _CORTEX_NUDGE_THRESHOLD:
        return None
    get_session_state()["cortex_nudged"] = True
    # THIS LINE CONTRADICTED THE PACKET OUTRIGHT. "available" is permission
    # where the packet says ALWAYS; "faster and cheaper" is an efficiency
    # argument that invites the cost-benefit judgement the rule does not
    # offer; "your cortex usage is a compliance gap" is a scoreboard. The
    # agent resolves packet-vs-nudge toward the weaker statement, and the
    # nudge is the one it reads mid-task.
    return (
        "Reminder: find code with cortex_search() / cortex_route(). Grep and "
        "Glob are not the route for hunting code — this session has not used "
        "cortex yet."
    )


# Priority order: highest-severity nudge first. One per call.
_NUDGE_CHAIN = (
    _nudge_bootstrap_missing,
    _nudge_session_end,
    _nudge_behavioral,
    _nudge_cortex,
)


def _build_one_nudge() -> str | None:
    """Return at most ONE nudge, selected by priority. Avoids envelope saturation."""
    for producer in _NUDGE_CHAIN:
        msg = producer()
        if msg:
            return msg
    return None
