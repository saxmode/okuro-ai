# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Strip builder-addressed text and rule-contradicting lines out of a
#   role body before it is delivered to an agent as binding context.
# index: code
# AGENT_HEADER_END -->
"""A role body is wrapped in "treat them as binding instructions" — so it may
not contain anything that is not binding, and it may not contradict okuro.

MEASURED 2026-09-08, first-person, in live bootstrap packets:

    # # UX-WRITER - MICRO
    # Generated from role.md | 2026-01-23
    # DO NOT EDIT - Regenerate via role-efficiency-booster
    ...
    tools: [filesystem, read, grep, edit, webfetch]

Three separate defects in one payload:

1. ``# DO NOT EDIT - Regenerate via role-efficiency-booster`` is an instruction
   to the person maintaining the catalog. Delivered inside a directive the
   agent is told to treat as binding, it reads as a constraint on the agent's
   own output. 47 of 103 briefs carried authoring comments of this shape,
   including ``# One sentence, max 15 words`` and
   ``# Output types (3-4 bullet points)``.
2. ``tools: [... grep ...]`` and ``optional-tm-cortex-cli`` hand the agent grep
   and mark cortex optional, while every other okuro surface says cortex is
   mandatory and grep is banned for hunting. This is the ONLY surface that
   makes a binding rule impossible to obey while instructing the agent to obey
   it.
3. ``output: audit-report.md`` names a ``.md`` file as the role's deliverable,
   against ORCH-ARTIFACT-DISK and three other surfaces that say NEVER a ``.md``
   file, not even a copy.

WHY THIS RUNS AT THE SEAM AND NOT ONLY IN THE CATALOG. Fixing the 87 catalog
YAMLs reaches fresh installs only:

- ``seed_from_catalog`` defaults to ``overwrite=False``, so an existing row is
  never updated by a re-seed;
- a row with ``origin='user'`` is never overwritten *even with*
  ``overwrite=True`` — by design, those are the user's;
- the live body measured in the packet came from the DB, and no
  ``catalog/sys-engineer.yaml`` exists at all.

So the data fix cannot be the whole fix. Sanitising where a body BECOMES
agent-facing text covers shipped rows, user rows, runtime-created roles and
anything a future contributor adds, in one place (DP10/DP11). The catalog is
cleaned separately so the stored data is also right; this module is the
guarantee, not the excuse.
"""

from __future__ import annotations

import re

__all__ = ["sanitize_role_body", "SANITIZE_NOTES"]


#: Comment lines that address whoever maintains the catalog. Matched on a
#: whole line so a ``#`` inside role prose survives.
_MAINTAINER_COMMENT = re.compile(
    r"""^\s*\#\s*(?:
          \#?\s*[A-Z][A-Z0-9 _-]+\s+-\s+(?:MICRO|LEAN|FULL)\b   # "# UX-WRITER - MICRO"
        | DO\s+NOT\s+EDIT\b
        | Generated\b
        | Source\s*:\s*role\.md\b
        | Tokens\s*:
        | Status\s*:\s*Optimized\b
        | Purpose\s*:\s*Fast-refer
        | (?:One\s+sentence|Output\s+types|Max|Keep\s+(?:it\s+)?under)\b
        | Complex\s+work\s+may\s+upgrade\b
      ).*$""",
    re.VERBOSE | re.IGNORECASE,
)

#: A trailing ``# ...`` comment on a value line, e.g.
#: ``model: haiku  # Complex work may upgrade to sonnet``. Only the recognised
#: maintainer phrasings are stripped — a ``#`` inside a quoted value survives
#: because the pattern requires two spaces before the hash.
_TRAILING_MAINTAINER_COMMENT = re.compile(
    r"\s{2,}\#\s*(?:Complex\s+work\s+may\s+upgrade|Generated\b|DO\s+NOT\s+EDIT\b|"
    r"One\s+sentence|Output\s+types|Max\b|Keep\s+(?:it\s+)?under)\b.*$",
    re.IGNORECASE,
)

#: HTML-comment generation banners, e.g.
#: ``<!-- GENERATED LEAN SPECIFICATION ... GENERATED_END -->``.
_GENERATED_BLOCK = re.compile(
    r"<!--\s*GENERATED\b.*?-->\s*", re.DOTALL | re.IGNORECASE
)

