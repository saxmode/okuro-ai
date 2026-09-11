# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: The bootstrap packet CONTRACT — the design pinned in one place.
# index: constants | def required_markers_for | def verify_required_markers
# AGENT_HEADER_END -->
"""The bootstrap packet CONTRACT — the design, pinned in ONE place.

Why this module exists. The packet's design used to live scattered across
modules — section priorities and minimums and the uncompressible set
(budget.py), and the completeness gate's marker list (mcp_tools.py). Nothing
tied them together, so a session fixing one task could change a priority, a
minimum, or the gate and drift the DESIGN with no single test noticing. That is
how the packet came to brief agents wrong.

This module is the single source of truth for what an assembled packet MUST
guarantee. The completeness gate (mcp_tools), the assembler and its envelope
enforcer (budget.py), and the contract test
(tests/sense/test_bootstrap_contract.py) all read from HERE. Changing the packet's design now means editing THIS file — a deliberate,
reviewable act — and anything that removes a guarantee below fails the contract
test LOUDLY instead of silently shipping a degraded packet to an agent.

Nothing here imports from its siblings: this is a leaf module, so the gate, the
assembler and the envelope enforcer can all depend on it without an import
cycle.
"""

from __future__ import annotations

# Budget tiers that USED to ship (orchestrator/config.py bootstrap_budget:
# fast=2000, standard/strategic=5000, max=13000). Retained because callers may
# still pass a `budget`, and the contract must hold whatever they pass — the
# packet is now a FIXED core plus a separately-fetched project half, so nothing
# competes for a budget and nothing is ranked. `budget` is accepted and ignored.
# Measured 2026-08-27: no production caller reads orchestrator config's
# bootstrap_budget into assemble(); the only live value was the 13000 default.
SHIPPING_TIERS: tuple[int, ...] = (2000, 5000, 13000)

# ---------------------------------------------------------------------------
# The core / project SPLIT — the reason this packet stopped fitting
# ---------------------------------------------------------------------------
#
# ROOT CAUSE, measured 2026-08-27. `estimate_tokens` is `len(text) // 4`, and
# the default budget was 13,000 tokens — a 52,000-CHAR target aimed at a host
# whose result envelope is 50,000 chars. Over that line the host discards the
# whole result and substitutes a ~2,000-char preview, so the agent boots with
# nothing. Live packets: okuro 51,056 chars; the largest registered project
# 57,314 chars. Both over.
#
# THE FIX IS A SPLIT, NOT A TUNE. The two halves answer different questions and
# have different lifetimes:
#
#   CORE     — session-invariant. Identical for every task on this machine:
#              who the user is, how to behave, what tools exist, how to close
#              out. Measured 22,580 chars with no task hint at all.
#   PROJECT  — everything that varies with the resolved project and task hint:
#              memory, todos, reminders, progress, people, thoughts, the role
#              assignment, brand/design/stack, the codebase map.
#
# Each half fits a 50,000-char envelope alone with room to spare; only the sum
# broke. Splitting removes the priority contest entirely — which is why
# SECTION_PRIORITIES and allocate_budget are GONE rather than retuned. There is
# nothing left to rank: the core is fixed and fits with ~25,000 chars of
# headroom.
#
# ORDER IS THE READING ORDER. These tuples are ordered, and the assembler
# renders in exactly this order. Both preserve the relative order the deleted
# SECTION_PRIORITIES table produced, so the split is a partition of the old
# packet and not a re-layout of it.
CORE_SECTIONS: tuple[str, ...] = (
    "behavioral",
    "recipient",
    "principles_core",
    "profile",
    "conventions",
    "protocol",
    "tools",
    "tool_protocol",
    "hardware",
    "during_work",
    "session",
    "principles",
    "roles",
)

PROJECT_SECTIONS: tuple[str, ...] = (
    "role_assignment",
    "memory",
    "project",
    "intake",
    "todos",
    "reminders",
    "memory_index",
    "people",
    "tunnels",
    "brand",
    "design_profile",
    "stack",
    "thoughts",
    "progress",
    "codebase",
    "distill_candidates",
    "recent",
)

# Every section the assembler can build. The partition test asserts this is
# EXACTLY the assembler's builder set — a new builder that lands in neither half
# would otherwise be silently unreachable.
ALL_SECTIONS: tuple[str, ...] = CORE_SECTIONS + PROJECT_SECTIONS

# ---------------------------------------------------------------------------
# The envelope — measured in CHARS, never in estimated tokens
# ---------------------------------------------------------------------------
#
# HOST_RESULT_LIMIT_CHARS is the cliff: Claude Code replaces any tool result
# longer than this with a short preview. ENVELOPE_CHARS is where okuro trips
# FIRST, so an oversized half is okuro's loud, in-packet failure rather than
# the host's silent substitution. The 2,000-char gap is deliberate slack for
# the off-budget blocks (stale-code warning, security alert, degraded-mode
# summary) that are appended after the sections are measured.
HOST_RESULT_LIMIT_CHARS: int = 50_000
ENVELOPE_CHARS: int = 48_000

# The margin between the host's cliff and okuro's own trip line. Named so the
# per-provider table below derives its envelope instead of restating it.
ENVELOPE_MARGIN_CHARS: int = HOST_RESULT_LIMIT_CHARS - ENVELOPE_CHARS

