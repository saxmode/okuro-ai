# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: The repair ENGINE, lifted out of migration 156's generator so the
#   fleet update pipeline and that generator compute the same plan from the
#   same code — markdown surgery, the dead-string sweep, the lean/micro
#   builders, the per-host guard scan, the carrier rule and the split emitter.
# index:
#   OPERATIONS | class Operation | def parse_operations
#   markdown parsing
#   legacy sweep
#   section authoring + rendering
#   grade repair (full / lean / micro)
#   class RolePlan / class Plan / def plan_for_roles
#   def migration_carried_ids  (the carrier rule)
#   def emit_migration_sql     (the split emitter)
# AGENT_HEADER_END -->
"""One repair engine, two callers.

WHY THIS MODULE EXISTS, AND WHY IT IS A LIFT RATHER THAN A COPY.

``scripts/roles_repair_156.py`` computed a fleet-wide body repair and emitted
it as a migration. It works, and the parts of it that work are not specific to
what migration 156 happened to change: parsing an H2 body without reading the
headings inside a fenced output template, sweeping a dead string without
inventing a role id, building a lean grade from the full body's own fields
rather than truncating it, asking the CONTENT guard whether a body may leave
this host, and deciding whether a role is carried by a migration at all.

P5 needs every one of those to turn an APPROVED structure action into a
change. Copying them would produce two engines that agree on the day of the
copy and diverge after it — and the thing that would diverge is the code that
decides which bodies are allowed into the repo. So they are MOVED here, and
the generator imports them. Its emitted SQL is asserted byte-identical to the
committed migration by ``tests/roles/test_repair_plan.py``, against the
pre-migration snapshot the repair was computed from.

WHAT AN OPERATION IS.

Migration 156's repair was one instance of a class: "apply a structural change
to a named set of roles, keep the three grades in agreement, keep the gates
green, keep private text out of the repo." The class needs a vocabulary, and
this module's is deliberately small — seven verbs, each one a thing a
``role_structure_actions`` row can actually describe:

====================  =====================================================
op                    what it does
====================  =====================================================
``add_section``       insert a canonical H2 at its rank in one grade
``rename_section``    change an H2's heading, keeping its body
``remove_section``    drop an H2 and its body
``rewrite_block``     re-author one section's body under an instruction
``sweep_string``      replace a literal or pattern across the grades
``set_field``         map a non-body column (tier, model, …) to a new value
``rebuild_grade``     rebuild lean or micro from the full body's own parts
====================  =====================================================

An action whose change does not fit one of these is an action this pipeline
refuses rather than approximates. A planner that silently accepts an operation
it cannot perform is worse than one that says no: the dry-run diff would show
nothing missing and the implement step would write nothing.

AUTHORED TEXT IS GATED, NOT TRUSTED. ``add_section`` and ``rewrite_block``
route through ``bridge_invoke``, and every body the planner produces must pass
``gate_role_body`` and ``validate_role_structure`` and stay inside the lean and
micro budgets before it reaches a Plan. A role whose authored text fails is
left UNCHANGED with the failure recorded as its reason — never shipped half
repaired, and never silently skipped.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field as dc_field
from pathlib import Path

import yaml

from okuro.roles.designer import (
    FULL_REQUIRED,
    TRAIT_AXES,
    gate_role_body,
    validate_role_structure,
)
from okuro.roles.fences import fence_spans, search_unfenced
from okuro.roles.fit import LEGACY_STRINGS, SIZE_BUDGETS

log = logging.getLogger("okuro.roles.repair_plan")

#: Where the migrations live, relative to this package. The generator resolved
#: it from the repo root; a module inside the package must not assume it has
#: one, because an installed okuro has no ``src/`` above it.
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"


class EmitRefused(ValueError):
    """The emitter will not write this SQL, and nothing was written.

    IT USED TO BE ``SystemExit``, WHICH WAS RIGHT EXACTLY ONCE. This code was
    a script, so "refuse to emit" meant "stop the process with a message", and
    ``SystemExit`` said that in one line. It is now reached from an API route
    and an MCP handler, and there ``SystemExit`` inherits from
    ``BaseException``: it walks straight past every ``except Exception``, takes
    down the worker instead of answering the request, and does it AFTER the
    pre-write snapshot has been taken — so the caller sees a dropped
    connection and the row sits in ``approved`` beside a snapshot nobody knows
    about.

    A refusal from the emitter is a normal answer to a normal question, so it
    is a normal exception. The pipeline catches it, records it on the row, and
    leaves the state where it was.
    """


class PlanRefused(ValueError):
    """The planner will not produce a plan for this instruction.

    Same shape and same reason as ``actions.ActionRefused``: one exception
    whose message names the gate, because every caller does the same thing
    with it — hand the sentence back — and a refusal that does not say what
    failed is a refusal the caller retries identically.
    """


# ---------------------------------------------------------------------------
# constants shared by every repair
# ---------------------------------------------------------------------------


#: H2 order in the canonical full body, from designer.get_role_template.
CANONICAL_H2 = (
    "IDENTITY",
    "COGNITIVE PROFILE",
    "EXPERTISE",
    "SKILLS",
    "TOOLS",
    "PROTOCOL",
    "SELF-MAINTENANCE",
    "KNOWLEDGE PROTOCOL",
    "SUCCESS CRITERIA",
    "FAILURE MODES",
    "COLLABORATION",
    "SOURCES",
)
_H2_INDEX = {h: i for i, h in enumerate(CANONICAL_H2)}

#: Short axis labels for the lean persona digest, in TRAIT_AXES order.
_AXIS_SHORT = ("I/D", "C/B", "D/BP", "I/C", "R/P", "S/G")

#: The cognitive-profile vocabulary, MEASURED from the live bodies on
#: 2026-09-17 (`### Primary:`/`### Secondary:` headings across all 103 roles),
#: not invented here. Ten families; the fleet also spells three of them a
#: second way (``Synesthetic``, ``ADHD-Hyperfocus``, ``Dyslexic-Spatial``,
#: ``Alexithymia-Analytical``), which existing bodies keep and new authoring
#: does not add.
#:
#: The owner ruled 2026-09-17 that an authored profile must come from this set.
#: The first authoring run invented one for 26 of 34 roles — each individually
#: apter than the canonical name, and together a second vocabulary nobody
#: agreed to. A persona axis is only comparable across the fleet if every role
#: is scored on the same axis.
COGNITIVE_PROFILES: tuple[tuple[str, str], ...] = (
    ("High-Functioning Autism", "hfa"),
    ("OCD-Channeled", "ocd"),
    ("Schizotypal-Creative", "schizotypal"),
    ("Synesthesia", "synesthesia"),
    ("Dyslexia", "spatial"),
    ("Hyperthymesia", "hyperthymesia"),
    ("ADHD-Channeled", "adhd"),
    ("Alexithymia", "analytical"),
    ("Hypomanic-Creative", "hypomania"),
    ("Hyperlexia", "hyperlexia"),
)
_PROFILE_KEY = dict(COGNITIVE_PROFILES)



def knowledge_protocol_block(role_id: str) -> str:
    """The KNOWLEDGE PROTOCOL section every repaired body gets.

    Replaces the filesystem tree the old block named. ``roles_knowledge`` and
    ``roles_learn`` are live MCP verbs against the same database the role row
    lives in, so the instruction is followable — which the old one had not
    been since migration 155 deleted the directory it pointed at.
    """
    return (
        "## KNOWLEDGE PROTOCOL\n"
        "\n"
        "### On Activation\n"
        f"1. `roles_knowledge('{role_id}')` — read this role's accumulated "
        "learnings from the store.\n"
        "2. If the freshness metadata says the knowledge is stale, run "
        f"`roles_maintenance('{role_id}')` before high-stakes execution.\n"
        "\n"
        "### On Completion\n"
        "| Finding type | Action |\n"
        "|--------------|--------|\n"
        f"| Novel technique or pattern | `roles_learn('{role_id}', ...)` |\n"
        "| Counter-intuitive finding | `write_memory(topic='gotcha')` |\n"
        "| Strategic decision | `write_memory(topic='decision')` |\n"
        "| Nothing new | No action |\n"
    )


# ---------------------------------------------------------------------------
# markdown parsing
# ---------------------------------------------------------------------------


_H2 = re.compile(r"(?m)^##[ \t]+(?!#)(.+?)[ \t]*$")

#: THE FENCE ENGINE LIVES IN ``roles/fences.py`` NOW, AND ``fence_spans`` IS
#: IMPORTED AT THE TOP OF THIS MODULE RATHER THAN DEFINED HERE.
#:
#: It had to move so the GATE could share it: ``validate_role_structure`` was
#: fence-blind and this module imports FROM ``designer``, so a detector
#: defined here was unreachable there without a cycle. The name stays
#: importable from this module because ``scripts/roles_repair_156.py`` takes
#: it from here and that script's emitted SQL is asserted byte-identical to
#: the committed migration.


def split_h2(text: str) -> tuple[str, list[tuple[str, str]]]:
    """``text`` -> ``(preamble, [(heading, body), ...])`` on H2 boundaries.

    Headings inside a fenced code block are NOT boundaries.
    """
    spans = fence_spans(text)
    marks = [m for m in _H2.finditer(text or "")
             if not any(s <= m.start() < e for s, e in spans)]
    if not marks:
        return (text or ""), []
    preamble = text[: marks[0].start()]
    out: list[tuple[str, str]] = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out.append((m.group(1).strip(), text[m.end(): end].strip("\n")))
    return preamble, out


def join_h2(preamble: str, sections: list[tuple[str, str]]) -> str:
    parts = [preamble.rstrip("\n")]
    for heading, body in sections:
        parts.append(f"## {heading}\n\n{body.strip()}\n")
    return "\n\n".join(p for p in parts if p.strip()).rstrip() + "\n"


#: Canonical names LONGEST FIRST. The order matters and the bug it prevents is
#: not hypothetical: ``KNOWLEDGE PROTOCOL`` ends with ``PROTOCOL``, so a scan in
#: declaration order classified every knowledge section as the protocol
#: section — which silently skipped the dead-path replacement in 53 roles,
#: mis-ranked the section on insert, and made ``section_body(secs, "PROTOCOL")``
#: return the wrong text.
_CANON_BY_LENGTH = tuple(sorted(CANONICAL_H2, key=len, reverse=True))


def _canon_h2(heading: str) -> str | None:
    """Map a real heading onto a CANONICAL_H2 name, or None. Longest wins."""
    h = re.sub(r"[^A-Z ]", " ", heading.upper()).strip()
    h = re.sub(r"\s+", " ", h)
    for name in _CANON_BY_LENGTH:
        if h == name or h.startswith(name + " ") or h.endswith(" " + name):
            return name
    return None


def insert_sections(
    preamble: str,
    sections: list[tuple[str, str]],
    new: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Place ``new`` sections at their canonical rank among ``sections``.

    Existing sections keep their relative order: a known heading takes its
    canonical rank, an unknown one takes a hair more than whatever preceded
    it. An inserted section takes its canonical rank exactly, so it lands
    before the unknown sections that trail the same anchor rather than at the
    end of the document.
    """
    ranked: list[tuple[float, str, str]] = []
    last = 0.0
    for heading, body in sections:
        key = _canon_h2(heading)
        last = float(_H2_INDEX[key]) if key is not None else last + 1e-3
        ranked.append((last, heading, body))
    for heading, body in new:
        key = _canon_h2(heading)
        ranked.append((float(_H2_INDEX[key]) if key is not None else 99.0,
                       heading, body))
    ranked.sort(key=lambda r: r[0])
    return [(h, b) for _, h, b in ranked]


