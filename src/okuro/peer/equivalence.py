# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: peer.equivalence — informational-equivalence gate for rewrites.
#   A per-recipient translation may change format, tone, length and
#   vocabulary. It may not change WHAT IS TRUE. This extracts the atoms a
#   rewrite must preserve and reports what a translation dropped.
# index: Atom kinds | extract_atoms | equivalence_report | equivalence_clause
# AGENT_HEADER_END -->
"""Did the rewrite keep every fact?

``person_translate`` hands an LLM a source text and asks for it back in
another register. Its own prompt says "preserve every fact and every ask",
and until now nothing checked. A dropped figure, a lost deadline or a
vanished URL reads as a perfectly fluent message — the failure mode is
invisible precisely because the output is well written, which is the
worst possible shape for a defect in a delivery path.

DETERMINISTIC ON PURPOSE. Asking a second LLM whether the first one kept
the facts adds a second thing that can be wrong and no way to tell which.
Numbers, dates, URLs and money are extractable by pattern with no
judgement, and they are what a rewrite actually loses. This is a smoke
alarm, not a proof: it cannot see a dropped *nuance*, and it does not
claim to.

WHAT IT DOES NOT DO: reject. It reports. A rewrite that drops an atom may
be correct — the atom may have been in a sentence the recipient's profile
says to suppress. The caller decides; the gate makes the loss visible
instead of silent.
"""

from __future__ import annotations

import re
from typing import Any

# Atoms a faithful rewrite must carry through. Each pattern is deliberately
# narrow: a false alarm costs a reader's attention, and this runs on every
# translation.
_PATTERNS: dict[str, re.Pattern[str]] = {
    # http(s) links — a dropped link is a dropped call to action
    "url": re.compile(r"https?://[^\s<>\"'\)\]]+"),
    # ISO dates and common written dates
    "date": re.compile(
        r"\b\d{4}-\d{2}-\d{2}\b"
        r"|\b\d{1,2}[./]\d{1,2}[./]\d{2,4}\b"
        r"|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4}\b",
        re.IGNORECASE,
    ),
    # money, with the symbol or code attached either side
    "money": re.compile(
        r"(?:(?:CHF|EUR|USD|GBP|\$|€|£)\s?\d[\d'’,. ]*\d|\d[\d'’,. ]*\d\s?(?:CHF|EUR|USD|GBP))",
        re.IGNORECASE,
    ),
    "percent": re.compile(r"\b\d+(?:[.,]\d+)?\s?%"),
}

# Bare numbers are extracted last, after the richer kinds have claimed their
# spans, so "CHF 1'200" does not also surface as the number 1200.
_NUMBER = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)?(?![\w%])")


def _normalise(kind: str, raw: str) -> str:
    """Canonical form, so formatting differences are not reported as loss.

    A rewrite is ALLOWED to reformat: "1'200 CHF" and "CHF 1200" are the
    same fact, and flagging that would train the reader to ignore the gate.
    """
    text = raw.strip().lower()
    if kind in ("money", "percent", "number"):
        # Strip grouping marks and unify the decimal comma.
        text = re.sub(r"[\s'’]", "", text)
        text = re.sub(r",(?=\d{3}\b)", "", text)
        text = text.replace(",", ".")
        text = re.sub(r"(\.\d*?)0+\b", r"\1", text)
        text = re.sub(r"\.$", "", text)
    if kind == "url":
        text = text.rstrip("/.,;")
    return text


def extract_atoms(text: str | None) -> dict[str, set[str]]:
    """``{kind: {normalised atom}}`` for one text. Never raises."""
    out: dict[str, set[str]] = {k: set() for k in (*_PATTERNS, "number")}
    if not isinstance(text, str) or not text.strip():
        return out
    claimed: list[tuple[int, int]] = []
    for kind, pattern in _PATTERNS.items():
        for m in pattern.finditer(text):
            out[kind].add(_normalise(kind, m.group()))
            claimed.append(m.span())
    for m in _NUMBER.finditer(text):
        start, end = m.span()
        if any(s <= start and end <= e for s, e in claimed):
            continue
        out["number"].add(_normalise("number", m.group()))
    return out


def equivalence_report(source: str | None, rewritten: str | None) -> dict[str, Any]:
    """What the rewrite dropped, and what it invented.

    ``dropped`` is the real signal. ``added`` is weaker but worth having:
    a figure in the output that was not in the input is a hallucination,
    and this is the cheapest place in the pipeline to notice one.

    ``equivalent`` is True only when nothing was dropped. An empty source
    is trivially equivalent — there was nothing to lose.
    """
    src = extract_atoms(source)
    out = extract_atoms(rewritten)
    dropped = {k: sorted(v - out[k]) for k, v in src.items() if v - out[k]}
    added = {k: sorted(v - src[k]) for k, v in out.items() if v - src[k]}
    return {
        "equivalent": not dropped,
        "dropped": dropped,
        "added": added,
        "atoms_checked": sum(len(v) for v in src.values()),
    }


def equivalence_clause(report: dict[str, Any] | None) -> str:
    """One line for a log or a caller, or "" when there is nothing to say."""
    if not report or report.get("equivalent"):
        return ""
    bits = [f"{kind}: {', '.join(vals)}" for kind, vals in
            (report.get("dropped") or {}).items()]
    return "rewrite dropped — " + " · ".join(bits)
