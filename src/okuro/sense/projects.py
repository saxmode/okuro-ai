# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Project registry — CRUD for projects table.
# index:
#   imports
#   def list_projects
#   def get_project
#   def register_project
#   def update_project
#   def update_project_agent
#   def scan_projects
#   def _detect_stack
# AGENT_HEADER_END -->
"""Project registry — CRUD for projects table.

Ported from tm-launcher brain/projects.py.
"""

import json
import os
from pathlib import Path


def list_projects(active_only: bool = True, include_provisional: bool = True) -> str:
    """List all projects.

    ``include_provisional`` defaults True to preserve prior behavior. Pass
    False on canonical surfaces (project finder, charter eligibility) to hide
    auto-registered rows — a count of what was hidden is appended so the
    quarantine is visible, never silent.
    """
    from okuro.db import get_db

    db = get_db()
    where = []
    if active_only:
        where.append("active = 1")
    if not include_provisional:
        where.append("COALESCE(provisional, 0) = 0")
    sql = "SELECT id, name, path, url, stack, port_range, roles, description, active FROM projects"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY name"

    rows = db.fetchall(sql)
    if not rows:
        return "No projects found."

    lines = []
    for r in rows:
        line = f"**{r['id']}**: {r['name']}"
        if r.get("description"):
            line += f" — {r['description']}"
        details = []
        if r.get("path"):
            details.append(f"path: {r['path']}")
        stack = json.loads(r["stack"]) if isinstance(r.get("stack"), str) else (r.get("stack") or [])
        if stack:
            details.append(f"stack: {', '.join(stack)}")
        if r.get("port_range"):
            details.append(f"ports: {r['port_range']}")
        roles = json.loads(r["roles"]) if isinstance(r.get("roles"), str) else (r.get("roles") or [])
        if roles:
            details.append(f"roles: {', '.join(roles)}")
        if details:
            line += "\n  " + " | ".join(details)
        lines.append(line)

    if not include_provisional:
        clause = "active = 1 AND " if active_only else ""
        hidden = db.fetchone(
            f"SELECT COUNT(*) AS n FROM projects WHERE {clause}COALESCE(provisional, 0) = 1"
        )
        n = hidden["n"] if hidden else 0
        if n:
            lines.append(f"\n_({n} provisional/auto-registered hidden — list with include_provisional=true)_")

    return "\n".join(lines)