def field(text: str, label: str) -> str:
    """The value of a ``**Label:** ...`` field, joined across its paragraph."""
    m = re.search(
        rf"(?ms)^\*\*{re.escape(label)}:?\*\*[ \t]*(.*?)(?=\n[ \t]*\n|\n\*\*|\Z)",
        text or "",
    )
    if not m:
        return ""
    return " ".join(x.strip() for x in m.group(1).splitlines() if x.strip())


#: Tool names the vocabulary gate calls retired. A body may not RE-STATE one:
#: the `tools` column still carries them on old rows, and copying that column
#: into a micro body would put a dead tool into the text an agent is told to
#: treat as binding. Read from the gate so there is one list, not two.
def retired_tools() -> frozenset[str]:
    try:
        from okuro.roles.vocabulary import RETIRED_TOOL_ALIASES
        return RETIRED_TOOL_ALIASES
    except Exception:  # noqa: BLE001
        return frozenset()


def constraint_items(full: str) -> list[str]:
    """The ``**Constraints:**`` items, whether they are a list or a sentence.

    Two shapes in the fleet and they need different handling. A bullet list
    under the label has to be read as bullets — ``field()`` joins a paragraph
    onto one line, and splitting THAT on sentence punctuation produced
    game-designer's single 400-character bullet that began ``- - Every
    mechanic…``. A prose sentence is split on semicolons instead.
    """
    # (?m) and NOT (?s): with DOTALL the `.+` swallows the blank line and the
    # `**Skills:**` list after it, and game-designer's five constraints came
    # back as eight with three skills among them.
    m = re.search(r"(?m)^\*\*Constraints:?\*\*[ \t]*\n((?:[ \t]*[-*][ \t]+.+\n?)+)", full)
    if m:
        return [x for x in bullets(m.group(1), 8)]
    text = field(full, "Constraints")
    return [c.strip() for c in re.split(r";\s+|\.\s+(?=[A-Z])", text) if c.strip()][:6]


def protocol_steps(proto_body: str, authored: dict, limit: int = 6) -> list[str]:
    """What the role DOES, in order — never what it reads.

    ``### INPUT`` is the first subsection of a spelled-out PROTOCOL, so the
    first bullets in the section are the role's input list. system-documenter's
    micro came out claiming its protocol was ``/proc/cpuinfo, /proc/meminfo``.
    Four sources in priority order, and the last one drops INPUT first.
    """
    m = re.search(r"(?ms)^###\s+(?:PROCESS|Process)\b.*?\n(.+?)(?=\n###|\Z)", proto_body)
    if m:
        got = bullets(m.group(1), limit)
        if got:
            return got
    steps = re.findall(r"(?m)^###\s+(?:STEP\s+)?\d+[.:)]?\s*(.+?)\s*$", proto_body)
    if len(steps) >= 2:
        return [s.strip(": ") for s in steps][:limit]
    if (authored.get("protocol") or {}).get("process"):
        return list(authored["protocol"]["process"])[:limit]
    without_input = re.sub(r"(?ms)^###\s+(?:INPUT|Input)\b.*?(?=\n###|\Z)", "", proto_body)
    return bullets(without_input, limit)


def bullets(body: str, limit: int = 8) -> list[str]:
    """Bullet or numbered-list items in ``body``, stripped of markup."""
    out: list[str] = []
    for line in (body or "").splitlines():
        m = re.match(r"^[ \t]*(?:[-*]|\d+[.)])[ \t]+(.+?)[ \t]*$", line)
        if not m:
            continue
        item = re.sub(r"\*\*(.+?)\*\*", r"\1", m.group(1)).strip()
        item = re.sub(r"^`(.+?)`", r"\1", item).strip(" -—:")
        if item and item not in out:
            out.append(item)
        if len(out) >= limit:
            break
    return out


def section_body(sections: list[tuple[str, str]], name: str) -> str:
    for heading, body in sections:
        if _canon_h2(heading) == name:
            return body
    return ""


def clip(text: str, limit: int) -> str:
    """Shorten to ``limit`` on a WORD boundary, with an ellipsis.

    A hard slice cut prism-critic's micro mid-word — "record any untraceable
    or drifted token as a defe" — which reads as a corrupted row rather than
    a summary.
    """
    t = " ".join(str(text).split())
    if len(t) <= limit:
        return t
    cut = t[:limit].rsplit(" ", 1)[0].rstrip(" ,;:—-")
    return cut + " …"


def tools_list(raw: str | None) -> list[str]:
    """The ``tools`` column, which stores a JSON array."""
    try:
        val = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(t) for t in val] if isinstance(val, list) else []



# ---------------------------------------------------------------------------
# legacy sweep
# ---------------------------------------------------------------------------


#: A knowledge path in EITHER spelling the fleet uses:
#: ``knowledge/roles/<id>/...`` and ``knowledge/<domain>/<id>/...``. The first
#: version matched only the former, and seven roles kept a live-looking
#: instruction to read ``knowledge/marketing/web-writer/knowledge.md``. Same
#: dead tree, one directory level different.
#: ANY path under the dead knowledge tree, whatever its shape.
#:
#: Guessing the role id by POSITION does not work, and two attempts proved it.
#: The tree has at least six shapes — ``knowledge/roles/<id>/``,
#: ``knowledge/<domain>/<id>/``, ``knowledge/performance/``,
#: ``knowledge/failures/incidents/``, ``knowledge/global/quality-standards/``,
#: ``knowledge/domains/{relevant}/`` — and only the first two name a role at
#: all. A positional rule read the second segment of the others as a role and
#: produced ``roles_knowledge('incidents')`` and ``roles_knowledge('system')``:
#: an instruction to read the accumulated learnings of a role that does not
#: exist, which is worse than the dead path it replaced.
#:
#: So the rule is not positional, it is a LOOKUP: a path is a role's knowledge
#: when one of its segments IS a role id in the store. Everything else under
#: ``knowledge/`` is the general tree, equally dead, and names no role.
_KNOWLEDGE_ANY = re.compile(r"`?knowledge/[^\s`|)\"']*`?")

#: What a dead knowledge path that names no role becomes. Honest about what it
#: is — the store — without inventing a role to attribute it to.
_GENERIC_STORE = "`the knowledge store (roles_knowledge / write_memory)`"

#: A KNOWLEDGE PROTOCOL section that routes findings to a FILE. Beyond the
#: dead tree above, these sections carry a "Location" column naming
#: ``knowledge.md``, ``lessons.md``, ``research/{date}-{topic}.md`` or
#: ``decisions/{date}-{topic}.md`` — destinations that do not exist AND that
#: ORCH-ARTIFACT-DISK forbids on their own ("never a .md file, not even a
#: copy"). A section like that is replaced wholesale; one that already names
#: the store verbs is left alone.
_FILE_DESTINATION = re.compile(
    r"knowledge/|\bknowledge\.md\b|\blessons\.md\b"
    r"|\b(?:research|decisions)/\{[^}]*\}[^\s|]*\.md\b")
_PINGPONG_PATH = re.compile(
    r"`?(?:okuro/)?(?:tasks/)?\{?[a-z_]*task[_a-z-]*\}?/pingpong\.md`?"
    r"|`?okuro/tasks/\*/pingpong\.md`?|`?tasks/\*/pingpong\.md`?",
    re.IGNORECASE,
)


