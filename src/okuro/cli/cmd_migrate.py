# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: `okuro migrate` — apply pending DB migrations explicitly (or --check to report).
# index: imports | def migrate
# AGENT_HEADER_END -->
"""Explicit database migration command.

Migrations are otherwise applied implicitly at daemon boot and during
`okuro init`. This command exposes the same apply path as a first-class,
synchronous operation so update.sh (and operators) can bring the schema
current deterministically — instead of relying on the doctor health check
to mutate the DB as a side effect (which it no longer does).
"""

import click

from .output import ok, warn, fail


@click.command()
@click.option(
    "--check",
    is_flag=True,
    help="Report pending migrations without applying them (exit 1 if any pending).",
)
def migrate(check: bool):
    """Apply pending database migrations (writes a pre-migration snapshot first)."""
    from .db_helpers import get_db

    try:
        db = get_db()
        try:
            if check:
                pending = db.pending_migrations()
                if pending:
                    warn(f"{len(pending)} migration(s) pending: {', '.join(pending)}")
                    raise SystemExit(1)
                ok("Database up to date")
                return
            applied = db.migrate()
        finally:
            db.close()
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        fail(f"Migration failed: {e}")
        raise SystemExit(1)

    if applied:
        ok(f"Applied {len(applied)} migration(s) — schema current")
    else:
        ok("Database already up to date")
