# SPDX-License-Identifier: Apache-2.0
"""Deterministic numeric reconciliation — the corrected analogue of
``term_consistency`` for numbers (Pillar 2 / Phase B).

LLMs are unreliable at multi-step arithmetic: procedural execution degrades
with step count and cross-references, and an LLM-as-judge that agrees with
ground truth <80% of the time is unfit as a CI gate. So number-heavy
deliverables (business plans, TCO scenarios, financial models) must NOT have
their totals judged by the Critic — they loop. Instead the producer emits a
**numbers ledger** (one place each number is written) and the reviewer
RECOMPUTES every declared total deterministically, BEFORE the LLM stages.

Ledger format — one fenced block in the deliverable body::

    ```numbers
    # facts: name = <arithmetic over numbers and earlier names>
    revenue_y1 = 1200
    revenue_y2 = 1800
    cost_y1    = 800
    total_revenue = 3000
    # checks: assert <expr> == <expr>  (recomputed; mismatch => load-bearing FAIL)
    assert total_revenue == revenue_y1 + revenue_y2
    assert margin_y1 == revenue_y1 - cost_y1
    margin_y1 = 400
    ```

Design choices (high precision, zero false positives):
  * Only EXPLICIT ``assert`` lines are verified — the gate never invents
    constraints, so a deliverable without a ledger (or without asserts) simply
    passes. It is harmless when absent and exact when present.
  * The expression evaluator is a hardened AST walker: numbers, names bound to
    facts, ``+ - * / // % **``, unary minus, and parentheses ONLY. No calls,
    attributes, comprehensions, or names outside the ledger. Untrusted text
    can never execute code here.
  * Float comparison uses a relative+absolute tolerance so legitimate rounding
    (e.g. CHF thousands) does not trip the gate.

Pure module: no okuro imports, no I/O. ``reconcile_text`` is the one entry the
deterministic check calls.
"""

from __future__ import annotations

import ast
import math
import re
from dataclasses import dataclass, field

# Fenced ```numbers ... ``` block(s). Tolerant of a language tag with trailing
# spaces and of ~~~ fences.
_LEDGER_BLOCK_RE = re.compile(
    r"(?:```|~~~)[ \t]*numbers[ \t]*\n(.*?)(?:```|~~~)",
    re.DOTALL | re.IGNORECASE,
)

# A fact assignment:  name = expression   (not an assert line).
_FACT_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*$")
# An assertion:  assert LHS == RHS   |   check: LHS == RHS
# Optional leading ``~<tol>`` sets an ABSOLUTE tolerance for THIS assert, e.g.
# ``assert ~1 total == a + b`` accepts up to ±1 of rounding residue when line
# items are presented rounded (P-NUM-1). Derived values (CAGR, margin, %) that
# carry rounding are better asserted with round() in the expression (P-NUM-2),
# e.g. ``assert cagr == round((end/start)**(1/7)*100 - 100, 1)``.
_ASSERT_RE = re.compile(
    r"^\s*(?:assert|check:)\s*(?:~\s*([0-9]*\.?[0-9]+)\s+)?(.+?)\s*==\s*(.+?)\s*$",
    re.IGNORECASE,
)

# Tolerance for float reconciliation (relative, with an absolute floor).
_REL_TOL = 1e-6
_ABS_TOL = 1e-6

_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)


class LedgerError(Exception):
    """Raised for a malformed ledger expression — surfaced as a finding, never
    propagated (a bad ledger is a deliverable defect, not a reviewer crash)."""


@dataclass
class Ledger:
    facts: dict[str, float] = field(default_factory=dict)
    # (lhs_src, rhs_src, abs_tol|None) assertion sources, in document order.
    asserts: list[tuple[str, str, "float | None"]] = field(default_factory=list)
    # parse-time errors (bad fact expression) — reported as findings.
    errors: list[str] = field(default_factory=list)


def _strip_inline_comment(s: str) -> str:
    # Drop a trailing ``# ...`` comment that is not inside the expression.
    h = s.find("#")
    return s[:h] if h != -1 else s