def sweep_legacy(text: str, role_id: str,
                 known_roles: frozenset[str] = frozenset(),
                 *, tier_map: dict | None = None,
                 body_fixes: dict | None = None) -> tuple[str, list[str]]:
    """Remove every dead reference from one grade. Returns (text, reasons).

    ``tier_map`` and ``body_fixes`` are the two CALLER-OWNED tables, and they
    are parameters rather than constants because they are the only parts of
    this sweep that belong to one migration rather than to the fleet: a tier
    vocabulary someone is retiring, and per-role corrections no rule
    generalises. Omitting them runs every fleet-wide rule and neither of
    those, which is what a caller with no such table wants.

    ``known_roles`` is the store's role-id set. It is what lets the knowledge
    rewrite tell a role directory from the rest of the dead tree instead of
    guessing by position; passing an empty set degrades every knowledge path to
    the generic store reference, which is wrong-but-harmless rather than a
    fabricated role id.

    Each rule below was written against the MEASURED occurrences (all 393 of
    them across 52 roles, dumped 2026-09-17), not against a guess at what the
    strings might look like. The replacements name things that exist today.
    """
    if not text:
        return text, []
    before = text
    reasons: list[str] = []

    # -- the knowledge tree -> the store verbs -------------------------------
    # A whole KNOWLEDGE PROTOCOL section that routes findings to a FILE is
    # replaced wholesale; stray references elsewhere (SELF-MAINTENANCE
    # bullets, tables, a `knowledge_base:` key) are rewritten token by token.
    pre, secs = split_h2(text)
    if secs and any(_canon_h2(h) == "KNOWLEDGE PROTOCOL"
                    and _FILE_DESTINATION.search(b) for h, b in secs):
        text = join_h2(pre, [
            (h, knowledge_protocol_block(role_id).split("\n", 1)[1].strip()
             if _canon_h2(h) == "KNOWLEDGE PROTOCOL" and _FILE_DESTINATION.search(b)
             else b)
            for h, b in secs
        ])
        reasons.append("legacy:KNOWLEDGE PROTOCOL routed findings to files")

    if "knowledge/" in text:

        def _tok(m: re.Match) -> str:
            raw = m.group(0).strip("`")
            named = next((s for s in raw.split("/")[1:] if s in known_roles), None)
            if named is None:
                return _GENERIC_STORE
            if raw.endswith("lessons.md"):
                return f"`roles_learn('{named}', ...)`"
            return f"`roles_knowledge('{named}')`"

        text = _KNOWLEDGE_ANY.sub(_tok, text)
        text = re.sub(
            r"(?i)\bUpdate (`roles_learn\('[a-z0-9-]+', \.\.\.\)`)(?: with (?:learnings|insights))?",
            r"Record learnings via \1", text,
        )
        text = re.sub(r"(?i)\bScan (`roles_knowledge\()", r"Re-read \1", text)
        reasons.append("legacy:knowledge/roles/ -> roles_knowledge/roles_learn")

    # -- pingpong.md -> the review queue ------------------------------------
    # Verified dead: `grep -rn pingpong src/` on 2026-09-17 matches only
    # fit.py's own legacy list and migration 155's copy of these bodies. The
    # channel it names is `await_review` / `orchestrator_approve` now.
    if "pingpong" in text.lower():
        text = _PINGPONG_PATH.sub("the task's review queue", text)
        pairs = (
            # These three run FIRST because the generic rule below turns the
            # filename into a noun phrase, and a noun phrase does not fit
            # every sentence the filename did: "consistent pingpong.md
            # communication" became "consistent the review queue
            # communication" on the first pass.
            (r"(?i)\bpingpong\.md\s+(communication|format|status updates)\b",
             r"review-queue \1"),
            (r"(?i),\s*pingpong\.md\s*,", ", review requests,"),
            (r"(?i)\bpingpong\.md\s+(?:with|containing)\b",
             "a review request carrying"),
            (r"(?i)\b(?:Append to|Write to|write to)\s+pingpong\.md\s*:",
             "Raise a review request (`await_review`):"),
            (r"(?i)\bWrite\s+(.+?)\s+to\s+pingpong\.md\b",
             r"Raise \1 via `await_review`"),
            (r"(?i)\bUpdate\s+pingpong\.md\s+(?:status\s*)?:",
             "Emit a status event (`emit_task_event`):"),
            (r"(?i)\bUpdate\s+pingpong\.md\s+(?:LIVE ACTIVITY|with|on)\b",
             "Emit a status event (`emit_task_event`) for"),
            (r"(?i)\bUpdate\s+pingpong\.md\b", "Emit a status event (`emit_task_event`)"),
            (r"(?i)\bvia (?:structured |standardized )?pingpong\.md(?: format| status updates| decisions)?",
             "via `await_review`"),
            (r"(?i)\bpingpong\.md\s+HISTORY\b", "the review queue's history"),
            (r"(?i)\bpingpong\s+approval\b", "review approval"),
            # An ALL-CAPS heading needs its own rule: the generic noun phrase
            # below turns "## PINGPONG MANAGEMENT" into "## the review queue
            # MANAGEMENT", which is not a heading anyone wrote.
            (r"(?m)^(#{1,4}\s+)PINGPONG\b", r"\1REVIEW QUEUE"),
            (r"(?i)\bPingpong Communication\b", "Review-Queue Communication"),
            (r"(?i)\bPingpong communication gaps\b", "Review-queue communication gaps"),
            (r"(?i)\bpingpong-updates\b", "review-queue-updates"),
            (r"(?i)\bpingpong decisions\b", "review-queue decisions"),
            (r"(?i)\bpingpong\.md\b", "the review queue"),
            (r"(?i)\bpingpong\b", "the review queue"),
        )
        for pat, rep in pairs:
            text = re.sub(pat, rep, text)
        reasons.append("legacy:pingpong.md -> await_review/emit_task_event")

    # -- start.md ------------------------------------------------------------
    # Two different dead things share one filename. `okuro/start.md` never
    # existed after the rewrite — the entry point is the bootstrap packet. The
    # `tm-docs/**/start.md` tree is this role's OWN output convention, so the
    # file is renamed rather than the instruction deleted; deleting it would
    # remove the role's documentation spec, not a dead reference.
    if "start.md" in text:
        text = re.sub(r"(?i)`?okuro/start\.md`?",
                      "the okuro bootstrap packet", text)
        text = re.sub(r"(?i)\bRead the okuro bootstrap packet and verify environment",
                      "Call `bootstrap` and verify the environment", text)
        text = text.replace("start.md", "index.md")
        reasons.append("legacy:start.md -> bootstrap packet / index.md")

    # -- TM-Cortex -> cortex -------------------------------------------------
    if "TM-Cortex" in text or "tm_cortex" in text:
        text = re.sub(r"python -m tm_cortex search\s+", "cortex_search ", text)
        text = re.sub(
            r"(?i)`?tm-cortex/docs/specification\.md`?",
            "`cortex_search` / `cortex_route` / `cortex_read_header`", text,
        )
        text = re.sub(r"(?i)\bTM-Cortex (pre-task|post-task) hook\b",
                      r"cortex \1 lookup", text)
        text = text.replace("TM-Cortex", "cortex")
        reasons.append("legacy:TM-Cortex -> cortex_* tools")

    # -- claude-sonnet-4 -> the tier's model ---------------------------------
    if "claude-sonnet-4" in text or "claude-opus-4" in text:
        text = text.replace("claude-sonnet-4", "<the tier's model>")
        # Same line, same class: a hardcoded model name in a role body is the
        # defect, and leaving its neighbour behind fixes half a sentence.
        text = text.replace("claude-opus-4", "<the strategic tier's model>")
        reasons.append("legacy:claude-sonnet-4 -> <the tier's model>")

    # -- tm-nightbird -> okuro ----------------------------------------------
    if "tm-nightbird" in text.lower():
        text = "\n".join(
            line for line in text.splitlines()
            if "python -m tm-nightbird" not in line
        )
        text = re.sub(
            r"(?i)`?tm-nightbird(?:-package)?/docs/protocol\.md`?",
            "the okuro bootstrap packet", text,
        )
        text = re.sub(r"(?i)\bTM-Nightbird protocol\b", "the okuro protocol", text)
        text = re.sub(r"(?i)tm-nightbird", "okuro", text)
        reasons.append("legacy:tm-nightbird -> okuro")

    # -- tm-eichi -> roles_match ---------------------------------------------
    # Not in fit.py's LEGACY_STRINGS, but roles/vocabulary.py's
    # RETIRED_TOOL_ALIASES already refuses it on the `tools` COLUMN — and five
    # bodies still grant it in their TOOLS section, which is the same dead
    # name in the one place that is handed to an agent as binding. The role DB
    # it named was replaced by okuro.db, so the live verb is `roles_match`.
    if "tm-eichi" in text.lower():
        text = re.sub(r"(?i)`?mcp__tm-eichi__eichi_match`?", "`roles_match`", text)
        text = re.sub(r"(?i)`?tm-eichi`?", "`roles_match`", text)
        reasons.append("legacy:tm-eichi -> roles_match (retired alias)")

    # -- non-canonical tier named inside the body ---------------------------
    # The body and the column have to agree after the column is normalised,
    # or the row says one thing and the text it ships says another.
    tier_map = tier_map or {}

    def _tier_line(m: re.Match) -> str:
        return f"{m.group(1)}{tier_map[m.group(2).lower()]}{m.group(3) if m.lastindex and m.lastindex >= 3 else ''}"

    # Four spellings, all measured: a YAML-ish `tier: specialist` line, the
    # same with a list dash, the lean grade's inline
    # `**Domain:** … | **Tier:** specialist | **Model:** …` banner, and a
    # parenthesised `(specialist tier)` inside a Domain line. The first
    # version had only the first two, and the orchestrator — the role the
    # brief named — hides its stale tier in the third.
    # The alternation is built from the map's OWN keys, in its own order, so
    # a caller that retires a fourth tier name gets it swept by adding one
    # entry — and a caller with no map compiles no pattern at all.
    _alt = "|".join(re.escape(k) for k in tier_map)
    tier_pats = (
        rf"(?im)^(\s*-?\s*tier:\s*)({_alt})\b()",
        rf"(?i)(\*\*Tier:?\*\*\s*)({_alt})\b()",
        rf"(?i)(\()({_alt})(\s+tier\b)",
    ) if _alt else ()
    if any(re.search(p, text) for p in tier_pats):
        for p in tier_pats:
            text = re.sub(p, _tier_line, text)
        reasons.append("tier: body tier line normalised")

    # -- per-role corrections no rule generalises ---------------------------
    for pattern, repl in (body_fixes or {}).get(role_id, ()):
        fixed = re.sub(pattern, repl, text)
        if fixed != text:
            text = fixed
            # The reason NAMES the class, never the text. Quoting the removed
            # string here wrote it straight back into the migration's comment
            # column — the same mistake as listing a held-back role's tokens
            # in the note explaining why it is held back.
            reasons.append("content: a cited first name that collides with the "
                           "people registry was dropped; the citation stands")

    if not reasons:
        # Nothing matched. Return the ORIGINAL bytes — a body that needs no
        # sweep must not appear in the diff because this function reflowed its
        # blank lines. 103 rows "changed" is not a repair, it is noise that
        # hides the 57 that are.
        return before, []
    text = re.sub(r"\n{3,}", "\n\n", text).rstrip() + "\n"
    return text, reasons


def legacy_hits(text: str) -> dict[str, int]:
    return {s: (text or "").count(s) for s in LEGACY_STRINGS if s in (text or "")}



# ---------------------------------------------------------------------------
# the CONTENT guard — may this body leave the host
# ---------------------------------------------------------------------------

def guard_hits(text: str) -> set[str]:
    """Guard tokens in ``text``, using the guard's OWN anchored matcher.

    Never a substring scan: the guard compiles
    ``(?<![A-Za-z0-9])token(?![A-Za-z0-9])``, and one of the GPU-name tokens
    is a tail of the word "okuro" — so an unanchored test matches every line
    that says the product's name. A hand-rolled ``in`` test reported 17 roles
    carrying that token when the real answer was 2, which is worse than no
    measurement at all.

    Returns an empty set where the guard is inert (no token files — a fresh
    install, or any machine but this one).
    """
    try:
        from okuro.cli.guard_content import compile_tokens, load_tokens
        rx = compile_tokens(load_tokens())
    except Exception:  # noqa: BLE001 — the guard is okuro-repo-only
        return set()
    return {m.group(0).lower() for m in rx.finditer(text)} if rx else set()


def guard_is_armed() -> bool:
    """Whether the CONTENT guard has tokens to judge with.

    Deliberately NOT "did it find anything": those are different questions and
    conflating them inverted the safety check.
    """
    try:
        from okuro.cli.guard_content import load_tokens
        return bool(load_tokens())
    except Exception:  # noqa: BLE001 — the guard is okuro-repo-only
        return False


# ---------------------------------------------------------------------------
# the carrier rule — is this role carried by a migration
# ---------------------------------------------------------------------------

_INSERT_INTO_ROLES = re.compile(r"(?i)\bINSERT\s+(?:OR\s+\w+\s+)?INTO\s+roles\b")
_VALUES_KEYWORD = re.compile(r"(?i)\bVALUES\b")


def _past_literal(sql: str, i: int) -> int:
    """Index just past the single-quoted literal that starts at ``i``."""
    i += 1
    n = len(sql)
    while i < n:
        if sql[i] == "'":
            if i + 1 < n and sql[i + 1] == "'":   # '' is an escaped quote
                i += 2
                continue
            return i + 1
        i += 1
    return n


def _paren_group(sql: str, i: int) -> tuple[list[str], int]:
    """Split the parenthesised group at ``i`` on its TOP-LEVEL commas.

    Returns the items and the index just past the closing paren. Literals are
    skipped whole and nesting is counted, because a role body contains both
    commas and parentheses by the thousand — ``scaleX(0->1)``, ``(0-4)``,
    every list in every prose section.
    """
    n = len(sql)
    depth = 0
    start = i
    items: list[str] = []
    while i < n:
        ch = sql[i]
        if ch == "'":
            i = _past_literal(sql, i)
            continue
        if ch == "(":
            depth += 1
            i += 1
            if depth == 1:
                start = i
            continue
        if ch == ")":
            depth -= 1
            if depth == 0:
                items.append(sql[start:i])
                return items, i + 1
            i += 1
            continue
        if ch == "," and depth == 1:
            items.append(sql[start:i])
            start = i + 1
            i += 1
            continue
        i += 1
    return items, n