def get_project(slug_or_path: str) -> str | None:
    """Get project details including project-scoped memories."""
    from okuro.db import get_db

    db = get_db()

    row = db.fetchone(
        "SELECT id, name, path, url, stack, port_range, roles, description, charter, kind "
        "FROM projects WHERE id = ? AND active = 1",
        (slug_or_path,),
    )
    if not row:
        row = db.fetchone(
            "SELECT id, name, path, url, stack, port_range, roles, description, charter, kind "
            "FROM projects WHERE path = ? AND active = 1",
            (slug_or_path,),
        )
    if not row:
        return None

    pid = row["id"]
    lines = [f"## {row['name']} ({pid})"]
    if row.get("description"):
        lines.append(row["description"])
    lines.append("")
    if row.get("kind"):
        lines.append(f"- **Kind:** {row['kind']}")
    # `unknown/<slug>` is the sentinel progress.py:97 writes when auto-
    # registration cannot infer a directory — 52 of 106 rows hold it. Rendering
    # it as `- **Path:** unknown/northwind` states a placeholder as a fact, and
    # an agent that reads a Path acts on it: it cds there, greps there, and
    # reports the absence as a finding. Say "no directory" or say nothing.
    from okuro.sense.overview import _path_is_real

    if _path_is_real(row.get("path")):
        lines.append(f"- **Path:** {row['path']}")
    else:
        lines.append(
            "- **Path:** none — this project has no directory. "
            "Do not cd, search or write anywhere on its behalf."
        )
    if row.get("url"):
        lines.append(f"- **URL:** {row['url']}")
    stack = json.loads(row["stack"]) if isinstance(row.get("stack"), str) else (row.get("stack") or [])
    if stack:
        lines.append(f"- **Stack:** {', '.join(stack)}")

    # Charter — the per-project sub-bootstrap: compact, human-authored
    # doctrine (architecture, core job, axioms). Rendered high (right after
    # the identity fields, before churn-y learnings) so it anchors the
    # agent's model of the project before task-specific noise.
    if row.get("charter"):
        lines.append("")
        lines.append("### Charter")
        lines.append(row["charter"].strip())

    # Project-scoped memories
    memories = db.fetchall(
        "SELECT topic, content, confidence FROM agent_memory "
        "WHERE project = ? AND confidence > 0.3 "
        "ORDER BY confidence DESC, created_at DESC LIMIT 10",
        (pid,),
    )
    if memories:
        lines.append("")
        lines.append("### Learnings")
        for m in memories:
            content = m['content']
            if len(content) > 500:
                content = content[:500] + "..."
            lines.append(f"- [{m['topic']}] {content}")

    # Last progress
    progress = db.fetchone(
        "SELECT agent, status, summary, next_steps, blockers, branch, updated_at "
        "FROM progress WHERE project = ? ORDER BY updated_at DESC LIMIT 1",
        (pid,),
    )
    if progress:
        lines.append("")
        lines.append(f"### Last Progress ({progress['agent']}, {progress.get('updated_at', '?')})")
        lines.append(f"Status: {progress['status']} — {progress['summary']}")
        if progress.get("next_steps"):
            lines.append(f"Next: {progress['next_steps']}")
        if progress.get("blockers"):
            lines.append(f"Blocked: {progress['blockers']}")
        if progress.get("branch"):
            lines.append(f"Branch: {progress['branch']}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Write-surface classification
# ---------------------------------------------------------------------------
#
# THE CLASS THIS GUARDS AGAINST: a `projects` column exists in the schema, real
# consumers read it, and no write surface can set it — so the feature ships
# unreachable and every call that tries looks like it worked.
#
#   observes_path (migration 131) shipped exactly that way. It was settable
#   only by raw SQL until it was added to the allowlist below, because
#   update_project used to IGNORE any field it did not recognise: adding a
#   column and forgetting this set produced a successful-looking no-op.
#
#   design_profile (migration 069) is STILL in that state — read at
#   bootstrap/sections.py:1383,1909 and status.py:310, written by nothing in
#   src/. It is listed in AGENT_REFUSED with the reason, so it is now a
#   recorded decision rather than an omission.
#
# The silent drop is what made the failure invisible, so update_project now
# RAISES on an unrecognised field, and the three sets below partition EVERY
# column of the table. tests/sense/test_project_mutation_surface.py asserts
# that partition against PRAGMA table_info, so a column added by a future
# migration fails a test until somebody classifies it — the same "surface it as
# unclassified and exclude it until decided" stance sense/envelope.py takes for
# an unclassified table.

#: Columns ``update_project`` will write for an IN-PROCESS caller. Wider than
#: the agent surface: ``charter`` lives here because set_project_charter (the
#: validated, single-purpose tool that owns it) writes through this function,
#: and ``path``/``active``/``provisional`` because lifecycle code legitimately
#: sets them.
VALID_FIELDS = {"name", "path", "observes_path", "url", "stack", "port_range",
                "roles", "description", "active", "charter", "kind",
                "provisional"}

#: Columns an AGENT may set through the MCP surface. Every one of these is
#: read only for DISPLAY, or — for observes_path — by exactly one consumer
#: that cannot confer ownership.
AGENT_SETTABLE = {
    "observes_path",  # many-to-one; read ONLY by status.py::_repo_activity
    "name",           # display label; `id` is the primary key, not this
    "description",    # free text, rendered by list_projects / get_project
    "kind",           # classification label (migration 092), display only
    "stack",          # list, display only
    "port_range",     # display only
    "url",            # display only
}

#: Columns deliberately kept OFF the agent surface, each with the reason a
#: reader needs to disagree with the decision. A smaller correct surface beats
#: a complete dangerous one.
AGENT_REFUSED = {
    "path": (
        "OWNERSHIP — one-to-one, read by 12 sites including cortex root "
        "registration (cortex/roots.py) and session->project resolution "
        "(sense/commitments.py). Re-pointing it silently moves a cortex root "
        "or captures another project's sessions. Set observes_path instead to "
        "give a project a git signal without claiming the tree."
    ),
    "active": (
        "LIFECYCLE — the removal tombstone. Every writer sets it together with "
        "`indexed` (repos/lifecycle.py:770, corpus/lifecycle.py:572, "
        "sense/subjects.py:458); setting it alone desyncs the pair."
    ),
    "indexed": (
        "DERIVED — asserts this path IS a registered cortex root. Owned by "
        "cortex/roots.py (register/unregister). Setting it by hand claims a "
        "root that was never registered, or orphans one that was."
    ),
    "provisional": (
        "QUARANTINE — migration 093 flags auto-registered rows. It is cleared "
        "BY the curation signal in update_project when a human-meaningful "
        "`path` is set. Letting an agent clear its own row defeats the "
        "quarantine it exists to impose."
    ),
    "roles": (
        "INSTRUCTION CHANNEL, not data — bootstrap/assembler.py:218 reads it "
        "to pick the expert role injected into EVERY future session for this "
        "project. An agent writing it would author its own operating context."
    ),
    "charter": (
        "OWNED BY set_project_charter, which is the validated single-purpose "
        "tool for it. Two write paths for one field is how they drift."
    ),
    "design_profile": (
        "UNREACHABLE BY DESIGN, NOT BY OVERSIGHT — it is a REF into the "
        "design-profile registry, validated against a YAML file on disk by "
        "stack/validator.py:311. A generic setter would write a dangling ref "
        "that degrades every subsequent bootstrap. It needs a validating "
        "setter of its own; see the note in this module's class comment."
    ),
}

#: Columns no write surface should ever expose: identity and timestamps.
SYSTEM_MANAGED = {"id", "created_at", "updated_at"}


#: Fields ``register_project`` accepts at creation. Deliberately the DISPLAY
#: fields only — the same set ``update_project`` exposes minus the two path
#: columns, because a slug being registered has no verified tree yet and
#: ``path`` is UNIQUE-indexed (migration 131): a bad guess here steals another
#: project's session resolution. Set paths afterwards via ``update_project``.
REGISTER_FIELDS = {"name", "kind", "description", "url", "port_range", "stack"}


def insert_project_row(db, slug: str, **fields) -> dict:
    """INSERT a project row and DO NOT COMMIT — the shared writer.

    Split out because the two callers own different transaction boundaries:
    ``register_project`` commits, while ``envelope._ensure_registered`` runs
    inside the envelope's ``db.write()`` block, where committing early would
    break the atomicity that lets a failed re-file leave nothing half-done.
    One INSERT, two boundaries — not two INSERTs that drift.

    Assumes the caller already checked the row is absent.
    """
    cols = ["id", "name", "active"]
    values: list = [slug, fields.get("name") or slug, 1]

    # provisional exists from migration 093; guard so a pre-093 store still works.
    table_cols = {c["name"] for c in db.fetchall("PRAGMA table_info(projects)")}
    if "provisional" in table_cols:
        cols.append("provisional")
        values.append(1)

    for key in ("kind", "description", "url", "port_range", "stack"):
        if key not in fields or fields[key] is None:
            continue
        value = fields[key]
        cols.append(key)
        values.append(json.dumps(value) if key == "stack" and isinstance(value, list) else value)

    placeholders = ", ".join("?" for _ in cols)
    db.execute(
        f"INSERT INTO projects ({', '.join(cols)}) VALUES ({placeholders})",
        tuple(values),
    )
    return {
        "action": "created",
        "provisional": "provisional" in table_cols,
        "note": (
            "Registered provisional=1 (migration 093) — quarantined until a "
            "human-meaningful path is set via update_project."
            if "provisional" in table_cols
            else "Registered (pre-093 store, no provisional column)."
        ),
    }


def register_project(slug: str, **fields) -> dict:
    """Create a project row for ``slug``. IDEMPOTENT — the missing writer.

    Until this existed there was NO way to register a project: no MCP tool
    created one, ``update_project`` only updates, ``scan_projects`` walks
    directories so it cannot reach a pathless slug, and the fix named in
    ``envelope._ensure_registered``'s own error text (``okuro cortex add
    <path> --slug``) needs a path. ``project_envelope`` looked like the path
    but registers inside its apply block, which a slug whose rows are ALREADY
    tagged never reaches — exactly the orphan case. That gap is how orphan
    slugs and provisional rows accumulated: the only working route was calling
    a private function from a shell, which no agent finds.

    Rows are created ``provisional=1`` (migration 093). That is not a
    formality — quarantine is the whole reason this is safe to expose. A
    caller cannot clear it here, and ``update_project`` clears it only via the
    curation signal, when a human-meaningful ``path`` is set.

    Returns a dict with ``created`` False when the row already existed, so a
    caller can tell registration from a no-op. Never raises on re-registration
    and never overwrites an existing row's fields — that is ``update_project``.
    """
    from okuro.db import get_db

    slug = (slug or "").strip()
    if not slug:
        raise ValueError("register_project needs a non-empty slug.")

    unknown = set(fields) - REGISTER_FIELDS
    if unknown:
        raise ValueError(
            f"register_project cannot write at creation: {', '.join(sorted(unknown))}. "
            f"Writable here: {', '.join(sorted(REGISTER_FIELDS))}. "
            "`path`/`observes_path` are deliberately excluded — set them with "
            "update_project once the tree is known, so a guess cannot claim a "
            "UNIQUE-indexed path another project owns. `charter` belongs to "
            "set_project_charter; `provisional`/`active`/`indexed`/`roles`/"
            "`design_profile` are lifecycle or instruction channels."
        )

    db = get_db()
    existing = db.fetchone("SELECT id FROM projects WHERE id = ?", (slug,))
    if existing:
        return {
            "slug": slug,
            "created": False,
            "action": "pre-existing",
            "note": (
                f"'{slug}' is already registered — nothing written. Change fields "
                "with update_project."
            ),
        }

    result = insert_project_row(db, slug, **fields)
    db.conn.commit()
    return {"slug": slug, "created": True, **result}


def update_project(slug: str, **fields) -> str:
    """Update project fields — the IN-PROCESS write surface.

    Raises ValueError on a field this function cannot write, rather than
    dropping it. See the class comment above VALID_FIELDS: the silent drop is
    precisely how a new column ships unreachable while every call reports
    success. Agent callers want ``update_project_agent``, which is narrower.
    """
    from okuro.db import get_db

    db = get_db()
    unknown = set(fields) - VALID_FIELDS
    if unknown:
        raise ValueError(
            f"update_project cannot write: {', '.join(sorted(unknown))}. "
            "Dropping unrecognised fields silently is how projects.observes_path "
            "shipped unreachable (migration 131) — so this raises instead. "
            f"Writable here: {', '.join(sorted(VALID_FIELDS))}."
        )

    # `path` is OWNERSHIP (one-to-one, unique-indexed since migration 131);
    # `observes_path` is OBSERVATION (many-to-one, read only by
    # status.py::_repo_activity). Both are settable here; only `path` carries
    # the curation signal below, because watching a tree is not owning one.
    set_parts = []
    values = []
    for key, value in fields.items():
        set_parts.append(f"{key} = ?")
        if key in ("stack", "roles") and isinstance(value, list):
            values.append(json.dumps(value))
        else:
            values.append(value)

    if not set_parts:
        return "No valid fields to update."

    # Curation signal: setting a concrete (non-`unknown/*`) path promotes an
    # auto-registered row out of quarantine — unless provisional was set
    # explicitly in this same call.
    if "provisional" not in fields:
        path_val = fields.get("path")
        if path_val and not str(path_val).startswith("unknown/"):
            set_parts.append("provisional = 0")

    set_parts.append("updated_at = datetime('now')")
    values.append(slug)

    db.execute(f"UPDATE projects SET {', '.join(set_parts)} WHERE id = ?", tuple(values))
    db.conn.commit()
    return f"Project {slug} updated."


def update_project_agent(slug: str, **fields) -> str:
    """Agent-facing project mutation — the consent boundary for the MCP tool.

    Deliberately NARROWER than ``update_project``. The MCP tool's input schema
    already declares only AGENT_SETTABLE fields, but a JSON Schema without
    ``additionalProperties: false`` does not reject extra keys, so the schema
    is documentation and THIS is the gate. Enforcing here rather than in the
    handler also keeps the refusal reasons next to the allowlist that
    qualifies them, and means a second handler cannot route around it.

    Refusals raise with the reason, so an agent that tries to set `path` learns
    that observation exists — a silent drop would just look like it worked.
    """
    if not fields:
        return "No fields to update."

    for key in fields:
        if key in AGENT_REFUSED:
            raise ValueError(f"refused — `{key}`: {AGENT_REFUSED[key]}")
        if key in SYSTEM_MANAGED:
            raise ValueError(
                f"refused — `{key}` is system-managed (identity/timestamps)."
            )
        if key not in AGENT_SETTABLE:
            raise ValueError(
                f"unknown field `{key}`. Agent-settable: "
                f"{', '.join(sorted(AGENT_SETTABLE))}."
            )

    from okuro.db import get_db

    db = get_db()
    if not db.fetchone("SELECT id FROM projects WHERE id = ?", (slug,)):
        return f"No such project: {slug}"
    return update_project(slug, **fields)


def get_project_charter(slug: str) -> str | None:
    """Return the RAW charter text for a project (None if unset).

    Distinct from get_project (which renders the charter inside a markdown
    packet with learnings/progress): this returns just the charter body so
    an agent can round-trip it — fetch, edit, set_project_charter — without
    scraping it back out of the rendered section.
    """
    from okuro.db import get_db

    db = get_db()
    row = db.fetchone(
        "SELECT charter FROM projects WHERE id = ? AND active = 1", (slug,)
    )
    if not row:
        return None
    return row.get("charter")


def set_project_charter(slug: str, charter: str) -> str:
    """Set/replace a project's charter (the sub-bootstrap knowledge payload).

    Safe, single-purpose write surface: touches ONLY the charter column, never
    path/active/roles. Stamps updated_at (via update_project) so charter
    freshness is observable. Authoring flow: charter-scribe drafts → human
    approves → set_project_charter.
    """
    from okuro.db import get_db

    db = get_db()
    exists = db.fetchone("SELECT id FROM projects WHERE id = ?", (slug,))
    if not exists:
        return f"No such project: {slug}"
    return update_project(slug, charter=charter)


def scan_projects(root_dir: str = None) -> str:
    """Auto-detect projects from a directory."""
    from okuro.db import get_db

    if not root_dir:
        root_dir = str(Path.home())

    db = get_db()
    found = []

    # Look for directories with pyproject.toml, package.json, etc.
    root = Path(root_dir)
    if not root.is_dir():
        return f"Directory not found: {root_dir}"

    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue

        slug = entry.name.lower().replace(" ", "-")
        stack = _detect_stack(str(entry))

        existing = db.fetchone("SELECT id FROM projects WHERE id = ?", (slug,))
        if existing:
            found.append(f"{slug} (exists)")
            continue

        db.execute(
            "INSERT OR IGNORE INTO projects (id, name, path, stack, active) VALUES (?, ?, ?, ?, 1)",
            (slug, entry.name, str(entry), json.dumps(stack)),
        )
        found.append(f"{slug} (new)")

    db.conn.commit()
    return f"Scanned: {', '.join(found)}" if found else "No projects found."


def _detect_stack(project_path: str) -> list[str]:
    """Detect project stack from files."""
    stack = []
    p = Path(project_path)
    if (p / "package.json").exists():
        stack.append("node.js")
        try:
            pkg = json.loads((p / "package.json").read_text())
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "next" in deps:
                stack.append("next.js")
            if "react" in deps:
                stack.append("react")
            if "tailwindcss" in deps:
                stack.append("tailwind")
        except Exception:
            pass
    if (p / "pyproject.toml").exists() or (p / "setup.py").exists():
        stack.append("python")
    if (p / "Cargo.toml").exists():
        stack.append("rust")
    if (p / "docker-compose.yml").exists() or (p / "docker-compose.yaml").exists():
        stack.append("docker")
    return stack or ["unknown"]
