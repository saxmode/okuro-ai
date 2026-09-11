# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Two-layer role catalog — shipped (in-tree) vs user (~/.okuro/roles/catalog).
# index:
#   imports
#   SHIPPED_CATALOG_DIR
#   def user_catalog_dir
#   def catalog_dirs
#   def check_layer_collisions
# AGENT_HEADER_END -->
"""Two-layer role catalog: shipped roles vs personal roles.

Shipped roles live next to the package (``roles/catalog``) and are product
content — updated by releases, exported to the public repo. Personal roles
live in ``~/.okuro/roles/catalog`` — user data: never tracked in any repo,
never touched by a release or an update. Same layering as design_systems'
shipped-vs-user kits, including the collision rule: a user file reusing a
shipped role_id is REFUSED loudly (kits.py precedent) rather than silently
shadowing a role the orchestrator may dispatch by name (workforce-reviewer,
orchestrator, …).

Every reader of the catalog directory MUST resolve it through this module —
seed.py and peer/presets.py each computing their own path is exactly how
the user layer would silently fall out of one of them.
"""

from __future__ import annotations

from pathlib import Path

from okuro.db.engine import okuro_home

SHIPPED_CATALOG_DIR = Path(__file__).parent / "catalog"


def user_catalog_dir() -> Path:
    """Personal role catalog under the okuro home (honours $OKURO_HOME)."""
    return okuro_home() / "roles" / "catalog"


def catalog_dirs() -> list[tuple[Path, str]]:
    """All catalog layers as (directory, origin), shipped first."""
    return [(SHIPPED_CATALOG_DIR, "shipped"), (user_catalog_dir(), "user")]


def check_layer_collisions(shipped_ids: set[str], user_ids: set[str]) -> None:
    """Refuse a user role_id that collides with a shipped one."""
    clash = sorted(shipped_ids & user_ids)
    if clash:
        raise ValueError(
            f"user role catalog collides with shipped role id(s) {clash} — "
            f"rename the file(s) under {user_catalog_dir()} to ids okuro "
            "does not already ship (silent shadowing of a dispatchable "
            "role id is refused, matching the design-systems kits rule)"
        )