def _inserted_role_ids(sql: str) -> set[str]:
    """Role ids the given SQL INSERTs into `roles`, read by column position.

    WHY NOT A LAYOUT PATTERN. This used to be one regex requiring exactly four
    leading spaces, ``role_id`` immediately followed by ``domain`` on the same
    line, and the domain to be one of eleven hard-coded names. All three are
    properties of the emitter that happened to write migrations 155 and 156,
    none is a property of SQL, and each failure is SILENT in the direction that
    says "no migration carries this role":

    * a hand-written INSERT, a different indent, or a columns-reordered emit
      stops being seen;
    * a TWELFTH domain — adding one is a data change, not a code change —
      quietly un-ships every role in it.

    Since E3 dropped ``roles.origin`` this function is the ONLY provenance
    answer in the system. A false "not carried" now routes a repaired body away
    from the repo and into the database, silently, so the parser has to follow
    the SQL rather than the house style: find ``role_id``'s index in the column
    list, take that position out of each VALUES tuple.
    """
    from okuro.db.sqlite import _strip_sql_comments

    out: set[str] = set()
    # Comments stripped first: a migration's header quotes its own INSERT more
    # often than not, and 162's header quotes a SELECT over this very column.
    body = _strip_sql_comments(sql)

    for match in _INSERT_INTO_ROLES.finditer(body):
        i = match.end()
        while i < len(body) and body[i] in " \t\r\n":
            i += 1
        if i >= len(body) or body[i] != "(":
            # `INSERT INTO roles SELECT …` or `… DEFAULT VALUES` — no column
            # list to position against, so there is nothing to read here.
            continue

        columns, i = _paren_group(body, i)
        names = [c.strip().strip('"').strip().lower() for c in columns]
        if "role_id" not in names:
            continue
        index = names.index("role_id")

        values = _VALUES_KEYWORD.search(body, i)
        if not values:
            continue
        j = values.end()

        # One statement may carry many tuples: `VALUES (…), (…), (…)`.
        while True:
            while j < len(body) and body[j] in " \t\r\n":
                j += 1
            if j >= len(body) or body[j] != "(":
                break
            items, j = _paren_group(body, j)
            if index < len(items):
                raw = items[index].strip()
                if len(raw) >= 2 and raw[0] == "'" and raw[-1] == "'":
                    out.add(raw[1:-1].replace("''", "'"))
            while j < len(body) and body[j] in " \t\r\n":
                j += 1
            if j < len(body) and body[j] == ",":
                j += 1
                continue
            break
    return out


def migration_carried_ids(exclude: str | None = None) -> set[str]:
    """Role ids any migration INSERTs — the 'is shipped' signal without a column.

    The owner ruled E3: ``roles.origin`` may not be read by the shipping split,
    and migration 162 has since dropped the column outright. "Carried by a
    migration" is the computed replacement, and now the only answer there is.

    ``exclude`` skips the file being generated, because 156 asking whether 156
    already ships a role would answer yes on every regeneration and the split
    would drift on each run.
    """
    out: set[str] = set()
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if exclude and path.name == exclude:
            continue
        sql = path.read_text(encoding="utf-8", errors="replace")
        if not _INSERT_INTO_ROLES.search(sql):
            continue
        out |= _inserted_role_ids(sql)
    return out



# ---------------------------------------------------------------------------
# section authoring + rendering
# ---------------------------------------------------------------------------



def render_cognitive(a: dict) -> str:
    c = a["cognitive"]
    beh = "\n".join(f"- {b}" for b in c["behavioral"])
    hyp = "\n".join(f"- {b}" for b in c["hyperfocus"])
    cond = "\n".join(f"- {b}" for b in c["conditions"])
    return (
        "> **Intensity: Neutral (0)** — Profile documented but not actively "
        "expressed. Adjust via `Cognitive intensity: {0-4}`\n"
        "\n"
        # The key comes from the canonical map, never from the authored JSON:
        # a model that spells the NAME right can still hand back a key of its
        # own ("adhd-hf"), and the pair is what the fleet is read on.
        f"### Primary: {c['primary']['name']} "
        f"({_PROFILE_KEY.get(c['primary']['name'], c['primary'].get('key', ''))})\n"
        f"{c['primary']['text']}\n"
        "\n"
        f"### Secondary: {c['secondary']['name']} "
        f"({_PROFILE_KEY.get(c['secondary']['name'], c['secondary'].get('key', ''))})\n"
        f"{c['secondary']['text']}\n"
        "\n"
        "### Behavioral Expression (when active)\n"
        f"{beh}\n"
        "\n"
        "### Hyperfocus Triggers\n"
        f"{hyp}\n"
        "\n"
        "### Communication Preferences\n"
        f"- **Prefers:** {c['prefers']}\n"
        f"- **Avoids:** {c['avoids']}\n"
        "\n"
        "### Optimal Working Conditions\n"
        f"{cond}\n"
        "\n"
        "### Intensity Adjustment\n"
        "```\n"
        "Cognitive intensity: {0-4}\n"
        "```\n"
        "| Level | Expression |\n"
        "|-------|------------|\n"
        "| 0 | Neutral (default) — profile dormant |\n"
        "| 1 | Subtle |\n"
        "| 2 | Moderate |\n"
        "| 3 | Strong |\n"
        "| 4 | Maximum |\n"
    )


def render_expertise(a: dict) -> str:
    parts = []
    for area in a["expertise_areas"]:
        parts.append(f"### {area['area']}\n**{area['capability']}**: {area['detail']}")
    if a.get("sources"):
        parts.append("### Authoritative Sources\n"
                     + "\n".join(f"- {s}" for s in a["sources"]))
    return "\n\n".join(parts) + "\n"


def render_tools(a: dict, declared: list[str]) -> str:
    req = a.get("tools_required") or []
    opt = a.get("tools_optional") or []
    out = ["### Required"]
    out += [f"- **{t['tool']}:** {t['use']}" for t in req]
    declared = [t for t in declared if t.lower() not in retired_tools()]
    if not req:
        out += [f"- **{t}:** declared on the role row" for t in declared] or [
            "- **cortex_search / cortex_route:** locate the code and documents "
            "this role reasons about"
        ]
    out.append("")
    out.append("### Optional")
    out += [f"- **{t['tool']}:** {t['use']}" for t in opt] or [
        "- **bridge_invoke:** delegate a bounded sub-question to another model"
    ]
    return "\n".join(out) + "\n"


def render_protocol(a: dict) -> str:
    p = a["protocol"]
    steps = "\n".join(f"{i}. {s}" for i, s in enumerate(p["process"], 1))
    return (
        "### INPUT\n"
        f"{p['input']}\n"
        "\n"
        "### PROCESS\n"
        f"{steps}\n"
        "\n"
        "### OUTPUT\n"
        f"{p['output']}\n"
    )


# ---------------------------------------------------------------------------
# grade repair
# ---------------------------------------------------------------------------


def missing_full(text: str) -> set[str]:
    """Which FULL_REQUIRED labels ``text`` lacks — the GATE's own patterns.

    Deliberately not ``_canon_h2``: the gate matches ``^##\\s+PROTOCOL``, so a
    heading named ``## cortex INTEGRATION PROTOCOL`` satisfies a loose
    canonical mapping and NOT the gate. Three roles (phase-summarizer,
    sys-engineer, sys-researcher) failed exactly that way on the first run —
    the repair thought PROTOCOL was present and the audit disagreed.

    FENCE-AWARE, for the same reason and with the same urgency. The gate now
    ignores markers inside a fenced output template; if this function did
    not, the repair would read a fenced ``**Archetype:**`` as present, insert
    nothing, and hand back a body the gate still fails — a repair that
    reports success and changes nothing. The agreement between these two
    scans is the contract, not an optimisation.
    """
    return {label for label, pat in FULL_REQUIRED
            if not search_unfenced(pat, text or "", re.M | re.I)}


def render_personality(a: dict) -> str:
    return (
        "### Personality\n"
        f"- **Archetype:** {a['archetype']}\n"
        f"- **Experience Level:** {a['experience_level']}\n"
    )


def render_traits(a: dict) -> str:
    rows = "\n".join(
        f"| {t['axis']} | {int(t['value'])} | {t['meaning']} |" for t in a["traits"]
    )
    return ("### Traits (0-100)\n"
            "| Trait | Value | Meaning |\n"
            "|-------|-------|---------|\n"
            f"{rows}\n")


def render_neurotype(a: dict) -> str:
    nt = "\n".join(f"- {b}" for b in a["neurotypical"])
    na = "\n".join(f"- {b}" for b in a["neuroatypical"])
    return ("### Neurotype Balance (50/50 MANDATORY)\n"
            "**Neurotypical Behaviors (50%):**\n"
            f"{nt}\n\n"
            "**Neuroatypical Behaviors (50%):**\n"
            f"{na}\n")


def repair_full(row: dict, full: str, authored: dict) -> tuple[str, list[str]]:
    """Insert every missing gating section into the full body.

    Never rewrites prose the role already has: a missing section is ADDED, an
    existing one is left exactly as it is, and a body that is already complete
    comes back byte-identical. The three persona subsections live inside
    ``## IDENTITY``, so each missing one is appended to the existing IDENTITY
    rather than a second IDENTITY being created — one role (weblog-maintainer)
    has Personality and Traits and only lacks the Neurotype half.
    """
    rid = row["role_id"]
    reasons: list[str] = []
    miss = missing_full(full)
    a = authored.get(rid) or {}
    if not miss:
        return full, []
    if not a.get("traits") and miss & {
        "Personality (Archetype + Experience Level)",
        "Traits (0-100) table",
        "Neurotype Balance (50/50 MANDATORY)",
        "COGNITIVE PROFILE",
    }:
        # No authored data yet: leave the body alone rather than ship a
        # skeleton with placeholders in it.
        pass

    pre, secs = split_h2(full)
    identity_add: list[str] = []
    if "Personality (Archetype + Experience Level)" in miss and a.get("archetype"):
        identity_add.append(render_personality(a))
    if "Traits (0-100) table" in miss and a.get("traits"):
        identity_add.append(render_traits(a))
    if "Neurotype Balance (50/50 MANDATORY)" in miss and a.get("neurotypical"):
        identity_add.append(render_neurotype(a))

    if identity_add:
        block = "\n".join(identity_add)
        if "IDENTITY" not in miss:
            secs = [
                (h, (b.rstrip() + "\n\n" + block).strip()
                 if _canon_h2(h) == "IDENTITY" else b)
                for h, b in secs
            ]
        else:
            secs = insert_sections(pre, secs, [("IDENTITY", block)])
        reasons.append("structure: IDENTITY persona sections authored — "
                       + ", ".join(sorted(
                           miss & {"Personality (Archetype + Experience Level)",
                                   "Traits (0-100) table",
                                   "Neurotype Balance (50/50 MANDATORY)",
                                   "IDENTITY"})))

    new: list[tuple[str, str]] = []
    if "COGNITIVE PROFILE" in miss and a.get("cognitive"):
        new.append(("COGNITIVE PROFILE", render_cognitive(a)))
        reasons.append("structure: COGNITIVE PROFILE authored")
    if "EXPERTISE" in miss and a.get("expertise_areas"):
        new.append(("EXPERTISE", render_expertise(a)))
        reasons.append("structure: EXPERTISE authored")
    if "TOOLS" in miss:
        new.append(("TOOLS", render_tools(a, tools_list(row.get("tools")))))
        reasons.append("structure: TOOLS authored")
    if "PROTOCOL" in miss and a.get("protocol"):
        new.append(("PROTOCOL", render_protocol(a)))
        reasons.append("structure: PROTOCOL authored")
    if new:
        secs = insert_sections(pre, secs, new)

    if not reasons:
        return full, []

    if not any(_canon_h2(h) == "KNOWLEDGE PROTOCOL" for h, _ in secs):
        secs = insert_sections(
            pre, secs,
            [("KNOWLEDGE PROTOCOL",
              knowledge_protocol_block(rid).split("\n", 1)[1].strip())],
        )
        reasons.append("structure: KNOWLEDGE PROTOCOL added (advisory)")

    if not pre.strip().startswith("#"):
        title = rid.replace("-", " ").title()
        pre = f"# {title}\n\n{pre.lstrip()}"
    return join_h2(pre, secs), reasons


