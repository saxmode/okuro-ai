# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro distill — review, approve and reject mined lessons before they become rules.
# index: imports | def distill | def lessons | def approve | def reject
# AGENT_HEADER_END -->
"""okuro distill — the human half of the self-improvement loop.

Mining finds defects that recur across a cluster of sessions and writes them as
CANDIDATE lessons. Corroboration proves the defect is real; it does not prove
the proposed remedy is right, and an active lesson becomes a rule okuro applies
to itself. So the candidate -> active step is a decision a person makes, and
this is where they make it.

The same three functions back the MCP tools (``distill_lessons_review`` /
``distill_lesson_approve`` / ``distill_lesson_reject``). One implementation,
two front-ends — a CLI that works before a reconnect and MCP tools that work
from inside an agent session.
"""

from __future__ import annotations

import click

from .output import console, data_table, heading, info, ok, warn


@click.group()
def distill():
    """Review mined lessons before they become rules."""


@distill.command("lessons")
@click.option("--status", type=click.Choice(["candidate", "active", "retired"]),
              default="candidate", show_default=True)
@click.option("--limit", default=20, show_default=True)
@click.option("--full/--table", default=True,
              help="--full shows evidence snippets; --table is one row per lesson.")
def lessons(status: str, limit: int, full: bool):
    """List mined lessons. Default: the ones awaiting your decision."""
    from okuro.sense.distill.lessons import lessons_for_review

    rows = lessons_for_review(status=status, limit=limit)
    heading(f"Distill lessons — {status}")
    if not rows:
        info("Nothing here.")
        if status == "candidate":
            info("Mining writes candidates; check distill.mining_enabled and "
                 "tier-1 coverage if you expected some.")
        return

    if not full:
        console.print(data_table(
            ["id", "class", "surface", "target", "corrob.", "status"],
            [[str(r["id"]), r["lesson_class"] or "-", r["target_surface"] or "-",
              r["target_ref"] or "(surface-wide)",
              str(r["corroboration_count"]), r["status"]] for r in rows],
        ))
        return

    for r in rows:
        console.print()
        console.print(f"[bold]#{r['id']}[/bold]  {r['lesson_class']}  "
                      f"-> {r['target_surface']}/{r['target_ref'] or '(surface-wide)'}")
        console.print(f"  {r['lesson_text']}")
        console.print(
            f"  corroborated by {r['corroboration_count']} distinct sessions "
            f"(cluster {r['cluster_id']}), markers: "
            f"{', '.join(r['markers']) or '(none)'}"
        )
        if r.get("approved_by"):
            console.print(f"  approved by {r['approved_by']} at {r['approved_at']}")
        if r.get("retired_reason"):
            console.print(f"  retired: {r['retired_reason']}")
        for ev in r["evidence"]:
            snippet = ev["snippet"] or "(no failure phrase recorded)"
            console.print(f"    - {ev['session_id'][:28]}: {snippet}")
        if r["evidence_session_count"] > len(r["evidence"]):
            console.print(f"    … and {r['evidence_session_count'] - len(r['evidence'])} "
                          f"more evidence sessions")

    console.print()
    if status == "candidate":
        info("Approve:  okuro distill approve <id> --by <your name>")
        info("Reject:   okuro distill reject <id> --reason '<why>'")


@distill.command("approve")
@click.argument("lesson_id", type=int)
@click.option("--by", "approved_by", required=True,
              help="Who is approving. A person, not a daemon.")
def approve(lesson_id: int, approved_by: str):
    """Approve a lesson — the second key on candidate -> active.

    Records the decision. Activation still needs the corroboration threshold
    and the dwell clock, so this does not force a thin lesson through.
    """
    from okuro.sense.distill.lessons import approve_lesson

    try:
        result = approve_lesson(lesson_id, approved_by=approved_by)
    except ValueError as exc:
        warn(str(exc))
        raise SystemExit(1)
    ok(f"Lesson #{result['lesson_id']} approved by {result['approved_by']}.")
    info(result["note"])


@distill.command("reject")
@click.argument("lesson_id", type=int)
@click.option("--reason", required=True, help="Why this should not become a rule.")
@click.option("--by", "rejected_by", default="", help="Who is rejecting (optional).")
def reject(lesson_id: int, reason: str, rejected_by: str):
    """Reject a lesson — retires it, with the reason on the row."""
    from okuro.sense.distill.lessons import reject_lesson

    try:
        result = reject_lesson(lesson_id, reason=reason, rejected_by=rejected_by)
    except ValueError as exc:
        warn(str(exc))
        raise SystemExit(1)
    ok(f"Lesson #{result['lesson_id']} retired.")
    info(result["retired_reason"])
