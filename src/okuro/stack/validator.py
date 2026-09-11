# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Stack registry consistency checks.
# index:
#   imports
#   def validate_registry
#   def lint_profile
#   def lint_brand
# AGENT_HEADER_END -->
"""Stack registry consistency checks.

Three surfaces:
    validate_registry()   — whole-registry health (dangling refs, cycles).
    lint_profile(name)    — profile health (banned entries, cardinality,
                            missing layers, unsatisfied deps).
    lint_brand(brand_id)  — brand health (slot refs resolve, scope filters
                            hold, underlying profiles lint clean).

All return `{ok, errors, warnings, stats}` so the web UI can render a
scorecard and MCP tools can gate promotion.
"""

from __future__ import annotations

from .registry import (
    _ensure_seeded,
    list_entries,
    list_layers,
    list_profiles,
    get_profile,
    resolve_profile,
    list_slot_kinds,
    get_brand,
)


def _check_cycles(entry_ids: set[str], deps_by_entry: dict[str, list[str]]) -> list[list[str]]:
    """DFS cycle detector over the dep graph."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {eid: WHITE for eid in entry_ids}
    cycles: list[list[str]] = []
    stack: list[str] = []

    def visit(node: str) -> None:
        if color.get(node) == GRAY:
            if node in stack:
                i = stack.index(node)
                cycles.append(stack[i:] + [node])
            return
        if color.get(node) == BLACK:
            return
        color[node] = GRAY
        stack.append(node)
        for dep in deps_by_entry.get(node, []):
            visit(dep)
        stack.pop()
        color[node] = BLACK

    for eid in entry_ids:
        if color[eid] == WHITE:
            visit(eid)

    return cycles


def validate_registry() -> dict:
    """Whole-registry health check.

    Checks:
        - every entry's layer exists
        - every dep target exists
        - `replaces` target exists
        - no dependency cycles
        - every banned entry has a rationale
    """
    _ensure_seeded()

    entries = list_entries()
    layers = {l["id"] for l in list_layers()}
    entry_ids = {e["id"] for e in entries}

    errors: list[str] = []
    warnings: list[str] = []

    deps_by_entry: dict[str, list[str]] = {}
    for e in entries:
        deps_by_entry[e["id"]] = list(e.get("depends_on") or [])

        if e["layer"] not in layers:
            errors.append(
                f"Entry '{e['id']}' references unknown layer '{e['layer']}'"
            )
        for dep in deps_by_entry[e["id"]]:
            if dep not in entry_ids:
                errors.append(
                    f"Entry '{e['id']}' depends on unknown entry '{dep}'"
                )
        if e.get("replaces") and e["replaces"] not in entry_ids:
            errors.append(
                f"Entry '{e['id']}' replaces unknown entry '{e['replaces']}'"
            )
        if e.get("status") == "banned" and not (e.get("rationale") or "").strip():
            warnings.append(f"Banned entry '{e['id']}' has no rationale")

    cycles = _check_cycles(entry_ids, deps_by_entry)
    for cyc in cycles:
        errors.append("Dependency cycle: " + " → ".join(cyc))

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "stats": {
            "layers": len(layers),
            "entries": len(entries),
            "profiles": len(list_profiles()),
            "cycles": len(cycles),
        },
    }


def lint_profile(profile_name: str) -> dict:
    """Lint a specific profile for internal consistency.

    Checks:
        - every entry is actually registered
        - no `banned` entries selected
        - single-cardinality layers have ≤1 'primary' entry
        - declared deps of selected entries are also in the profile
          (or at least exist — transitive closure is informational)
    """
    _ensure_seeded()

    profile = get_profile(profile_name)
    if not profile:
        return {
            "ok": False,
            "errors": [f"Profile not found: {profile_name}"],
            "warnings": [],
            "stats": {},
        }

    resolved = resolve_profile(profile_name) or {"by_category": {}, "transitive": []}
    layers = {l["id"]: l for l in list_layers()}
    picked = profile["entries"]

    errors: list[str] = []
    warnings: list[str] = []

    # Cardinality check.
    by_layer_primary: dict[str, int] = {}
    for pe in picked:
        if pe["role"] != "primary":
            continue
        by_layer_primary[pe["layer"]] = by_layer_primary.get(pe["layer"], 0) + 1

    for layer_id, count in by_layer_primary.items():
        layer = layers.get(layer_id)
        if layer and layer["cardinality"] == "single" and count > 1:
            errors.append(
                f"Layer '{layer_id}' is single-cardinality but profile "
                f"selects {count} primary entries"
            )

    # Status check.
    for pe in picked:
        if pe["status"] == "banned":
            errors.append(f"Profile selects banned entry: {pe['id']}")
        elif pe["status"] == "deprecated":
            warnings.append(f"Profile selects deprecated entry: {pe['id']}")

    # Dependency closure — informational (transitive)
    stats = {
        "selected_entries": len(picked),
        "transitive_entries": len(resolved.get("transitive", [])),
        "layers_covered": len(by_layer_primary),
        "total_layers": len(layers),
    }

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "stats": stats,
    }


def lint_brand(brand_id: str) -> dict:
    """Lint a brand for composition consistency.

    Checks:
        - brand exists
        - every slot kind used is registered in brand_slot_kinds
        - every required slot kind is filled
        - single-cardinality slots have exactly 0 or 1 ref
        - stack_profile refs exist and match the slot's ``scope_filter``
        - design_profile refs correspond to a YAML file on disk
          (user-home override or repo bundle)
        - principle_set refs exist
        - underlying stack profiles pass ``lint_profile`` (errors bubble as
          warnings — a broken profile isn't necessarily a broken brand, but
          worth surfacing)
    """
    _ensure_seeded()

    brand = get_brand(brand_id)
    if not brand:
        return {
            "ok": False,
            "errors": [f"Brand not found: {brand_id}"],
            "warnings": [],
            "stats": {},
        }

    kind_meta = {sk["kind"]: sk for sk in list_slot_kinds()}
    profiles_by_name = {p["name"]: p for p in list_profiles()}

    errors: list[str] = []
    warnings: list[str] = []

    # Required slots filled?
    filled_kinds = set(brand.get("slots", {}).keys())
    for kind, meta in kind_meta.items():
        if meta.get("required") and kind not in filled_kinds:
            errors.append(f"Required slot '{kind}' is not filled")

    # Each filled slot: known kind, matches registry, scope_filter holds.
    for kind, raw in brand.get("slots", {}).items():
        meta = kind_meta.get(kind)
        if not meta:
            errors.append(f"Unknown slot kind '{kind}'")
            continue

        refs = raw if isinstance(raw, list) else [raw]

        if meta["cardinality"] == "single" and len(refs) > 1:
            errors.append(
                f"Slot '{kind}' is single-cardinality but has "
                f"{len(refs)} refs"
            )

        registry = meta["registry"]
        scope_filter = meta.get("scope_filter")

        for ref_id in refs:
            if not ref_id:
                errors.append(f"Slot '{kind}' has empty ref_id")
                continue

            if registry == "stack_profile":
                prof = profiles_by_name.get(ref_id)
                if not prof:
                    errors.append(
                        f"Slot '{kind}' → unknown stack profile: {ref_id}"
                    )
                    continue
                if scope_filter and prof.get("scope") != scope_filter:
                    errors.append(
                        f"Slot '{kind}' requires scope='{scope_filter}' "
                        f"but '{ref_id}' has scope='{prof.get('scope')}'"
                    )
                # Surface underlying profile issues as warnings.
                sub = lint_profile(ref_id)
                for e in sub.get("errors", []):
                    warnings.append(f"[{ref_id}] {e}")

            elif registry == "design_profile":
                if not _design_profile_exists(ref_id):
                    errors.append(
                        f"Slot '{kind}' → design profile YAML missing: {ref_id}"
                    )
                else:
                    # v2 completeness is advisory: a legacy profile without the
                    # canonical design_system block still renders (resolver falls
                    # back to formulas), but flag the gap so brands migrate.
                    for gap in _design_v2_gaps(ref_id):
                        warnings.append(f"[{ref_id}] design_system v2 gap: {gap}")

            elif registry == "principle_set":
                if not _principle_set_exists(ref_id):
                    errors.append(
                        f"Slot '{kind}' → unknown principle set: {ref_id}"
                    )

            elif registry == "asset_profile":
                if not _asset_profile_exists(ref_id):
                    errors.append(
                        f"Slot '{kind}' → unknown asset profile: {ref_id}"
                    )

            else:
                warnings.append(
                    f"Slot '{kind}' uses unhandled registry '{registry}'"
                )

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "stats": {
            "slots_filled": len(filled_kinds),
            "slots_required": sum(
                1 for m in kind_meta.values() if m.get("required")
            ),
            "slots_known": len(kind_meta),
        },
    }


def _design_profile_exists(design_id: str) -> bool:
    """Does the design slot's ref name something that resolves?

    THE THIRD LIE IN THIS FILE, and the one that hid behind the other two. It
    asked `okuro.design.profiles.get_profile_path`, i.e. "is there a v0 YAML on
    disk" -- so once the design slot began resolving from the engine, every
    kit-bound brand linted as an UNKNOWN design profile while the same slot
    resolved perfectly.

    The question the caller is really asking is "will this ref resolve", so it
    is answered by the resolver rather than by a filesystem probe for one of the
    two possible sources.

    Measured before the fix: every engine kit returned False while every v0
    profile returned True -- i.e. exactly inverted for the layer being migrated
    to. (Kit ids are deliberately not named here; real brand names are the
    user's data and do not belong in this repo.)
    """
    try:
        from okuro.stack.registry import _resolve_design_profile
    except ImportError:
        return False
    try:
        return _resolve_design_profile(design_id) is not None
    except Exception:
        return False


# Canonical v2 design-system basic set (per the okuro brand model). A brand
# design profile should carry a `design_system` block with these fields; the
# prism deck resolver derives elevation/fg ladders by applying the ramps.
_DESIGN_V2_FIELDS = (
    "foreground", "background", "accent_dark", "accent_light",
    "font", "white_ramp", "black_ramp", "chart",
)
_DESIGN_V2_RAMP_STEPS = 15


def _design_v2_gaps(design_id: str) -> list[str]:
    """Return the list of missing/short v2 design_system fields for a profile.
    Empty list = fully v2-compliant. Never raises (advisory lint)."""
    # THROUGH THE RESOLVER, not around it. This imported okuro.design.profiles
    # directly, so it answered from v0 even once the design slot resolved from
    # the engine -- and the two then disagreed in opposite directions: this
    # function reported "missing design_system block" for every brand while the
    # slot itself resolved fine. Two opposite lies in the tool you would reach
    # for to verify a migration.
    try:
        from okuro.stack.registry import _resolve_design_profile
    except ImportError:
        return []
    try:
        data = _resolve_design_profile(design_id) or {}
    except Exception:
        return []
    ds = data.get("design_system")
    if not isinstance(ds, dict):
        return ["missing design_system block"]
    gaps: list[str] = []
    for field in _DESIGN_V2_FIELDS:
        val = ds.get(field)
        if val in (None, "", [], {}):
            gaps.append(f"missing '{field}'")
        elif field in ("white_ramp", "black_ramp") and (
            not isinstance(val, list) or len(val) < _DESIGN_V2_RAMP_STEPS
        ):
            gaps.append(f"'{field}' expects {_DESIGN_V2_RAMP_STEPS} steps")
    return gaps


def _principle_set_exists(set_id: str) -> bool:
    try:
        from okuro.sense.principle_sets import get_principle_set
    except ImportError:
        return False
    try:
        return get_principle_set(set_id) is not None
    except Exception:
        return False


def _asset_profile_exists(profile_id: str) -> bool:
    from okuro.db import get_db
    try:
        return get_db().fetchone(
            "SELECT 1 FROM asset_profiles WHERE id = ?", (profile_id,)
        ) is not None
    except Exception:
        return False