def persona_digest(full: str, a: dict) -> str:
    """The one-line persona the lean grade carries instead of the block."""
    arch = field(full, "Archetype") or a.get("archetype", "")
    if not arch:
        m = re.search(r"(?m)^\s*-?\s*\*\*Archetype:\*\*\s*(.+)$", full)
        arch = m.group(1).strip() if m else "Analyst"
    m = re.search(r"(?m)^\s*-?\s*\*\*Experience Level:\*\*\s*(.+)$", full)
    exp = m.group(1).strip() if m else a.get("experience_level", "Senior")

    values: list[str] = []
    for axis, short in zip(TRAIT_AXES, _AXIS_SHORT):
        m = re.search(rf"(?m)^\|\s*{re.escape(axis)}\s*\|\s*(\d+)\s*\|", full)
        if m:
            values.append(f"{short} {m.group(1)}")
    if not values and a.get("traits"):
        values = [f"{s} {int(t['value'])}"
                  for s, t in zip(_AXIS_SHORT, a["traits"])]

    nt = na = ""
    m = re.search(r"(?ms)\*\*Neurotypical Behaviors \(50%\):\*\*\s*\n-\s*(.+?)\s*$", full)
    if m:
        nt = m.group(1).splitlines()[0].strip()
    m = re.search(r"(?ms)\*\*Neuroatypical Behaviors \(50%\):\*\*\s*\n-\s*(.+?)\s*$", full)
    if m:
        na = m.group(1).splitlines()[0].strip()
    if not nt and a.get("neurotypical"):
        nt, na = a["neurotypical"][0], a["neuroatypical"][0]

    cog = ""
    m = re.search(r"(?m)^###\s+Primary:\s*(.+?)\s*\(", full)
    p = m.group(1).strip() if m else (a.get("cognitive", {}).get("primary", {}) or {}).get("name", "")
    m = re.search(r"(?m)^###\s+Secondary:\s*(.+?)\s*\(", full)
    s = m.group(1).strip() if m else (a.get("cognitive", {}).get("secondary", {}) or {}).get("name", "")
    if p:
        cog = f" · cognitive {p}" + (f"+{s}" if s else "")

    line = f"{arch}, {exp}"
    if values:
        line += "\ntraits " + " · ".join(values)
    if nt:
        line += f"\n· neurotype 50/50 ({nt} / {na})"
    return line + cog


def build_core(row: dict, full: str, authored: dict) -> str:
    """The lean grade's ``## CORE`` body, from the full body's own fields."""
    rid = row["role_id"]
    a = authored.get(rid) or {}
    _, secs = split_h2(full)

    purpose = field(full, "Purpose") or (row.get("description") or rid)
    expertise = field(full, "Expertise")
    if not expertise:
        expertise = ", ".join(x["area"] for x in a.get("expertise_areas", [])[:6])
    constraints = field(full, "Constraints")

    skills = bullets(section_body(secs, "SKILLS"), 6)
    if not skills:
        skills = [x["capability"] for x in a.get("expertise_areas", [])[:4]]

    core = [
        f"name: {rid}",
        f"domain: {row['domain']}",
        f"tier: {row['_tier_new']}",
        f"model: {row.get('model') or 'sonnet'}",
        "",
        f"**Purpose:** {purpose}",
    ]
    if expertise:
        core += ["", f"**Expertise:** {expertise}"]
    core += ["", f"**Persona digest:** {persona_digest(full, a)}"]
    if constraints:
        core += ["", "**Constraints:**"] + [f"- {c}" for c in constraint_items(full)]
    if skills:
        core += ["", "**Skills:**"] + [f"{i}. {s}" for i, s in enumerate(skills, 1)]
    return "\n".join(core) + "\n"


def repair_lean(row: dict, full: str, lean: str, authored: dict) -> tuple[str, list[str]]:
    """Add the lean grade's missing gating sections, keeping its own prose.

    The first version of this rebuilt every lean that failed the gate, which
    would have replaced 27 roles' real lean text because it lacked one
    heading. A missing section is ADDED here, exactly as in the full grade —
    only a lean that is EMPTY or was never a lean (the inverted rows, where
    the lean text is being promoted to full) gets assembled from scratch.
    """
    rid = row["role_id"]
    reasons: list[str] = []
    has_core = bool(re.search(r"(?m)^##\s+CORE", lean or "", re.I))
    has_proto = bool(re.search(r"(?m)^##\s+PROTOCOL", lean or "", re.I))
    if has_core and has_proto:
        return lean, []

    pre, secs = split_h2(lean or "")
    if not has_core:
        secs = [("CORE", build_core(row, full, authored))] + secs
        reasons.append("structure: lean CORE assembled from the full body's fields")
    if not has_proto:
        body = section_body(split_h2(full)[1], "PROTOCOL")
        if not body and (authored.get(rid) or {}).get("protocol"):
            body = render_protocol(authored[rid])
        if body:
            secs = insert_sections(pre, secs, [("PROTOCOL", body)])
            reasons.append("structure: lean PROTOCOL carried over from the full grade")
    if not reasons:
        # Nothing was added, so return the ORIGINAL bytes. ``join_h2`` reflows
        # blank lines, and a lean that comes back reflowed counts as changed
        # with an empty reason list — an UPDATE in the migration that no line
        # of the diff can explain.
        return lean, []
    if not pre.strip():
        pre = f"# {rid.replace('-', ' ').title()}"
    return join_h2(pre, secs), reasons


def build_lean(row: dict, full: str, authored: dict) -> str:
    """A lean grade built from the full body's own parts.

    NOT a truncation. ``m5_fix_broken_roles._project_lean_from_full`` cut the
    full body at ~45% on a paragraph boundary, and the result cannot pass
    ``LEAN_REQUIRED``: the top of a full body is IDENTITY, never ``## CORE``.
    So this assembles the spine the lean grade is specified to have — CORE
    with the persona compressed to a digest line, then PROTOCOL, REFERENCES
    and COLLABORATION carried over from the full body.
    """
    rid = row["role_id"]
    a = authored.get(rid) or {}
    pre, secs = split_h2(full)
    core = ["## CORE", "", build_core(row, full, authored).rstrip()]

    proto = section_body(secs, "PROTOCOL")
    if not proto and a.get("protocol"):
        proto = render_protocol(a)
    refs = section_body(secs, "SOURCES") or ""
    if not refs:
        exp_body = section_body(secs, "EXPERTISE")
        m = re.search(r"(?ms)^###\s+Authoritative Sources\s*\n(.+?)(?=\n###|\Z)", exp_body)
        refs = m.group(1).strip() if m else ""
    collab = section_body(secs, "COLLABORATION")
    success = section_body(secs, "SUCCESS CRITERIA")

    out = [f"# {(pre.strip().splitlines() or ['# ' + rid])[0].lstrip('# ').strip()}",
           "", "\n".join(core)]
    if proto:
        out += ["", "---", "", "## PROTOCOL", "", proto.strip()]
    if success:
        out += ["", "### Success Criteria", "", success.strip()]
    if refs:
        out += ["", "---", "", "## REFERENCES", "", refs.strip()]
    if collab:
        out += ["", "---", "", "## COLLABORATION", "", collab.strip()]
    out += ["", "---", "", "END OF ROLE"]
    return "\n".join(out).strip() + "\n"


_TRUNC_NOTE = ("\n\n_(trimmed to the lean budget — the full grade is on demand "
               'via `roles_get(level="full")`)_')


def trim_to_budget(lean: str, budget: int) -> str:
    """Bring a lean grade under budget, cutting the least load-bearing first.

    Works on parsed sections, not on a regex over separators: the first
    version matched ``\\n---\\n\\n## NAME`` and every lean written without that
    rule kept its sections and stayed over budget (industrial-designer came
    out at 9,031c against a 7,500c budget).

    Order: the lean grade's RECOMMENDED sections go first, then anything not
    gating, then the largest gating section is truncated. ``## CORE`` is cut
    last and never removed — it is what the bootstrap role slice reads.
    """
    if len(lean) <= budget:
        return lean
    pre, secs = split_h2(lean)
    for drop in ("COLLABORATION", "REFERENCES", "SOURCES"):
        if len(join_h2(pre, secs)) <= budget:
            break
        secs = [(h, b) for h, b in secs if _canon_h2(h) != drop]
    keep_names = {"CORE", "PROTOCOL"}
    while len(join_h2(pre, secs)) > budget:
        extras = [i for i, (h, _) in enumerate(secs)
                  if (_canon_h2(h) or h.upper()) not in keep_names]
        if extras:
            biggest = max(extras, key=lambda i: len(secs[i][1]))
            del secs[biggest]
            continue
        over = len(join_h2(pre, secs)) - budget + len(_TRUNC_NOTE) + 8
        idx = max(range(len(secs)), key=lambda i: len(secs[i][1]))
        if _canon_h2(secs[idx][0]) == "CORE" and len(secs) > 1:
            idx = max((i for i in range(len(secs))
                       if _canon_h2(secs[i][0]) != "CORE"),
                      key=lambda i: len(secs[i][1]))
        h, b = secs[idx]
        keep = max(len(b) - over, 300)
        if keep >= len(b):
            break
        cut = b[:keep]
        nl = cut.rfind("\n")
        secs[idx] = (h, (cut[:nl] if nl > 200 else cut).rstrip() + _TRUNC_NOTE)
    return join_h2(pre, secs)


