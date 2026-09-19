# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Role fit endpoints — fleet rollup and per-role detail.
# index:
#   imports
#   router
#   def _load_fleet
#   def fleet_fit_endpoint
#   def role_fit_endpoint
# AGENT_HEADER_END -->
"""Role fit endpoints — ``GET /api/roles/fit`` and ``GET /api/roles/{id}/fit``.

**Why this is a separate module and not appended to ``api/roles.py``.**
Starlette matches routes in registration order, first match wins, and
``api/roles.py`` registers ``GET /{role_id}`` partway down the file. A literal
``/fit`` appended after it is unreachable — every request lands on the detail
route and comes back ``Role 'fit' not found``. The same trap already shaped
that file: ``/maintenance`` and ``/maintenance/status`` sit ABOVE ``/{role_id}``
for exactly this reason. Its own router, registered ahead of the roles router,
puts the literal in front of the wildcard without reordering a file another
branch is editing.

Read-only, so no loopback guard: these two are in the same class as
``GET /api/roles`` and ``GET /api/roles/maintenance``, which are ungated. The
guard is on the write endpoints.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger("okuro.orchestrator.api.roles_fit")

router = APIRouter(prefix="/api/roles", tags=["roles"])

_ROLE_COLS = (
    "role_id, domain, tier, maintenance_schedule, "
    "prompt, lean_prompt, micro_prompt"
)

#: Rows come back UNFILTERED — no `confidence > 0` clause. Suppression is a
#: counted, named bucket inside compute_fit so the fit view's total equals the
#: table's count; as a SQL filter it shrank the total with nothing saying why.
_KNOWLEDGE_COLS = "role_id, content, source_url, created_at, confidence"

#: THE ONE PLACE the API decides what "sourced" means. Today the store can only
#: answer "the row claims a source_url", so that is what the numerator counts
#: and what the labels say. Flipping this to "fetch_verified" reads false for
#: every one of the 844 existing rows and would zero the knowledge segment
#: fleet-wide, so it is a migration with a backfill decision behind it, not a
#: setting — see okuro.roles.fit.SOURCED_DEFINITIONS. It is hashed into
#: rubric_version, so the two eras never compare as one measurement.
SOURCED_DEFINITION = "url_claimed"


def _load_fleet() -> tuple[
    list[dict], dict[str, list[dict]], list[dict], dict[str, list[dict]]
]:
    """Every role body, knowledge row and check row, in exactly three queries.

    The per-role listing path can afford a query per role; a fleet rollup over
    103 roles cannot, so the rows come back in one read each and are grouped in
    memory by ``role_id``.
    """
    from okuro.db import get_db
    from okuro.roles.checks import check_summaries_by_role

    db = get_db()
    roles = [dict(r) for r in db.fetchall(
        f"SELECT {_ROLE_COLS} FROM roles ORDER BY domain, role_id"
    )]
    knowledge = [dict(k) for k in db.fetchall(
        f"SELECT {_KNOWLEDGE_COLS} FROM role_knowledge"
    )]

    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in knowledge:
        grouped[row["role_id"]].append(row)
    return roles, grouped, knowledge, check_summaries_by_role(db)


@router.get("/fit")
def fleet_fit_endpoint(worst_n: int = Query(10, ge=1, le=103)):
    """Fleet fit rollup + the standing sourced-row metric.

    Returns the per-segment means and counts, the worst-N roles by their worst
    segment, the sourced-row ratio per month (amendment A1's standing metric,
    which is what would have shown the June 2026 collapse in the week it
    happened), and the ``rubric_version`` every score was taken under.
    """
    from okuro.roles.fit import (
        SOURCED_LABEL,
        compute_fit,
        fleet_fit,
        knowledge_sourced_ratio_by_month,
        rubric_version,
    )

    try:
        roles, grouped, knowledge, checks = _load_fleet()
        fits = [
            compute_fit(
                r,
                grouped.get(r["role_id"], []),
                sourced_definition=SOURCED_DEFINITION,
                check_rows=checks.get(r["role_id"], []),
            )
            for r in roles
        ]
        rollup = fleet_fit(fits, worst_n=worst_n)
    except Exception as exc:  # noqa: BLE001
        logger.exception("fleet fit rollup failed")
        raise HTTPException(500, f"Fleet fit rollup failed: {exc}")

    return {
        **rollup,
        "rubric_version": rubric_version(SOURCED_DEFINITION),
        "sourced_definition": SOURCED_DEFINITION,
        "sourced_label": SOURCED_LABEL[SOURCED_DEFINITION],
        "knowledge_by_month": knowledge_sourced_ratio_by_month(
            knowledge, SOURCED_DEFINITION
        ),
        "roles_scored": [
            {
                "role_id": f["role_id"],
                "domain": r["domain"],
                "scores": f["scores"],
                "overall": f["overall"],
                "worst": f["worst"],
                "worst_score": f["worst_score"],
            }
            for r, f in zip(roles, fits)
        ],
    }


@router.get("/{role_id}/fit")
def role_fit_endpoint(role_id: str):
    """One role's fit, with every defect named rather than counted.

    This is what the viewer's Fit tab renders: the missing sections by label,
    the empty grades, the oversize grades with their budgets, the legacy
    strings with their hit counts, and the knowledge split across its four
    buckets — sourced, unverified, filler and suppressed.
    """
    from okuro.roles.checks import check_summaries_for_role
    from okuro.roles.fit import SIZE_BUDGETS, compute_fit

    from okuro.db import get_db

    db = get_db()
    row = db.fetchone(f"SELECT {_ROLE_COLS} FROM roles WHERE role_id = ?", (role_id,))
    if not row:
        raise HTTPException(404, f"Role '{role_id}' not found")

    knowledge = [dict(k) for k in db.fetchall(
        f"SELECT {_KNOWLEDGE_COLS} FROM role_knowledge WHERE role_id = ?",
        (role_id,),
    )]
    # The scorer reads dates and counts rows; the prose in `note` is for a
    # detail view that would render it, and this endpoint does not.
    checks = check_summaries_for_role(role_id, db=db)

    try:
        fit = compute_fit(
            dict(row),
            knowledge,
            sourced_definition=SOURCED_DEFINITION,
            check_rows=checks,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("fit computation failed for role %s", role_id)
        raise HTTPException(500, f"Fit computation failed: {exc}")

    return {**fit, "domain": row["domain"], "budgets": dict(SIZE_BUDGETS)}
