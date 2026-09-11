# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: User profile CRUD against okuro.db.
# index:
#   def get_profile
#   def owner_display_name
#   def update_profile
#   def _schedule_regen
#   def _run_regen
#   def seed_default_profile
#   def get_profile_raw
#   def _format_markdown
#   def _format_value
# AGENT_HEADER_END -->
"""User profile CRUD against okuro.db."""

import json
import logging
import threading

import yaml

log = logging.getLogger(__name__)


# Debounce state for provider-instruction regeneration.
#
# Settings-page edits fan out as 5-10 PATCH calls in a single burst (one
# per field). Regenerating CLAUDE.md / AGENTS.md on every
# call would mean 5-10 file rewrites for one user action — wasteful and
# noisy in journalctl. Instead we schedule a single regen 2s after the
# LAST call, cancelling any pending timer when a new edit arrives.
#
# The timer is daemon=True so it never blocks process exit.
_REGEN_DEBOUNCE_SEC: float = 2.0
_regen_timer: threading.Timer | None = None
_regen_lock = threading.Lock()


def _run_regen() -> None:
    """Timer callback: regenerate provider instruction files + audit log."""
    try:
        from okuro.sense.providers import regen_all_provider_instructions
        paths = regen_all_provider_instructions()
        log.info("regenerated provider instructions: paths=%s", paths)
    except Exception as exc:  # noqa: BLE001
        log.warning("provider-instruction regen failed: %s", exc)


def _schedule_regen() -> None:
    """(Re)start the debounce timer. Cancels any pending timer first."""
    global _regen_timer
    with _regen_lock:
        if _regen_timer is not None:
            _regen_timer.cancel()
        _regen_timer = threading.Timer(_REGEN_DEBOUNCE_SEC, _run_regen)
        _regen_timer.daemon = True
        _regen_timer.start()


def get_profile(
    format: str = "markdown",
    section: str | None = None,
    exclude_sections: tuple[str, ...] | list[str] | None = None,
) -> str:
    """Read user profile from database.

    Args:
        format: "markdown", "yaml", or "json"
        section: Optional filter — "identity", "cognitive_style", "communication",
                "expertise", "work_style", "decision_style", "boundaries", or None for all.
        exclude_sections: Optional list/tuple of top-level section names to
            omit from the rendered output. Used by the bootstrap profile
            block to skip sections already rendered by the operative
            Behavioral Contract (cognitive_style, communication) so the
            same content is not stamped twice. Ignored when ``section``
            is provided (the explicit single-section filter wins).
    """
    from okuro.db import get_db

    db = get_db()
    row = db.fetchone("SELECT profile FROM user_profile WHERE id = 1")
    if not row:
        return "No profile found. Run `okuro init` first."

    profile = json.loads(row["profile"]) if isinstance(row["profile"], str) else row["profile"]

    if section:
        if section not in profile:
            available = ", ".join(profile.keys())
            return f"Unknown section: {section}. Available: {available}"
        profile = {section: profile[section]}
    elif exclude_sections:
        skip = set(exclude_sections)
        profile = {k: v for k, v in profile.items() if k not in skip}
        if not profile:
            return "(profile fields covered above)"

    if format == "json":
        return json.dumps(profile, indent=2)
    elif format == "yaml":
        return yaml.dump(profile, default_flow_style=False, sort_keys=False)
    else:
        return _format_markdown(profile)


def owner_display_name(fallback: str = "your user") -> str:
    """The user's display name from the profile's identity section.

    For prompts and user-facing strings that address the person okuro works
    for. Never hardcode a name: the same code ships to every install. Falls
    back to ``fallback`` when no profile exists or the name is unset.
    """
    try:
        data = json.loads(get_profile(format="json", section="identity"))
        name = (data.get("identity") or {}).get("name")
        if not name:
            return fallback
        return str(name).strip() or fallback
    except Exception:  # noqa: BLE001 — a missing profile is not an error here
        return fallback