#: Tool-list entries that contradict the routing rules.
#:
#: REVISED 2026-09-09 by the owner's ruling: "grep can be used if cortex has been
#: tried first." So grep, glob and find are NOT contradictions — they are the
#: documented fallback, and a role listing them is stating a real capability.
#: The set is now empty and kept for the shape: a future entry belongs here
#: only if the routing contract forbids the tool OUTRIGHT, with no sequence
#: that makes it legal.
#:
#: What remains banned is the ``optional-`` PREFIX, handled separately below.
#: That one is still a contradiction whatever the sequence rule says: a role
#: body may not mark cortex optional when every other surface makes it the
#: mandatory first move.
_BANNED_TOOLS: set[str] = set()

#: ``invocation:`` blocks point at ``roles/<domain>/<id>.md`` paths that do not
#: exist in the repo and leave ``{task-id}`` unsubstituted. The live form is a
#: YAML block scalar — ``invocation: |`` — not a bare key, which is why the
#: first version of this pattern matched 0 of the 10 real occurrences.
_INVOCATION_BLOCK = re.compile(
    r"^invocation:[ \t]*[|>]?[-+]?[ \t]*$.*?(?=^\S|\Z)",
    re.MULTILINE | re.DOTALL,
)

#: The markdown twin of the YAML block: a ``## INVOCATION`` section telling an
#: OPERATOR how to shell into this role —
#: ``claude -p --model opus "Execute role: roles/c-level/ceo/role.md ..."``.
#: The agent reading it is already operating as the role, so the section
#: instructs nobody present. It also cites `roles/<domain>/<id>/role.md` paths
#: that do not exist and a retired orchestration mode of okuro's predecessor;
#: the provider golden tests already ban that system's name as a dead reference.
#: Runs to the next H2 or the end of the body.
_INVOCATION_SECTION = re.compile(
    r"^\#\#\s+INVOCATION\b.*?(?=^\#\#\s|\Z)",
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)

# NOT SANITISED, DELIBERATELY: `{task-id}` and `{timestamp}` also appear inside
# role PROSE, in lines like "Log: {timestamp} | CEO | claimed | {task-id}".
# That is the role describing its own logging format — 67 bodies carry it — and
# it is a role-content question (these placeholders belong to a task/council
# workflow), not builder text leaking into an agent channel. Stripping them
# would mangle the directive this module exists to protect. Reported separately
# rather than silently rewritten.

SANITIZE_NOTES = (
    "Role bodies are sanitised before delivery: maintainer comments, "
    "generation banners, banned tool entries and .md deliverables are removed."
)


def _clean_tools_line(line: str) -> str | None:
    """Rewrite an inline ``tools: [a, b]`` line, or return None to drop it."""
    head, _, rest = line.partition(":")
    inner = rest.strip()
    if not (inner.startswith("[") and inner.endswith("]")):
        return line
    items = [t.strip() for t in inner[1:-1].split(",") if t.strip()]
    kept = []
    for item in items:
        # `optional-tm-cortex-cli` -> a mandatory route marked optional.
        bare = item[len("optional-"):] if item.startswith("optional-") else item
        if bare.lower() in _BANNED_TOOLS:
            continue
        kept.append(bare)
    if not kept:
        return None
    return f"{head}: [{', '.join(kept)}]"


def _rewrite_tool_list_item(line: str) -> str | None:
    """Rewrite one ``  - <tool>`` entry, or return None to drop it.

    Two rules, both from the routing contract:
    - a banned hunting tool is dropped outright;
    - an ``optional-`` prefix is stripped rather than the entry dropped. A role
      body may not mark a mandatory route optional, but the tool itself is
      still the right tool — ``optional-tm-cortex-cli`` becomes
      ``tm-cortex-cli``.
    """
    m = re.match(r"^(\s*-\s+)(\S+)\s*$", line)
    if not m:
        return line
    indent, item = m.group(1), m.group(2)
    bare = item[len("optional-"):] if item.startswith("optional-") else item
    if bare.lower() in _BANNED_TOOLS:
        return None
    return f"{indent}{bare}"