def build_micro(row: dict, full: str, lean: str, authored: dict, existing: str) -> str:
    """Flat YAML. ``purpose`` and ``expertise`` drive description + embedding."""
    rid = row["role_id"]
    a = authored.get(rid) or {}
    parsed = None
    if (existing or "").strip():
        try:
            got = yaml.safe_load(existing)
            parsed = got if isinstance(got, dict) else None
        except yaml.YAMLError:
            parsed = None

    purpose = (parsed or {}).get("purpose") or field(full, "Purpose") \
        or (row.get("description") or rid)
    purpose = re.sub(r"\s+", " ", str(purpose)).strip()

    expertise = (parsed or {}).get("expertise")
    if not expertise:
        raw = field(full, "Expertise") or field(lean, "Expertise")
        expertise = [x.strip() for x in raw.split(",") if x.strip()][:8]
    if isinstance(expertise, str):
        expertise = [x.strip() for x in expertise.split(",") if x.strip()]
    if not expertise:
        expertise = [x["area"] for x in a.get("expertise_areas", [])][:6]
    if not expertise:
        expertise = [row["domain"]]

    _, secs = split_h2(full)
    data: dict = {
        "id": rid,
        "domain": row["domain"],
        "tier": row["_tier_new"],
        "model": row.get("model") or "sonnet",
        "purpose": purpose,
        "expertise": [str(e) for e in expertise][:8],
    }
    cons = (parsed or {}).get("constraints") or constraint_items(full)[:4]
    if cons:
        data["constraints"] = [str(c) for c in cons][:4]
    skills = (parsed or {}).get("skills") or bullets(section_body(secs, "SKILLS"), 5)
    if skills:
        data["skills"] = [clip(s, 120) for s in skills][:5]
    proto = (parsed or {}).get("protocol") \
        or protocol_steps(section_body(secs, "PROTOCOL"), a)
    if proto:
        data["protocol"] = [clip(s, 120) for s in proto][:6]
    tools = (parsed or {}).get("tools") or tools_list(row.get("tools")) or [
        t["tool"] for t in (a.get("tools_required") or [])
    ]
    tools = [str(t) for t in tools if str(t).lower() not in retired_tools()]
    if tools:
        data["tools"] = tools[:8]
    succ = (parsed or {}).get("success") or bullets(section_body(secs, "SUCCESS CRITERIA"), 3)
    if succ:
        data["success"] = [clip(s, 160) for s in succ][:3]

    def _dump() -> str:
        return yaml.safe_dump(data, sort_keys=False, default_flow_style=None,
                              allow_unicode=True, width=200)

    # TERMINATION IS STRUCTURAL, not hoped for. The first version looped
    # `while over budget`, dropping one optional key per pass and falling back
    # to `expertise[:3]` / `purpose[:400]` once there was nothing left to drop
    # — and that fallback is IDEMPOTENT, so a role whose remaining required
    # fields are still over budget (one 2,000-character expertise item is
    # enough) spins forever. Every step below strictly shrinks the payload or
    # the sequence ends.
    text = _dump()
    droppable = ["success", "protocol", "skills", "constraints", "tools"]
    while len(text) > SIZE_BUDGETS["micro"] and droppable:
        data.pop(droppable.pop(0), None)
        text = _dump()

    # Nothing optional is left. Shrink what MUST stay, in decreasing steps.
    # `purpose` and `expertise` cannot be dropped: they are the only inputs to
    # the role's description and its match embedding.
    for limit in (200, 120, 80, 50, 30):
        if len(text) <= SIZE_BUDGETS["micro"]:
            break
        data["expertise"] = [clip(e, limit) for e in data["expertise"][:3]]
        data["purpose"] = clip(data["purpose"], max(limit * 2, 200))
        text = _dump()
    return text



# ---------------------------------------------------------------------------
# the operations vocabulary
# ---------------------------------------------------------------------------

#: The grades an operation may name. ``full`` is the source of truth; the
#: other two are projections of it, which is why ``rebuild_grade`` only
#: accepts those two.
PLAN_GRADES: tuple[str, ...] = ("full", "lean", "micro")

#: op -> the keys it REQUIRES. Extra keys are refused rather than ignored: a
#: typo'd key that is silently dropped is an instruction the reviewer read and
#: the planner did not.
OPERATIONS: dict[str, frozenset[str]] = {
    "add_section": frozenset({"label"}),
    "rename_section": frozenset({"label", "to"}),
    "remove_section": frozenset({"label"}),
    "rewrite_block": frozenset({"label", "instruction"}),
    "sweep_string": frozenset({"pattern"}),
    "set_field": frozenset({"column"}),
    "rebuild_grade": frozenset({"grade"}),
}

#: op -> the keys it ACCEPTS beyond the required ones.
_OPTIONAL: dict[str, frozenset[str]] = {
    "add_section": frozenset({"grade", "body", "instruction", "authored"}),
    "rename_section": frozenset({"grade"}),
    "remove_section": frozenset({"grade"}),
    "rewrite_block": frozenset({"grade", "authored"}),
    "sweep_string": frozenset({"replacement", "grades", "regex", "reason"}),
    "set_field": frozenset({"value", "map"}),
    "rebuild_grade": frozenset(set()),
}

#: Columns ``set_field`` may write. Body columns are excluded ON PURPOSE: a
#: prompt column changed by ``set_field`` would bypass every gate in this
#: module, which is the one thing the operation list exists to prevent.
SETTABLE_FIELDS: frozenset[str] = frozenset(
    {"tier", "model", "maintenance_schedule", "panel_eligible", "domain"}
)


@dataclass(frozen=True)
class Operation:
    """One structural change, as a row a human can read in the dry-run diff.

    Frozen because a plan is computed twice — once for the diff the owner
    approves and once for the write — and an operation that mutated between
    them would make the approved diff a description of something else.
    """

    op: str
    args: dict

    # -- the accessors the planner uses, so a typo is a KeyError here rather
    # -- than an AttributeError three frames down --------------------------
    @property
    def grade(self) -> str:
        return str(self.args.get("grade") or "full")

    @property
    def label(self) -> str:
        return str(self.args.get("label") or "")

    def describe(self) -> str:
        """The one line the dry-run diff shows for this operation."""
        a = self.args
        if self.op == "add_section":
            return f"add section {self.label!r} to the {self.grade} grade"
        if self.op == "rename_section":
            return f"rename section {self.label!r} to {a['to']!r} ({self.grade})"
        if self.op == "remove_section":
            return f"remove section {self.label!r} from the {self.grade} grade"
        if self.op == "rewrite_block":
            return f"rewrite section {self.label!r} ({self.grade})"
        if self.op == "sweep_string":
            kind = "pattern" if a.get("regex") else "string"
            return f"sweep {kind} {a['pattern']!r} -> {a.get('replacement', '')!r}"
        if self.op == "set_field":
            if a.get("map"):
                return f"map column {a['column']} through {len(a['map'])} value(s)"
            return f"set column {a['column']} = {a.get('value')!r}"
        if self.op == "rebuild_grade":
            return f"rebuild the {a['grade']} grade from the full body"
        return f"{self.op} {a}"  # unreachable: parse_operations gates it


def parse_operations(instructions) -> list[Operation]:
    """``instructions`` -> validated operations, or a refusal naming the key.

    Accepts the two shapes an action's instruction arrives in: the dict the
    API and MCP layers carry (``{"operations": [...]}``) and a bare list.
    Anything else is refused — including an empty list, because "change these
    roles somehow" is the instruction shape this whole workstream exists to
    stop.
    """
    if isinstance(instructions, str):
        try:
            instructions = json.loads(instructions)
        except ValueError as exc:
            raise PlanRefused(f"instructions is not JSON: {exc}") from exc
    if isinstance(instructions, dict):
        instructions = instructions.get("operations")
    if not isinstance(instructions, (list, tuple)):
        raise PlanRefused(
            "instructions must carry an `operations` list — a structural "
            "change with no named operation cannot be diffed, reviewed or "
            "reversed"
        )
    if not instructions:
        raise PlanRefused(
            "the operations list is empty. 'Change these roles' is not an "
            f"instruction; the vocabulary is {', '.join(sorted(OPERATIONS))}"
        )

    out: list[Operation] = []
    for i, raw in enumerate(instructions):
        if not isinstance(raw, dict):
            raise PlanRefused(f"operation {i} is not an object: {raw!r}")
        args = dict(raw)
        op = str(args.pop("op", "") or "").strip()
        if op not in OPERATIONS:
            raise PlanRefused(
                f"operation {i}: {op!r} is not an operation this planner "
                f"performs. The vocabulary is {', '.join(sorted(OPERATIONS))}."
            )
        missing = sorted(OPERATIONS[op] - set(args))
        if missing:
            raise PlanRefused(
                f"operation {i} ({op}): missing required key(s) "
                f"{', '.join(missing)}"
            )
        extra = sorted(set(args) - OPERATIONS[op] - _OPTIONAL[op])
        if extra:
            # Refused rather than dropped. A key the planner ignores is a key
            # the reviewer believed was applied.
            raise PlanRefused(
                f"operation {i} ({op}): {', '.join(extra)} is not a key this "
                f"operation accepts — it would be read by the reviewer and "
                f"ignored by the planner"
            )
        grade = str(args.get("grade") or ("full" if op != "rebuild_grade" else ""))
        if op == "rebuild_grade":
            if args["grade"] not in ("lean", "micro"):
                raise PlanRefused(
                    f"operation {i}: rebuild_grade takes 'lean' or 'micro'. "
                    f"The full grade is the source the other two are built "
                    f"from; rebuilding it from itself is not a change."
                )
        elif grade not in PLAN_GRADES:
            raise PlanRefused(
                f"operation {i} ({op}): {grade!r} is not a grade "
                f"({', '.join(PLAN_GRADES)})"
            )
        if op == "set_field" and args["column"] not in SETTABLE_FIELDS:
            raise PlanRefused(
                f"operation {i}: set_field may not write {args['column']!r}. "
                f"Writable: {', '.join(sorted(SETTABLE_FIELDS))}. A body "
                f"column set here would bypass every gate in this module."
            )
        if op == "set_field" and "value" not in args and "map" not in args:
            raise PlanRefused(
                f"operation {i}: set_field needs either `value` or `map`"
            )
        out.append(Operation(op=op, args=args))
    return out

# ---------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------

#: The columns a plan reads and an emitted migration writes. Same list as the
#: generator's ``_SELECT`` and for the same reason: NO ``origin``. The owner
#: ruled E3 that the column is dropped later, so nothing that outlives one
#: migration may read it — "is this role carried by a migration" comes from
#: :func:`migration_carried_ids`, which parses the migrations themselves.
PLAN_COLUMNS: tuple[str, ...] = (
    "role_id", "domain", "description", "maturity", "tier", "model",
    "prompt", "lean_prompt", "micro_prompt", "tools", "panel_eligible",
    "maintenance_schedule", "person_preset",
)

#: How a role's repaired body reaches the store. Two carriers, and which one a
#: role takes is COMPUTED per body at emit time rather than read from a
#: column — see :func:`carrier_for`.
CARRIER_MIGRATION = "migration"
CARRIER_DATABASE = "database"


def carrier_for(migration_carried: bool, private_tokens) -> str:
    """Which half of the split this role's write belongs to.

    TWO FACTS, AND BOTH HAVE TO BE TRUE FOR THE REPO HALF.

    *Migration-carried* is the owner's Q2 split by origin, computed the way E3
    forces it to be computed: membership in the role-id set some migration
    already INSERTs, parsed out of the migration files. Never
    ``roles.origin`` — migration 162 dropped that column, so a split reading
    it does not break silently any more, it does not run at all.

    *Guard-clean* is amendment E1, decided per BODY at emit time rather than
    per role once: a role whose text names a real customer, a board member,
    this host or its disks does not go into a file the release-by-export
    pipeline reads, however it got into the store. A role that loses its last
    private token ships on the next run with nobody editing a list, and one
    that gains one stops shipping the same way.

    So the migration carries a role only when it is already a migration's to
    carry AND its text may leave this machine. Everything else is written in
    the database, where it already was.
    """
    return (
        CARRIER_MIGRATION
        if migration_carried and not private_tokens
        else CARRIER_DATABASE
    )