def _safe_eval(expr: str, facts: dict[str, float]) -> float:
    """Evaluate a pure-arithmetic expression over ``facts``. Raises LedgerError
    on any disallowed node or unknown name. Thousands separators (commas) and
    a leading currency unit are tolerated: ``1,200`` -> 1200, ``CHF 800`` is
    rejected (names must be ledger facts) — numbers carry no unit in a ledger.
    """
    # Strip ONLY thousands-grouping commas (a comma followed by exactly three
    # digits): "1,200" -> "1200", "1,200,000" -> "1200000". An argument comma
    # like round(16.5, 1) is left intact (it is not followed by 3 digits).
    cleaned = re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", expr)
    try:
        node = ast.parse(cleaned, mode="eval")
    except SyntaxError as exc:
        raise LedgerError(f"cannot parse {expr!r}: {exc}") from exc

    def _ev(n: ast.AST) -> float:
        if isinstance(n, ast.Expression):
            return _ev(n.body)
        if isinstance(n, ast.Constant):
            if isinstance(n.value, bool) or not isinstance(n.value, (int, float)):
                raise LedgerError(f"non-numeric constant {n.value!r}")
            return float(n.value)
        if isinstance(n, ast.Name):
            if n.id not in facts:
                raise LedgerError(f"unknown name {n.id!r}")
            return facts[n.id]
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.UAdd, ast.USub)):
            v = _ev(n.operand)
            return +v if isinstance(n.op, ast.UAdd) else -v
        if isinstance(n, ast.BinOp) and isinstance(n.op, _ALLOWED_BINOPS):
            l, r = _ev(n.left), _ev(n.right)
            if isinstance(n.op, ast.Add):
                return l + r
            if isinstance(n.op, ast.Sub):
                return l - r
            if isinstance(n.op, ast.Mult):
                return l * r
            if isinstance(n.op, ast.Div):
                return l / r
            if isinstance(n.op, ast.FloorDiv):
                return l // r
            if isinstance(n.op, ast.Mod):
                return l % r
            if isinstance(n.op, ast.Pow):
                return l ** r
        # round(x[, ndigits]) — the ONLY allowed call. Lets a derived value
        # (CAGR, margin, %) be asserted exactly against its presented, rounded
        # form (P-NUM-2) without loosening the global tolerance.
        if isinstance(n, ast.Call):
            if (
                isinstance(n.func, ast.Name)
                and n.func.id == "round"
                and not n.keywords
                and 1 <= len(n.args) <= 2
            ):
                x = _ev(n.args[0])
                ndigits = int(_ev(n.args[1])) if len(n.args) == 2 else 0
                return float(round(x, ndigits))
            raise LedgerError("only round(x[, ndigits]) calls are allowed")
        raise LedgerError(f"disallowed expression element: {type(n).__name__}")

    return _ev(node)


def parse_ledger(block: str) -> Ledger:
    """Parse one ledger block body into facts + asserts. Order matters: a fact
    may reference earlier facts. Assert lines are collected, not evaluated."""
    led = Ledger()
    for raw in block.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.strip().startswith("#"):
            continue
        m_assert = _ASSERT_RE.match(line)
        if m_assert:
            tol = float(m_assert.group(1)) if m_assert.group(1) else None
            led.asserts.append((m_assert.group(2), m_assert.group(3), tol))
            continue
        m_fact = _FACT_RE.match(line)
        if m_fact:
            name = m_fact.group(1)
            rhs = _strip_inline_comment(m_fact.group(2)).strip()
            try:
                led.facts[name] = _safe_eval(rhs, led.facts)
            except LedgerError as exc:
                led.errors.append(f"fact {name!r}: {exc}")
            continue
        # Unrecognized non-comment line — report but do not fail hard.
        led.errors.append(f"unparsable ledger line: {line.strip()[:120]}")
    return led


def _close(a: float, b: float, abs_tol: "float | None" = None) -> bool:
    return math.isclose(
        a, b, rel_tol=_REL_TOL,
        abs_tol=(abs_tol if abs_tol is not None else _ABS_TOL),
    )


def reconcile_ledger(block: str) -> list[dict]:
    """Reconcile one ledger block. Returns a list of finding dicts (empty =>
    consistent). Each finding is deterministic and carries the exact numbers."""
    led = parse_ledger(block)
    findings: list[dict] = []
    for err in led.errors:
        findings.append({
            "evidence": err,
            "kind": "ledger_parse_error",
            "suggested_fix": (
                "Fix the numbers-ledger syntax: each line is `name = <number "
                "or arithmetic over earlier names>` or `assert lhs == rhs`."
            ),
        })
    for lhs_src, rhs_src, abs_tol in led.asserts:
        try:
            lhs = _safe_eval(lhs_src, led.facts)
            rhs = _safe_eval(rhs_src, led.facts)
        except LedgerError as exc:
            findings.append({
                "evidence": f"assert {lhs_src} == {rhs_src} — {exc}",
                "kind": "ledger_assert_error",
                "suggested_fix": (
                    "Reference only names defined as facts in the same ledger; "
                    "the only call allowed is round(x[, ndigits])."
                ),
            })
            continue
        if not _close(lhs, rhs, abs_tol):
            _tol_note = f", tol ±{abs_tol:g}" if abs_tol is not None else ""
            findings.append({
                "evidence": (
                    f"ledger assertion FAILED: {lhs_src.strip()} == "
                    f"{rhs_src.strip()}  →  {lhs:g} != {rhs:g} "
                    f"(off by {lhs - rhs:g}{_tol_note})"
                ),
                "kind": "ledger_mismatch",
                "lhs": lhs,
                "rhs": rhs,
                "suggested_fix": (
                    f"The recomputed value is {rhs:g}; the declared/derived "
                    f"value is {lhs:g}. Correct the wrong number in the ledger "
                    f"so the deliverable's totals are arithmetically sound."
                ),
            })
    return findings


def reconcile_text(text: str) -> list[dict]:
    """Find every ```numbers block in ``text`` and reconcile each. Empty list
    means: no ledger present, or all ledgers consistent — both PASS. This is
    the single entry point the deterministic ``numeric_consistency`` check
    calls; it never raises."""
    if not text or "numbers" not in text.lower():
        return []
    findings: list[dict] = []
    for i, m in enumerate(_LEDGER_BLOCK_RE.finditer(text)):
        for f in reconcile_ledger(m.group(1)):
            f.setdefault("ledger_index", i)
            findings.append(f)
    return findings