#: The MARKDOWN twin of a ``tools:`` block. Catalog roles written as prose use
#:
#:     **Tools:**
#:     - Required: filesystem, Read, Write
#:     - Optional: Grep/Glob (user pattern lookup)
#:
#: which grants the banned hunting tools in a syntax the YAML rules never see.
#: MEASURED: 22 live bodies, 6 of them in `content` — the field `roles_get`
#: returns and frames as binding. Found only because a live render was read
#: after the YAML paths were already clean; the class was one syntax wider
#: than the first fix assumed.
_MD_TOOL_LINE = re.compile(
    r"^(?P<indent>\s*[-*]\s*)(?P<label>Required|Optional)(?P<sep>\s*:\s*)"
    r"(?P<items>.+?)(?P<trailer>\s*\([^)]*\))?\s*$",
    re.IGNORECASE,
)


def _clean_md_tool_line(line: str) -> str | None:
    """Drop banned tools from a ``- Required:``/``- Optional:`` line.

    Returns None when nothing survives, so the line disappears rather than
    leaving a dangling label.
    """
    m = _MD_TOOL_LINE.match(line)
    if not m:
        return line
    items = [i.strip() for i in m.group("items").split(",") if i.strip()]
    kept = []
    for item in items:
        # `Grep/Glob` is one entry naming two banned tools; drop the whole
        # entry only when every name in it is banned, so `Read/Write` stays.
        names = [n.strip().lower() for n in re.split(r"[/|]", item) if n.strip()]
        if names and all(n in _BANNED_TOOLS for n in names):
            continue
        kept.append(item)
    if not kept:
        return None
    trailer = m.group("trailer") or ""
    return f"{m.group('indent')}{m.group('label')}{m.group('sep')}{', '.join(kept)}{trailer}"


def _is_md_deliverable(line: str) -> bool:
    """True for an ``output:`` list entry naming a ``.md`` or ``.log`` file."""
    m = re.match(r"^\s*-\s+([\w./-]+\.(?:md|log))\s*$", line)
    return bool(m)


def sanitize_role_body(body: str) -> str:
    """Return ``body`` with every builder-addressed and rule-breaking line gone.

    Conservative by construction: it removes whole lines it recognises and
    never rewrites role prose. An unrecognised line survives untouched, so a
    role whose body is already clean round-trips unchanged.
    """
    if not body:
        return body

    text = _GENERATED_BLOCK.sub("", body)
    text = _INVOCATION_BLOCK.sub("", text)
    text = _INVOCATION_SECTION.sub("", text)

    out: list[str] = []
    in_tools_block = False
    in_output_block = False
    in_md_tools = False

    for line in text.splitlines():
        stripped = line.strip()

        # Markdown `**Tools:**` heading opens a prose tool list; any other
        # bold heading or blank line closes it.
        if re.match(r"^\*\*Tools:?\*\*\s*$", stripped, re.IGNORECASE):
            in_md_tools = True
            out.append(line)
            continue
        if in_md_tools:
            if not stripped or (stripped.startswith("**") and stripped != "**"):
                in_md_tools = False
            elif stripped.startswith(("-", "*")):
                cleaned = _clean_md_tool_line(line)
                if cleaned is None:
                    continue
                out.append(cleaned)
                continue

        # Track which YAML-ish block we are inside so a bare `- grep` under
        # `tools:` is dropped while the same text in prose survives.
        if re.match(r"^tools:\s*$", stripped):
            in_tools_block, in_output_block = True, False
            out.append(line)
            continue
        if re.match(r"^output:\s*$", stripped):
            in_output_block, in_tools_block = True, False
            out.append(line)
            continue
        if stripped and not stripped.startswith("-") and ":" in stripped:
            in_tools_block = in_output_block = False

        if _MAINTAINER_COMMENT.match(line):
            continue
        if in_tools_block:
            rewritten_item = _rewrite_tool_list_item(line)
            if rewritten_item is None:
                continue
            line = rewritten_item
        if in_output_block and _is_md_deliverable(line):
            continue
        if stripped.lower().startswith("tools:") and "[" in stripped:
            rewritten = _clean_tools_line(line)
            if rewritten is None:
                continue
            out.append(rewritten)
            continue

        out.append(_TRAILING_MAINTAINER_COMMENT.sub("", line))

    # Collapse the blank runs the removals leave behind, without touching
    # deliberate single blank lines inside prose.
    collapsed: list[str] = []
    blanks = 0
    for line in out:
        if line.strip():
            blanks = 0
            collapsed.append(line)
        else:
            blanks += 1
            if blanks < 2:
                collapsed.append(line)

    return "\n".join(collapsed).strip()
