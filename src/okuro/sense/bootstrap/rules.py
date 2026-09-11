# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Load + render governing agent rules from data/agent_rules.yaml.
# index: def get_agent_rule | def render_rule
# AGENT_HEADER_END -->
"""Load + render the governing agent rules (data/agent_rules.yaml).

The rule WORDING lives in yaml, not in a builder, so reshaping a rule
(declarative -> imperative DO/NEVER) is a settings-data edit, not a code
change. Renderers in sections.py call render_rule(id); no rule text is
hardcoded there.
"""

from __future__ import annotations

import functools
from pathlib import Path

import yaml

# data/ sits under sense/, one level up from sense/bootstrap/.
_RULES_PATH = Path(__file__).parent.parent / "data" / "agent_rules.yaml"


@functools.lru_cache(maxsize=1)
def _load() -> dict:
    return yaml.safe_load(_RULES_PATH.read_text(encoding="utf-8")) or {}


def get_agent_rule(rule_id: str) -> dict:
    """Return one rule's structured data (empty dict if absent)."""
    return _load().get(rule_id, {}) or {}


def get_actions(rule_id: str) -> list[dict]:
    """Return a rule's typed action contract.

    This is the SECOND consumer's entry point. `render_rule` builds prose from
    the same list, so a gate compiled from `get_actions` and the table an agent
    reads cannot describe different rules — which is precisely how the cortex
    gate ended up bound to `Grep` while the packet said "never hunt with grep"
    and the traffic moved to Bash.
    """
    return list(get_agent_rule(rule_id).get("actions") or [])


def _rows_from_actions(rule: dict, spec: dict) -> list[list[str]]:
    """Project the typed actions into display rows for one table."""
    group = spec.get("group")
    columns = spec.get("columns") or []
    rows: list[list[str]] = []
    for action in rule.get("actions") or []:
        if action.get("group") != group:
            continue
        rows.append([str(action.get(c, "")) for c in columns])
    return rows


def _render_table(tbl: dict, rule: dict | None = None) -> list[str]:
    header = tbl.get("header") or []
    # Rows are GENERATED from the typed actions when the table says so. A table
    # may still carry literal `rows:` — that path stays for rules with no
    # machine-readable contract yet, but tool_routing must not use it (see the
    # test that forbids hand-written rows there).
    if tbl.get("from_actions") is not None and rule is not None:
        rows = _rows_from_actions(rule, tbl["from_actions"] or {})
    else:
        rows = tbl.get("rows") or []
    if not header or not rows:
        return []
    out: list[str] = []
    if tbl.get("title"):
        out.append(f"### {tbl['title']}")
    out.append("| " + " | ".join(header) + " |")
    out.append("|" + "|".join(["---"] * len(header)) + "|")
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return out


def render_rule(rule_id: str, *, prefix: str | None = None) -> str:
    """Render a rule to its bootstrap-section markdown.

    prefix: an optional dynamic line inserted right after the heading (e.g. the
    live cortex-coverage line for tool_routing). Everything else is data.
    """
    rule = get_agent_rule(rule_id)
    if not rule:
        return ""
    lines: list[str] = [f"## {rule['heading']}"]
    if prefix:
        lines += ["", prefix.strip()]
    if rule.get("lead"):
        lines += ["", f"**{rule['lead']}**"]
    if rule.get("note"):
        lines += ["", f"_{rule['note']}_"]
    if rule.get("why"):
        lines += ["", f"**Why.** {rule['why']}"]
    for tbl in rule.get("tables") or []:
        rendered = _render_table(tbl, rule)
        if rendered:
            lines += [""] + rendered
    for group in rule.get("groups") or []:
        if group.get("title"):
            lines += ["", f"### {group['title']}"]
        for item in group.get("items") or []:
            lines.append(f"- {item}")
    return "\n".join(lines)
