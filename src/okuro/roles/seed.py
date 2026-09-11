# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Seed the roles table from the two catalog layers (shipped + user).
# index:
#   imports
#   CATALOG_DIR
#   RETIRED_TOOL_ALIASES
#   def _load_catalog
#   def validate_catalog_vocabulary
#   def seed_from_catalog
#   def seed_if_empty
# AGENT_HEADER_END -->
"""Seed the roles table from YAML fixtures on fresh install.

Two layers (okuro.roles.layers): the `catalog/` directory next to this file
ships the curated roles as source of truth, and ~/.okuro/roles/catalog holds
the user's personal roles — user data, never in a repo, never overwritten
here (rows with origin='user' are skipped even with overwrite=True). A user
role_id colliding with a shipped one is refused at load. On a fresh install
the `roles` table is empty (migration 002 creates the schema but inserts
nothing) — without this seed, `roles_match` returns zero results forever.
`seed_if_empty` is the idempotent entry point called from `okuro init` +
`/api/onboarding/complete`.

Fields written per role:
  role_id, domain, description, maturity, tier, model,
  prompt, lean_prompt, micro_prompt, tools

Runtime counters (sessions, learnings, maintenance_schedule,
last_maintained, created_at, updated_at) are DB-managed; the catalog
doesn't carry them.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import yaml

from okuro.orchestrator.config import CANONICAL_TIERS
from okuro.roles.layers import (
    SHIPPED_CATALOG_DIR,
    catalog_dirs,
    check_layer_collisions,
)

log = logging.getLogger("okuro.roles.seed")

# Back-compat alias — layers.py is the canonical resolver now.
CATALOG_DIR = SHIPPED_CATALOG_DIR

#: MCP servers / CLI aliases the runtime no longer has. A role that declares
#: one hands every adopting agent a confident method built on a tool that
#: cannot answer. Measured 2026-08-25: neither is configured for any provider,
#: and the eichi role DB that `tm-eichi` names was replaced by okuro.db.
#: `tm-launcher` is deliberately NOT listed — 52 shipped catalog files still
#: declare it and its liveness was not measured; that is its own sweep.
RETIRED_TOOL_ALIASES = frozenset({"tm-eichi", "tm-nightbird"})

#: An okuro MCP verb: lowercase snake_case with at least one underscore.
#: Anything else in `tools:` names an MCP server ("filesystem"), a CLI alias
#: ("tm-cortex") or a harness tool ("Read") and is out of scope — 62 of the 86
#: shipped files declare at least one, all legitimately. Same rule as
#: tests/roles/test_catalog_tool_integrity.py, kept deliberately identical.
_OKURO_VERB = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")


def _load_catalog() -> list[dict[str, Any]]:
    """Read every *.yaml from both catalog layers and return validated dicts.

    Each dict carries ``_origin`` ('shipped' | 'user'). Skips files with a
    missing role_id/domain — those are the only two fields every seed row
    must have. A user role_id that collides with a shipped one raises.
    """
    out: list[dict[str, Any]] = []
    ids_by_origin: dict[str, set[str]] = {"shipped": set(), "user": set()}
    for cdir, origin in catalog_dirs():
        if not cdir.exists():
            continue
        for path in sorted(cdir.glob("*.yaml")):
            try:
                with path.open() as f:
                    data = yaml.safe_load(f) or {}
            except yaml.YAMLError as exc:
                log.warning("skipping malformed role catalog %s: %s", path.name, exc)
                continue
            if not data.get("role_id") or not data.get("domain"):
                log.warning("skipping role catalog %s: missing role_id/domain", path.name)
                continue
            data["_origin"] = origin
            ids_by_origin[origin].add(data["role_id"])
            out.append(data)
    check_layer_collisions(ids_by_origin["shipped"], ids_by_origin["user"])
    return out


def _live_tool_names() -> set[str]:
    """Tool names the running MCP registry serves; empty set if unreadable."""
    try:
        import asyncio

        from okuro.mcp import _registry

        return {t.name for t in asyncio.run(_registry.list_tools_impl())}
    except Exception as exc:  # noqa: BLE001 — advisory check, never fatal
        log.warning("seed: live tool registry unreadable (%s)", exc)
        return set()


def validate_catalog_vocabulary(
    catalog: list[dict[str, Any]], *, check_tools_live: bool = False
) -> list[str]:
    """One message per vocabulary violation in a loaded catalog.

    Both classes used to degrade SILENTLY, which is why they are checked here
    — at the write boundary — rather than at the point of use:

    * a non-canonical ``tier``: ``orchestrator/config.py`` logs "Unresolvable
      tier" and runs the role as ``standard``, so a role built for the top
      model quietly runs on the default one (the failure migration 106
      cleaned up in the DB and the fixtures then re-introduced);
    * a dead tool name: the adopting agent receives a complete, confident
      method built on a tool that hard-errors.

    ``check_tools_live`` resolves okuro-verb-shaped names against the running
    MCP registry. Off by default: importing the registry during an install is
    neither cheap nor guaranteed to succeed.
    """
    live = _live_tool_names() if check_tools_live else set()
    problems: list[str] = []
    for role in catalog:
        rid = role.get("role_id", "?")
        tier = role.get("tier")
        if tier is not None and tier not in CANONICAL_TIERS:
            problems.append(
                f"{rid}: tier {tier!r} is outside the canonical set "
                f"{tuple(CANONICAL_TIERS)} — dispatch will silently run this "
                f"role as 'standard'"
            )
        for name in role.get("tools") or []:
            if not isinstance(name, str):
                continue
            if name in RETIRED_TOOL_ALIASES:
                problems.append(
                    f"{rid}: tools declares {name!r}, a retired alias the "
                    f"runtime no longer has"
                )
            elif live and _OKURO_VERB.match(name) and name not in live:
                problems.append(
                    f"{rid}: tools declares okuro verb {name!r}, which the "
                    f"live MCP registry does not serve"
                )
    return problems


def seed_from_catalog(
    db, *, overwrite: bool = False, strict: bool = False
) -> tuple[int, int]:
    """Insert roles from the catalog into the DB.

    Returns (inserted, skipped). When ``overwrite`` is False (default), only
    roles whose role_id is NOT already present get inserted — so re-running
    after the user has customized a role won't clobber their edits.

    ``strict`` turns the vocabulary gate below from loud into fatal. It is
    False by default for one measured reason: 22 of the 86 shipped catalog
    YAMLs still carry a pre-migration-106 tier, so raising unconditionally
    would break ``okuro init`` and every test that seeds the real catalog
    (tests/roles/test_tour_builder_role.py) until that backlog is cleared.
    The per-violation ``log.error`` is what keeps a NEW violation from
    entering unnoticed; ``strict=True`` is the CI / ``okuro doctor`` gate.
    """
    catalog = _load_catalog()
    if not catalog:
        return 0, 0

    problems = validate_catalog_vocabulary(catalog, check_tools_live=strict)
    if problems:
        if strict:
            raise ValueError(
                f"role catalog vocabulary violations ({len(problems)}):\n  "
                + "\n  ".join(problems)
            )
        for problem in problems:
            log.error("role catalog vocabulary violation — %s", problem)

    # Backfill (idempotent): any DB role absent from the SHIPPED catalog was
    # created at runtime or belongs to the user layer — both are personal.
    # This also converts pre-migration-132 rows (default 'shipped') and means
    # a role removed from the shipped catalog flips to 'user', i.e. is
    # preserved rather than orphaned.
    shipped_ids = sorted(
        r["role_id"] for r in catalog if r["_origin"] == "shipped"
    )
    if shipped_ids:
        marks = ",".join("?" * len(shipped_ids))
        db.execute(
            f"UPDATE roles SET origin='user' "
            f"WHERE role_id NOT IN ({marks}) AND origin != 'user'",
            tuple(shipped_ids),
        )

    existing_origin = {
        row["role_id"]: row["origin"]
        for row in db.fetchall("SELECT role_id, origin FROM roles")
    }
    existing_ids = set(existing_origin)

    inserted = 0
    skipped = 0
    for role in catalog:
        rid = role["role_id"]
        # A personal role is NEVER overwritten by a seed run — not even with
        # overwrite=True. Updates flow only shipped-catalog -> shipped-row.
        if existing_origin.get(rid) == "user":
            skipped += 1
            continue
        if rid in existing_ids and not overwrite:
            skipped += 1
            continue

        # Refuse to seed a role whose prompts are all empty — the runtime
        # path (config.resolve_role) raises ValueError on dispatch and
        # blocks any task that picks it. The catalog stub is metadata-only;
        # without prompt content the role is unrunnable. Logging at WARN so
        # this surfaces in install logs and `okuro doctor`.
        if not (role.get("prompt") or role.get("lean_prompt") or role.get("micro_prompt")):
            log.warning(
                "skipping role catalog %s: all prompt fields empty "
                "(prompt/lean_prompt/micro_prompt) — role would be unrunnable",
                rid,
            )
            skipped += 1
            continue

        tools_json = json.dumps(role.get("tools") or [])
        panel_eligible = 0 if role.get("panel_eligible") is False else 1
        params = (
            rid,
            role["domain"],
            role.get("description") or "",
            role.get("maturity") or "active",
            role.get("tier") or "standard",
            role.get("model") or "sonnet",
            role.get("prompt") or "",
            role.get("lean_prompt"),
            role.get("micro_prompt"),
            tools_json,
            panel_eligible,
            role["_origin"],
        )

        if rid in existing_ids:
            db.execute(
                "UPDATE roles SET domain=?, description=?, maturity=?, tier=?, "
                "model=?, prompt=?, lean_prompt=?, micro_prompt=?, tools=?, "
                "panel_eligible=?, origin=?, updated_at=datetime('now') "
                "WHERE role_id=?",
                params[1:] + (rid,),
            )
        else:
            db.execute(
                "INSERT INTO roles (role_id, domain, description, maturity, tier, "
                "model, prompt, lean_prompt, micro_prompt, tools, panel_eligible, "
                "origin) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                params,
            )

        # Seed vec_roles so the proposer's match_roles() can see this
        # role. Without this, a catalog-only role never surfaces in the
        # panel proposer even after promote_role flips maturity to
        # 'active' — the vector index queried by match_roles is empty
        # for it. match_roles still filters out maturity='draft', so
        # this is safe to do for drafts too; they remain invisible to
        # the proposer until promoted.
        description = role.get("description") or ""
        if description:
            try:
                from okuro.embed import embed_one
                from okuro.embed.client import to_bytes
                emb = embed_one(description)
                db.execute("DELETE FROM vec_roles WHERE id = ?", (rid,))
                db.execute(
                    "INSERT INTO vec_roles (id, embedding) VALUES (?, ?)",
                    (rid, to_bytes(emb)),
                )
            except Exception as exc:
                log.warning(
                    "seed_from_catalog: vec_roles seed failed for %s: %s",
                    rid, exc,
                )

        inserted += 1

    return inserted, skipped


def seed_if_empty(db) -> int:
    """If the roles table has zero rows, seed it from the catalog.

    Returns the number of rows inserted. Zero when the table already had
    data OR when the catalog is missing.
    """
    row = db.fetchone("SELECT COUNT(*) AS c FROM roles")
    count = row["c"] if row else 0
    if count > 0:
        return 0
    inserted, _ = seed_from_catalog(db, overwrite=False)
    log.info("seeded %d roles from catalog", inserted)
    return inserted