@dataclass
class RolePlan:
    """One role's before and after, with the reason for every difference."""

    role_id: str
    domain: str | None
    maturity: str | None
    #: The raw row, for the emitter and for ``update_role_fields``.
    row: dict = dc_field(default_factory=dict)
    before: dict = dc_field(default_factory=dict)
    after: dict = dc_field(default_factory=dict)
    #: The three grades as they will be written.
    body: dict = dc_field(default_factory=dict)
    #: The three grades as they WERE. Stored rather than re-read at restore
    #: time, and rendered opposite `body` in the dry-run diff.
    #:
    #: WHY THE PLAN CARRIES IT. Two callers needed the before text and both
    #: were reconstructing it from somewhere else. The diff rendered only the
    #: `after` bodies, so the reviewer read the proposed text with nothing to
    #: compare it against and the word "diff" was doing work the document did
    #: not do. And a rollback that reads the store's snapshot cannot tell the
    #: change it is reversing from an unrelated edit made after it — the plan
    #: can, because it knows what it started from.
    body_before: dict = dc_field(default_factory=dict)
    reasons: list[str] = dc_field(default_factory=list)
    #: Why this role was left alone, when it was. Empty means it was not.
    refused: str = ""
    migration_carried: bool = False
    private_tokens: list[str] = dc_field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.reasons) and not self.refused

    @property
    def carrier(self) -> str:
        return carrier_for(self.migration_carried, self.private_tokens)

    def to_emit_entry(self) -> dict:
        """This role in the shape :func:`emit_migration_sql` consumes.

        The generator builds the same shape by hand. Keeping ONE consumer
        shape is what lets both callers share the emitter, which is the half
        of this module that decides what reaches the repo.
        """
        return {
            "domain": self.domain,
            "maturity": self.maturity,
            "db_only": not self.migration_carried,
            "private_tokens": list(self.private_tokens),
            "ships_in_migration": self.carrier == CARRIER_MIGRATION,
            "changed": self.changed,
            "reasons": list(self.reasons),
            "before": self.before,
            "after": self.after,
            "_body": dict(self.body),
            "_body_before": dict(self.body_before),
            "_row": {k: self.row.get(k) for k in
                     ("domain", "description", "model", "tools",
                      "panel_eligible", "maintenance_schedule",
                      "person_preset", "maturity")},
        }


