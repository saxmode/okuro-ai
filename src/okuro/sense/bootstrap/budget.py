# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Envelope enforcement for a bootstrap half — measured in chars, not estimated tokens.
# index: def estimate_tokens | def compress_section | def enforce_envelope
# AGENT_HEADER_END -->
"""Envelope enforcement for one bootstrap half.

WHAT THIS MODULE STOPPED BEING. It used to be a token-budget allocator:
``SECTION_PRIORITIES`` ranked ~30 sections against each other, ``allocate_budget``
distributed a token budget across them in three weighted rounds, and
``compress_section`` head-truncated whatever lost. All of that is deleted.

WHY IT IS DELETED AND NOT TUNED. The ranking existed because one packet had to
carry everything and could not. The packet is now a FIXED core plus a
separately-fetched project half (``contract.CORE_SECTIONS`` /
``contract.PROJECT_SECTIONS``), and each half fits its envelope alone with
thousands of chars to spare — measured 2026-08-27: core 22,580, largest project
half 34,692, envelope 48,000. With no scarcity there is no contest, so a
priority table is not a smaller version of the right answer; it is a leftover
that invites a future session to re-derive the same bug.

WHY CHARS. The old budget was denominated in tokens estimated as ``len(text) //
4``, and the host limit that actually bites is denominated in CHARACTERS. A
13,000-token budget was therefore a 52,000-char target aimed past a 50,000-char
wall — the packet was pointed at the cliff by arithmetic, not by drift.
Everything in this module now measures real ``len(str)``. ``estimate_tokens``
survives only because ``sections.py`` builders return a token estimate in their
tuple; nothing consumes it for a size decision.
"""

# Emitted by compress_section whenever it drops lines. Kept as a module
# constant so tests and packet-diffing tools can assert on the exact string
# rather than a substring guess.
_TRUNCATION_MARKER = "_[… {n} lines truncated to fit budget — section is INCOMPLETE]_"

# Emitted by enforce_envelope when a half exceeds its envelope. This is a BUG
# banner, not a routine notice: with the split in place no real half comes near
# the envelope, so seeing this means a section grew without bound.
_OVERFLOW_BANNER = (
    "## ⚠ PACKET OVER ENVELOPE — CONTENT WAS TRIMMED\n\n"
    "This half assembled to {actual:,} chars against a {limit:,}-char envelope. "
    "Sections below were head-truncated to fit and are INCOMPLETE. This is a "
    "defect in okuro, not a normal state — report it.\n\n{detail}"
)

# The bootstrap design's structural invariants live in ONE place — contract.py.
# UNCOMPRESSIBLE_SECTIONS (never head-truncate the operative/safety sections) is
# sourced from there so this module and the contract test cannot drift apart.
from .contract import UNCOMPRESSIBLE_SECTIONS  # noqa: E402,F401

# Minimum tokens per section (0 = can be dropped entirely). Retained as the
# human-auditable DECLARATION of which sections are non-negotiable —
# contract.MANDATORY_SECTIONS is asserted against ``{s for s, m in this if m > 0}``
# by the contract test, and that lock is what keeps a section from quietly
# becoming droppable.
SECTION_MINIMUMS = {
    "behavioral": 200,    # operative rules — never truncate below this
    "recipient": 80,
    "role_assignment": 300,   # inlined brief — the point is that it arrives whole
    "principles_core": 210,
    "profile": 100,
    "conventions": 50,
    "project": 0,
    "people": 0,      # droppable: demoted (0% relevance to technical hints).
                      # A nonzero floor made it mandatory-by-reservation, which
                      # contradicted the demotion — it could starve a real
                      # section under tight budget. 0 lets it yield first.
    "intake": 0,
    "hardware": 30,
    "protocol": 30,
    "tools": 300,         # agents MUST see available tools — never truncate below this
    "tool_protocol": 50,
    "principles": 50,
    # Was 0 — explicitly droppable. A packet with no memory is a packet with
    # no answer to the task. 250 tok floors it at ~3-4 pointers.
    "memory": 250,
    "memory_index": 0,
    "tunnels": 0,
    "todos": 0,           # action layer — drop entirely under tight budget rather than partially truncate
    "reminders": 0,       # same drop policy as todos
    "thoughts": 0,
    "progress": 0,
    "codebase": 0,
    "roles": 0,
    "during_work": 50,
    "session": 80,        # session compliance — non-negotiable
    "design_profile": 0,
    "stack": 0,
    "brand": 0,          # optional; renders only if a brand is bound
}

