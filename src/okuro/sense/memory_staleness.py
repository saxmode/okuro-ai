# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Detect memories the CODE has since contradicted — the tooling
#   ORCH-RETIRE-ON-FIX assumed existed. Flags for review; never demotes.
# index: imports | extraction | checks | scan | main
# AGENT_HEADER_END -->
"""Find memories that the code they describe has moved on from.

Why this exists. On 2026-07-13 a session measured the memory-recall outage
exactly right — "similarities are COMPRESSED, tops out ~0.15-0.25", "the 0.4
floor returns ZERO scored rows and silently falls back to recency" — then
diagnosed it as bad embeddings rather than a vec0 table declared L2 and scored
as cosine. It filed that at confidence 0.9 and prescribed a workaround: bypass
_read_memory_rows, use your own ~0.05 floor. The symptoms were real, the cause
was wrong, and the memory ENSHRINED the bug: anyone who later hit the same
numbers would read it and stop looking. It also reached code — a branch still
hard-codes min_similarity=0.05 on that rationale.

That memory was found by a human reading recall output, two months late.
ORCH-RETIRE-ON-FIX says to supersede a gotcha when its bug is fixed, but
nothing connected a code change to the memories describing the old code, so
the principle relied on somebody remembering. This closes that: memories cite
paths and literals, and both are checkable.

What it does NOT do: judge truth, and never write. A memory is prose; only the
mechanical claims inside it are decidable. So this reports four kinds, in
descending strength, and a human or agent decides:

  contradicted_literal  PROOF. The memory says ``X = a``; the code says
                        ``X = b``. Skipped when the memory also mentions ``b``
                        (it is narrating the change, not asserting the old
                        value) — that check is what keeps a corrected memory
                        from flagging itself forever. The symbol search is
                        SCOPED to the files the memory itself names; a
                        same-named constant elsewhere in the tree is not a
                        contradiction of this memory.
  dangling_path         PROOF. The memory cites a file that no longer exists
                        AND was written before that file died. A memory
                        written AFTER the deleting commit is citing history
                        deliberately and is not reported.

  drifted_anchor        PROOF. The memory cites ``path:LINE`` and names a
                        ``def``/``class`` that still exists in that file but
                        now sits far from the cited line. The POINTER is stale
                        whatever the prose argued. Added 2026-09-09 after a
                        memory citing ``vectorstore.py:204`` was repeated as
                        current fact four months after the branch it described
                        moved and a fallback above it made its conclusion
                        false — invisible to all three checks above, because
                        it pinned no literal and its file still existed.

  file_changed_since    SUSPECT, not proof. The cited file was modified after
                        the memory was written. Most such memories are fine.
                        Triage only — never act on this alone.

Both of those qualifiers were added on 2026-07-28 after an audit where 4 of 13
proof findings were artifacts rather than stale memories. They share one root
cause, and it is the thing to keep in mind before adding another check here:
the scanner was attributing a CODE fact to a memory that never asserted it.
Before reporting anything as proof, confirm the memory actually makes the
claim being contradicted.

A memory carrying ``supersedes`` is a correction some agent already
adjudicated. Its findings stay visible but drop to SUSPECT, so a finished
cleanup can actually reach ``proof_count == 0`` instead of reporting its own
retractions back as outstanding work forever.

PRECISION IS THE PRODUCT. A sibling deterministic check, term_consistency,
learned this the expensive way: it tokenized parenthetical config detail and
pulled "viewport" out of "(file:// URL, 1440px viewport, headless=false)",
which matched an unrelated HTML deliverable and produced an unrecoverable
block loop. Its fix was to STRIP parentheticals entirely. The parenthetical
matcher here deliberately goes the other way — but only anchored to a SYMBOL
name and only when the value starts with a digit or a quote, because nothing
downstream filters a `proof` finding. Agents act on it directly.

Deliberately no auto-demote. Demoting on a heuristic is how a cleanup becomes
a regression: 201 near-duplicate memory pairs were measured on 2026-07-15 and
only 34 passed mechanical filters — and even those included distinct facts
("Maintenance run: 2026-05-10" vs "2026-05-31" score 0.988). Mechanical
signals find candidates. They do not settle meaning.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

# Repo root: src/okuro/sense/memory_staleness.py -> up 3
_REPO = Path(__file__).resolve().parents[3]

# A path that looks like okuro source. Matched on the src/okuro/... tail so a
# prose-embedded absolute path ("~/okuro/src/okuro/sense/x.py")
# and a repo-relative one both reduce to the same key — the first cut of this
# regex prefixed "src/" onto an already-absolute match and reported live files
# as missing.
_PATH_RE = re.compile(r"(src/okuro/[\w/]+\.(?:py|sql))\b")

# `src/okuro/x/y.py:204` — the same path WITH the line anchor kept.
#
# THE GAP THIS CLOSES, and it is the shape that got past every other generator
# here on 2026-09-09. A memory read vectorstore.py:204, described the skip
# branch it found there, and drew a conclusion. The branch later moved and a
# fallback was added above it that made the conclusion false. Checked against
# this file's three existing generators the memory looked PERFECT:
#
#   contradicted_literal  it pinned no NAME = value, so nothing to compare
#   dangling_path         vectorstore.py still exists, so nothing dangling
#   file_changed_since    suspect only, and the convention says never act on
#                         it alone — correctly, since every live file changes
#
# So a memory can cite a real file, assert no literal, and still be wrong about
# what the code DOES. An agent then repeats its conclusion as current fact,
# which is exactly what happened: the claim "CSS and comment-free TSX are not
# indexed" was reported to the user four months after the fallback landed.
#
# A general "does this conclusion still hold" check needs to understand the
# code and is not a regex. What IS mechanically provable is narrower and is
# what this generator claims: the memory pointed at a LINE, named a SYMBOL, and
# that symbol is no longer anywhere near that line. The pointer is stale
# whatever the prose around it argued.
_PATH_LINE_RE = re.compile(r"(src/okuro/[\w/]+\.py):(\d{1,6})\b")

# Identifiers the memory mentions anywhere in its prose — candidates for "the
# thing the line anchor points AT".
#
# DELIBERATELY WIDE HERE, NARROWED BY THE CODE. The first cut required the
# literal words `def `/`class ` in the prose, and measured against the live
# corpus that gave the generator a surface of 10 memories out of 5569 — it
# would not even have caught the memory it was written for, which said
# "vectorstore.index_file at src/okuro/cortex/vectorstore.py:204 returns
# False". Agents write `module.symbol` and bare `snake_case`, not `def x`.
#
# The precision does not come from this pattern. It comes from the check that
# follows: a candidate only counts if it is ACTUALLY a def/class in the file
# the memory itself cited. That is the same scoping rule contradicted_literal
# uses, and it is what stops a stray word being read as a claim.
_SYMBOL_MENTION_RE = re.compile(
    r"(?:\b(?:def|class)\s+)?\b([a-zA-Z_]\w{3,})\b"
)

# How far a cited line may drift before the pointer is called stale.
#
# Deliberately loose. Edits above a function shift it constantly, and a
# detector that fires on ordinary drift is the "goes blind by going loud"
# failure this module already paid for once. 120 lines is far enough that
# ordinary churn stays quiet and a symbol that has genuinely relocated — the
# measured case moved 361 lines — still reports.
_ANCHOR_DRIFT_TOLERANCE = 120

# `NAME = 0.4` / `NAME=0.4` / `NAME = "cosine"`. Uppercase-ish identifiers
# only: lowercase `x = 1` in prose is noise, constants are what memories pin.
#
# The value charset admits a COMMA so a thousands-separated number arrives
# whole. Without it `36,000` matched as `36`, which then "contradicted" the
# code's `36_000` — measured 2026-09-03, and the same shape hit a second
# constant in the same memory. Any number over 999 written the way a human
# writes it tripped this.
_LITERAL_RE = re.compile(
    r"\b(_?[A-Z][A-Z0-9_]{3,})\s*=\s*([\"']?[\w.\-]+(?:,\d{3})*[\w.\-]*[\"']?)"
)

# `NAME (0.75, new constant in clarity.py)` — the PARENTHETICAL form, which is
# how a memory names a value while still writing prose. The assignment matcher
# above cannot see it, and that blind spot cost real money: a memory describing
# the smart-gate deliberation band wrote
#     "0.6 <= confidence < DELIBERATE_CONFIDENCE_CEILING (0.75, new constant)"
# and stayed top-ranked and uncorrected for two months after the band was
# retired the same day it shipped. A full scan of 3,141 memories produced 10
# proofs and missed that one entirely — it was found by a human reading code.
#
# DELIBERATELY NARROW: the value must START with a digit or a quote. `FOO (see
# below)` and `STEP (a)` are prose, not claims. Requiring a literal keeps this
# from becoming the noise generator that gets the whole tool switched off —
# precision is the product here, and 4 of 13 findings in the 2026-07-28 audit
# were already artifacts.
_PAREN_LITERAL_RE = re.compile(
    r"\b(_?[A-Z][A-Z0-9_]{3,})\s*\(\s*"
    r"([\"']?-?\d[\w.\-]*(?:,\d{3})*[\w.\-]*[\"']?|[\"'][\w.\-]+[\"'])\s*[,)]"
)

_DATE_LIKE = re.compile(r"^\d{4}-\d{2}(-\d{2})?")

_NUMERIC = re.compile(r"^-?\d+(\.\d+)?$")

# A unit a memory writes onto a number that the code carries in its NAME, not
# its value. Measured 2026-09-03 over a full scan (4,872 memories, 16 proof
# findings): `_DAEMON_RECONCILE_GRACE_S = 300` documented as "300s" and
# `RELAYOUT_TOLERANCE = 24.0` documented as "24px" were both reported as PROOF
# of a contradiction. They are the same magnitude in two renderings. A checker
# that calls a rendering difference "proof" spends the reader's trust on
# nothing, and the reader is the scarce resource here.
_UNIT_SUFFIX_RE = re.compile(
    r"^(-?\d+(?:\.\d+)?)\s*"
    r"(s|ms|us|ns|sec|secs|min|h|d|px|rem|em|pt|%|k|kb|mb|gb|tb|b|c|ch|chars|bytes)$",
    re.IGNORECASE,
)

# Words that say a path is SUPPOSED to be missing. A memory whose subject is a
# deletion ("grid.py IS GONE", "these endpoints NEVER LANDED") names the path
# precisely because it is asserting the absence — the dangling-path check then
# reports the memory's own conclusion back as a defect. The date rule above
# catches this only when git can date the death AND the memory postdates it;
# measured 2026-09-03, three of four dangling_path proofs slipped through it.
_ABSENCE_RE = re.compile(
    r"\b(deleted|deletes|gone|removed|never landed|never committed|never added|"
    r"no longer exists?|does not exist|doesn't exist|retraction|retracted|"
    r"is dead|absent|fictional|unbuilt|replaced by|superseded by)\b",
    re.IGNORECASE,
)

# Markers that scope the WHOLE memory, not one sentence of it. Two shapes, one
# rule: the memory has already told the reader why this path is not in the main
# checkout, so reporting its absence tells nobody anything.
#
#   RETRACTION / NEVER LANDED — the memory's subject IS the absence. The
#   windowed check misses these when the retraction headline and the path sit
#   more than 200 chars apart, which in a real retraction they usually do: the
#   headline is at the top and the inventory of absent paths is further down.
#
#   NOT MERGED / branch / worktree — the code exists, on a ref this scanner
#   does not check. `_REPO` is the main checkout, so every branch-scoped
#   memory reports its own files as dangling. Measured 2026-09-03: one such
#   memory produced three of the four surviving proofs.
_SCOPED_ELSEWHERE_RE = re.compile(
    r"(RETRACTION|NEVER LANDED|NOT MERGED|IS GONE|ARE GONE|"
    r"\bworktree\s+/|\bon branch\s+[\w./-]+|\bbranch\s+(?:feat|fix|chore)/)",
    re.IGNORECASE,
)


def _claimed_literals(content: str) -> list[tuple[str, str]]:
    """Every `SYMBOL -> claimed value` pair a memory asserts, both forms.

    Deduped on (name, value): a memory that writes the constant twice, once
    assigned and once parenthetically, is making ONE claim and must not
    produce two identical findings.
    """
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for pattern in (_LITERAL_RE, _PAREN_LITERAL_RE):
        for name, claimed in pattern.findall(content):
            if _DATE_LIKE.match(claimed.strip().strip("\"'")):
                # `VALIDATED (2026-06-28, re-checked)` is a memory stamping WHEN
                # it verified something, not claiming the constant equals a
                # date. Caught live on the first run of the parenthetical
                # matcher: it reported VALIDATED says 2026-06-28 / code says
                # "validated", which is two different facts wearing one name.
                # Memories date themselves constantly, so without this the new
                # form would generate steady noise — and a checker people learn
                # to skim is worse than one that misses.
                continue
            key = (name, claimed.strip())
            if key not in seen:
                seen.add(key)
                out.append((name, claimed))
    return out


def _iter_code_literals(name: str) -> list[tuple[str, str]]:
    """Every real `name = <bare literal>` binding in tracked python.

    Returns ONLY assignments whose right-hand side is a bare literal, and
    skips comments. Both filters are load-bearing:

    * Comments mentioning the constant get matched by any line-anchored grep —
      a docstring line about `_SIM_FLOOR` yielded a "value" of prose.
    * A computed RHS (`X = _env_floor()`) is not a claim this can adjudicate.

    Leaving either in poisoned the caller's all()-must-conflict test: one
    unusable "value" made a genuine contradiction look non-unanimous, so the
    finding was dropped. The detector then missed the very memory it was
    written for — a defensive filter swallowing its own signal, which is the
    exact failure it exists to catch.
    """
    try:
        out = subprocess.run(
            ["git", "grep", "-n", "-E", rf"^\s*{re.escape(name)}\s*=", "--",
             "*.py"],
            cwd=_REPO, capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception:
        return []
    hits: list[tuple[str, str]] = []
    for line in out.splitlines():
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        path, _, code = parts
        if code.lstrip().startswith("#"):
            continue
        m = re.match(rf"\s*{re.escape(name)}\s*=\s*(.+?)\s*(?:#.*)?$", code)
        if not m:
            continue
        val = m.group(1).strip()
        if not re.fullmatch(r"[\"']?[\w.\-]+[\"']?", val):
            continue  # computed/complex RHS — not adjudicable
        hits.append((path, val))
    return hits


def _norm(v: str) -> str:
    # Strip sentence punctuation before quotes: memories are prose, so a
    # constant at the end of a clause arrives as "5." or "0.9," and comparing
    # that raw reported `_DEFAULT_KIND_CAP = 5.` as contradicting `= 5`.
    v = v.strip().rstrip(".,;:)]}").strip().strip("\"'")

    # ONE MAGNITUDE, MANY RENDERINGS. Prose writes 36,000; Python writes
    # 36_000; a memory writes 300s where the code writes 300. All three are the
    # same number, and reporting any of them as a contradiction is a false
    # proof. Separators are removed only when what remains is a number, so an
    # identifier-valued RHS (`X = SOME_CONST`) is never mangled.
    digits = v.replace(",", "").replace("_", "")
    if _NUMERIC.match(digits):
        # 0.4 == 0.40 == .4 ; 36,000 == 36_000 == 36000
        return str(float(digits))
    unit = _UNIT_SUFFIX_RE.match(digits)
    if unit:
        return str(float(unit.group(1)))

    if _NUMERIC.match(v):
        return str(float(v))
    return v.lower()


def _is_code_claim(content: str, name: str) -> bool:
    """Is `name` used as a SYMBOL here, or is it just an uppercase word?

    Memories shout. FAILED, HALTED, BASE and ARTIFACT all appear as emphasis in
    ordinary prose (and in German prose — one finding came from
    "EIN SPIEGELVERZEICHNIS ... BASE"), and each was matched as a constant and
    compared against an unrelated same-named binding elsewhere in the tree.
    Four of the sixteen proof findings on 2026-09-03 were this.

    THE DISCRIMINATOR, measured against that run: every genuinely stale symbol
    carried an underscore (CONFIDENCE_THRESHOLD, MAX_BODY_CHARS,
    _M5_PER_SPAWN_CEILING, _DAEMON_RECONCILE_GRACE_S, RELAYOUT_TOLERANCE);
    every false one was a single bare word. So a single word must additionally
    appear inside a code span to count as a claim.

    COST OF THIS RULE, stated plainly: a memory that writes `BASE (8.0)` in
    plain prose is no longer checked. That is a MISS, and misses are the right
    side to err on — this module's docstring says precision is the product,
    because nothing downstream filters a `proof` finding and agents act on it
    directly.
    """
    if "_" in name:
        return True
    return re.search(rf"`[^`\n]*\b{re.escape(name)}\b[^`\n]*`", content) is not None


def _asserts_absence(content: str, rel: str) -> bool:
    """Does the memory itself say this path is supposed to be missing?

    The dangling-path check proves a fact about the FILESYSTEM and reports it
    as a fact about the MEMORY. Those differ exactly on a memory whose SUBJECT
    is the deletion — and such a memory must name the path to be useful, so it
    re-arms the finding forever. The existing date rule handles only the case
    where git can date the death and the memory postdates it.

    Scoped to a window around each mention rather than the whole text: a long
    memory that happens to contain the word "deleted" somewhere else must not
    silence an unrelated dangling path.
    """
    if _SCOPED_ELSEWHERE_RE.search(content):
        return True
    base = rel.rsplit("/", 1)[-1]
    for m in re.finditer(re.escape(base), content):
        window = content[max(0, m.start() - 200):m.end() + 200]
        if _ABSENCE_RE.search(window):
            return True
    return False


def _mentions_value(content: str, value: str) -> bool:
    """Does the memory text mention this value anywhere?

    Guards the narrating-the-change escape hatch ("was 0.4, now 0.55"), which
    is what keeps a corrected memory from flagging itself forever.

    THE BUG THIS FIXES, found 2026-07-28 on the memory documenting this
    scanner's own false positives: the hatch compared ``_norm(actual)``
    against ``_norm(content)``, and ``_norm`` routes numbers through
    ``float`` — so "60" became "60.0", a form that never appears in prose.
    The hatch therefore never fired for any integer-valued constant, and a
    memory that DID quote the current value was reported anyway. Only
    fractional constants like 0.45 happened to survive the round-trip.

    Word-boundary matched on both forms so "6" does not match inside "60".
    """
    raw = value.strip().strip("\"'")
    if not raw:
        return False
    forms = {raw}
    # Python's own separator is not how prose writes the number. A code value
    # of 36_000 is quoted in a memory as "36,000" or "36000", and without these
    # forms the narrating-the-change hatch never fires for any large constant.
    bare = raw.replace("_", "")
    if _NUMERIC.match(bare):
        forms.add(bare)
        f = float(bare)
        forms.add(str(f))
        if f == int(f):
            forms.add(str(int(f)))
            forms.add(f"{int(f):,}")
    if _NUMERIC.match(raw):
        f = float(raw)
        forms.add(str(f))
        if f == int(f):
            forms.add(str(int(f)))
    return any(
        re.search(rf"(?<![\w.]){re.escape(form)}(?![\w.])", content)
        for form in forms
    )


def _values_conflict(claimed: str, actual: str) -> bool:
    c, a = _norm(claimed), _norm(actual)
    if not c or not a:
        return False
    if _NUMERIC.match(c) and _NUMERIC.match(a):
        return c != a
    # Non-numeric: only flag when the actual value is a bare literal too;
    # `X = some_call(...)` is not a claim this can adjudicate.
    if not re.fullmatch(r"[\w.\-]+", a):
        return False
    return c != a


def _symbol_line(rel: str, symbol: str) -> Optional[int]:
    """1-based line of `def symbol` / `class symbol` in `rel`, or None.

    Top-level or nested — the anchor question is "where did it go", and a
    method that moved 400 lines is as stale a pointer as a function that did.

    RETURNS None WHEN THE NAME IS DEFINED MORE THAN ONCE. If a file has four
    `__init__`s, "the first one" is not where anything moved to, and a finding
    built on it says nothing. Measured: before this rule the corpus produced a
    finding reading `main.py:1872 -> __init__ @615`, which is noise wearing the
    `proof` label — the exact thing that made two agents trust a bare string
    match once before. Ambiguity is not evidence.
    """
    found: Optional[int] = None
    try:
        with open(_REPO / rel, encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):
                m = re.match(r"\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)", line)
                if m and m.group(1) == symbol:
                    if found is not None:
                        return None  # ambiguous — cannot prove which one moved
                    found = i
    except OSError:
        return None
    return found


def _file_line_count(rel: str) -> Optional[int]:
    try:
        with open(_REPO / rel, encoding="utf-8") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return None


def _drifted_anchors(content: str) -> list[tuple[str, int, str, int]]:
    """(path, cited_line, symbol, actual_line) for every stale line anchor.

    A finding requires ALL of:
      * the memory cites `path:line` — an explicit pointer, not just a file;
      * the memory mentions an identifier that IS a def/class in that file —
        the scoping rule that makes this a claim about the memory rather than
        about the tree, exactly as contradicted_literal scopes its symbol
        search to the files the memory names;
      * that symbol still EXISTS, so this is drift and not deletion (deletion
        is dangling_path's job, and double-reporting one fact as two findings
        is how a scan stops reading as progress);
      * it sits more than the tolerance away from the cited line.

    Requiring the symbol to resolve INSIDE THE CITED FILE is what keeps this
    honest about the root cause this module already named: never attribute a
    code fact to a memory that did not assert it. The memory wrote the line
    number, and it wrote a word that turns out to name a definition in the
    very file it pointed at. Both are its own claims.
    """
    out: list[tuple[str, int, str, int]] = []
    if not _PATH_LINE_RE.search(content):
        return out

    # Candidate identifiers WITH their position, so the anchor can be matched
    # to the symbol the memory actually associates with it.
    mentions = [(m.start(), m.group(1)) for m in _SYMBOL_MENTION_RE.finditer(content)]
    if not mentions:
        return out

    for am in _PATH_LINE_RE.finditer(content):
        rel, cited = am.group(1), int(am.group(2))
        if _file_line_count(rel) is None:
            continue  # missing file is dangling_path's finding, not this one

        # NEAREST MENTION WINS, and this is a precision fix with a measured
        # reason. Taking the first identifier that happened to resolve in the
        # file produced a real false attribution on the live corpus: a memory
        # anchoring `critic.py:245` to `critique_ladder` — a symbol since
        # DELETED — was reported against `critique_faithfulness`, which merely
        # appears elsewhere in the same memory and still exists. That is the
        # module's own root cause: a code fact attributed to a claim the
        # memory never made. A deleted symbol is not this generator's finding,
        # and picking a neighbour to stand in for it is worse than silence.
        best: Optional[tuple[int, str, int]] = None
        for pos, name in mentions:
            actual = _symbol_line(rel, name)
            if actual is None:
                continue
            distance = abs(pos - am.start())
            if best is None or distance < best[0]:
                best = (distance, name, actual)
        if best is None:
            continue
        _, symbol, actual = best
        if abs(actual - cited) > _ANCHOR_DRIFT_TOLERANCE:
            out.append((rel, cited, symbol, actual))
    return out


_DRIFT_NARRATION_RE = re.compile(
    # The gap admits dots. `[^.]` looked like a sensible "stay in one
    # sentence" guard and could not cross the very thing these phrases are
    # always about — a filename. "not at vectorstore.py:204 any more" was
    # missed by the rule written for it.
    r"(no longer at|not at\b[^\n]{0,40}\bany ?more|used to (?:be|live) at|"
    r"has since moved|since moved|now (?:sits|lives) at|is now at|moved \d+|"
    r"drifted|stale (?:anchor|pointer)|MOVED)",
    re.IGNORECASE,
)


def _asserts_drift(content: str) -> bool:
    """True when the memory is NARRATING a moved anchor, not asserting it.

    The convergence rule this module already learned once, now for the fourth
    generator. A correct write-up of a drift must QUOTE the old location to be
    useful — "index_file is no longer at vectorstore.py:204" — which re-arms
    the finding forever and stops the scan reaching zero. That is exactly the
    dangling_path retraction defect, and it reproduced immediately: two of the
    first 20 drifted_anchor proofs were memories written that same day
    documenting the drift the generator had just found.

    Mirrors `_mentions_value` for contradicted_literal — a memory that names
    the change is describing it, not claiming the old state.
    """
    return bool(_DRIFT_NARRATION_RE.search(content))


def _lives_on_an_unmerged_branch(rel: str) -> bool:
    """True when `rel` is absent from HEAD but present on some other ref.

    THE SIGNAL THE PHRASING RULES CANNOT REACH. A memory describing work on a
    feature branch cites files that are real there and missing from main, and
    it is not stale — it is scoped elsewhere. `_SCOPED_ELSEWHERE_RE` catches
    that only when the prose happens to say so, and prose is optional: of the 5
    surviving dangling_path proofs on 2026-09-09, four said "on feat/gridlab"
    and the fifth named no branch at all, only a commit hash. No wording rule
    can catch the fifth without inventing a claim it never made.

    Git knows regardless of how the memory was written, which is the same
    reason `_path_deleted_at` uses `--diff-filter=D` rather than reading dates
    out of prose. Two cheap calls: find the last commit touching the path on
    ANY ref, then ask whether the file is actually in that commit's tree — a
    path whose newest reference is its own deletion is genuinely gone, and
    only a path that still exists somewhere is branch-scoped.
    """
    try:
        sha = subprocess.run(
            ["git", "log", "--all", "--format=%H", "-1", "--", rel],
            cwd=_REPO, capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if not sha:
            return False
        present = subprocess.run(
            ["git", "cat-file", "-e", f"{sha}:{rel}"],
            cwd=_REPO, capture_output=True, timeout=10,
        )
        return present.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _file_mtime_iso(path: str) -> Optional[str]:
    """Last git-commit date for path (not filesystem mtime — checkouts lie)."""
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%cI", "--", path],
            cwd=_REPO, capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        return out or None
    except Exception:
        return None


def _path_deleted_at(path: str) -> Optional[str]:
    """When the commit that DELETED this path landed, or None.

    None has two very different meanings and the caller must not conflate
    them: the path may never have existed at all (a memory inventing a file),
    or git may simply be unavailable. Both are handled as "cannot date the
    death", which is why the supersedes rule below exists as a second signal.
    """
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%cI", "--diff-filter=D", "--", path],
            cwd=_REPO, capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        return out or None
    except Exception:
        return None


# Any python path a memory names, kept as a SUFFIX so the same file matches
# whether the memory wrote it absolute, repo-relative, or package-relative
# ("~/okuro/src/okuro/embed/client.py", "src/okuro/embed/client.py"
# and "embed/client.py" are one file). Deliberately more permissive than
# _PATH_RE: this is used only to SCOPE a symbol search, never to decide
# whether a file exists, so a false match costs a missed finding rather than
# a wrong one.
_ANY_PY_RE = re.compile(r"([\w][\w/\-]*\.(?:py|sql))\b")


def _memory_cites(content: str) -> set[str]:
    return set(_ANY_PY_RE.findall(content))


def _scope_hits_to_cited(
    code_hits: list[tuple[str, str]], cited: set[str]
) -> Optional[list[tuple[str, str]]]:
    """Keep only the code hits in files the memory itself names.

    THE DEFECT THIS CLOSES, measured 2026-07-28: `_iter_code_literals` greps
    the whole tree for a bare symbol, so a memory correctly documenting
    `embed/client.py` `_TIMEOUT = 30` was reported as contradicted by
    `handover/transform.py` `_TIMEOUT = 60` — a different constant, in a file
    that memory never mentions. Two separate audit agents hit this
    independently; 2 of 8 findings in one pass were this false positive.

    Returns None when the memory names no files at all (nothing to scope to —
    fall back to the unscoped behaviour), and an empty list when it names
    files but the symbol lives in none of them, which is the false-positive
    case and must NOT be reported.
    """
    if not cited:
        return None
    scoped = [
        (p, v) for p, v in code_hits
        if any(p == c or p.endswith("/" + c.lstrip("/")) for c in cited)
    ]
    return scoped


def _adjudications() -> dict[tuple[str, str, str], Optional[str]]:
    """Every recorded 'I read this and it is fine', keyed by what it decided.

    Returns {(memory_id, kind, subject): code_value_at_decision}. The value is
    load-bearing: a verdict is about a SPECIFIC disagreement, so if the code
    moves again the finding must come back. "0.50 vs 0.45 is immaterial" says
    nothing about 0.50 vs 0.90.

    Best-effort. A missing table (migration not yet applied — okuro migrations
    do NOT auto-apply on get_db()) must degrade to "nothing adjudicated", never
    break the scan.
    """
    try:
        from okuro.db import get_db

        rows = get_db().fetchall(
            "SELECT memory_id, kind, subject, code_value FROM memory_adjudications"
        )
    except Exception:  # noqa: BLE001 — table absent or DB busy
        return {}
    return {
        (r["memory_id"], r["kind"], r["subject"]): r["code_value"]
        for r in rows
    }


def adjudicate(
    memory_id: str,
    kind: str,
    subject: str,
    code_value: Optional[str] = None,
    note: str = "",
    adjudged_by: str = "",
) -> str:
    """Record that a finding was examined and deliberately left alone.

    This is the ONLY write in a module that otherwise never writes, and it
    writes a verdict — not a change to the memory. The memory itself stays
    exactly as it was; what is recorded is that somebody read the finding and
    decided the stale detail does not undermine the claim.
    """
    from okuro.db import get_db

    get_db().execute(
        """INSERT INTO memory_adjudications
               (memory_id, kind, subject, code_value, verdict, note, adjudged_by)
           VALUES (?, ?, ?, ?, 'leave', ?, ?)
           ON CONFLICT (memory_id, kind, subject) DO UPDATE SET
               code_value  = excluded.code_value,
               note        = excluded.note,
               adjudged_by = excluded.adjudged_by,
               adjudged_at = datetime('now')""",
        (memory_id, kind, subject, code_value, note, adjudged_by),
    )
    return f"adjudicated {kind} on {subject} for {memory_id[:8]} — left as-is"


def scan(min_confidence: float = 0.3, limit: int = 0) -> dict[str, Any]:
    """Report memories whose mechanical claims the code contradicts.

    Read-only. Returns findings ranked strongest-first.
    """
    from okuro.db import get_db

    db = get_db()
    rows = db.fetchall(
        "SELECT id, topic, project, confidence, created_at, content, supersedes "
        "FROM agent_memory WHERE confidence >= ? ORDER BY created_at",
        (min_confidence,),
    )

    findings: list[dict] = []
    mtime_cache: dict[str, Optional[str]] = {}
    deleted_cache: dict[str, Optional[str]] = {}
    branch_cache: dict[str, bool] = {}
    adjudged = _adjudications()
    suppressed = 0

    for r in rows:
        content = r["content"] or ""
        cited = _memory_cites(content)
        # A memory written with supersedes= IS a correction: some agent already
        # adjudicated it. Its findings stay VISIBLE but drop to suspect, so a
        # completed cleanup can actually reach proof_count 0.
        is_correction = bool((r["supersedes"] or "").strip())

        # --- contradicted literals (PROOF) --------------------------------
        for name, claimed in _claimed_literals(content):
            if not _is_code_claim(content, name):
                # An uppercase word in prose, not a symbol. Cheapest filter
                # first: it runs before the git grep it would otherwise pay for.
                continue
            code_hits = _iter_code_literals(name)
            if not code_hits:
                continue
            scoped = _scope_hits_to_cited(code_hits, cited)
            if scoped is not None:
                if not scoped:
                    # The memory names files, and this symbol is in none of
                    # them. It is a same-named constant elsewhere in the tree.
                    continue
                code_hits = scoped
            actuals = {v for _, v in code_hits}
            if not all(_values_conflict(claimed, a) for a in actuals):
                continue
            # The memory may be narrating the change ("was 0.4, now 0.55").
            # If any current value appears anywhere in the text, it is not
            # asserting the stale one.
            if any(_mentions_value(content, a) for a in actuals):
                continue
            # Already read and deliberately left — but ONLY while the code
            # still says what it said when that call was made. A verdict is
            # about a specific disagreement, so a code change re-arms it.
            _verdict_key = (r["id"], "contradicted_literal", name)
            if _verdict_key in adjudged:
                if adjudged[_verdict_key] == ",".join(sorted(actuals)):
                    suppressed += 1
                    continue
            findings.append({
                "kind": "contradicted_literal",
                "strength": "suspect" if is_correction else "proof",
                "memory_id": r["id"],
                "confidence": r["confidence"],
                "created_at": r["created_at"],
                "symbol": name,
                "memory_says": claimed,
                "code_says": sorted(actuals),
                "where": sorted({p for p, _ in code_hits}),
                "excerpt": content[:150],
            })

        # --- dangling paths (PROOF) + changed-since (SUSPECT) -------------
        for rel in set(_PATH_RE.findall(content)):
            if not (_REPO / rel).exists():
                # CONVERGENCE. The raw check proves a fact about the
                # FILESYSTEM and reports it as a fact about the MEMORY. Those
                # differ exactly on a retraction: to be useful, "path X never
                # landed" must NAME X — so every correct correction re-armed
                # this finding and the scan could never reach zero. Measured
                # 2026-07-28: after a clean 5-of-5 path cleanup the scan still
                # reported 5 dangling paths, all of them the new retractions.
                #
                # Git already knows when each path died, so ask the question
                # the finding actually wants — does this memory believe the
                # path exists NOW? A memory written AFTER the deleting commit
                # is citing history deliberately.
                if rel not in deleted_cache:
                    deleted_cache[rel] = _path_deleted_at(rel)
                died = deleted_cache[rel]
                if died and r["created_at"] and r["created_at"] > died[:19]:
                    continue
                # Second half of the same convergence: a memory that ASSERTS
                # the absence is not contradicted by it, whatever the dates
                # say. The date rule needs git to have recorded the death and
                # the memory to postdate it; neither holds for a path that was
                # never committed, or for a retraction written the same day.
                if _asserts_absence(content, rel):
                    continue
                # Present on another ref = scoped elsewhere, not stale. Asked
                # of git rather than of the prose, because a memory need not
                # mention its branch and four of five such memories only did
                # so in passing.
                if rel not in branch_cache:
                    branch_cache[rel] = _lives_on_an_unmerged_branch(rel)
                if branch_cache[rel]:
                    continue
                # `died is None` means the path was never in git at all — a
                # memory that invented a file. The date rule cannot separate
                # that from a retraction OF such a memory, which is why
                # is_correction carries the second half of this.
                #
                # A path adjudication has no code_value to compare — absence
                # has no value — so it re-arms only if the path RESURRECTS,
                # which the `exists()` check above already handles by never
                # reaching here.
                if (r["id"], "dangling_path", rel) in adjudged:
                    suppressed += 1
                    continue
                findings.append({
                    "kind": "dangling_path",
                    "strength": "suspect" if is_correction else "proof",
                    "memory_id": r["id"],
                    "confidence": r["confidence"],
                    "created_at": r["created_at"],
                    "path": rel,
                    "path_deleted_at": died[:19] if died else None,
                    "excerpt": content[:150],
                })
                continue
            if rel not in mtime_cache:
                mtime_cache[rel] = _file_mtime_iso(rel)
            changed = mtime_cache[rel]
            if changed and changed > r["created_at"]:
                findings.append({
                    "kind": "file_changed_since",
                    "strength": "suspect",
                    "memory_id": r["id"],
                    "confidence": r["confidence"],
                    "created_at": r["created_at"],
                    "path": rel,
                    "code_changed_at": changed[:19],
                    "excerpt": content[:150],
                })

        # --- drifted line anchors (PROOF) ---------------------------------
        # Runs last: it is the weakest of the three proofs in what it decides
        # (the pointer moved, not necessarily the prose), and a memory whose
        # file is missing entirely should read as dangling_path, not as this.
        for rel, cited_line, symbol, actual_line in _drifted_anchors(content):
            if _asserts_absence(content, rel):
                continue
            # Narrating the move is not asserting the old location. Without
            # this the scan cannot converge: the correction that documents a
            # drift has to quote the stale anchor to be readable.
            if _asserts_drift(content):
                continue
            _adj_key = (r["id"], "drifted_anchor", f"{rel}:{cited_line}")
            if _adj_key in adjudged:
                # RE-ARM ON FURTHER MOVEMENT. The verdict recorded WHERE the
                # symbol was when somebody looked; if it has moved materially
                # since, that judgement was made about a different state and
                # deserves a second look. dangling_path re-arms on
                # resurrection for the same reason — an adjudication that can
                # never re-arm is a permanent blindfold, not a decision.
                _seen = adjudged.get(_adj_key)
                try:
                    _seen_line = int(_seen) if _seen is not None else None
                except (TypeError, ValueError):
                    _seen_line = None
                if _seen_line is None or \
                        abs(actual_line - _seen_line) <= _ANCHOR_DRIFT_TOLERANCE:
                    suppressed += 1
                    continue
            findings.append({
                "kind": "drifted_anchor",
                "strength": "suspect" if is_correction else "proof",
                "memory_id": r["id"],
                "confidence": r["confidence"],
                "created_at": r["created_at"],
                "path": rel,
                "cited_line": cited_line,
                "symbol": symbol,
                "actual_line": actual_line,
                "excerpt": content[:150],
            })

    order = {"contradicted_literal": 0, "dangling_path": 1,
             "drifted_anchor": 2, "file_changed_since": 3}
    findings.sort(key=lambda f: (order[f["kind"]], -f["confidence"]))
    if limit:
        findings = findings[:limit]

    proof = [f for f in findings if f["strength"] == "proof"]
    return {
        "scanned": len(rows),
        "findings": findings,
        "proof_count": len(proof),
        "suspect_count": len(findings) - len(proof),
        # Reported, never silent. A suppression the reader cannot see is
        # indistinguishable from a check that stopped working — the same
        # failure as the eval gate that existed for months and ran nowhere.
        "adjudicated_hidden": suppressed,
    }


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Find memories the code has since contradicted. "
                    "Reports only — never demotes.",
    )
    p.add_argument("--min-confidence", type=float, default=0.3,
                   help="skip already-superseded rows (default 0.3)")
    p.add_argument("--proof-only", action="store_true",
                   help="hide 'suspect' (file changed since) findings")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)

    res = scan(min_confidence=a.min_confidence, limit=a.limit)
    if a.proof_only:
        res["findings"] = [f for f in res["findings"]
                           if f["strength"] == "proof"]
    if a.json:
        print(json.dumps(res, indent=2))
        return 0

    print()
    print(f"  scanned {res['scanned']} memories "
          f"(confidence >= {a.min_confidence})")
    print(f"  {res['proof_count']} contradicted by code · "
          f"{res['suspect_count']} suspect")
    print()
    for f in res["findings"]:
        tag = "CONTRADICTED" if f["strength"] == "proof" else "suspect"
        print(f"  [{tag}] {f['kind']} · conf={f['confidence']} "
              f"· {f['created_at'][:10]}")
        if f["kind"] == "contradicted_literal":
            print(f"      memory says  {f['symbol']} = {f['memory_says']}")
            print(f"      code says    {f['symbol']} = "
                  f"{', '.join(f['code_says'])}  ({', '.join(f['where'])})")
        elif f["kind"] == "dangling_path":
            print(f"      cites missing file: {f['path']}")
        elif f["kind"] == "drifted_anchor":
            print(f"      memory points at {f['path']}:{f['cited_line']}")
            print(f"      {f['symbol']} is now at line {f['actual_line']} "
                  f"(moved {abs(f['actual_line'] - f['cited_line'])})")
        else:
            # The `else` used to be dangling_path's sibling and assumed
            # file_changed_since's shape. Adding a fourth kind made it a
            # KeyError on the CLI while scan() itself stayed correct — the
            # renderer is a separate contract from the finding, and a new kind
            # has to teach both.
            print(f"      {f['path']} changed {f['code_changed_at']}, "
                  f"memory written {f['created_at'][:19]}")
        print(f"      \"{f['excerpt'][:104].strip()}...\"")
        print()
    if res["proof_count"]:
        print("  Review each. To retire one, write the correction with "
              "write_memory(supersedes=<id>) — keep whatever is still true.")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