# PER-PROVIDER, because the cliff is the HOST's, not okuro's. One constant
# taken from one provider's measured behaviour and applied to all of them is
# the same shape of bug as the token budget that aimed at a char wall: a number
# true somewhere, asserted everywhere.
#
# MEASURED — Claude Code 2.1.226, bracketed 49,459 < T <= ~51,700 bytes, and
# 50,000 is the working value the split was built against and verified on.
#
# ASSUMED — every other provider. They inherit the same number because it is
# the only one anyone has measured, NOT because it was checked for them. That
# is why they are absent from the table rather than listed with a copied value:
# an entry here means somebody measured it. Add one when they do.
HOST_RESULT_LIMITS: dict[str, int] = {
    "claude-code": 50_000,
}

# Fraction of the envelope at which a half is REPORTED as growing, long before
# anything is trimmed. The split left ~20,000 chars of headroom on the largest
# half (measured 2026-09-03: core 22,698, worst project half 27,822), and
# nothing observes that headroom shrinking — the first signal under the old
# design was content already being cut. A trip line at three quarters turns
# "we lost a section" into "a section is growing", which is a fact somebody can
# still act on.
ENVELOPE_WARN_FRACTION: float = 0.75


def host_limit_for(provider: str | None) -> int:
    """The host result cliff for this provider, in chars.

    Falls back to the measured Claude Code value for anyone not in the table.
    The fallback is deliberately the SAME number, so behaviour does not change
    for an unmeasured provider — what changes is that adding a measurement is
    now a one-line edit instead of a refactor.
    """
    if provider:
        measured = HOST_RESULT_LIMITS.get(provider.strip().lower())
        if measured:
            return measured
    return HOST_RESULT_LIMIT_CHARS


def envelope_for(provider: str | None) -> int:
    """The char budget one half must fit for this provider.

    Always below :func:`host_limit_for` by ``ENVELOPE_MARGIN_CHARS`` so okuro
    trips first, loudly and in-packet, rather than the host silently swapping
    the whole result for a preview.
    """
    return max(host_limit_for(provider) - ENVELOPE_MARGIN_CHARS, 1)


def warn_threshold_for(provider: str | None) -> int:
    """Chars at which a half is reported as growing toward its envelope."""
    return int(envelope_for(provider) * ENVELOPE_WARN_FRACTION)

# Markers the completeness gate REFUSES a packet for missing, on the default
# (unfiltered) path every real session uses. Without ## Behavioral Contract an
# agent has no operative rules; without ## MCP Tools, no toolbox; without
# ## Session, no close-out contract — and must never be marked bootstrapped. A
# FILTERED bootstrap (sections=[...]) is a diagnostic special-case that keeps
# only the behavioral requirement, so intentional single-section fetches work.
REQUIRED_MARKERS: tuple[str, ...] = (
    "## Behavioral Contract", "## MCP Tools", "## Session",
)
FILTERED_REQUIRED_MARKERS: tuple[str, ...] = ("## Behavioral Contract",)

# Sections that must NEVER be head-truncated: they carry the user's operative
# instructions rationale-first, directives-last, so cutting the tail deletes
# exactly the governing content. `behavioral` also carries the safety
# boundaries. The assembler skips compression for these.
UNCOMPRESSIBLE_SECTIONS: frozenset[str] = frozenset(
    {"behavioral", "principles_core", "role_assignment", "recipient"}
)

# The sections the completeness gate's markers map to. All three live in the
# CORE half, which is exactly what makes the core a valid packet on its own: a
# bootstrap that returns only the core still satisfies verify_required_markers.
# Asserted by the split test.
GATE_CRITICAL_SECTIONS: frozenset[str] = frozenset({"behavioral", "tools", "session"})

# The gate markers plus the never-truncate safety set. Since the allocator was
# deleted (nothing to rank once the packet is split) this is no longer a
# reservation ORDER — it is the set the envelope enforcer may never trim and the
# gate may never be missing.
RESERVED_FIRST: frozenset[str] = UNCOMPRESSIBLE_SECTIONS | GATE_CRITICAL_SECTIONS

# Sections the design treats as non-negotiable (a nonzero SECTION_MINIMUMS in
# budget.py). Declared here as the human-auditable list; the contract test
# asserts it stays identical to that table's own {min > 0} set, so bumping a
# minimum to 0 (dropping a section from the mandatory set) or adding a new
# mandatory section fails the test until this list is consciously updated.
MANDATORY_SECTIONS: frozenset[str] = frozenset({
    "behavioral", "recipient", "role_assignment", "principles_core", "profile",
    "conventions", "hardware", "protocol", "tools", "tool_protocol",
    "principles", "memory", "during_work", "session",
})

# Operative profile rules that MUST appear in the Behavioral Contract when the
# user's profile defines them. These are the lines that GOVERN a response — what
# this subsystem exists to deliver intact. Pinned by rendered label, so a
# renderer regression (the error_handling line silently vanished once — audit
# NF-1) fails the contract test. `When the user is frustrated` is the
# frustration/anger protocol; `## Boundaries` is the "never without asking"
# safety block. Both reach an agent ONLY through the behavioral section.
REQUIRED_OPERATIVE_RULES: tuple[str, ...] = (
    "**Response length:**",
    "**Tone:**",
    "**When the user is frustrated**",
    "**Interruption policy**",
    "**Error handling:**",
    "## Boundaries",
)


def required_markers_for(*, filtered: bool) -> tuple[str, ...]:
    """The markers a packet must carry. A filtered bootstrap is a diagnostic
    special-case that keeps only the behavioral requirement."""
    return FILTERED_REQUIRED_MARKERS if filtered else REQUIRED_MARKERS


def verify_required_markers(packet: str, *, filtered: bool) -> list[str]:
    """Return the required markers ABSENT from `packet` (empty list = valid)."""
    return [m for m in required_markers_for(filtered=filtered) if m not in packet]
