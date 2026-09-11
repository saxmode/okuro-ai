# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Canonical confidence-tier vocabulary for the code graph. The
#   confidence column on a code-graph triple IS its provenance tier — no
#   separate column. Pure edge classifier with import-evidence promotion +
#   ambiguity detection (graphify's EXTRACTED/INFERRED/AMBIGUOUS, adapted).
# index:
#   TIER_EXTRACTED
#   TIER_CONFIDENCE
#   def confidence_for
#   def tier_of
#   def base_name
#   def symbol_base
#   class EdgeResolution
#   def classify_edge
# AGENT_HEADER_END -->
"""Confidence tiers for the code graph — one vocabulary, used everywhere.

Why this module exists
----------------------
Before this, code-graph triples carried arbitrary magic confidences
(defines 0.95, imports 0.9, calls 0.8, cross-repo 0.6) that meant nothing
in particular. This module makes the confidence column a *named provenance
tier* so an agent (or the index self-audit) can tell a directly-parsed edge
from a guessed one — the single most valuable idea harvested from graphify
(`PA-0008`), and the point `C-code-intelligence` REQ-CI-005 / REQ-CI-021
make: confidence must be legible so approximate is never mistaken for precise.

The tiers (Tree-sitter has no type resolution, so this is how okuro marks
*where determinism ends* instead of paying per-language LSP-hosting cost):

    extracted  1.0   directly parsed, or resolved with import evidence
    inferred   0.75  single name-match, no import proof — a best guess
    ambiguous  0.4   name matches >1 candidate, import evidence didn't decide
    external   0.5   points outside the project (stdlib / third-party)

Storage decision: the tier is encoded *as* the confidence value (canonical
floats below), not a new column — the confidence column already exists and
is already okuro's provenance channel. Decoding is scoped to code predicates.
Legacy rows with a non-canonical confidence decode to ``unclassified`` — the
audit surfaces them as "re-ingest to tier".
"""

from __future__ import annotations

from dataclasses import dataclass, field


TIER_EXTRACTED = "extracted"
TIER_INFERRED = "inferred"
TIER_AMBIGUOUS = "ambiguous"
TIER_EXTERNAL = "external"
TIER_UNCLASSIFIED = "unclassified"

# Tiers that count toward the "how deterministic is this index" ratio.
RESOLVED_TIERS: tuple[str, ...] = (TIER_EXTRACTED, TIER_INFERRED, TIER_AMBIGUOUS)

# Canonical confidence per tier — the single source of truth. The confidence
# on a code-graph triple IS its tier.
TIER_CONFIDENCE: dict[str, float] = {
    TIER_EXTRACTED: 1.0,
    TIER_INFERRED: 0.75,
    TIER_AMBIGUOUS: 0.4,
    TIER_EXTERNAL: 0.5,
}

# Display / iteration order: most-trusted first.
TIER_ORDER: tuple[str, ...] = (
    TIER_EXTRACTED,
    TIER_INFERRED,
    TIER_AMBIGUOUS,
    TIER_EXTERNAL,
    TIER_UNCLASSIFIED,
)

# Reverse map for decoding (exact float match; code predicates only).
_BY_CONF: dict[float, str] = {round(v, 4): k for k, v in TIER_CONFIDENCE.items()}


def confidence_for(tier: str) -> float:
    """Canonical confidence for a tier. Raises KeyError on an unknown tier."""
    return TIER_CONFIDENCE[tier]


def tier_of(confidence: float | None) -> str:
    """Decode a code-graph triple's confidence back to its provenance tier.

    Non-canonical / legacy values (pre-tier data) decode to ``unclassified``
    so the audit can flag them for re-ingest rather than silently mislabel.
    """
    if confidence is None:
        return TIER_UNCLASSIFIED
    return _BY_CONF.get(round(float(confidence), 4), TIER_UNCLASSIFIED)


def base_name(callee: str) -> str:
    """Last identifier segment of a callee expression.

    ``os.getenv`` -> ``getenv``; ``parser.parse`` -> ``parse``;
    ``Foo.bar`` -> ``bar``; ``run`` -> ``run``. Strips call/generic noise
    defensively (``fn(...)`` / ``Box<...>``) in case the extractor left any.
    """
    c = callee.split("(", 1)[0].split("<", 1)[0].strip()
    return c.rsplit(".", 1)[-1] if "." in c else c