def update_profile(
    section: str,
    path: str,
    action: str,
    value,
    *,
    _regen_now: bool = False,
) -> str:
    """Update user profile at a specific path.

    Args:
        section: Top-level section (e.g. "communication")
        path: Dot-separated path within section (e.g. "patterns" or "format_preferences.preferred")
        action: "set", "append", "remove"
        value: Value to set/append/remove
        _regen_now: If True, regenerate provider instruction files
            synchronously before returning (skipping the 2s debounce).
            Used by tests and by the ``/canon/deploy`` endpoint where the
            caller needs to know the new files are on disk by the time
            the call returns.
    """
    # The MCP boundary can stringify a nested-dict/list `value` — a caller
    # passing {..}/[..] arrives here as a JSON string, which then lands in the
    # profile blob as a string that every downstream consumer mis-reads (it
    # blanked the live frustration rule 2026-07-20). Parse it back so a
    # structured write stores a real dict/list. A scalar value (a rule, a label)
    # never starts with { or [, so this cannot corrupt string/number writes.
    if isinstance(value, str):
        _stripped = value.strip()
        if _stripped[:1] in ("{", "["):
            try:
                value = json.loads(_stripped)
            except (ValueError, TypeError):
                pass

    from okuro.db import get_db

    db = get_db()
    row = db.fetchone("SELECT profile FROM user_profile WHERE id = 1")
    if not row:
        return "No profile found. Run `okuro init` first."

    profile = json.loads(row["profile"]) if isinstance(row["profile"], str) else row["profile"]

    if section not in profile:
        if action == "set" and not path:
            profile[section] = value
        else:
            return f"Unknown section: {section}. Available: {', '.join(profile.keys())}"
    else:
        target = profile[section]
        keys = path.split(".") if path else []

        # Navigate to parent
        for key in keys[:-1]:
            if isinstance(target, dict) and key in target:
                target = target[key]
            else:
                return f"Path not found: {section}.{path}"

        final_key = keys[-1] if keys else None

        if action == "set":
            if final_key:
                target[final_key] = value
            else:
                profile[section] = value
        elif action == "append":
            container = target[final_key] if final_key else target
            if not isinstance(container, list):
                return f"Cannot append to non-list at {section}.{path}"
            container.append(value)
        elif action == "remove":
            container = target[final_key] if final_key else target
            if not isinstance(container, list):
                return f"Cannot remove from non-list at {section}.{path}"
            try:
                container.remove(value)
            except ValueError:
                return f"Value not found in list at {section}.{path}"
        else:
            return f"Unknown action: {action}. Use 'set', 'append', or 'remove'."

    db.execute(
        "UPDATE user_profile SET profile = ?, updated_at = datetime('now') WHERE id = 1",
        (json.dumps(profile),),
    )
    db.conn.commit()

    # Fire-and-forget enrichment pass so any new bare-string rules added
    # via settings edits / MCP update_profile calls gain rationale +
    # evidence before the next bootstrap reads them.
    try:
        from okuro.sense.rules import auto_enrich_async
        auto_enrich_async()
    except Exception:
        pass

    # Keep static instruction files (CLAUDE.md / AGENTS.md)
    # in sync with the live profile. Debounced because a settings-page
    # save fans out as 5-10 PATCH calls in a burst — we want one regen
    # at the end, not one per field. ``_regen_now=True`` overrides the
    # debounce for tests and for /canon/deploy.
    if _regen_now:
        _run_regen()
    else:
        try:
            _schedule_regen()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not schedule provider-instruction regen: %s", exc)

    return f"Profile updated: {section}.{path} ({action})"


def seed_default_profile() -> str:
    """Insert the default profile if none exists. Returns status message."""
    from okuro.db import get_db

    db = get_db()
    row = db.fetchone("SELECT id FROM user_profile WHERE id = 1")
    if row:
        return "Profile already exists."

    db.execute(
        "INSERT INTO user_profile (id, profile) VALUES (1, ?)",
        (json.dumps(_DEFAULT_PROFILE),),
    )
    db.conn.commit()
    return "Default profile seeded."


def get_profile_raw() -> dict:
    """Return profile as a dict. For internal use by other modules."""
    from okuro.db import get_db

    db = get_db()
    row = db.fetchone("SELECT profile FROM user_profile WHERE id = 1")
    if not row:
        return {}
    profile = row["profile"]
    return json.loads(profile) if isinstance(profile, str) else profile


# --- Formatting ---


def _format_markdown(profile: dict) -> str:
    lines = []
    for section_name, section_data in profile.items():
        lines.append(f"## {section_name.replace('_', ' ').title()}")
        lines.append(_format_value(section_data, depth=0))
        lines.append("")
    return "\n".join(lines)


def _format_value(value, depth: int = 0) -> str:
    indent = "  " * depth
    if isinstance(value, dict):
        lines = []
        for k, v in value.items():
            label = k.replace("_", " ").title()
            if isinstance(v, (dict, list)):
                lines.append(f"{indent}**{label}:**")
                lines.append(_format_value(v, depth + 1))
            else:
                lines.append(f"{indent}- **{label}:** {v}")
        return "\n".join(lines)
    elif isinstance(value, list):
        return "\n".join(f"{indent}- {_format_list_item(item)}" for item in value)
    else:
        return str(value)


def _format_list_item(item) -> str:
    """Render one list entry, unwrapping the {rule, rationale, evidence} shape.

    A bare f-string dropped the raw Python dict repr into the packet:

        - {'rule': 'clutter', 'rationale': 'attention is finite and non-
          renewable; clutter burns it on zero-value tokens', 'evidence': ...}

    Four of those shipped under "Hates" in every bootstrap, so the user's own
    stated hatred of clutter reached agents AS clutter — and cost ~180 tokens
    restating four rules that render correctly fifty lines earlier as
    "**Never do these — user hates:** repetition, using systems, ...".

    build_behavioral runs these through rules.parse_triple; this renderer never
    did. The rationale is kept — it is genuinely useful, it just needs to be
    prose rather than a dict literal.
    """
    if not isinstance(item, dict):
        return str(item)
    rule = item.get("rule") or item.get("name") or item.get("label")
    if rule:
        rationale = item.get("rationale") or item.get("why")
        return f"**{rule}** — {rationale}" if rationale else f"**{rule}**"
    # Unknown dict shape: render readably rather than as a repr.
    return ", ".join(f"{k}: {v}" for k, v in item.items())


# --- Default profile (populated by okuro init, can be customized) ---
#
# Empty-container shape only. Every onboarding-collected section starts
# empty so the backend step checks in `orchestrator/api/onboarding.py`
# can tell "the user hasn't filled this in yet" from "the user confirmed
# an answer." Previously this dict pre-populated communication/
# cognitive_style/boundaries with opinionated defaults, which made
# `_check_communication`, `_check_cognitive_style`, and `_check_boundaries`
# return True on a fresh install — the wizard then skipped straight over
# those sections and dropped the user at the first still-empty step
# (keyring / design), not step one. Leaving these containers empty is the
# single source of truth for "not done yet."
_DEFAULT_PROFILE: dict = {
    "identity": {},
    "cognitive_style": {},
    "communication": {},
    "expertise": {},
    "work_style": {},
    "decision_style": {},
    "boundaries": {},
}