@dataclass
class Plan:
    """Every affected role's before/after, plus what produced it."""

    operations: list[Operation] = dc_field(default_factory=list)
    roles: dict[str, RolePlan] = dc_field(default_factory=dict)
    #: Roles named by the action that the store does not have. Listed rather
    #: than dropped: an action naming a role that does not exist is a finding
    #: about the action, and a planner that quietly skips it hides that.
    unknown_role_ids: list[str] = dc_field(default_factory=list)
    guard_armed: bool = False

    def by_carrier(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {CARRIER_MIGRATION: [], CARRIER_DATABASE: []}
        for rid, rp in sorted(self.roles.items()):
            if rp.changed:
                out[rp.carrier].append(rid)
        return out

    def to_emit_plan(self) -> dict:
        """The whole plan in the generator's dict shape."""
        return {rid: rp.to_emit_entry() for rid, rp in sorted(self.roles.items())}

    def summary(self) -> dict:
        changed = [r for r in self.roles.values() if r.changed]
        refused = [r for r in self.roles.values() if r.refused]
        split = self.by_carrier()
        return {
            "roles": len(self.roles),
            "changed": len(changed),
            "refused": len(refused),
            "unknown_role_ids": list(self.unknown_role_ids),
            "guard_armed": self.guard_armed,
            "migration_carried": split[CARRIER_MIGRATION],
            "database_only": split[CARRIER_DATABASE],
            "operations": [op.describe() for op in self.operations],
        }


# ---------------------------------------------------------------------------
# authoring — the judgement half, gated
# ---------------------------------------------------------------------------

_SECTION_SYSTEM = (
    "You write ONE section of an okuro role brief. You reply with the section "
    "BODY as markdown and nothing else: no heading, no preamble, no code "
    "fence, no commentary. Everything you write is derived from the role "
    "material you are given. The text must be host-agnostic — never name a "
    "machine, a customer, a company or a private person."
)


def _author_section(row: dict, label: str, grade: str, instruction: str,
                    current: str, invoke=None) -> str:
    """Author one section's body through the bridge. Returns "" on failure.

    An empty return is NOT an error the caller may ignore: the planner turns
    it into a refusal for that role, so a bridge that is down leaves the fleet
    exactly as it was rather than writing a section that says nothing. The
    recorded failure mode this guards is the opposite — a repair pass that
    treated an empty model response as "no change needed" and reported success
    over 34 roles it had not touched.
    """
    if invoke is None:  # pragma: no cover - exercised through the pipeline
        from okuro.bridge.invoke import invoke  # noqa: PLC0415

    prompt = (
        f"Role id: {row.get('role_id')}\n"
        f"Domain: {row.get('domain')}\n"
        f"Description: {row.get('description') or '(none)'}\n"
        f"Grade being edited: {grade}\n"
        f"Section: {label}\n\n"
        f"INSTRUCTION\n{instruction.strip()}\n\n"
        + (f"CURRENT SECTION BODY\n{current.strip()}\n\n" if current.strip() else "")
        + "ROLE BODY (the full grade, for context)\n"
        + (row.get("prompt") or row.get("lean_prompt") or "")[:12_000]
        + "\n\nReply with the new body of that section only."
    )
    try:
        res = invoke(prompt=prompt, capability="quality", tool=True,
                     system_prompt=_SECTION_SYSTEM, timeout=600)
    except Exception as exc:  # noqa: BLE001 — a bridge failure is a refusal
        log.error("authoring %s/%s for %s failed: %s",
                  grade, label, row.get("role_id"), exc)
        return ""
    if not res.get("success"):
        log.error("authoring %s/%s for %s refused: %s",
                  grade, label, row.get("role_id"), res.get("error"))
        return ""
    text = (res.get("output") or "").strip()
    text = re.sub(r"^```(?:markdown|md)?\s*|\s*```$", "", text).strip()
    # A heading the model added anyway: the caller owns the heading, because
    # `insert_sections` ranks on it and a body carrying its own "## LABEL"
    # would produce two.
    text = re.sub(rf"(?im)^##+[ \t]*{re.escape(label)}[ \t]*$\n?", "", text).strip()
    return text


# ---------------------------------------------------------------------------
# applying one operation to one grade
# ---------------------------------------------------------------------------


def _find_section(sections: list[tuple[str, str]], label: str) -> int:
    """Index of the section ``label`` names, or -1.

    Matches the canonical name first and the literal heading second, so
    ``{"label": "SOURCES"}`` finds a section headed ``## SOURCES & REFERENCES``
    the same way :func:`section_body` does — one matcher for the whole module,
    because two would disagree about exactly the headings the fleet spells
    unusually.
    """
    want = _canon_h2(label) or label.strip().upper()
    for i, (heading, _body) in enumerate(sections):
        if (_canon_h2(heading) or heading.strip().upper()) == want:
            return i
    return -1


def _apply_operation(op: Operation, row: dict, bodies: dict, fields: dict,
                     *, invoke=None) -> list[str]:
    """Apply one operation in place. Returns the reasons it produced.

    Raises :class:`PlanRefused` when the operation cannot be performed on THIS
    role — the planner catches it, leaves the role unchanged and records the
    sentence. Nothing here writes a store.
    """
    reasons: list[str] = []

    if op.op == "sweep_string":
        pattern = op.args["pattern"]
        repl = op.args.get("replacement", "")
        grades = op.args.get("grades") or list(PLAN_GRADES)
        rx = re.compile(pattern) if op.args.get("regex") else None
        for grade in grades:
            if grade not in PLAN_GRADES:
                raise PlanRefused(f"sweep_string names grade {grade!r}")
            text = bodies.get(grade) or ""
            new = rx.sub(repl, text) if rx else text.replace(pattern, repl)
            if new != text:
                bodies[grade] = new
                reasons.append(
                    op.args.get("reason")
                    or f"sweep: {pattern!r} replaced in the {grade} grade"
                )
        return reasons

    if op.op == "set_field":
        column = op.args["column"]
        current = fields.get(column, row.get(column))
        if "map" in op.args:
            mapped = op.args["map"].get(str(current or "").lower())
            if mapped is None or mapped == current:
                return []
            new = mapped
        else:
            new = op.args["value"]
            if new == current:
                return []
        fields[column] = new
        return [f"{column}: {current} -> {new}"]

    if op.op == "rebuild_grade":
        grade = op.args["grade"]
        full = bodies.get("full") or ""
        if not full.strip():
            raise PlanRefused(
                f"rebuild_grade({grade}) needs a full body to build from and "
                f"this role's is empty"
            )
        built = (
            build_lean(row, full, {})
            if grade == "lean"
            else build_micro(row, full, bodies.get("lean") or "", {},
                             bodies.get("micro") or "")
        )
        if built == bodies.get(grade):
            return []
        bodies[grade] = built
        return [f"fidelity: {grade} rebuilt from the full body's own parts"]

    # -- the four section operations ---------------------------------------
    grade = op.grade
    text = bodies.get(grade) or ""
    pre, secs = split_h2(text)
    idx = _find_section(secs, op.label)

    if op.op == "add_section":
        if idx >= 0:
            # Present already. Not an error and not a change: a fleet-wide
            # "add X" is expected to be a no-op on the roles that have it.
            return []
        body = str(op.args.get("body") or "")
        if op.args.get("instruction"):
            body = _author_section(row, op.label, grade,
                                   op.args["instruction"], "", invoke=invoke)
        if not body.strip():
            raise PlanRefused(
                f"add_section({op.label!r}) produced no body — a section "
                f"inserted empty passes no gate and says nothing"
            )
        secs = insert_sections(pre, secs, [(op.label, body.strip())])
        reasons.append(f"structure: {op.label} added to the {grade} grade")

    elif op.op == "remove_section":
        if idx < 0:
            return []
        del secs[idx]
        reasons.append(f"structure: {op.label} removed from the {grade} grade")

    elif op.op == "rename_section":
        if idx < 0:
            return []
        heading, body = secs[idx]
        secs[idx] = (str(op.args["to"]), body)
        reasons.append(
            f"structure: {heading} renamed to {op.args['to']} ({grade})"
        )

    elif op.op == "rewrite_block":
        if idx < 0:
            raise PlanRefused(
                f"rewrite_block({op.label!r}) found no such section in the "
                f"{grade} grade — rewriting a section that is not there would "
                f"silently become an insertion"
            )
        heading, body = secs[idx]
        new = _author_section(row, op.label, grade, op.args["instruction"],
                              body, invoke=invoke)
        if not new.strip():
            raise PlanRefused(
                f"rewrite_block({op.label!r}) came back empty; the role keeps "
                f"its current text rather than losing the section"
            )
        if new.strip() == body.strip():
            return []
        secs[idx] = (heading, new.strip())
        reasons.append(f"structure: {op.label} rewritten ({grade})")

    if reasons:
        if not pre.strip().startswith("#"):
            pre = f"# {row['role_id'].replace('-', ' ').title()}\n\n{pre.lstrip()}"
        bodies[grade] = join_h2(pre, secs)
    return reasons


# ---------------------------------------------------------------------------
# the planner
# ---------------------------------------------------------------------------


def _rows_for(db, role_ids) -> dict[str, dict]:
    """The named roles, whatever kind of handle ``db`` is.

    Accepts okuro's ``Database`` (``fetchall``) and a bare ``sqlite3``
    connection (``execute``), because the generator opens a store copy
    read-only and the pipeline holds the live handle. One reader, so the two
    callers cannot drift on which columns a plan is computed from.
    """
    cols = ", ".join(PLAN_COLUMNS)
    ids = [str(r).strip() for r in role_ids if str(r).strip()]
    if not ids:
        return {}
    marks = ", ".join("?" for _ in ids)
    sql = f"SELECT {cols} FROM roles WHERE role_id IN ({marks}) ORDER BY role_id"
    if hasattr(db, "fetchall"):
        rows = db.fetchall(sql, tuple(ids))
    else:  # sqlite3.Connection
        rows = db.execute(sql, tuple(ids)).fetchall()
    return {dict(r)["role_id"]: dict(r) for r in rows}


def plan_for_roles(db, role_ids, *, instructions, invoke=None) -> Plan:
    """Compute every named role's before and after under ``instructions``.

    NOTHING IS WRITTEN HERE, and that is the property the dry-run rests on:
    the same call produces the diff the owner approves and the bodies the
    implement step writes, so what he approved is what lands.

    A role is left UNCHANGED, with the reason recorded, when any of these is
    true after the operations run — never repaired half way:

    * an operation refused on this role (``PlanRefused`` from the operation);
    * the resulting body fails ``gate_role_body`` or loses a
      ``validate_role_structure`` section it had before;
    * the lean or micro grade is over budget after a trim attempt.

    The third is deliberately a refusal rather than a harder trim. A micro
    grade squeezed past its budget by cutting expertise is a role whose match
    embedding changed as a side effect of a structural edit nobody asked to
    have affect routing.
    """
    ops = parse_operations(instructions)
    rows = _rows_for(db, role_ids)
    wanted = [str(r).strip() for r in role_ids if str(r).strip()]
    carried = migration_carried_ids()
    plan = Plan(operations=ops, guard_armed=guard_is_armed())
    plan.unknown_role_ids = [r for r in wanted if r not in rows]

    for rid in wanted:
        row = rows.get(rid)
        if not row:
            continue
        full0 = row["prompt"] or ""
        lean0 = row["lean_prompt"] or ""
        micro0 = row["micro_prompt"] or ""
        before_gate = validate_role_structure(
            {"full": full0, "lean": lean0, "micro": micro0})

        bodies = {"full": full0, "lean": lean0, "micro": micro0}
        fields: dict = {}
        reasons: list[str] = []
        refused = ""
        try:
            for op in ops:
                reasons += _apply_operation(op, row, bodies, fields, invoke=invoke)
        except PlanRefused as exc:
            refused = str(exc)

        if not refused and len(bodies["lean"]) > SIZE_BUDGETS["lean"]:
            bodies["lean"] = trim_to_budget(bodies["lean"], SIZE_BUDGETS["lean"])
            reasons.append(
                f"size: lean trimmed to the {SIZE_BUDGETS['lean']}c budget")

        after_gate = validate_role_structure(bodies)
        gate = gate_role_body(bodies)

        if not refused and reasons:
            lost = _sections_lost(before_gate, after_gate)
            if lost:
                refused = (
                    "the change would drop gating section(s) "
                    f"{', '.join(lost)} that this role has today"
                )
            elif not gate["ok"]:
                refused = f"gate_role_body refused the result: {gate['error']}"
            elif len(bodies["micro"]) > SIZE_BUDGETS["micro"]:
                refused = (
                    f"micro would be {len(bodies['micro'])}c against the "
                    f"{SIZE_BUDGETS['micro']}c budget"
                )

        if refused:
            bodies = {"full": full0, "lean": lean0, "micro": micro0}
            fields = {}

        tier_after = fields.get("tier", row["tier"])
        tokens = sorted(
            guard_hits(bodies["full"]) | guard_hits(bodies["lean"])
            | guard_hits(bodies["micro"]))
        leg_after = {g: h for g, h in
                     ((g, legacy_hits(bodies[g])) for g in PLAN_GRADES) if h}

        plan.roles[rid] = RolePlan(
            role_id=rid,
            domain=row["domain"],
            maturity=row["maturity"],
            row={**row, **fields, "_tier_new": tier_after},
            before={
                "tier": row["tier"],
                "chars": {g: len(v) for g, v in
                          (("full", full0), ("lean", lean0), ("micro", micro0))},
                "ok": before_gate["ok"],
                "missing": before_gate["missing"],
                "legacy": {g: h for g, h in
                           (("full", legacy_hits(full0)),
                            ("lean", legacy_hits(lean0)),
                            ("micro", legacy_hits(micro0))) if h},
            },
            after={
                "tier": tier_after,
                "chars": {g: len(bodies[g]) for g in PLAN_GRADES},
                "ok": after_gate["ok"],
                "missing": after_gate["missing"],
                "gate_ok": gate["ok"],
                "gate_error": gate["error"],
                "legacy": leg_after,
                "fields": dict(fields),
            },
            body=bodies,
            body_before={"full": full0, "lean": lean0, "micro": micro0},
            reasons=sorted(set(reasons)) if not refused else [],
            refused=refused,
            migration_carried=rid in carried,
            private_tokens=tokens,
        )
    return plan


def _sections_lost(before: dict, after: dict) -> list[str]:
    """Gating sections the role HAD and would not have after the change.

    Asked as a difference rather than as "does it pass now", because 57 of the
    103 live roles did not pass before migration 156 either. A planner that
    demanded a green gate would refuse every change to a role that is already
    red, which is exactly the population a structural change is for. What must
    never happen is a change that makes it WORSE.
    """
    lost: list[str] = []
    for grade in PLAN_GRADES:
        had = set(before.get("missing", {}).get(grade) or [])
        now = set(after.get("missing", {}).get(grade) or [])
        lost += sorted(f"{grade}:{s}" for s in now - had)
    return lost

# ---------------------------------------------------------------------------
# sql emission — the split emitter
# ---------------------------------------------------------------------------

def q(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def emit_migration_sql(plan: dict, header: str) -> str:
    db_only = [r for r, e in plan.items() if e["db_only"]]
    if db_only and not guard_is_armed():
        # Fail closed when the guard CANNOT judge — which is not the same
        # question as whether it found anything. The first version refused
        # when the fleet produced zero hits, so a fleet that is genuinely
        # clean (the state this work is driving towards) would have been
        # unshippable, while the danger it guards against is the opposite
        # case: token files absent, every role reading as clean, the eleven
        # private roles going out silently. Ask whether the guard is armed.
        raise EmitRefused(
            "refusing to emit: the CONTENT guard loaded no tokens, so the E1 "
            "split cannot be computed. Generate this migration on the machine "
            "whose ~/.okuro/guard token files define what is private."
        )

    held = sorted(r for r, e in plan.items() if not e["ships_in_migration"])
    out = [header, ""]
    if held:
        out.append(
            "-- HELD BACK FROM THIS MIGRATION, by the owner's ruling of "
            "2026-09-17.\n"
            "-- These roles exist only in the database and their own text "
            "names real\n"
            "-- customers, board members, this host or its disks. Shipping "
            "them here\n"
            "-- would put that in the repo the release-by-export pipeline "
            "reads. They\n"
            "-- get the SAME repair, applied in the database by\n"
            "-- scripts/roles_repair_156_user_roles.py:\n"
            # The ids, and a COUNT — never the tokens themselves. The first
            # version of this comment listed what each role holds, which wrote
            # every customer and board-member name into the very file they are
            # being kept out of. A note about private data is not exempt from
            # the rule it is explaining.
            + "\n".join(
                f"--   {r}  ({len(plan[r]['private_tokens'])} private "
                f"reference{'s' if len(plan[r]['private_tokens']) != 1 else ''})"
                for r in held)
            + "\n"
        )
    for rid in sorted(plan):
        e = plan[rid]
        if not e["ships_in_migration"]:
            continue
        if not e["changed"] and not e["db_only"]:
            continue
        b, r = e["_body"], e["_row"]
        if e["db_only"]:
            out.append(
                f"-- {rid}: DB-only today and carries no private token — E1 "
                f"ships it so a fresh install gets it.\n"
                "INSERT INTO roles (\n"
                "    role_id, domain, description, maturity, tier, model,\n"
                "    prompt, lean_prompt, micro_prompt, tools, panel_eligible,\n"
                "    maintenance_schedule, person_preset\n"
                ") VALUES (\n"
                f"    {q(rid)}, {q(r['domain'])}, {q(r['description'])}, "
                f"{q(r['maturity'] or 'active')}, {q(e['after']['tier'])}, "
                f"{q(r['model'])},\n"
                f"    {q(b['full'])},\n    {q(b['lean'])},\n    {q(b['micro'])},\n"
                f"    {q(r['tools'])}, {q(r['panel_eligible'])},\n"
                f"    {q(r['maintenance_schedule'])}, {q(r['person_preset'])}\n"
                ") ON CONFLICT(role_id) DO UPDATE SET\n"
                "    prompt       = excluded.prompt,\n"
                "    lean_prompt  = excluded.lean_prompt,\n"
                "    micro_prompt = excluded.micro_prompt,\n"
                "    tier         = excluded.tier,\n"
                "    updated_at   = datetime('now');\n"
            )
        else:
            out.append(
                f"-- {rid}: {'; '.join(e['reasons'])}\n"
                "UPDATE roles SET\n"
                f"    prompt       = {q(b['full'])},\n"
                f"    lean_prompt  = {q(b['lean'])},\n"
                f"    micro_prompt = {q(b['micro'])},\n"
                f"    tier         = {q(e['after']['tier'])},\n"
                "    updated_at   = datetime('now')\n"
                f"WHERE role_id = {q(rid)};\n"
            )
    sql = "\n".join(out)

    # The column-authority rule, applied to this file before it is written
    # rather than discovered by the suite afterwards. A repaired body is prose
    # a model helped write, so "no role text contains a line that looks like
    # `tags = ...`" is a property to CHECK, not to assume.
    from okuro.roles.columns import illegal_writes
    bad = illegal_writes(sql)
    if bad:
        raise EmitRefused(
            "refusing to emit: the generated SQL assigns runtime-owned "
            f"columns {sorted(bad)} — see okuro/roles/columns.py"
        )
    return sql



#: The name the migration-156 generator imports it under. One function, two
#: names, because renaming it there would change a file whose output is
#: asserted byte-identical to what shipped.
emit_sql = emit_migration_sql
