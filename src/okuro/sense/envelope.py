# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Project envelope — enumerate and re-file every brain row a project owns.
# index:
#   imports
#   COLUMN PREFERENCE lists
#   _TABLE_POLICY / _CASCADES
#   class TableSpec
#   def discover_project_tables
#   def project_inventory
#   def project_envelope
#   def project_envelope_undo
# AGENT_HEADER_END -->
"""Project envelope — enumerate and re-file every brain row a project owns.

Two problems this closes, both measured on 2026-08-06:

1. **No enumeration.** ``read_memory`` ranks semantically and returns full
   bodies; it cannot prove completeness over ~100 rows and cannot produce a
   compact listing. Building an index of what a project owns meant raw SQL.
2. **No re-filing.** ``write_memory`` / ``artifact_write`` set ``project``
   only at CREATION. Nothing moves an existing row, so a session told to
   "combine everything under slug X" writes three NEW rows, moves nothing,
   and reports success. That happened.

The load-bearing insight is that a ``project`` column does NOT mean one
thing. Three distinct semantics share the name, and conflating them is the
root defect — a hand-rolled ``UPDATE ... WHERE project = ?`` sweep hits all
three:

  **tag**      an authored, free-form label on a knowledge row. Re-filable;
               this is the only class the envelope moves.
  **binding**  a foreign key that binds a *resource* to a project
               (``managed_repos.project_id``, ``stack_project_brand``).
               Rewriting it re-points the resource — a different operation
               with different consequences, never a re-file.
  **derived**  written by machinery. Either regenerable (``cortex_docs``
               rebuilds on re-index) or an immutable historical record
               (``sessions`` says where a session actually ran). Rewriting
               it either gets clobbered or falsifies an audit trail.

Tables are DISCOVERED at call time from ``sqlite_master`` + ``PRAGMA
table_info`` — never hardcoded. Classification is a declared policy map, so
a table that gains a project column and is not yet classified surfaces as
``unclassified`` and is EXCLUDED until someone decides. Silent inclusion is
how 14 unrelated rows got swept in; silent omission is how a store loses
half its contents. The tool reports both rather than guessing.

Safety contract for the writer:
  - ``dry_run=True`` is the default and writes nothing.
  - A dry run persists a PLAN file; applying by ``plan_ref`` replays exactly
    those ids, so a reviewed set cannot drift between review and apply.
  - Applying persists an UNDO file before the first write.
  - One transaction per apply; any error rolls the whole thing back.
  - Rows already at the target are reported ``already_filed``, never moved.
  - Content is never modified and nothing is ever deleted. Only ``project``.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Sequence

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Column discovery — preference order, not a per-table hardcode
# ---------------------------------------------------------------------------
#
# Every column below is resolved against the LIVE PRAGMA table_info output.
# A table only gets a column role if it actually has one of these names, so
# a schema change degrades the listing rather than raising.

_PROJECT_COLS = ("project", "project_slug", "project_id")
_ID_COLS = ("id", "memory_id", "session_id", "project_slug", "project_id")
_KIND_COLS = ("topic", "kind", "type", "status", "topic_kind", "predicate")
_TITLE_COLS = ("title", "concept", "name", "subject", "goal", "role_id")
_BODY_COLS = (
    "content", "body", "summary", "detail", "entry", "note", "document",
    "text", "discarded_text", "title",
)
_DATE_COLS = (
    "created_at", "started_at", "generated_at", "discarded_at", "indexed_at",
    "ts", "first_ts", "updated_at", "retired_at", "disposed_at",
)
_CONF_COLS = ("confidence",)
_SUPERSEDES_COLS = ("supersedes",)


# ---------------------------------------------------------------------------
# Table policy — which class of `project` column each table carries
# ---------------------------------------------------------------------------
#
# Discovery is dynamic; CLASSIFICATION is declared, because "is this column a
# tag or a foreign key" is a judgement no amount of introspection can make.
# Anything discovered and absent from this map is reported `unclassified`.

Role = Literal["tag", "binding", "derived", "unclassified"]

_TABLE_POLICY: dict[str, tuple[Role, str]] = {
    # ----- tag: authored knowledge rows; the envelope moves these -----
    "agent_memory": ("tag", "authored memory row; project is a free-form tag"),
    "artifacts": ("tag", "authored document row; project is a free-form tag"),
    "progress": ("tag", "authored work log; project is a free-form tag"),
    "todos": ("tag", "authored task row; project is a free-form tag"),
    "thoughts": ("tag", "captured user idea; project is a free-form tag"),
    "notes": ("tag", "user's own note surface; project is a free-form tag"),
    "commitments": ("tag", "authored commitment; project is a free-form tag"),
    "role_diary_entries": ("tag", "authored diary entry; project is a free-form tag"),
    "resonance_sessions": ("tag", "authored resonance session; project is a free-form tag"),

    # ----- binding: FK that points a resource at a project -----
    "managed_repos": ("binding", "project_id BINDS a cloned repo to a project; rewriting re-points the repo, it does not re-file a note"),
    "managed_corpora": ("binding", "project_id BINDS a corpus to a project; same as managed_repos"),
    "stack_project_profile": ("binding", "project_slug is the PRIMARY KEY of a stack assignment; rewriting reassigns the tech stack"),
    "stack_project_brand": ("binding", "project_slug is the PRIMARY KEY of a brand assignment; rewriting reassigns the brand"),
    "engagements": ("binding", "project_slug links a CRM engagement to a project; rewriting rewires the relationship"),
    "project_phases": ("binding", "project is half the PRIMARY KEY of a declared phase plan (migration 130) — the plan IS the project's, not a label on a note. Re-filing one phase would split a plan across two projects; moving a plan is a whole-plan operation, so project_phases_set owns it"),

    # ----- derived: machine-written; regenerable or an audit record -----
    "cortex_docs": ("derived", "search index; project is set by root registration and is rewritten on every re-index"),
    "vec_cortex": ("derived", "sqlite-vec virtual table shadowing cortex_docs; `project` is a vec0 PARTITION KEY — part of the physical index layout, not an updatable tag"),
    "cortex_query_log": ("derived", "query telemetry; records the project a search was scoped to"),
    "sessions": ("derived", "session audit log; records where a session actually ran — rewriting falsifies history"),
    "agent_sessions": ("derived", "transcript index; project_path is a FILESYSTEM PATH, not a slug — different domain entirely"),
    "transcript_messages": ("derived", "ingested external transcripts; project is assigned by the ingest adapter"),
    "inbox": ("derived", "ranking surface rebuilt from ref_table/ref_id; follows its source row"),
    "inbox_dispositions": ("derived", "disposition audit log of what was surfaced and when"),
    "memory_write_discards": ("derived", "audit log of REJECTED writes; rewriting falsifies the rejection record"),
    "memory_dedup_log": ("derived", "dedup audit log"),
    "progress_quarantine": ("derived", "quarantine audit log"),
    "memory_pointers": ("derived", "1:1 denormalised mirror of agent_memory — CASCADED, see _CASCADES"),
    "memory_tunnels": ("derived", "cross-project bridge; project is one END of the bridge, moving it collapses the bridge"),
    "kg_entities": ("derived", "knowledge-graph node derived from source rows; re-filing needs graph-aware reasoning, not a column update"),
    "kg_triples": ("derived", "knowledge-graph edge carrying source_memory_id/source_artifact_id; same as kg_entities"),
}

# Denormalised copies that MUST move with their parent, in the same
# transaction. memory_pointers.project is read by read_pointers(project=...)
# and rendered as the `[scope]` on every bootstrap pointer line
# (sense/memory_index.py). Move agent_memory.project alone and bootstrap
# keeps rendering the OLD project — a silent, invisible split. This is
# exactly the class of miss a hand-written UPDATE makes.
_CASCADES: dict[str, list[tuple[str, str, str]]] = {
    # parent table -> [(child table, child fk col -> parent id, child project col)]
    "agent_memory": [("memory_pointers", "memory_id", "project")],
}

_ENVELOPE_DIR = ".okuro/envelope"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

@dataclass
class TableSpec:
    """One discovered table that carries a project-ish column."""

    table: str
    project_col: str
    role: Role
    reason: str
    id_col: str | None = None
    kind_col: str | None = None
    title_col: str | None = None
    body_col: str | None = None
    date_col: str | None = None
    conf_col: str | None = None
    supersedes_col: str | None = None
    columns: list[str] = field(default_factory=list)

    @property
    def movable(self) -> bool:
        """Only tag-role tables with a usable primary key can be re-filed."""
        return self.role == "tag" and self.id_col is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "project_col": self.project_col,
            "role": self.role,
            "reason": self.reason,
            "id_col": self.id_col,
            "movable": self.movable,
        }


def _pick(columns: Sequence[str], candidates: Iterable[str]) -> str | None:
    """First candidate present in ``columns``, preference-ordered."""
    lowered = {c.lower(): c for c in columns}
    for cand in candidates:
        if cand in lowered:
            return lowered[cand]
    return None


def discover_project_tables(db=None) -> list[TableSpec]:
    """Every table carrying a project-ish column, classified.

    Introspects ``sqlite_master`` + ``PRAGMA table_info`` at call time. No
    table list is hardcoded — a table that gains a ``project`` column shows
    up here on the next call, classified ``unclassified`` until someone adds
    a policy entry for it.
    """
    if db is None:
        from okuro.db import get_db
        db = get_db()

    specs: list[TableSpec] = []
    tables = db.fetchall(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    for row in tables:
        name = row["name"]
        try:
            info = db.fetchall(f'PRAGMA table_info("{name}")')
        except Exception:  # pragma: no cover — virtual/shadow tables
            continue
        cols = [c["name"] for c in info]
        project_col = _pick(cols, _PROJECT_COLS)
        if not project_col:
            continue

        pk_cols = [c["name"] for c in info if c["pk"]]
        id_col = pk_cols[0] if len(pk_cols) == 1 else _pick(cols, _ID_COLS)

        role, reason = _TABLE_POLICY.get(
            name,
            ("unclassified", "no policy entry — excluded until classified"),
        )
        specs.append(
            TableSpec(
                table=name,
                project_col=project_col,
                role=role,
                reason=reason,
                id_col=id_col,
                kind_col=_pick(cols, _KIND_COLS),
                title_col=_pick(cols, _TITLE_COLS),
                body_col=_pick(cols, _BODY_COLS),
                date_col=_pick(cols, _DATE_COLS),
                conf_col=_pick(cols, _CONF_COLS),
                supersedes_col=_pick(cols, _SUPERSEDES_COLS),
                columns=cols,
            )
        )
    return specs


# ---------------------------------------------------------------------------
# Row shaping
# ---------------------------------------------------------------------------

def _superseded_sql(spec: TableSpec) -> str | None:
    """SQL predicate that is TRUE for a superseded row, or None.

    Mirrors okuro's own two filters, which disagree unless combined:
      - confidence <= 0.1 (the value artifact_supersede / write_memory stamp)
      - id pointed at by another row's `supersedes` (memory.py
        _EXCLUDE_SUPERSEDED, artifacts.py _EXCLUDE_SUPERSEDED)
    A row is superseded if EITHER holds. Measured on okuro-design-systems:
    32 artifacts sit at confidence <= 0.1 but only 31 are pointed at, so
    checking one alone miscounts.
    """
    parts = []
    if spec.conf_col:
        parts.append(f'"{spec.conf_col}" <= 0.1')
    if spec.supersedes_col and spec.id_col:
        parts.append(
            f'"{spec.id_col}" IN (SELECT "{spec.supersedes_col}" FROM "{spec.table}" '
            f'WHERE "{spec.supersedes_col}" IS NOT NULL AND "{spec.supersedes_col}" != \'\')'
        )
    if not parts:
        return None
    return "(" + " OR ".join(parts) + ")"


def _excerpt(value: Any, limit: int) -> str:
    """Whitespace-collapsed, length-capped preview of a body column."""
    if value is None:
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _shape_row(row: dict, spec: TableSpec, excerpt_chars: int) -> dict[str, Any]:
    """Compact listing row — id/kind/date/confidence/excerpt, never a body."""
    rid = str(row[spec.id_col]) if spec.id_col else ""
    title = row.get(spec.title_col) if spec.title_col else None
    body = row.get(spec.body_col) if spec.body_col else None
    out: dict[str, Any] = {
        "table": spec.table,
        "id": rid,
        "handle": rid[:8],
    }
    if spec.kind_col:
        out["kind"] = row.get(spec.kind_col)
    if title:
        out["title"] = _excerpt(title, 90)
    if spec.date_col:
        out["date"] = row.get(spec.date_col)
    if spec.conf_col:
        out["confidence"] = row.get(spec.conf_col)
    out["superseded"] = bool(row.get("_superseded"))
    out["project"] = row.get(spec.project_col)
    out["excerpt"] = _excerpt(body, excerpt_chars)
    return out


def _select_columns(spec: TableSpec) -> str:
    """Distinct, quoted column list for a listing query."""
    wanted = [
        spec.id_col, spec.kind_col, spec.title_col, spec.body_col,
        spec.date_col, spec.conf_col, spec.project_col,
    ]
    seen: list[str] = []
    for c in wanted:
        if c and c not in seen:
            seen.append(c)
    return ", ".join(f'"{c}"' for c in seen)


# ---------------------------------------------------------------------------
# project_inventory — the enumeration
# ---------------------------------------------------------------------------

def project_inventory(
    slug: str,
    include_superseded: bool = False,
    tables: list[str] | None = None,
    limit_per_table: int = 500,
    excerpt_chars: int = 150,
    include_bindings: bool = True,
) -> dict[str, Any]:
    """Enumerate everything filed under ``slug``. READ-ONLY.

    Returns a COMPACT listing — id, kind, date, confidence and a short
    excerpt per row, never a full body — plus exact counts per table. This
    is what building an index needs and what ``read_memory`` structurally
    cannot do: semantic ranking with full bodies can neither prove
    completeness nor fit ~100 rows in a reviewable answer.

    ``include_superseded=False`` (default) still COUNTS superseded rows, it
    only omits them from the listing. A count you cannot see is how a store
    silently loses half its contents.
    """
    from okuro.db import get_db

    db = get_db()
    specs = discover_project_tables(db)
    by_name = {s.table: s for s in specs}

    if tables:
        unknown = [t for t in tables if t not in by_name]
        if unknown:
            return {
                "error": f"unknown table(s): {', '.join(sorted(unknown))}",
                "known_tables": sorted(by_name),
            }
        selected = [by_name[t] for t in tables]
    else:
        roles = {"tag", "binding"} if include_bindings else {"tag"}
        selected = [s for s in specs if s.role in roles]

    registered = db.fetchone("SELECT id, name FROM projects WHERE id = ?", (slug,))

    per_table: dict[str, Any] = {}
    listing: list[dict[str, Any]] = []
    totals = {"total": 0, "active": 0, "superseded": 0}

    for spec in selected:
        if not spec.id_col:
            continue
        sup_sql = _superseded_sql(spec)
        where = f'"{spec.project_col}" = ?'
        total = db.fetchone(
            f'SELECT COUNT(*) AS n FROM "{spec.table}" WHERE {where}', (slug,)
        )["n"]
        if sup_sql:
            superseded = db.fetchone(
                f'SELECT COUNT(*) AS n FROM "{spec.table}" WHERE {where} AND {sup_sql}',
                (slug,),
            )["n"]
        else:
            superseded = 0
        active = total - superseded
        if total == 0:
            continue

        per_table[spec.table] = {
            "role": spec.role,
            "total": total,
            "active": active,
            "superseded": superseded,
        }
        totals["total"] += total
        totals["active"] += active
        totals["superseded"] += superseded

        sup_expr = sup_sql or "0"
        sql = (
            f'SELECT {_select_columns(spec)}, {sup_expr} AS _superseded '
            f'FROM "{spec.table}" WHERE {where}'
        )
        if not include_superseded and sup_sql:
            sql += f" AND NOT {sup_sql}"
        if spec.date_col:
            sql += f' ORDER BY "{spec.date_col}" DESC'
        sql += " LIMIT ?"
        rows = db.fetchall(sql, (slug, limit_per_table))
        shown = [_shape_row(r, spec, excerpt_chars) for r in rows]
        listing.extend(shown)
        per_table[spec.table]["listed"] = len(shown)
        if len(rows) == limit_per_table:
            per_table[spec.table]["truncated"] = True

    result: dict[str, Any] = {
        "slug": slug,
        "registered": bool(registered),
        "counts": totals,
        "per_table": per_table,
        "rows": listing,
        "include_superseded": include_superseded,
    }
    if not registered:
        result["warning"] = (
            f"'{slug}' is NOT a registered project — rows are stamped with a slug "
            f"no project owns. set_project_charter and other project surfaces will "
            f"fail with 'No such project'. Fix: project_envelope(slug, dry_run=False) "
            f"auto-registers, or register the path with `okuro cortex add`."
        )
    excluded = [
        {"table": s.table, "role": s.role, "reason": s.reason}
        for s in specs
        if s.role in ("derived", "unclassified")
    ]
    result["excluded_tables"] = excluded
    unclassified = [s.table for s in specs if s.role == "unclassified"]
    if unclassified:
        result["unclassified_warning"] = (
            "These tables carry a project column but have no policy entry, so they "
            "are excluded from both inventory and envelope: "
            + ", ".join(sorted(unclassified))
        )
    return result


# ---------------------------------------------------------------------------
# project_envelope — the re-filer
# ---------------------------------------------------------------------------

def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _envelope_dir() -> Path:
    d = Path.home() / _ENVELOPE_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _parse_ids(ids: Sequence[str]) -> tuple[dict[str, set[str]], set[str]]:
    """Split ``["agent_memory:abc", "def"]`` into per-table and bare ids."""
    qualified: dict[str, set[str]] = {}
    bare: set[str] = set()
    for raw in ids:
        item = str(raw).strip()
        if not item:
            continue
        if ":" in item:
            table, _, rid = item.partition(":")
            qualified.setdefault(table.strip(), set()).add(rid.strip())
        else:
            bare.add(item)
    return qualified, bare


def _id_matches(row_id: str, wanted: set[str]) -> bool:
    """Exact id, or an 8-char (or longer) handle prefix."""
    if row_id in wanted:
        return True
    return any(len(w) >= 8 and row_id.startswith(w) for w in wanted)


def _collect_candidates(
    db,
    specs: list[TableSpec],
    slug: str,
    ids: Sequence[str] | None,
    match: str | None,
    match_regex: bool,
    match_fields: str,
    from_projects: Sequence[str] | None,
    created_after: str | None,
    created_before: str | None,
    include_superseded: bool,
    excerpt_chars: int,
) -> tuple[list[dict], list[dict]]:
    """Rows that WOULD move, and rows already at the target."""
    qualified, bare = _parse_ids(ids or [])
    have_id_filter = bool(qualified or bare)
    pattern = None
    if match:
        pattern = re.compile(match if match_regex else re.escape(match), re.IGNORECASE)

    candidates: list[dict] = []
    already: list[dict] = []

    for spec in specs:
        if not spec.movable:
            continue
        if qualified and spec.table not in qualified and not bare:
            continue

        sup_sql = _superseded_sql(spec)
        sup_expr = sup_sql or "0"
        sql = (
            f'SELECT {_select_columns(spec)}, {sup_expr} AS _superseded '
            f'FROM "{spec.table}" WHERE 1=1'
        )
        params: list[Any] = []
        if from_projects:
            marks = ", ".join("?" * len(from_projects))
            sql += f' AND "{spec.project_col}" IN ({marks})'
            params.extend(from_projects)
        if created_after and spec.date_col:
            sql += f' AND "{spec.date_col}" >= ?'
            params.append(created_after)
        if created_before and spec.date_col:
            sql += f' AND "{spec.date_col}" < ?'
            params.append(created_before)
        if not include_superseded and sup_sql:
            sql += f" AND NOT {sup_sql}"

        for row in db.fetchall(sql, tuple(params)):
            rid = str(row[spec.id_col])
            current = row.get(spec.project_col)

            if have_id_filter:
                wanted = qualified.get(spec.table, set()) | bare
                if not _id_matches(rid, wanted):
                    continue
            elif pattern is not None:
                haystack = []
                if match_fields in ("auto", "title") and spec.title_col:
                    haystack.append(str(row.get(spec.title_col) or ""))
                if match_fields in ("auto", "body") and spec.body_col:
                    haystack.append(str(row.get(spec.body_col) or ""))
                if not pattern.search(" \n ".join(haystack)):
                    continue
            else:
                # No selector at all — refuse to sweep the whole store.
                continue

            shaped = _shape_row(row, spec, excerpt_chars)
            shaped["from_project"] = current
            shaped["to_project"] = slug
            if current == slug:
                already.append(shaped)
            else:
                candidates.append(shaped)

    candidates.sort(key=lambda r: (r["table"], str(r.get("date") or "")))
    return candidates, already


def _ensure_registered(db, slug: str, auto_register: bool) -> dict[str, Any]:
    """Guarantee a project row owns ``slug`` before any row is stamped with it.

    The incident this prevents: rows were stamped with a slug that existed
    only as a string on those rows. ``set_project_charter`` then failed with
    "No such project" — that is how it was found, after the fact.
    """
    row = db.fetchone("SELECT id FROM projects WHERE id = ?", (slug,))
    if row:
        return {"registered": True, "action": "pre-existing"}
    if not auto_register:
        return {
            "registered": False,
            "action": "refused",
            "error": (
                f"'{slug}' is not a registered project and auto_register=False. "
                f"Fix: call again with auto_register=True, or register the project "
                f"first (`register_project('{slug}')`). Refusing to stamp "
                f"rows with a slug no project owns."
            ),
        }
    # Delegates the INSERT to the registry module so there is ONE writer for the
    # projects table. insert_project_row deliberately does NOT commit: we are
    # inside the caller's db.write() block, and committing here would break the
    # atomicity that keeps a failed re-file from leaving a half-applied plan.
    from okuro.sense.projects import insert_project_row

    written = insert_project_row(db, slug)
    return {
        "registered": True,
        "action": "created",
        "note": (
            "auto-registered (provisional=1, per migration 093)"
            if written.get("provisional")
            else "auto-registered"
        ),
    }


def project_envelope(
    slug: str,
    ids: list[str] | None = None,
    match: str | None = None,
    match_regex: bool = False,
    match_fields: str = "auto",
    from_projects: list[str] | None = None,
    tables: list[str] | None = None,
    exclude_ids: list[str] | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
    include_superseded: bool = True,
    dry_run: bool = True,
    auto_register: bool = True,
    plan_ref: str | None = None,
    excerpt_chars: int = 150,
) -> dict[str, Any]:
    """Re-file brain rows under ``slug``. DRY RUN BY DEFAULT.

    Selection is by explicit ``ids`` (``"agent_memory:abc123"`` or a bare
    id/8-char handle) OR by ``match`` over title+body. At least one is
    required — there is deliberately no "move everything" mode.

    The review flow this exists for, in three calls:

      1. ``project_envelope(slug, match="figma")``            -> dry run + plan_ref
      2. read the rows, note the ones that do not belong
      3. ``project_envelope(slug, plan_ref=..., exclude_ids=[...],
         dry_run=False)``

    Step 3 replays the EXACT rows from the plan minus the exclusions. It is
    not a re-run of a tweaked pattern, so nothing new can enter the set
    between review and apply — the failure mode being designed out is a
    regex that over-matched by 14 rows and was caught only by reading all
    84 by eye.

    Every write happens in one transaction. An undo file is persisted before
    the first write; pass its ``backup_ref`` to ``project_envelope_undo``.
    Content is never touched and nothing is ever deleted — only ``project``.
    """
    from okuro.db import get_db

    db = get_db()
    specs = discover_project_tables(db)
    by_name = {s.table: s for s in specs}

    if tables:
        unknown = [t for t in tables if t not in by_name]
        if unknown:
            return {"error": f"unknown table(s): {', '.join(sorted(unknown))}",
                    "known_tables": sorted(by_name)}
        not_movable = [t for t in tables if not by_name[t].movable]
        if not_movable:
            return {
                "error": (
                    "these tables are not re-filable: "
                    + ", ".join(
                        f"{t} ({by_name[t].role} — {by_name[t].reason})"
                        for t in sorted(not_movable)
                    )
                )
            }
        active_specs = [by_name[t] for t in tables]
    else:
        active_specs = [s for s in specs if s.movable]

    excluded = set(exclude_ids or [])

    # ---- selection: replay a reviewed plan, or build a fresh one ----
    if plan_ref:
        plan_path = Path(plan_ref)
        if not plan_path.is_absolute():
            plan_path = _envelope_dir() / plan_ref
        if not plan_path.exists():
            return {"error": f"plan not found: {plan_path}"}
        plan = json.loads(plan_path.read_text())
        if plan.get("slug") != slug:
            return {
                "error": (
                    f"plan targets '{plan.get('slug')}' but slug='{slug}' was passed. "
                    f"Refusing to apply a plan to a different project."
                )
            }
        candidates = plan.get("would_move", [])
        already = plan.get("already_filed", [])
        selection = {"source": "plan", "plan_ref": str(plan_path)}
    else:
        if not ids and not match:
            return {
                "error": (
                    "no selector: pass ids=[...] or match='...'. There is no "
                    "move-everything mode — an unbounded sweep is the bug this "
                    "tool exists to prevent."
                )
            }
        candidates, already = _collect_candidates(
            db, active_specs, slug, ids, match, match_regex, match_fields,
            from_projects, created_after, created_before, include_superseded,
            excerpt_chars,
        )
        selection = {
            "source": "query",
            "ids": ids,
            "match": match,
            "match_regex": match_regex,
            "from_projects": from_projects,
        }

    dropped = [
        r for r in candidates
        if r["id"] in excluded or r["handle"] in excluded
        or f"{r['table']}:{r['id']}" in excluded
    ]
    would_move = [r for r in candidates if r not in dropped]

    from_breakdown: dict[str, int] = {}
    table_breakdown: dict[str, int] = {}
    for r in would_move:
        from_breakdown[str(r.get("from_project"))] = from_breakdown.get(str(r.get("from_project")), 0) + 1
        table_breakdown[r["table"]] = table_breakdown.get(r["table"], 0) + 1

    result: dict[str, Any] = {
        "slug": slug,
        "dry_run": dry_run,
        "selection": selection,
        "counts": {
            "would_move": len(would_move),
            "already_filed": len(already),
            "excluded_by_caller": len(dropped),
        },
        "by_table": table_breakdown,
        "by_source_project": from_breakdown,
        "would_move": would_move,
        "already_filed": already,
        "excluded_by_caller": dropped,
    }

    registered_now = db.fetchone("SELECT id FROM projects WHERE id = ?", (slug,))
    result["target_registered"] = bool(registered_now)

    # ---------------- dry run: persist the plan, write nothing -------------
    if dry_run:
        stamp = _now_stamp()
        plan_name = f"plan-{slug}-{stamp}-{uuid.uuid4().hex[:6]}.json"
        plan_file = _envelope_dir() / plan_name
        plan_file.write_text(json.dumps(
            {
                "slug": slug,
                "created_at": stamp,
                "selection": selection,
                "would_move": would_move,
                "already_filed": already,
            },
            indent=2,
            default=str,
        ))
        result["plan_ref"] = plan_name
        result["next"] = (
            f"Review would_move. To apply exactly this set: "
            f"project_envelope(slug='{slug}', plan_ref='{plan_name}', dry_run=False)"
            f" — add exclude_ids=[...] to drop rows you rejected."
        )
        if not registered_now:
            result["note_registration"] = (
                f"'{slug}' is not registered yet; applying will auto-register it "
                f"(auto_register={auto_register})."
            )
        return result

    # ---------------- apply ------------------------------------------------
    if not would_move:
        result["applied"] = {"moved": 0, "cascaded": 0}
        result["idempotent"] = True
        result["message"] = (
            f"Nothing to move — {len(already)} row(s) already filed under "
            f"'{slug}'. No-op."
        )
        return result

    stamp = _now_stamp()
    undo_name = f"undo-{slug}-{stamp}-{uuid.uuid4().hex[:6]}.json"
    undo_file = _envelope_dir() / undo_name
    undo_records = [
        {
            "table": r["table"],
            "id": r["id"],
            "previous_project": r.get("from_project"),
            "new_project": slug,
        }
        for r in would_move
    ]
    undo_file.write_text(json.dumps(
        {"slug": slug, "created_at": stamp, "records": undo_records},
        indent=2, default=str,
    ))

    moved = 0
    cascaded = 0
    skipped_drift: list[dict] = []
    try:
        with db.write():
            reg = _ensure_registered(db, slug, auto_register)
            if not reg["registered"]:
                raise RuntimeError(reg["error"])

            for rec in undo_records:
                spec = by_name[rec["table"]]
                current = db.fetchone(
                    f'SELECT "{spec.project_col}" AS p FROM "{spec.table}" '
                    f'WHERE "{spec.id_col}" = ?',
                    (rec["id"],),
                )
                if current is None:
                    skipped_drift.append({**rec, "why": "row no longer exists"})
                    continue
                if current["p"] == slug:
                    skipped_drift.append({**rec, "why": "already at target"})
                    continue
                if current["p"] != rec["previous_project"]:
                    # The row moved since the plan was written. Applying would
                    # silently overwrite someone else's decision.
                    skipped_drift.append({
                        **rec,
                        "why": f"project drifted to '{current['p']}' since the plan",
                    })
                    continue
                db.execute(
                    f'UPDATE "{spec.table}" SET "{spec.project_col}" = ? '
                    f'WHERE "{spec.id_col}" = ?',
                    (slug, rec["id"]),
                )
                moved += 1
                for child_table, fk_col, child_proj in _CASCADES.get(rec["table"], []):
                    if child_table not in by_name:
                        continue
                    cur = db.execute(
                        f'UPDATE "{child_table}" SET "{child_proj}" = ? '
                        f'WHERE "{fk_col}" = ?',
                        (slug, rec["id"]),
                    )
                    cascaded += getattr(cur, "rowcount", 0) or 0
    except Exception as exc:
        logger.exception("project_envelope apply failed — rolled back")
        result["applied"] = {"moved": 0, "cascaded": 0}
        result["error"] = f"apply failed, transaction rolled back: {exc}"
        result["backup_ref"] = undo_name
        return result

    result["applied"] = {"moved": moved, "cascaded": cascaded}
    result["backup_ref"] = undo_name
    result["registration"] = reg
    if skipped_drift:
        result["skipped"] = skipped_drift

    # Idempotence has to be VISIBLE, not merely true. Replaying a plan whose
    # rows all landed on a previous run reaches this point with moved=0 and
    # every row skipped as "already at target" — a no-op that, without this,
    # reports only a bare zero and reads like a failure.
    if moved == 0 and skipped_drift:
        at_target = [s for s in skipped_drift if s["why"] == "already at target"]
        if len(at_target) == len(skipped_drift):
            result["idempotent"] = True
            result["message"] = (
                f"No-op — all {len(at_target)} selected row(s) are already filed "
                f"under '{slug}'. Nothing was written."
            )
    result["undo"] = f"project_envelope_undo(backup_ref='{undo_name}')"
    return result


def project_envelope_undo(backup_ref: str, dry_run: bool = False) -> dict[str, Any]:
    """Restore every row in ``backup_ref`` to its previous project.

    Only restores rows still sitting at the value the envelope wrote. A row
    changed again since is reported, not clobbered.
    """
    from okuro.db import get_db

    path = Path(backup_ref)
    if not path.is_absolute():
        path = _envelope_dir() / backup_ref
    if not path.exists():
        return {"error": f"backup not found: {path}"}

    payload = json.loads(path.read_text())
    records = payload.get("records", [])
    db = get_db()
    specs = {s.table: s for s in discover_project_tables(db)}

    would: list[dict] = []
    skipped: list[dict] = []
    for rec in records:
        spec = specs.get(rec["table"])
        if not spec or not spec.id_col:
            skipped.append({**rec, "why": "table no longer discoverable"})
            continue
        cur = db.fetchone(
            f'SELECT "{spec.project_col}" AS p FROM "{spec.table}" '
            f'WHERE "{spec.id_col}" = ?',
            (rec["id"],),
        )
        if cur is None:
            skipped.append({**rec, "why": "row no longer exists"})
            continue
        if cur["p"] != rec.get("new_project"):
            skipped.append({
                **rec,
                "why": f"project is now '{cur['p']}', not the value the envelope wrote",
            })
            continue
        would.append(rec)

    if dry_run:
        return {
            "backup_ref": backup_ref,
            "dry_run": True,
            "would_restore": len(would),
            "skipped": skipped,
            "records": would,
        }

    restored = 0
    cascaded = 0
    try:
        with db.write():
            for rec in would:
                spec = specs[rec["table"]]
                db.execute(
                    f'UPDATE "{spec.table}" SET "{spec.project_col}" = ? '
                    f'WHERE "{spec.id_col}" = ?',
                    (rec.get("previous_project"), rec["id"]),
                )
                restored += 1
                for child_table, fk_col, child_proj in _CASCADES.get(rec["table"], []):
                    if child_table not in specs:
                        continue
                    cur = db.execute(
                        f'UPDATE "{child_table}" SET "{child_proj}" = ? '
                        f'WHERE "{fk_col}" = ?',
                        (rec.get("previous_project"), rec["id"]),
                    )
                    cascaded += getattr(cur, "rowcount", 0) or 0
    except Exception as exc:
        logger.exception("project_envelope_undo failed — rolled back")
        return {"backup_ref": backup_ref, "restored": 0, "error": str(exc)}

    return {
        "backup_ref": backup_ref,
        "restored": restored,
        "cascaded": cascaded,
        "skipped": skipped,
    }