def symbol_base(sym_entity: str) -> str:
    """Match-key for a symbol entity name.

    ``dir/file.py::Service.helper`` -> ``helper``;
    ``m.py::helper#L5`` -> ``helper``; ``m.py::func`` -> ``func``.
    """
    qual = sym_entity.split("::", 1)[-1]
    qual = qual.split("#", 1)[0]
    return qual.rsplit(".", 1)[-1]


@dataclass(frozen=True)
class EdgeResolution:
    """The outcome of tiering one call / inherits edge."""

    tier: str
    target: str          # resolved ``file::sym`` entity, or the verbatim raw name
    resolved: bool       # True when target is a concrete project symbol entity
    candidates: tuple[str, ...] = field(default_factory=tuple)  # set only for ambiguous

    @property
    def confidence(self) -> float:
        return TIER_CONFIDENCE[self.tier]


def classify_edge(
    raw_target: str,
    *,
    caller_file: str,
    symbol_index: dict[str, list[tuple[str, str]]],
    imported_files: set[str],
) -> EdgeResolution:
    """Tier one call / inherits edge, with import-evidence promotion.

    Args:
        raw_target: the stored object — either a resolved ``file::sym`` entity
            (from the ingester's in-file resolution or a prior resolve pass) or
            a bare callee/parent name (``parse``, ``os.getenv``, ``BaseModel``).
        caller_file: the relpath of the file the edge originates in.
        symbol_index: ``base_name -> [(def_file, sym_entity), ...]`` over every
            symbol the project defines.
        imported_files: the set of project files ``caller_file`` imports
            (the import-evidence set).

    Resolution rules (idempotent — safe to re-run):
        - already a ``file::sym`` entity: re-tier only — EXTRACTED if its file
          is the caller's own file or an imported file, else INFERRED.
        - bare name, no candidate: EXTERNAL (stdlib / third-party), kept verbatim.
        - exactly one candidate: EXTRACTED if import-proven or same-file, else
          INFERRED. Resolves the target to the symbol entity.
        - many candidates: import evidence must narrow to exactly one → EXTRACTED;
          otherwise AMBIGUOUS (kept verbatim, candidates recorded, never guessed).
    """
    # Already resolved to a concrete symbol entity — just (re)tier it.
    if "::" in raw_target:
        obj_file = raw_target.split("::", 1)[0]
        proven = obj_file == caller_file or obj_file in imported_files
        return EdgeResolution(
            TIER_EXTRACTED if proven else TIER_INFERRED, raw_target, True
        )

    cands = symbol_index.get(base_name(raw_target)) or []
    if not cands:
        return EdgeResolution(TIER_EXTERNAL, raw_target, False)

    if len(cands) == 1:
        def_file, sym = cands[0]
        proven = def_file == caller_file or def_file in imported_files
        return EdgeResolution(
            TIER_EXTRACTED if proven else TIER_INFERRED, sym, True
        )

    # More than one candidate — import evidence must disambiguate to exactly one.
    proven = [
        (f, sym) for (f, sym) in cands
        if f == caller_file or f in imported_files
    ]
    if len(proven) == 1:
        return EdgeResolution(TIER_EXTRACTED, proven[0][1], True)

    return EdgeResolution(
        TIER_AMBIGUOUS,
        raw_target,
        False,
        candidates=tuple(sorted(sym for _f, sym in cands)),
    )


def build_symbol_index(
    defines: list[tuple[str, str]],
) -> tuple[dict[str, list[tuple[str, str]]], set[str]]:
    """Build the ``base_name -> [(def_file, sym_entity)]`` index + file set.

    ``defines`` is an iterable of ``(file, sym_entity)`` pairs (the subject and
    object of active ``defines`` triples). Pure — no DB access — so the resolver
    stays unit-testable.
    """
    index: dict[str, list[tuple[str, str]]] = {}
    files: set[str] = set()
    for def_file, sym in defines:
        files.add(def_file)
        index.setdefault(symbol_base(sym), []).append((def_file, sym))
    return index, files