# The same declaration expressed in the unit the envelope actually uses.
#
# The ×4 is applied to a POLICY CONSTANT, once, here — never to measured text.
# That distinction is the whole point: `len(text) // 4` as a size ESTIMATE is
# what aimed a 13,000-token budget at a 50,000-char wall. A floor of "200" is a
# number someone chose; expressing that choice in chars is a unit change, not a
# measurement.
SECTION_MIN_CHARS = {name: tokens * 4 for name, tokens in SECTION_MINIMUMS.items()}


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~1 token per 4 characters.

    KEPT ONLY for the third element of every ``sections.py`` builder tuple,
    which nothing consumes for a size decision. Never use it to decide whether
    something fits: the limit that bites is measured in characters, and this
    function is the arithmetic that pointed the old packet past it.
    """
    if not text:
        return 0
    return max(1, len(text) // 4)


def compress_section(content: str, target_chars: int) -> str:
    """Truncate content to fit ``target_chars``. Keeps first N lines.

    UNIT CHANGED 2026-08-27: this took a token target and measured with
    ``estimate_tokens``. It now takes and measures CHARS, because chars are what
    the host envelope counts.

    A truncated section ALWAYS ends with a marker naming how many lines were
    dropped. Silent truncation was the defect: sections render header-first, so
    under budget pressure every heading survived and every body was cut, with
    nothing to distinguish a gutted section from a short one.

    The marker does not repair the truncation — it makes it visible, to the
    agent reading the packet and to anyone diffing it later.
    """
    if not content:
        return ""

    if len(content) <= target_chars:
        return content

    lines = content.split("\n")
    result: list[str] = []
    used = 0

    # Reserve room for the marker up front so emitting it cannot push the
    # section back over the size it was just compressed to fit.
    marker = _TRUNCATION_MARKER.format(n=len(lines))
    effective = max(target_chars - len(marker) - 1, 0)

    for line in lines:
        # +1 for the newline this line costs once joined.
        cost = len(line) + 1
        if used + cost > effective:
            break
        result.append(line)
        used += cost

    if not result and lines:
        # Degenerate case: the FIRST line alone is over target, so a line-wise
        # cut yields nothing but a marker — a section reduced to an apology,
        # far below its declared floor. Hard-cut mid-line instead. Rare (real
        # sections are multi-line) but the alternative is that one long line
        # silently deletes a whole section.
        result.append(lines[0][:effective])

    dropped = len(lines) - len(result)
    if dropped > 0:
        result.append(_TRUNCATION_MARKER.format(n=dropped))

    return "\n".join(result)


def enforce_envelope(
    rendered: dict[str, str],
    order: tuple[str, ...] | list[str],
    limit_chars: int,
    overhead_chars: int = 0,
) -> tuple[dict[str, str], str | None]:
    """Keep one assembled half inside ``limit_chars``, loudly.

    Args:
        rendered: section name -> rendered content, for this half only.
        order: the render order (only names present in ``rendered`` matter).
        limit_chars: the envelope this half must fit, measured in real chars.
        overhead_chars: chars this half spends outside ``rendered`` — header,
            banner, off-budget warnings — counted against the same envelope.

    Returns:
        ``(sections, banner)``. ``banner`` is None on the normal path. When it
        is not None the half overflowed: some sections were head-truncated and
        the banner names them, so the failure is visible in the packet instead
        of being discarded by the host.

    WHY THIS IS NOT AN ALLOCATOR. It does nothing at all unless the half is
    over the envelope, which — with the core/project split in place — no real
    half is. There is no ranking, no distribution, no per-section target on the
    happy path. The overflow path exists so that a future section that grows
    without bound degrades visibly rather than silently costing the agent the
    entire packet. Tests assert the happy path holds for real content; this is
    the belt under that brace.
    """
    names = [n for n in order if rendered.get(n)]
    joiner = 2  # "\n\n" between parts

    def _total() -> int:
        return overhead_chars + sum(len(rendered[n]) + joiner for n in names)

    actual = _total()
    if actual <= limit_chars:
        return rendered, None

    out = dict(rendered)
    trimmed: list[str] = []

    # Give back from the LARGEST trimmable section first: it is where the
    # overflow came from, and it is the one truncation costs least per char.
    # UNCOMPRESSIBLE_SECTIONS are never touched — they carry the user's
    # operative instructions rationale-first, so cutting the tail deletes
    # exactly the governing content, and `behavioral` carries the safety
    # boundaries. A size limit must not be able to delete a safety rule.
    candidates = sorted(
        (n for n in names if n not in UNCOMPRESSIBLE_SECTIONS),
        key=lambda n: len(out[n]),
        reverse=True,
    )

    for name in candidates:
        over = _total_for(out, names, overhead_chars, joiner) - limit_chars
        if over <= 0:
            break
        floor = SECTION_MIN_CHARS.get(name, 0)
        current = len(out[name])
        givable = max(current - floor, 0)
        if givable <= 0:
            continue
        take = min(givable, over)
        target = current - take
        before = current
        out[name] = compress_section(out[name], target)
        trimmed.append(f"- `{name}`: {before:,} → {len(out[name]):,} chars")

    remaining = _total_for(out, names, overhead_chars, joiner)
    detail = "\n".join(trimmed) if trimmed else (
        "- (nothing was trimmable — every section is at its declared floor)"
    )
    if remaining > limit_chars:
        detail += (
            f"\n\nSTILL OVER by {remaining - limit_chars:,} chars after trimming "
            "everything trimmable."
        )
    banner = _OVERFLOW_BANNER.format(actual=actual, limit=limit_chars, detail=detail)
    return out, banner


def _total_for(
    rendered: dict[str, str],
    names: list[str],
    overhead_chars: int,
    joiner: int,
) -> int:
    """Current char total for a half. Split out so the trim loop re-measures
    real content after every cut rather than trusting its own arithmetic."""
    return overhead_chars + sum(len(rendered[n]) + joiner for n in names)
